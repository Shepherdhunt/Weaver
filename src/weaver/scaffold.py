"""``weaver init``: a ``weaver.yaml`` from what the project already says about its build.

The first run on cJSON showed what a template leaves to the user: which build to capture, how to
name the compiler so the capture sees the build's own flags, which build and tests validation should
run, and that the two must be the same configuration. This module reads the project instead:

* the build system (CMake, Meson, Autotools, Make, in that order; CMake before Make only when both
  exist, since a project that has both usually builds its tests with CMake);
* the compiler: ``--cc``, else ``CC`` from the environment, else the one a Makefile names in ``CC``,
  else ``cc``; the capture shim takes the name the build calls, so its flags are recorded;
* CMake ``option()``s: those about tests are turned on (the tests' calls are callers too, and
  validation runs them); the others are listed for the user to decide;
* the tests the project registers (``weaver.testdetect``).

It writes one profile whose capture and validation build use the same configuration, from a clean
build directory Weaver owns, and the tests to run. It records only facts it can read (the compiler's
target triple); nothing is a placeholder.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from weaver.util import run

CAPTURE_DIR = ".weaver/build/capture"  # inside the state dir: never copied into validation workspaces
VALIDATE_DIR = ".weaver-build"  # inside each validation workspace, which is a fresh copy
SYSTEMS = ("cmake", "meson", "autotools", "make", "custom")

_OPTION = re.compile(r'^\s*option\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s+"([^"]*)"\s*([A-Za-z0-9_${}]*)\s*\)', re.M | re.I)
_MAKE_CC = re.compile(r"^\s*CC\s*[:?]?=\s*(\S+)", re.M)
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", re.M)
_BUILT_DIRS = ("build", "builddir", "_build", "out", "cmake-build-debug", "cmake-build-release")


@dataclass
class Plan:
    system: str
    why: str
    capture: str
    clean: str | None
    validate: str | None
    tests: list[dict[str, Any]] = field(default_factory=list)
    compiler: str = "cc"  # the name the build calls
    compiler_path: str | None = None
    triple: str | None = None
    options_on: list[str] = field(default_factory=list)  # CMake options this plan turns on
    options_off: list[dict[str, str]] = field(default_factory=list)  # the other CMake options that are off
    workspace_exclude: list[str] = field(default_factory=lambda: [".git"])
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _jobs() -> int:
    return max(1, min(os.cpu_count() or 2, 16))


def cmake_options(text: str) -> list[dict[str, str]]:
    out = []
    for name, help_, default in _OPTION.findall(text):
        on = default.upper() in ("ON", "TRUE", "YES", "Y", "1")
        out.append({"name": name, "help": help_, "default": "ON" if on else "OFF"})
    return out


def _is_test_option(o: dict[str, str]) -> bool:
    return bool(re.search(r"TEST", o["name"], re.I)) or "test" in o["help"].lower()


def _makefile(root: Path) -> Path | None:
    return next((root / n for n in ("GNUmakefile", "makefile", "Makefile") if (root / n).is_file()), None)


def detect_system(root: Path) -> tuple[str | None, str]:
    from weaver.testdetect import _CMAKE_TESTS

    cm = root / "CMakeLists.txt"
    mk = _makefile(root)
    if cm.is_file():
        if mk is None:
            return "cmake", "CMakeLists.txt"
        if _CMAKE_TESTS.search(_read(cm)) or any(_is_test_option(o) for o in cmake_options(_read(cm))):
            return "cmake", f"CMakeLists.txt, which builds tests; a {mk.name} exists too"
        return "make", f"{mk.name}; a CMakeLists.txt exists too, but builds no tests"
    if (root / "meson.build").is_file():
        return "meson", "meson.build"
    if mk is not None:
        return "make", mk.name
    if (root / "configure").is_file() or (root / "configure.ac").is_file():
        return "autotools", "configure" if (root / "configure").is_file() else "configure.ac"
    return None, "no CMakeLists.txt, meson.build, configure or Makefile"


def _compiler(root: Path, system: str, cc: str | None) -> tuple[str, str]:
    """(the name the build calls, why)."""
    detected = "cc", "the default C compiler"
    mk = _makefile(root)
    if os.environ.get("CC"):
        detected = os.environ["CC"].split()[0], "from $CC"
    elif system == "make" and mk is not None:
        m = _MAKE_CC.search(_read(mk))
        if m and not m.group(1).startswith("$"):
            detected = m.group(1), f"the {mk.name} sets CC = {m.group(1)}"
    return (cc, "given") if cc and cc != detected[0] else detected


def plan(
    root: Path,
    system: str | None = None,
    cc: str | None = None,
    build: str | None = None,
    tests: list[str] | None = None,
    enable: list[str] | None = None,
) -> Plan:
    """What ``weaver init`` would write for the project at ``root``."""
    from weaver.testdetect import detect_tests

    root = Path(root).resolve()
    found, why = detect_system(root)
    if build:
        system, why = "custom", "the build command given"
    elif system and system != found:
        why = {"make": (_makefile(root) or Path("a Makefile")).name, "cmake": "CMakeLists.txt",
               "meson": "meson.build", "autotools": "configure"}.get(system, system) + " (chosen)"  # fmt: skip
    system = system or found
    if system is None:
        raise ValueError(f"cannot tell how {root} is built ({why}); give the build command: weaver init --build '...'")
    name, cc_why = _compiler(root, system, cc)
    j = _jobs()
    p = Plan(system=system, why=why, capture="", clean=None, validate=None, compiler=name)
    p.compiler_path = shutil.which(name)
    p.notes.append(f"compiler: {name} ({cc_why})" + ("" if p.compiler_path else "; not found on PATH"))
    if p.compiler_path:
        r = run([p.compiler_path, "-dumpmachine"], timeout=30)
        p.triple = r.stdout_text(200).strip() or None if r.ok else None

    if system == "cmake":
        text = _read(root / "CMakeLists.txt")
        opts = cmake_options(text)
        auto = [o["name"] for o in opts if o["default"] == "OFF" and _is_test_option(o)]
        on = auto + [n for n in enable or [] if n not in auto]
        p.options_on = on
        p.options_off = [o for o in opts if o["default"] == "OFF" and o["name"] not in on]
        defs = " ".join(f"-D{n}=ON" for n in on)
        cfg = f"-DCMAKE_C_COMPILER={shlex.quote(name)}" + (f" {defs}" if defs else "")
        p.capture = f"cmake -S . -B {CAPTURE_DIR} {cfg} && cmake --build {CAPTURE_DIR} --parallel {j}"
        p.clean = f"rm -rf {CAPTURE_DIR}"
        p.validate = f"cmake -S . -B {VALIDATE_DIR} {cfg} >/dev/null && cmake --build {VALIDATE_DIR} --parallel {j}"
        found_tests = detect_tests(root, f"cmake -B {VALIDATE_DIR}")
        p.tests = [
            {**t, "run": f"ctest --test-dir {VALIDATE_DIR} --output-on-failure -j {j}"}
            for t in found_tests
            if t["name"] == "ctest"
        ] or [t for t in found_tests if t["name"] != "ctest" and not t["name"].startswith("make-")]
        if auto:
            p.notes.append(f"turned on {', '.join(auto)}: the tests are callers too, and validation runs them")
        if enable:
            p.notes.append(f"turned on as asked: {', '.join(n for n in enable if n not in auto)}")
    elif system == "meson":
        env = f"CC={shlex.quote(name)} "
        p.capture = f"{env}meson setup {CAPTURE_DIR} && meson compile -C {CAPTURE_DIR}"
        p.clean = f"rm -rf {CAPTURE_DIR}"
        p.validate = f"{env}meson setup {VALIDATE_DIR} >/dev/null && meson compile -C {VALIDATE_DIR}"
        p.tests = [
            {**t, "run": f"meson test -C {VALIDATE_DIR} --print-errorlogs"}
            for t in detect_tests(root, f"meson setup {VALIDATE_DIR}")
            if t["name"] == "meson-test"
        ]
    elif system == "autotools":
        conf = "./configure" if (root / "configure").is_file() else "autoreconf -fi && ./configure"
        p.capture = f"{conf} CC={shlex.quote(name)} && make -B"
        p.validate = f"{conf} CC={shlex.quote(name)} >/dev/null && make -B"
        p.tests = [t for t in detect_tests(root) if t["name"] == "make-check"]
        p.notes.append("Autotools builds in the source tree; not yet piloted")
    elif system == "make":
        mk = _makefile(root)
        targets = set(_MAKE_TARGET.findall(_read(mk))) if mk else set()
        p.capture = "make -B"
        p.clean = "make clean" if "clean" in targets else None
        # the workspace is a copy of a tree the capture built in: clean it first (a recipe like 'ln -s'
        # fails when its output exists), and -B, so no copied object counts as built
        p.validate = "make clean >/dev/null 2>&1; make -B" if p.clean else "make -B"
        found_tests = [t for t in detect_tests(root) if t["name"].startswith("make-")]
        runs = [t for t in found_tests if t["name"] in ("make-check", "make-test")]
        p.tests = (runs or found_tests)[:1] + [
            t for t in detect_tests(root) if not t["name"].startswith(("make-", "ctest", "meson"))
        ]
        if name != "cc":
            p.notes.append(f"the capture shim is named {name}, as the Makefile calls it, so its own flags are recorded")
    else:  # custom
        p.capture = build or ""
        p.validate = (build or "").replace("{cc}", name)
        p.tests = [t for t in detect_tests(root, build)]
    if tests is not None:
        p.tests = []
        for t in tests:
            n = _test_name(t)
            n = n if n not in {x["name"] for x in p.tests} else f"{n}-{len(p.tests)}"
            p.tests.append({"name": n, "run": t, "why": "given", "per_test": "ctest" in t or "meson test" in t})
    # existing build trees are not copied into validation workspaces
    for d in _BUILT_DIRS:
        bd = root / d
        if bd.is_dir() and any(
            (bd / m).exists() for m in ("CMakeCache.txt", "build.ninja", "meson-info", "config.status")
        ):
            p.workspace_exclude.append(d)
    return p


def _test_name(cmd: str) -> str:
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    if not words:
        return "test"
    if words[0] in ("make", "sh", "bash", "ninja", "meson") and len(words) > 1:
        base = f"{words[0]}-{Path(words[1]).name}"
    else:
        base = Path(words[0]).name
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-.")[:40] or "test"


def _q(s: str) -> str:
    return json.dumps(s)  # a YAML double-quoted scalar


def render(
    p: Plan,
    name: str,
    compare: str | None = None,
    concurrency: str | None = None,
    secondary: str | None = None,
) -> str:
    """The weaver.yaml text: what was chosen and why, with comments where the user decides."""
    require = ["compile", "mechanical-recheck"] + (["testing"] if p.tests else [])
    require += ["differential-testing"] if compare else []
    tools = {p.compiler: p.compiler_path or p.compiler}
    if "{cc}" in p.capture:  # the placeholder is the chosen compiler's shim
        tools["cc"] = p.compiler_path or p.compiler
    elif p.system in ("make", "custom") and p.compiler != "cc" and shutil.which("cc"):
        tools.setdefault("cc", shutil.which("cc") or "cc")  # a recipe that calls plain cc is recorded too
    L: list[str] = [
        "schema: weaver.project/1",
        f"# written by 'weaver init' from {p.why}. Check it with: weaver doctor",
        "project:",
        f"  name: {_q(name)}",
        "  root: .",
        "  state_dir: .weaver",
        "  # names of files or directories never copied into validation workspaces (the state dir never is)",
        f"  workspace_exclude: [{', '.join(_q(x) for x in p.workspace_exclude)}]",
        "",
        "# What a change must preserve; recorded on every candidate card.",
        "preservation:",
        "  behaviors: [outputs, persistent-state, side-effect-ordering, termination, errors, shared-state]",
    ]
    if concurrency:
        L.append(f"  concurrency: {concurrency}")
    else:
        L += [
            "  # Declare when true: nothing else writes a pointer's target during a call. Weaver checks that no",
            "  # call starts a thread. For multi-task programs declare the tasks (see docs/onboarding.md, step 5).",
            "  # concurrency: single-threaded",
        ]
    L += [
        "",
        "acceptance:",
        f"  require: [{', '.join(require)}]   # also: coverage, configuration",
        "",
        "# AI explanations and drafts: off. See 'weaver ai'.",
        "ai:",
        "  enabled: false",
        "",
        "profiles:",
        f"  - id: {p.system if p.system != 'custom' else 'default'}",
        f"    description: {_q(f'{p.system} build captured by weaver init')}",
        f"    compile_commands: .weaver/compdb/{p.system if p.system != 'custom' else 'default'}/compile_commands.json",
    ]
    if p.triple:
        L += ["    target:", f"      triple: {p.triple}   # from {p.compiler} -dumpmachine"]
    L += [
        "    # The build Weaver watches: a shim named like each tool comes first on PATH and records every",
        "    # compile with the build's own flags ({" + p.compiler + "} in the command is the shim's path).",
        "    capture:",
        f"      command: {_q(p.capture)}",
    ]
    if p.clean:
        L.append(f"      clean: {_q(p.clean)}")
    L.append("      tools: {" + ", ".join(f"{_q(k)}: {_q(v)}" for k, v in tools.items()) + "}")
    if p.options_off:
        L.append("    # CMake options that are off. Turn on the ones whose code you want analysed, in both the")
        L.append("    # capture and the validation build (-DNAME=ON), then: weaver refresh --capture")
        for o in p.options_off[:20]:
            L.append(f"    #   {o['name']}: {o['help']}")
    if secondary:
        L.append(f"    secondary_frontend: {{compiler: {_q(secondary)}}}")
    else:
        L += [
            "    # A GCC or other non-Clang build is read through the clang on PATH (the secondary frontend).",
            "    # secondary_frontend: {compiler: clang-18}      # or: secondary_frontend: false",
        ]
    L += [
        "    # How a change is checked: the same configuration as the capture, in a copy of the project.",
        "    validation:",
    ]
    if p.validate:
        L.append(f'      build: {{run: {_q(p.validate)}, cwd: "{{workspace}}"}}')
    if p.tests:
        L.append("      tests:")
        for t in p.tests:
            L.append(f'        - {{name: {_q(t["name"])}, run: {_q(t["run"])}, cwd: "{{workspace}}"}}   # {t["why"]}')
    else:
        L.append("      # tests: none found. Add the command that runs your tests:")
        L.append('      # tests: [{name: unit, run: "make check", cwd: "{workspace}"}]')
    if compare:
        L.append(f'      compare: [{{name: run, run: {_q(compare)}, cwd: "{{workspace}}"}}]')
    else:
        L.append("      # a program whose exit status and output must not change (deterministic output only):")
        L.append('      # compare: [{name: demo, run: "./demo --self-test", cwd: "{workspace}"}]')
    return "\n".join(L) + "\n"


def summary(p: Plan) -> list[str]:
    out = [f"build: {p.system} ({p.why})", f"  capture:    {p.capture}"]
    if p.validate:
        out.append(f"  validation: {p.validate}")
    for t in p.tests:
        out.append(f"  tests:      {t['run']}   ({t['why']})")
    if not p.tests:
        out.append("  tests:      none found: validation will only compile and re-check")
    out += [f"  note: {n}" for n in p.notes]
    if p.options_off:
        out.append(f"  CMake options that are off: {', '.join(o['name'] for o in p.options_off)}")
    return out


def interactive(p: Plan, root: Path, ask: Callable[[str, str], str]) -> tuple[Plan, dict[str, str | None]]:
    """Let the user confirm or change each choice; Enter keeps the detected value."""
    extra: dict[str, str | None] = {}
    sysname = ask(f"Build system [{p.system}] ({'/'.join(SYSTEMS)}): ", p.system).strip() or p.system
    if sysname != p.system:
        build = ask("Build command: ", "").strip() if sysname == "custom" else None
        p = plan(root, system=None if sysname == "custom" else sysname, cc=p.compiler, build=build or None)
    cc = ask(f"Compiler the build calls [{p.compiler}]: ", p.compiler).strip() or p.compiler
    if cc != p.compiler:
        p = plan(root, system=p.system if p.system != "custom" else None, cc=cc,
                 build=p.capture if p.system == "custom" else None)  # fmt: skip
    if p.options_off:
        names = ", ".join(o["name"] for o in p.options_off)
        more = ask(f"CMake options that are off: {names}\nTurn any on? (names, comma-separated) []: ", "").strip()
        if more:
            p = plan(
                root,
                system="cmake",
                cc=p.compiler,
                enable=p.options_on + [x.strip() for x in more.split(",") if x.strip()],
            )
    p.capture = ask(f"Capture command [{p.capture}]: ", p.capture).strip() or p.capture
    if p.validate:
        p.validate = ask(f"Validation build [{p.validate}]: ", p.validate).strip() or p.validate
    kept = []
    for t in p.tests:
        if ask(f"Run '{t['run']}' as a test ({t['why']})? [Y/n]: ", "y").strip().lower() in ("", "y", "yes"):
            kept.append(t)
    p.tests = kept
    other = ask("Another test command (Enter for none): ", "").strip()
    if other:
        p.tests.append({"name": _test_name(other), "run": other, "why": "given", "per_test": False})
    extra["compare"] = ask("A program run whose output must not change (Enter for none): ", "").strip() or None
    single = ask("Is the program single-threaded (nothing else writes during a call)? [y/N]: ", "n").strip().lower()
    extra["concurrency"] = "single-threaded" if single in ("y", "yes") else None
    return p, extra
