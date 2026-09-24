"""The CI ratchet: pointers, high-risk pointers and profile violations may only go down."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import HAVE_CLANG, HAVE_MAKE, run_cli
from test_output_param import _project

from weaver.config import load_project
from weaver.errors import WeaverError
from weaver.ratchet import compare, ratchet

needs_build = pytest.mark.skipif(not (HAVE_CLANG and HAVE_MAKE), reason="clang and make required")


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    text = p.read_text()
    assert old in text
    p.write_text(text.replace(old, new, 1))
    assert run_cli(root, "refresh") == 0


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_counts_decide_and_ids_only_name_what_is_new():
    def fx(**files):
        return {"schema": "weaver.ratchet/1", "profile": "p", "flow_evidence": True, "weaver": "x",
                "totals": {}, "files": files, "names": {"P-2": {"name": "q", "kind": "local", "line": 9}}}  # fmt: skip

    def f(n, ids, high=0, v=None):
        return {"pointers": n, "high": high, "violations": v or {}, "ids": ids, "high_ids": []}

    base = fx(**{"a.c": f(1, ["P-1"], v={"goto": 1})})
    renamed = compare(base, fx(**{"a.c": f(1, ["P-2"], v={"goto": 1})}), ["pointers", "high", "violations"])
    assert renamed["ok"]  # a renamed pointer is not a new one
    grew = compare(base, fx(**{"a.c": f(2, ["P-1", "P-2"], v={"goto": 2})}), ["pointers", "high", "violations"])
    assert not grew["ok"] and [x["what"] for x in grew["failures"]] == ["pointers", "violations:goto"]
    assert grew["failures"][0]["new"] == [{"id": "P-2", "name": "q", "kind": "local", "line": 9}]
    only = compare(base, fx(**{"a.c": f(2, ["P-1", "P-2"])}), ["pointers"], only={"b.c"})
    assert only["ok"] and only["files_compared"] == 0  # a.c is not among the changed files
    other = compare(base, {**fx(**{"a.c": f(1, ["P-1"])}), "flow_evidence": False}, ["pointers", "high"])
    assert "high-risk pointers are not compared" in other["warnings"][0] and other["enforce"] == ["pointers"]


@pytest.fixture()
def proj(tmp_path):
    root = _project(tmp_path)
    assert run_cli(root, "refresh") == 0  # the same evidence (points-to included) for baseline and checks
    return root


@needs_build
def test_the_ratchet_fails_on_new_pointers_and_violations_and_locks_in_progress(proj, capsys):
    with pytest.raises(WeaverError, match="no ratchet baseline"):
        ratchet(load_project(proj))
    assert run_cli(proj, "ratchet", "--update") == 0
    base = json.loads((proj / "weaver-ratchet.json").read_text())
    assert base["schema"] == "weaver.ratchet/1" and base["profile"] == "clite-provisional"
    assert base["files"]["lib.c"]["pointers"] == len(base["files"]["lib.c"]["ids"]) > 0
    assert run_cli(proj, "ratchet") == 0

    _edit(
        proj,
        "lib.c",
        "int reads_back(int *io)\n{\n",
        "int reads_back(int *io)\n{\n    int *alias = io;\n    (void)alias;\n",
    )
    capsys.readouterr()
    assert run_cli(proj, "ratchet", "--format", "github") == 1
    out = capsys.readouterr().out
    assert "::error file=lib.c,line=" in out and "new local pointer 'alias' in reads_back()" in out
    assert "FAIL lib.c: pointer(s)" in out

    _edit(proj, "lib.c", "    int *alias = io;\n    (void)alias;\n", "    goto out;\nout:\n")
    res, _ = ratchet(load_project(proj))
    assert not res["ok"] and [f["what"] for f in res["failures"]] == ["violations:goto"]

    _edit(proj, "lib.c", "    goto out;\nout:\n", "")
    _edit(proj, "main.c", "static void fill_global(int *out)\n{\n    *out = 42;\n}\n", "")
    _edit(proj, "main.c", "    fill_global(&g_value);\n", "    g_value = 42;\n")
    res, _ = ratchet(load_project(proj))
    assert res["ok"] and any(p["file"] == "main.c" and p["what"] == "pointers" for p in res["progress"])
    assert run_cli(proj, "ratchet", "--strict") == 1  # progress not yet in the committed baseline
    assert run_cli(proj, "ratchet", "--update") == 0 and run_cli(proj, "ratchet", "--strict") == 0


@needs_build
@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_base_compares_only_the_files_a_change_touches(proj):
    assert run_cli(proj, "ratchet", "--update") == 0
    _git(proj, "init", "-q")
    _git(proj, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(proj, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "base")
    _edit(
        proj,
        "main.c",
        "static void leaf2(int *out)\n{\n",
        "static void leaf2(int *out)\n{\n    int *spare = out;\n    (void)spare;\n",
    )
    res, _ = ratchet(load_project(proj), base_rev="HEAD")
    assert not res["ok"] and res["scope"] == "changed files" and res["files_compared"] == 1
    assert res["failures"][0]["file"] == "main.c"


@needs_build
def test_a_stale_inventory_is_refused(proj):
    assert run_cli(proj, "ratchet", "--update") == 0
    (proj / "lib.c").write_text((proj / "lib.c").read_text() + "\n/* edited after the analysis */\n")
    with pytest.raises(WeaverError, match="run 'weaver refresh'"):
        ratchet(load_project(proj))
