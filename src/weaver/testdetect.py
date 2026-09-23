"""Find the project's own test commands, so validation can run the program and not only compile it.

A transaction validated only by compiling and re-checking the patched AST is
known to build and to have the intended shape; nothing has executed it.  Most C
projects already carry a test entry point that a contributor would run by hand,
and this module looks for the conventional ones:

* Make: a ``test``, ``tests`` or ``check`` target (``make check`` for Automake);
* CMake: ``enable_testing()`` / ``add_test()`` / ``include(CTest)`` -> ``ctest``
  in the build directory the build command configures;
* Meson: ``test()`` in a ``meson.build`` -> ``meson test`` in its build directory;
* scripts: ``test.sh``, ``run_tests.sh``, ``check.sh`` (also under ``tests/``).

Suggestions are proposals shown at setup and in the settings editor; nothing
is added to ``weaver.yaml`` without the user choosing it.  Each carries the
reason it was suggested, and whether the runner reports individual tests
(CTest, Meson), in which case validation compares baseline and candidate test
by test instead of by exit status.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

_MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
_MAKE_TARGET = re.compile(r"^(test|tests|check)\s*:(?!=)", re.M)
_CMAKE_TESTS = re.compile(r"^\s*(enable_testing\s*\(|add_test\s*\(|include\s*\(\s*CTest\b)", re.M | re.I)
_MESON_TESTS = re.compile(r"^\s*(?:\w+\s*=\s*)?test\s*\(", re.M)
_SCRIPTS = (
    "test.sh",
    "run_tests.sh",
    "run-tests.sh",
    "check.sh",
    "tests/run.sh",
    "tests/run_tests.sh",
    "tests/test.sh",
)
_SKIP_DIRS = {".git", ".weaver", "node_modules", "__pycache__"}


def _read(p: Path, limit: int = 2_000_000) -> str:
    try:
        return p.read_text(errors="replace")[:limit]
    except OSError:
        return ""


def _walk(root: Path, names: set[str], depth: int = 3) -> list[Path]:
    out = []
    base = len(root.parts)
    for d, dirs, files in os.walk(root):
        dp = Path(d)
        if len(dp.parts) - base >= depth:
            dirs[:] = []
        dirs[:] = sorted(x for x in dirs if x not in _SKIP_DIRS and not x.startswith("build"))
        out.extend(dp / f for f in sorted(files) if f in names)
    return out


def _build_dir(build: str | None, tool: str) -> str | None:
    """The build directory a configure command names (``cmake -B dir``, ``meson setup dir``, ``-C dir``)."""
    if not build:
        return None
    try:
        words = shlex.split(build.replace("&&", " ; ").replace(";", " ; "))
    except ValueError:
        words = build.split()
    for i, w in enumerate(words):
        if tool == "cmake":
            if w == "-B" and i + 1 < len(words):
                return words[i + 1]
            if w.startswith("-B") and len(w) > 2:
                return w[2:]
            if w == "--build" and i + 1 < len(words):
                return words[i + 1]
        if tool == "meson":
            if w in ("setup",) and i > 0 and words[i - 1].endswith("meson"):
                rest = [x for x in words[i + 1 :] if not x.startswith("-")]
                if rest and rest[0] != ";":
                    return rest[0]
            if w == "-C" and i + 1 < len(words) and ("ninja" in words[0] or "meson" in " ".join(words[:i])):
                return words[i + 1]
    if tool == "cmake":
        # "mkdir -p build && cd build && cmake .." style
        m = re.search(r"\bcd\s+(\S+)\s*(?:&&|;)\s*cmake\b", build)
        if m:
            return m.group(1)
    return None


def detect_tests(root: Path, build: str | None = None) -> list[dict[str, Any]]:
    """Suggested test commands for the project at ``root``, most specific first."""
    root = Path(root)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(name: str, run: str, why: str, per_test: bool = False, **extra: Any) -> None:
        if run in seen:
            return
        seen.add(run)
        out.append({"name": name, "run": run, "why": why, "per_test": per_test, **extra})

    cmake_lists = _walk(root, {"CMakeLists.txt"})
    if any(_CMAKE_TESTS.search(_read(p)) for p in cmake_lists):
        where = [str(p.relative_to(root)) for p in cmake_lists if _CMAKE_TESTS.search(_read(p))][:3]
        bdir = _build_dir(build, "cmake") or "build"
        add(
            "ctest",
            f"ctest --test-dir {shlex.quote(bdir)} --output-on-failure",
            f"CMake registers tests ({', '.join(where)}); run in the build directory '{bdir}'"
            + ("" if _build_dir(build, "cmake") else " (assumed: the build command does not name one)"),
            per_test=True,
            build_dir=bdir,
        )
    meson = [p for p in _walk(root, {"meson.build"}) if _MESON_TESTS.search(_read(p))]
    if meson:
        bdir = _build_dir(build, "meson") or "builddir"
        add(
            "meson-test",
            f"meson test -C {shlex.quote(bdir)} --print-errorlogs",
            f"Meson registers tests ({', '.join(str(p.relative_to(root)) for p in meson[:3])}); "
            f"build directory '{bdir}'",
            per_test=True,
            build_dir=bdir,
        )
    for mf in _MAKEFILES:
        p = root / mf
        if p.is_file():
            targets = sorted(set(_MAKE_TARGET.findall(_read(p))), key=("check", "test", "tests").index)
            for t in targets:
                add(f"make-{t}", f"make {t}", f"{mf} defines a '{t}' target")
            break
    if (root / "Makefile.am").is_file() or re.search(r"AM_INIT_AUTOMAKE", _read(root / "configure.ac")):
        add("make-check", "make check", "Automake project: 'make check' runs the TESTS it declares")
    for s in _SCRIPTS:
        p = root / s
        if p.is_file():
            run = f"./{s}" if os.access(p, os.X_OK) else f"sh {s}"
            add(Path(s).stem.replace("_", "-"), run, f"test script {s}")
    return out
