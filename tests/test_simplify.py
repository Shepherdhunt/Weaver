"""The simplification checker: each construct found on its line, profiles, project rules, the web view."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import HAVE_CLANG, HAVE_MAKE, run_cli

from weaver.analysis.inventory import load_inventory
from weaver.config import load_project
from weaver.errors import ConfigError
from weaver.simplify import check

needs_build = pytest.mark.skipif(not (HAVE_CLANG and HAVE_MAKE), reason="clang and make required")

SRC = """\
#include <setjmp.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

int counter;
static jmp_buf env;
union word { int i; float f; };

int clean(int a, int b) { return a * b + 1; }

int jumps(int c)
{
    if (c)
        goto out;
    c = 2;
out:
    return c;
}

float punned(int i)
{
    union word w;
    w.i = i;
    return w.f;
}

int total(int n, ...)
{
    va_list ap;
    va_start(ap, n);
    int s = va_arg(ap, int);
    va_end(ap);
    return s + n;
}

int tick(void)
{
    static int calls;
    counter++;
    return ++calls;
}

static int apply(int (*f)(int, int), int x) { return f(x, x); }

int *fresh(int n)
{
    int *p = malloc(sizeof(int) * (size_t)n);
    memset(p, 0, sizeof(int) * (size_t)n);
    return p + 1;
}

static int odd(int n);
static int even(int n) { return n == 0 ? 1 : odd(n - 1); }
static int odd(int n) { return n == 0 ? 0 : even(n - 1); }

int escape(void)
{
    if (setjmp(env))
        return 1;
    longjmp(env, 1);
}

unsigned long addr(int *q) { return (unsigned long)q; }

int main(void)
{
    int *p = fresh(3);
    free(p - 1);
    return clean(1, 2) + jumps(0) + (int)punned(1) + total(1, 2) + tick() + apply(clean, 2) + even(4)
        + escape() + (int)addr(&counter);
}
"""


def ln(snippet: str) -> int:
    """The line of SRC that contains ``snippet``."""
    return next(i for i, line in enumerate(SRC.splitlines(), 1) if snippet in line)


def _project(tmp: Path) -> Path:
    from weaver.capture.shims import finalize, make_shim

    root = tmp / "simp"
    root.mkdir()
    (root / "all.c").write_text(SRC)
    (root / "Makefile").write_text("app: all.c\n\t$(CC) -O0 -g -o app all.c\n")
    cap = root / ".weaver" / "capture"
    shim = make_shim(cap / "shims", "cc", shutil.which("clang"), cap / "log.jsonl")
    subprocess.run(["make", "-s", f"CC={shim}"], cwd=root, check=True)
    finalize(cap / "log.jsonl", root / "build")
    cfg = {
        "schema": "weaver.project/1",
        "project": {"name": "simp", "workspace_exclude": [".git", "build"]},
        "profiles": [{"id": "clang", "compile_commands": "build/compile_commands.json"}],
    }
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0
    return root


@pytest.fixture(scope="module")
def simp(tmp_path_factory):
    return _project(tmp_path_factory.mktemp("simplify"))


def _rows(root: Path, profile: str | None = None):
    proj = load_project(root)
    rep = check(proj, load_inventory(proj), profile)
    return rep, {r["function"]: r for r in rep["functions"]}


def _rules(row) -> set[tuple[str, int]]:
    return {(v["rule"], v["line"]) for v in row["violations"]}


@needs_build
def test_each_construct_is_found_on_its_line(simp):
    rep, rows = _rows(simp)
    assert rep["profile"]["id"] == "clite-provisional" and rep["profile"]["provisional"]
    assert rows["clean"]["violations"] == []
    assert _rules(rows["jumps"]) == {("goto", ln("goto out"))}
    assert {r for r, _ in _rules(rows["punned"])} == {"union"}
    assert {("varargs", ln("int total")), ("varargs", ln("va_arg"))} <= _rules(rows["total"])  # definition, va_arg
    assert ("function-pointer", ln("apply(int")) in _rules(rows["apply"])  # the parameter, the call through it
    fresh = _rules(rows["fresh"])
    assert {
        ("dynamic-allocation", ln("= malloc")),
        ("raw-memory", ln("memset")),
        ("pointer-arithmetic", ln("p + 1")),
    } <= fresh
    assert ("pointer", ln("= malloc")) in fresh
    assert {r for r, _ in _rules(rows["even"])} == {r for r, _ in _rules(rows["odd"])} == {"recursion"}
    assert {("setjmp-longjmp", ln("setjmp(env)")), ("setjmp-longjmp", ln("longjmp(env"))} <= _rules(rows["escape"])
    assert ("integer-pointer-cast", ln("(unsigned long)q")) in _rules(rows["addr"])
    main = _rules(rows["main"])
    assert ("address-of", ln("&counter")) in main and ("dynamic-allocation", ln("free(p")) in main
    # tick has only state, which CLite (provisional) does not exclude
    assert rows["tick"]["violations"] == []
    sm = rep["summary"]
    assert sm["ready"] == 2 and sm["functions"] == len(rows) and sm["by_rule"]["recursion"]["functions"] == 2


@needs_build
def test_profiles_and_project_rules(simp):
    rep, rows = _rows(simp, "modular")
    assert _rules(rows["tick"]) == {("global-write", ln("counter++")), ("static-local", ln("static int calls"))}
    assert _rules(rows["jumps"]) == {("goto", ln("goto out"))} and rows["punned"]["violations"] == []
    rep, rows = _rows(simp, "pointer-free")
    assert "recursion" not in rep["rules"] and rows["even"]["violations"] == []

    cfg = yaml.safe_load((simp / "weaver.yaml").read_text())
    cfg["simplify"] = {
        "profile": "app",
        "add": ["static-local"],
        "profiles": {"app": {"title": "Application layer", "rules": ["goto", "global-write"]}},
    }
    (simp / "weaver.yaml").write_text(yaml.safe_dump(cfg))
    try:
        rep, rows = _rows(simp)
        assert rep["profile"]["title"] == "Application layer" and rep["profile"]["rules"] == [
            "goto",
            "global-write",
            "static-local",
        ]
        assert [p["id"] for p in rep["profiles"]][-1] == "app"
        cfg["simplify"]["profiles"]["app"]["rules"] = ["no-such-rule"]
        (simp / "weaver.yaml").write_text(yaml.safe_dump(cfg))
        with pytest.raises(ConfigError, match="unknown rule"):
            _rows(simp)
    finally:
        del cfg["simplify"]
        (simp / "weaver.yaml").write_text(yaml.safe_dump(cfg))


@needs_build
def test_simplify_view_and_cli(simp, capsys):
    from weaver.web.api import Cache, simplify_view

    proj = load_project(simp)
    rep = simplify_view(proj, Cache(), "modular")
    assert rep["profile"]["id"] == "modular" and {p["id"] for p in rep["profiles"]} >= {"clite-provisional", "modular"}
    assert simplify_view(proj, Cache(scope=["elsewhere/"]))["functions"] == []
    capsys.readouterr()
    assert run_cli(simp, "simplify", "--json") == 0
    assert json.loads(capsys.readouterr().out)["summary"]["ready"] == 2
