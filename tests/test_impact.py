"""Change impact: explain how later edits (by anyone) changed pointer behavior since a baseline."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess

import pytest
from conftest import RAND_MODEL, SINGLE_THREADED, build_project, needs_clang, run_cli, validation_for
from test_pipeline import finding_id

from weaver.config import load_project
from weaver.impact import impact, pin_contract, save_snapshot
from weaver.ledger import Ledger

SUM2 = "long p_sum2(long *a, long *b)\n{\n    return *a + *b + (long)sizeof(*a);\n}"
SUM2_EDITED = (
    "long p_sum2(long *a, long *b)\n{\n    long r = *a + *b + (long)sizeof(*a);\n"
    "    *a = 0;                 /* teammate: reset the accumulator */\n"
    "    util_touch((int *)b);   /* teammate: record the access */\n    return r;\n}"
)


@pytest.fixture()
def project(tmp_path):
    root = build_project(
        tmp_path,
        [{"id": "clang", "cc": "clang", "validation": validation_for("clang")}],
        extra={
            "preservation": SINGLE_THREADED,
            "flow": RAND_MODEL,
            "acceptance": {"require": ["compile", "mechanical-recheck", "differential-testing"]},
        },
    )
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0
    return root


def teammate_edit(root):
    p = root / "src/params.c"
    text = p.read_text()
    assert SUM2 in text
    p.write_text(
        text.replace(SUM2, SUM2_EDITED).replace('#include "params.h"\n', '#include "params.h"\n#include "util.h"\n')
    )
    assert run_cli(root, "refresh", "--no-flow") == 0


def check(root, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_cli(root, "check", *args, "--json")
    return rc, json.loads(buf.getvalue())


@needs_clang
def test_unchanged_tree_is_quiet(project):
    save_snapshot(load_project(project), "base")
    rc, rep = check(project, "--since", "base")
    assert rc == 0 and rep["risk"] == "none" and rep["changes"] == [] and rep["changed_files"] == {}


@needs_clang
def test_teammate_edit_is_explained(project):
    proj = load_project(project)
    a = finding_id(project, "p_sum2", "a")
    pin_contract(proj, a, ["read-only", "no-escape"], "callers pass accumulators they reuse")
    save_snapshot(proj, "baseline")
    teammate_edit(project)

    rc, rep = check(project, "--since", "baseline", "--revalidate")
    assert rc == 1 and rep["risk"] == "high"
    assert list(rep["changed_files"]) == ["src/params.c"]
    got = {(c["name"], c["aspect"], c["severity"]) for c in rep["changes"] if c["function"] == "p_sum2"}
    assert {
        ("a", "use", "high"),
        ("a", "access-class", "high"),
        ("b", "use", "high"),
        ("b", "access-class", "high"),
    } <= got
    assert {("a", "verdict", "review"), ("b", "verdict", "review")} <= got  # scalar-input no longer applies
    write = next(c for c in rep["changes"] if c["name"] == "a" and c["aspect"] == "use")
    assert write["caused_by_change"] and "*a = 0;" in write["source"] and "was read-only" in write["text"]
    (contract,) = [c for c in rep["contracts"] if c.get("finding") == a]
    assert contract["status"] == "violated" and "deref (write)" in contract["text"]
    # The program's output is unchanged: the configured comparison alone would not have caught this.
    reval = {r["kind"]: r["outcome"] for r in rep["revalidation"]}
    assert reval == {"build": "passed", "differential-testing": "passed"}


@needs_clang
def test_reintroduced_alias_breaks_an_accepted_refactor(project):
    proj = load_project(project)
    led = Ledger(proj)
    txn = led.propose(finding_id(project, "la_basic", "p"))
    txn = led.validate(txn["id"])
    led.accept(txn["id"])
    assert run_cli(project, "refresh", "--no-flow") == 0
    save_snapshot(proj, "refactored")

    alias = project / "src/alias.c"
    alias.write_text(alias.read_text().replace("    total += 2;\n", "    unsigned *q = &total;\n    *q += 2;\n"))
    assert run_cli(project, "refresh", "--no-flow") == 0
    rep = impact(proj, "refactored")
    assert rep["risk"] == "high"
    implied = next(c for c in rep["contracts"] if c["contract"] == f"{txn['id']}/no-alias")
    assert implied["status"] == "violated" and "'q'" in implied["text"]
    (overlap,) = rep["transactions"]
    assert overlap["txn"] == txn["id"] and overlap["severity"] == "high"
    added = next(c for c in rep["changes"] if c["aspect"] == "added")
    assert added["name"] == "q" and added["caused_by_change"]


@needs_clang
@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_snapshot_of_a_git_revision(project):
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run([*git, "add", "src", "include", "Makefile"], cwd=project, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=project, check=True)
    teammate_edit(project)
    assert run_cli(project, "snapshot", "git", "HEAD", "--name", "head") == 0
    rc, rep = check(project, "--since", "head")
    assert rc == 1 and list(rep["changed_files"]) == ["src/params.c"]
    assert rep["base"]["source"]["kind"] == "git"
