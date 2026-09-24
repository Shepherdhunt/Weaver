"""Coverage of the changed lines: a change the tests never run is not called behavioural."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import HAVE_CLANG, HAVE_MAKE, run_cli

from weaver.config import load_project
from weaver.coverage import _read_text, changed_lines, describe
from weaver.ledger import Ledger

needs_gcov = pytest.mark.skipif(
    not (HAVE_CLANG and HAVE_MAKE and shutil.which("gcc") and shutil.which("gcov")),
    reason="clang (analysis), make, gcc and gcov required",
)

APP_C = """\
#include <stdio.h>

int used(int a)
{
    return a + 1;
}

int unused(int a)
{
    return a * 2;
}

int main(int argc, char **argv)
{
    (void)argv;
    printf("%d\\n", used(argc));
    if (argc > 5)
        printf("%d\\n", unused(argc));
    return 0;
}
"""

MAKEFILE = """\
CC ?= cc
BUILD ?= build

$(BUILD)/app: app.c | $(BUILD)
\t$(CC) -O0 -o $@ app.c

$(BUILD):
\tmkdir -p $(BUILD)
"""


def _diff(old: str, new: str) -> str:
    return f"--- a/app.c\n+++ b/app.c\n@@ -1,1 +1,1 @@\n-{old}\n+{new}\n"


USED = _diff("    return a + 1;", "    return 1 + a;")
UNUSED = _diff("    return a * 2;", "    return 2 * a;")
BOTH = (
    "--- a/app.c\n+++ b/app.c\n@@ -5,1 +5,1 @@\n-    return a + 1;\n+    return 1 + a;\n"
    "@@ -10,1 +10,1 @@\n-    return a * 2;\n+    return 2 * a;\n"
)


def _project(tmp: Path, cc: str = "gcc", coverage: bool = True, require: list[str] | None = None) -> Path:
    from weaver.capture.shims import finalize, make_shim

    root = tmp / "cov"
    root.mkdir(parents=True)
    (root / "app.c").write_text(APP_C)
    (root / "Makefile").write_text(MAKEFILE)
    cap = root / ".weaver" / "capture"
    shim = make_shim(cap / "shims", "cc", shutil.which("clang"), cap / "log.jsonl")
    subprocess.run(["make", "-s", f"CC={shim}", "BUILD=build"], cwd=root, check=True)
    finalize(cap / "log.jsonl", root / "build")
    validation = {
        "build": {"run": ["make", "-s", "-C", "{workspace}", f"CC={cc}", "BUILD=out"], "cwd": "{workspace}"},
        "compare": [{"name": "app", "run": ["./out/app"], "cwd": "{workspace}"}],
    }
    if not coverage:
        validation["coverage"] = False
    cfg = {
        "schema": "weaver.project/1",
        "project": {"name": "cov", "workspace_exclude": [".git", "build", "out"]},
        "acceptance": {"require": require or ["compile", "mechanical-recheck", "differential-testing"]},
        "profiles": [{"id": "gcc", "compile_commands": "build/compile_commands.json", "validation": validation}],
    }
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0
    return root


def _check(root: Path, diff: str) -> dict:
    led = Ledger(load_project(root))
    txn = led.propose_patch(diff, [], "coverage probe")
    assert txn["state"] == "proposed", txn["history"]
    return led.validate(txn["id"])


def _cov(txn: dict) -> dict:
    return next(r for r in txn["validation"]["records"] if r["kind"] == "coverage")


def test_changed_lines_and_the_gcov_text_format():
    old = b"a\nb\nc\nd\n"
    new = b"a\nB\nc\n\nnew\n"  # b replaced; d replaced by a blank line and a new line (blank lines never count)
    assert changed_lines({"x.c": (old, new)}) == ({"x.c": [2, 5]}, 0)
    assert changed_lines({"x.c": (old, b"a\nc\nd\n")}) == ({}, 1)  # a deletion leaves nothing to execute
    text = (
        "        -:    0:Source:/w/src/t.c\n        -:    1:#include <stdio.h>\n        3:    2:int f(void)\n"
        "    #####:    4:    return 1;\n        1*:   5:    x();\n    =====:    6:    y();\n"
    )
    ((path, lines),) = _read_text(text, Path("/w/b/t.c.gcda"), Path("/w"))
    assert str(path) == "/w/src/t.c" and lines == [(2, 3), (4, 0), (5, 1), (6, 0)]
    cov = {"measured": True, "executable": 2, "executed": 0, "missed": ["app.c:10", "app.c:11"]}
    assert describe(cov) == "the tests executed none of the 2 changed line(s) that have code: app.c:10, app.c:11"


@needs_gcov
def test_the_tests_ran_the_change(tmp_path):
    txn = _check(_project(tmp_path), USED)
    cov = _cov(txn)
    assert txn["state"] == "validated" and txn["validation"]["strength"] == "behavioural"
    assert cov["outcome"] == "passed" and cov["coverage"]["executed"] == cov["coverage"]["executable"] == 1
    assert "executed every changed line that has code (1 of 1)" in cov["detail"]
    assert cov["coverage"]["tool"] == "gcov"


@needs_gcov
def test_a_change_the_tests_never_run_is_unexercised(tmp_path):
    root = _project(tmp_path)
    txn = _check(root, UNUSED)
    cov = _cov(txn)
    assert txn["state"] == "validated" and txn["validation"]["strength"] == "unexercised"
    assert cov["outcome"] == "not-evaluated" and cov["coverage"]["missed"] == ["app.c:10"]
    from weaver.card import render_card

    assert "strength: unexercised" in render_card(txn)
    both = _check(root, BOTH)
    assert both["validation"]["strength"] == "partly-exercised"
    assert "executed 1 of 2 changed line(s) that have code; never executed: app.c:10" in _cov(both)["detail"]
    assert run_cli(root, "accept", both["id"]) == 0
    acc = Ledger(load_project(root)).load(both["id"])
    assert acc["acceptance"]["strength"] == "partly-exercised"
    assert "did not execute every changed line" in acc["history"][-1]["note"]


@needs_gcov
def test_a_policy_can_require_that_the_tests_run_the_change(tmp_path):
    root = _project(tmp_path, require=["compile", "mechanical-recheck", "differential-testing", "coverage"])
    txn = _check(root, UNUSED)
    assert txn["state"] == "provisional"
    assert "required 'coverage' validation could not be evaluated: gcc:changed lines: the tests executed none" in (
        " ".join(txn["validation"]["judgement"]["reasons"])
    )
    assert _check(root, USED)["state"] == "validated"


@needs_gcov
def test_coverage_can_be_off_or_unmeasurable(tmp_path):
    off = _check(_project(tmp_path / "off", coverage=False), UNUSED)
    assert not any(r["kind"] == "coverage" for r in off["validation"]["records"])
    assert off["validation"]["strength"] == "behavioural"  # not measured: the card says so
    from weaver.card import render_card

    assert "which changed lines it executed was not measured" in render_card(off)
    absolute = _check(_project(tmp_path / "abs", cc=shutil.which("gcc")), UNUSED)  # bypasses the PATH shims
    cov = _cov(absolute)
    assert cov["outcome"] == "not-evaluated" and "no compile went through the coverage shims" in cov["detail"]
    assert absolute["validation"]["strength"] == "behavioural"
