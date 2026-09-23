"""Stronger validation: test detection, per-test comparison, validation strength and the settings editor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from weaver.config import load_project
from weaver.errors import ConfigError
from weaver.evidence import ValidationOutcome
from weaver.settings import read_settings, write_settings
from weaver.testdetect import detect_tests
from weaver.validate import _compare_tests, configured_strength, parse_test_outcomes, validation_strength

CTEST_OUT = """\
Test project /w/build-val/native/default_cpu1
      Start  1: coverage-es-ALL
 1/4 Test  #1: coverage-es-ALL ..................   Passed    0.31 sec
 2/4 Test  #2: timer-test .......................***Failed    1.02 sec
 3/4 Test  #3: queue-test .......................***Timeout 120.01 sec
 4/4 Test  #4: sample_app-ALL-testrunner ........   Passed    0.02 sec
"""
MESON_OUT = """\
1/3 demo:unit / parse         OK              0.01s
2/3 demo:unit / emit          FAIL            0.20s   exit status 1
3/3 demo:slow / soak          SKIP            0.00s
"""


def test_ctest_and_meson_outcomes_are_parsed_per_test():
    assert parse_test_outcomes(CTEST_OUT) == {
        "coverage-es-ALL": "passed",
        "timer-test": "failed",
        "queue-test": "failed",
        "sample_app-ALL-testrunner": "passed",
    }
    assert parse_test_outcomes(MESON_OUT) == {
        "demo:unit / parse": "passed",
        "demo:unit / emit": "failed",
        "demo:slow / soak": "skipped",
    }
    assert parse_test_outcomes("all good\n") == {}


def test_per_test_comparison_separates_regressions_from_preexisting_failures():
    base = {"a": "passed", "b": "failed", "c": "passed"}
    outcome, detail, extra = _compare_tests(base, {"a": "passed", "b": "failed", "c": "passed"})
    assert outcome == ValidationOutcome.PASSED
    assert "2 test(s) pass on both" in detail and "1 fail on both" in detail
    assert extra["tests"]["failing_on_baseline"] == ["b"]

    outcome, detail, _ = _compare_tests(base, {"a": "failed", "b": "failed", "c": "passed"})
    assert outcome == ValidationOutcome.FAILED and "fail with the patch: a" in detail

    outcome, detail, _ = _compare_tests(base, {"a": "passed", "b": "failed"})
    assert outcome == ValidationOutcome.FAILED and "did not run with the patch: c" in detail

    outcome, _, _ = _compare_tests({"b": "failed"}, {"b": "failed"})
    assert outcome == ValidationOutcome.NOT_EVALUATED  # nothing passes anywhere: no behavioural evidence


def test_validation_strength_of_records():
    rec = lambda kind, outcome: {"kind": kind, "outcome": outcome}  # noqa: E731
    assert validation_strength([rec("compile", "passed"), rec("mechanical-recheck", "passed")]) == "compile-only"
    assert validation_strength([rec("compile", "passed"), rec("testing", "not-evaluated")]) == "compile-only"
    assert validation_strength([rec("compile", "passed"), rec("testing", "passed")]) == "behavioural"
    assert validation_strength([rec("differential-testing", "passed")]) == "behavioural"


def _write(p: Path, text: str, mode: int | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode is not None:
        os.chmod(p, mode)


def test_detect_tests(tmp_path):
    mk = tmp_path / "mk"
    _write(mk / "Makefile", "all:\n\tcc -o app app.c\ncheck: all\n\t./app --self-test\nCHECK := x\n")
    _write(mk / "tests/run.sh", "#!/bin/sh\n", 0o755)
    found = detect_tests(mk)
    assert [(t["name"], t["run"]) for t in found] == [("make-check", "make check"), ("run", "./tests/run.sh")]

    cm = tmp_path / "cm"
    _write(cm / "CMakeLists.txt", "project(x C)\nenable_testing()\nadd_subdirectory(src)\n")
    t = detect_tests(cm, "cmake -S . -B out/rel && make -C out/rel")[0]
    assert t["run"] == "ctest --test-dir out/rel --output-on-failure" and t["per_test"]
    t = detect_tests(cm, "mkdir -p b2 && cd b2 && cmake .. && make")[0]
    assert t["build_dir"] == "b2"
    assert "assumed" in detect_tests(cm, "make")[0]["why"]

    ms = tmp_path / "ms"
    _write(ms / "meson.build", "project('x', 'c')\nexe = executable('x', 'x.c')\ntest('basic', exe)\n")
    t = detect_tests(ms, "meson setup bd && ninja -C bd")[0]
    assert t["run"] == "meson test -C bd --print-errorlogs"

    am = tmp_path / "am"
    _write(am / "configure.ac", "AC_INIT([x], [1])\nAM_INIT_AUTOMAKE\n")
    assert detect_tests(am)[0]["run"] == "make check"

    assert detect_tests(tmp_path / "nothing-here") == []


def _config(tmp_path: Path, validation: dict | None = None, require: list[str] | None = None) -> Path:
    root = tmp_path / "p"
    _write(root / "Makefile", "app:\n\tcc -o app app.c\ntest: app\n\t./app\n")
    prof = {
        "id": "native",
        "compile_commands": "cc.json",
        "capture": {"command": "make -B CC={cc}", "tools": {"cc": "gcc"}},
    }
    if validation:
        prof["validation"] = validation
    cfg = {"schema": "weaver.project/1", "project": {"name": "p"}, "profiles": [prof]}
    if require:
        cfg["acceptance"] = {"require": require}
    (root / "weaver.yaml").write_text("# a comment the editor will not keep\n" + yaml.safe_dump(cfg, sort_keys=False))
    return root


def test_configured_strength_levels(tmp_path):
    root = _config(tmp_path)
    st = configured_strength(load_project(root))
    assert st["level"] == "compile-only" and "nothing runs the changed program" in st["notes"][0]

    root = _config(tmp_path / "b", {"build": "make", "tests": [{"name": "t", "run": "make test"}]})
    assert configured_strength(load_project(root))["level"] == "behavioural-optional"

    root = _config(tmp_path / "c", {"tests": ["make test"]}, ["compile", "mechanical-recheck", "testing"])
    st = configured_strength(load_project(root))
    assert st["level"] == "behavioural" and st["required"] == ["testing"]
    assert any("without a validation build" in n for n in st["notes"])


def test_settings_round_trip_with_backup_and_rollback(tmp_path):
    root = _config(
        tmp_path, {"build": "make", "tests": [{"name": "unit", "run": ["./app", "--self-test"], "env": {"X": "1"}}]}
    )
    proj = load_project(root)
    st = read_settings(proj)
    prof = st["profiles"][0]
    assert prof["tests"] == [{"name": "unit", "run": "./app --self-test"}]
    assert prof["capture_build"] == "make -B CC=gcc"
    assert [s["run"] for s in prof["suggestions"]] == ["make test"]
    assert st["strength"]["level"] == "behavioural-optional"

    # add the detected test, keep the untouched one as written (argv form and env), require testing
    new, warnings = write_settings(
        proj,
        {
            "acceptance": {"require": ["compile", "mechanical-recheck", "testing"], "allow_provisional": False},
            "concurrency": "single-threaded",
            "profiles": [
                {"id": "native", "tests": [*prof["tests"], {"name": "make-test", "run": "make test", "timeout": 60}]}
            ],
        },
    )
    assert warnings == []
    raw = yaml.safe_load((root / "weaver.yaml").read_text())
    tests = raw["profiles"][0]["validation"]["tests"]
    assert tests[0] == {"name": "unit", "run": ["./app", "--self-test"], "env": {"X": "1"}}
    assert tests[1] == {"name": "make-test", "run": "make test", "cwd": "{workspace}", "timeout": 60.0}
    assert raw["preservation"]["concurrency"] == "single-threaded"
    assert "a comment" in (root / "weaver.yaml.bak").read_text()
    assert configured_strength(new)["level"] == "behavioural"

    # removing the build leaves a warning; dropping 'compile' from the policy is refused
    _, warnings = write_settings(new, {"profiles": [{"id": "native", "build": {"run": ""}}]})
    assert any("without a validation build" in w for w in warnings)
    before = (root / "weaver.yaml").read_text()
    with pytest.raises(ConfigError):
        write_settings(load_project(root), {"acceptance": {"require": ["mechanical-recheck"]}})
    with pytest.raises(ConfigError):
        write_settings(load_project(root), {"flow": {"backend": "llvm"}})  # rejected on reload, file restored
    assert (root / "weaver.yaml").read_text() == before


def test_flow_backend_config(tmp_path):
    root = _config(tmp_path)
    cfg = yaml.safe_load((root / "weaver.yaml").read_text())
    for backend, expect in [("auto", ["svf", "gcc"]), ("gcc", ["gcc"]), ("none", []), (["svf"], ["svf"])]:
        cfg["flow"] = {"backend": backend}
        (root / "weaver.yaml").write_text(yaml.safe_dump(cfg))
        p = load_project(root)
        assert p.flow.backends == expect
        assert p.flow.svf_enabled == ("svf" in expect)
    cfg["flow"] = {"agreement": "most"}
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError):
        load_project(root)


def test_settings_keep_a_task_model(tmp_path):
    """Saving other settings leaves a declared task model alone."""
    root = _config(tmp_path)
    cfg = yaml.safe_load((root / "weaver.yaml").read_text())
    tasks = {"model": "tasks", "tasks": [{"name": "main", "entry": "main"}]}
    cfg["preservation"] = {"concurrency": tasks}
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg))
    proj = load_project(root)
    assert read_settings(proj)["concurrency"] == tasks
    write_settings(proj, {"acceptance": {"require": ["compile", "mechanical-recheck"]}})
    assert yaml.safe_load((root / "weaver.yaml").read_text())["preservation"]["concurrency"] == tasks
