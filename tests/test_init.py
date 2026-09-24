"""weaver init: the build, the compiler and the tests, read from the project instead of a template."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import HAVE_CLANG, HAVE_GCC, HAVE_MAKE, run_cli

from weaver import scaffold
from weaver.config import load_project

needs_cmake = pytest.mark.skipif(
    not (HAVE_CLANG and HAVE_GCC and HAVE_MAKE and shutil.which("cmake") and shutil.which("ctest")),
    reason="cmake, ctest, gcc, clang and make required",
)

CMAKELISTS = """\
cmake_minimum_required(VERSION 3.10)
project(t C)
option(T_TESTS "Build the tests" OFF)
option(T_EXTRA "Build the extra library" OFF)
option(T_FAST "Faster code" ON)
add_library(lib STATIC lib.c)
if(T_TESTS)
  enable_testing()
  add_executable(test_lib test_lib.c)
  target_link_libraries(test_lib lib)
  add_test(NAME lib COMMAND test_lib)
endif()
"""
LIB_C = "int twice(const int *p) { return 2 * *p; }\n"
TEST_C = "int twice(const int *p);\nint main(void) { int v = 21; return twice(&v) == 42 ? 0 : 1; }\n"
MAKEFILE = """\
CC = gcc -std=c99
all: app
app: lib.c
\t$(CC) -c lib.c -o lib.o
check: app
\ttrue
clean:
\trm -f lib.o
"""


def _cmake_project(tmp: Path, with_makefile: bool = True) -> Path:
    root = tmp / "t"
    root.mkdir()
    (root / "CMakeLists.txt").write_text(CMAKELISTS)
    (root / "lib.c").write_text(LIB_C)
    (root / "test_lib.c").write_text(TEST_C)
    if with_makefile:
        (root / "Makefile").write_text(MAKEFILE)
    return root


def _write(root: Path, p: scaffold.Plan, **kw) -> dict:
    (root / "weaver.yaml").write_text(scaffold.render(p, "t", **kw))
    load_project(root)  # it loads
    return yaml.safe_load((root / "weaver.yaml").read_text())


def test_cmake_is_chosen_when_it_builds_the_tests(tmp_path):
    root = _cmake_project(tmp_path)
    p = scaffold.plan(root)
    assert p.system == "cmake" and "builds tests" in p.why
    assert p.options_on == ["T_TESTS"]  # the tests' calls are callers too, and validation runs them
    assert [o["name"] for o in p.options_off] == ["T_EXTRA"]  # listed for the user; T_FAST is already on
    cfg = _write(root, p)
    prof = cfg["profiles"][0]
    assert "-DT_TESTS=ON" in prof["capture"]["command"] and "-DT_TESTS=ON" in prof["validation"]["build"]["run"]
    assert prof["capture"]["command"].startswith("cmake -S . -B .weaver/build/capture ")
    assert prof["validation"]["tests"][0]["run"].startswith("ctest --test-dir .weaver-build")
    assert cfg["acceptance"]["require"] == ["compile", "mechanical-recheck", "testing"]
    text = (root / "weaver.yaml").read_text()
    assert "recorded_" not in text and "#   T_EXTRA: Build the extra library" in text
    if p.triple:
        assert prof["target"] == {"triple": p.triple}
    # asked for: the other option too, in both builds
    p = scaffold.plan(root, enable=["T_EXTRA"])
    assert p.options_on == ["T_TESTS", "T_EXTRA"] and not p.options_off
    assert "-DT_EXTRA=ON" in p.validate


def test_make_names_the_compiler_the_makefile_calls(tmp_path):
    root = tmp_path / "m"
    root.mkdir()
    (root / "lib.c").write_text(LIB_C)
    (root / "Makefile").write_text(MAKEFILE)
    p = scaffold.plan(root)
    assert p.system == "make" and p.compiler == "gcc" and "the Makefile sets CC = gcc" in p.notes[0]
    assert p.capture == "make -B" and p.clean == "make clean"
    assert p.validate == "make clean >/dev/null 2>&1; make -B"  # a copied, already-built tree builds again
    assert [t["run"] for t in p.tests] == ["make check"]
    cfg = _write(root, p, concurrency="single-threaded", compare="./app --self-test")
    tools = cfg["profiles"][0]["capture"]["tools"]
    assert "gcc" in tools  # the shim takes the name the Makefile calls, so '-std=c99' is recorded with it
    assert cfg["preservation"]["concurrency"] == "single-threaded"
    assert cfg["profiles"][0]["validation"]["compare"][0]["run"] == "./app --self-test"
    assert "differential-testing" in cfg["acceptance"]["require"]
    # CMakeLists without tests next to a Makefile: the Makefile it is
    (root / "CMakeLists.txt").write_text("project(m C)\nadd_library(lib lib.c)\n")
    assert scaffold.plan(root).system == "make"


def test_a_build_weaver_cannot_read_needs_the_command(tmp_path, capsys):
    root = tmp_path / "x"
    root.mkdir()
    (root / "build.sh").write_text("cc -c lib.c\n")
    with pytest.raises(ValueError, match="give the build command"):
        scaffold.plan(root)
    assert run_cli(root, "init", "--yes") == 2
    assert "weaver init --build" in capsys.readouterr().err
    assert run_cli(root, "init", "--yes", "--build", "sh build.sh", "--test", "sh test.sh", "--cc", "gcc") == 0
    cfg = yaml.safe_load((root / "weaver.yaml").read_text())
    prof = cfg["profiles"][0]
    assert prof["capture"]["command"] == "sh build.sh" and prof["validation"]["build"]["run"] == "sh build.sh"
    assert prof["validation"]["tests"] == [{"name": "sh-test.sh", "run": "sh test.sh", "cwd": "{workspace}"}]
    assert run_cli(root, "init", "--yes") == 2  # it exists: --force to overwrite
    assert "exists" in capsys.readouterr().err


def test_each_choice_can_be_changed_when_asked(tmp_path):
    root = _cmake_project(tmp_path)
    answers = iter(["", "", "T_EXTRA", "", "", "n", "ctest --test-dir .weaver-build -R lib", "./t", "y"])
    asked: list[str] = []

    def ask(q: str, default: str) -> str:
        asked.append(q)
        return next(answers)

    p, extra = scaffold.interactive(scaffold.plan(root), root, ask)
    assert p.options_on == ["T_TESTS", "T_EXTRA"] and "-DT_EXTRA=ON" in p.capture
    assert [t["run"] for t in p.tests] == ["ctest --test-dir .weaver-build -R lib"]
    assert extra == {"compare": "./t", "concurrency": "single-threaded"}
    assert any("CMake options that are off: T_EXTRA" in q for q in asked)


@needs_cmake
def test_init_then_capture_analyses_the_tests_and_validates_with_the_same_build(tmp_path, capsys):
    root = _cmake_project(tmp_path, with_makefile=False)
    assert run_cli(root, "init", "--yes", "--concurrency", "single-threaded") == 0
    assert "next:" in capsys.readouterr().out
    assert run_cli(root, "refresh", "--capture", "--no-flow") == 0
    inv = json.loads((root / ".weaver/analysis/inventory.json").read_text())
    assert {Path(u["file"]).name for u in inv["units"]} == {"lib.c", "test_lib.c"}  # the tests are analysed
    capsys.readouterr()
    run_cli(root, "doctor", "--build", "--json")
    res = json.loads(capsys.readouterr().out)
    check = next(c for c in res["checks"] if c["name"] == "build configuration")
    assert check["status"] == "ok", check

    from weaver.ledger import Ledger

    led = Ledger(load_project(root))
    diff = "--- a/lib.c\n+++ b/lib.c\n@@ -1,1 +1,1 @@\n-" + LIB_C + "+" + LIB_C.replace("2 * *p", "*p * 2")
    txn = led.validate(led.propose_patch(diff, [], "commute")["id"])
    rec = {r["kind"]: r for r in txn["validation"]["records"]}
    assert txn["state"] == "validated" and rec["testing"]["outcome"] == "passed"
    assert rec["configuration"]["outcome"] == "passed"


def test_the_web_setup_form_uses_the_same_detection(tmp_path):
    from test_web import Client

    from weaver.web.server import App

    root = _cmake_project(tmp_path)
    c = Client(App())
    try:
        p = c.api(f"detect-setup?path={root}")
        assert p["system"] == "cmake" and p["options_on"] == ["T_TESTS"]
        assert c.api(f"detect-setup?path={tmp_path}")["system"] is None  # nothing to read there
        c.api("setup", {"path": str(root), "build": p["capture"], "compiler": p["compiler"],
                        "tests": [t["run"] for t in p["tests"]]})  # fmt: skip
        prof = yaml.safe_load((root / "weaver.yaml").read_text())["profiles"][0]
        assert prof["validation"]["build"]["run"] == p["validate"]  # the detected build, kept whole
    finally:
        c.close()
