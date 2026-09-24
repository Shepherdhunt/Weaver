"""``weaver doctor``: is this machine, and this project, ready for Weaver?

Every check says what it found, what depends on it, and how to fix it on this platform.  A tool that
is missing shows up here, before a run, instead of later as an empty inventory or an ``unsupported``
unit.  Checks that can be done by running the tool are done that way: a compiler that is installed
but cannot link a coverage build, or a GCC without its LTO plugin, is found by compiling a few lines.

Statuses: ``ok``; ``warn`` (something works less well or not at all, but Weaver runs); ``fail``
(Weaver cannot do its job, or will silently do less than it says); ``info`` (worth knowing).
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from weaver.util import run

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
TESTED = {"clang": 18, "gcc": 13}  # the versions the test suite and pilots ran with
MIN_CLANG = 14  # older Clangs lack parts of the JSON AST Weaver reads

PROBE_C = "int weaver_doctor(int *p) { return *p; }\nint main(void) { int v = 0; return weaver_doctor(&v); }\n"


@dataclass
class Check:
    section: str
    name: str
    status: str
    detail: str
    fix: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Install hints
# ---------------------------------------------------------------------------

PACKAGES: dict[str, dict[str, str]] = {
    "clang": {"apt": "clang", "dnf": "clang", "brew": "llvm"},
    "llvm": {"apt": "llvm", "dnf": "llvm", "brew": "llvm"},
    "gcc": {"apt": "gcc", "dnf": "gcc", "brew": "gcc"},
    "binutils": {"apt": "binutils", "dnf": "binutils", "brew": "binutils"},
    "clang-rt": {"apt": "libclang-rt-{major}-dev", "dnf": "compiler-rt", "brew": "llvm"},
    "make": {"apt": "make", "dnf": "make", "brew": "make"},
    "cmake": {"apt": "cmake", "dnf": "cmake", "brew": "cmake"},
    "ninja": {"apt": "ninja-build", "dnf": "ninja-build", "brew": "ninja"},
    "git": {"apt": "git", "dnf": "git", "brew": "git"},
}


def _os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            k, _, v = line.partition("=")
            out[k] = v.strip().strip('"')
    except OSError:
        pass
    return out


def package_manager() -> str | None:
    if sys.platform == "darwin":
        return "brew"
    if not sys.platform.startswith("linux"):
        return None
    rel = _os_release()
    ids = {rel.get("ID", "")} | set(rel.get("ID_LIKE", "").split())
    if ids & {"debian", "ubuntu"}:
        return "apt"
    if ids & {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        return "dnf"
    return None


def install_hint(key: str, **fmt: str) -> str:
    pkg = PACKAGES.get(key, {})
    m = package_manager()
    name = pkg.get(m or "", "").format(**fmt) if m else ""
    if m == "apt" and name:
        return f"sudo apt-get install {name}"
    if m == "dnf" and name:
        return f"sudo dnf install {name}"
    if m == "brew" and name:
        return f"brew install {name}"
    return f"install {key} with your system's package manager"


def _platform() -> str:
    if sys.platform.startswith("linux"):
        rel = _os_release()
        return f"Linux {platform.machine()}" + (f" ({rel['PRETTY_NAME']})" if rel.get("PRETTY_NAME") else "")
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]} {platform.machine()}"
    return f"{platform.system()} {platform.release()} {platform.machine()}"


# ---------------------------------------------------------------------------
# This machine
# ---------------------------------------------------------------------------


def _gib(n: float) -> str:
    return f"{n / 2**30:.1f} GiB"


def _available_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def system_checks(where: Path) -> list[Check]:
    s = "System"
    out: list[Check] = []
    v = sys.version_info
    out.append(
        Check(s, "Python", OK, f"{v.major}.{v.minor}.{v.micro}")
        if v >= (3, 10)
        else Check(s, "Python", FAIL, f"{v.major}.{v.minor}: Weaver needs 3.10 or later", "install Python 3.10+")
    )
    try:
        import yaml

        out.append(Check(s, "PyYAML", OK, yaml.__version__))
    except ImportError:
        out.append(Check(s, "PyYAML", FAIL, "not installed", "pip install PyYAML"))
    if sys.platform.startswith("linux"):
        out.append(Check(s, "Platform", OK, _platform()))
    elif sys.platform == "darwin":
        out.append(Check(s, "Platform", WARN, f"{_platform()}: untested; Linux is the tested platform"))
    else:
        out.append(
            Check(
                s,
                "Platform",
                FAIL,
                f"{_platform()}: build capture uses POSIX shell scripts as compiler shims",
                "run Weaver under WSL 2 or in the Linux container",
            )
        )
    if not (shutil.which("sh") or Path("/bin/sh").exists()):
        out.append(Check(s, "POSIX shell", FAIL, "no 'sh': the capture and coverage shims cannot run"))
    year = time.gmtime().tm_year
    if year < 2024:
        out.append(
            Check(
                s,
                "Clock",
                WARN,
                f"the system clock reads {time.strftime('%Y-%m-%d', time.gmtime())}: TLS to git hosts, "
                "package indexes and AI providers fails, and timestamps in the ledger are wrong",
                "set the clock (for example: sudo timedatectl set-ntp true)",
            )
        )
    mem = _available_memory()
    if mem is not None:
        msg = f"{_gib(mem)} available, {os.cpu_count() or 1} CPU(s)"
        if mem < 2 * 2**30:
            out.append(
                Check(
                    s,
                    "Memory",
                    WARN,
                    msg + ": whole-program evaluation of a large project needs about 1 GiB on top of the builds",
                )  # fmt: skip
            )
        else:
            out.append(Check(s, "Memory", OK, msg))
    probe = where if where.exists() else Path.cwd()
    free = shutil.disk_usage(probe).free
    msg = f"{_gib(free)} free at {probe}"
    if free < 2**30:
        out.append(Check(s, "Disk", FAIL, msg + ": evidence and validation workspaces will not fit"))
    elif free < 5 * 2**30:
        out.append(Check(s, "Disk", WARN, msg + ": a large project's evidence and workspaces take several GiB"))
    else:
        out.append(Check(s, "Disk", OK, msg))
    return out


def _compile(argv: list[str], tmp: Path, timeout: float = 120) -> tuple[bool, str]:
    src = tmp / "doctor.c"
    if not src.exists():
        src.write_text(PROBE_C)
    r = run(argv, cwd=tmp, timeout=timeout)
    lines = [ln for ln in r.stderr_text(4000).strip().splitlines() if ln.strip()]
    return r.ok, (lines[-1] if lines else f"exit {r.returncode}")


def _identify() -> Any:
    """``identify`` without its cache: a tool installed after Weaver started must show up here."""
    from weaver.capture.toolid import identify

    return identify.__wrapped__


def _major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except ValueError:
        return 0


def compiler_checks(tmp: Path) -> tuple[list[Check], dict[str, Any]]:
    identify = _identify()
    from weaver.flow.svf import _llvm_link_for

    s = "Compilers"
    out: list[Check] = []
    found: dict[str, Any] = {}
    clang = identify("clang")
    if clang.path is None:
        out.append(
            Check(
                s,
                "clang",
                FAIL,
                "not found: Weaver reads C through Clang (the compiler itself for Clang builds, the secondary "
                "frontend for GCC builds) and builds the bitcode for SVF with it",
                install_hint("clang"),
            )
        )
    else:
        found["clang"] = clang
        major = _major(clang.version)
        ok, why = _compile([clang.path, "-fsyntax-only", "-Xclang", "-ast-dump=json", "doctor.c"], tmp)
        where = f"{clang.version} at {clang.path}"
        if not ok:
            out.append(Check(s, "clang", FAIL, f"{where}: cannot dump a JSON AST ({why})", install_hint("clang")))
        elif major < MIN_CLANG:
            out.append(
                Check(
                    s,
                    "clang",
                    WARN,
                    f"{where}: older than {MIN_CLANG}; tested with {TESTED['clang']}",
                    install_hint("clang"),
                )  # fmt: skip
            )
        else:
            out.append(Check(s, "clang", OK, f"{where}; JSON AST works"))
        link = _llvm_link_for(clang.path, clang.version)
        if link:
            out.append(Check(s, "llvm-link", OK, link))
        else:
            out.append(
                Check(
                    s,
                    "llvm-link",
                    WARN,
                    "not found: SVF points-to links each program's bitcode with it",
                    install_hint("llvm"),
                )  # fmt: skip
            )
    gcc = identify("gcc")
    if gcc.path is None or gcc.family != "gcc":
        what = f"'gcc' is {gcc.family} ({gcc.path})" if gcc.path else "not found"
        out.append(
            Check(s, "gcc", INFO, f"{what}: needed only for projects built with GCC, and for GCC's own points-to")
        )
    else:
        found["gcc"] = gcc
        out.append(Check(s, "gcc", OK, f"{gcc.version} at {gcc.path}"))
        ok, why = _compile([gcc.path, "-O2", "-flto", "-fipa-pta", "doctor.c", "-o", "lto"], tmp)
        ar = shutil.which("gcc-ar") or shutil.which("ar")
        if ok and ar:
            out.append(Check(s, "GCC LTO", OK, f"-flto -fipa-pta links; archives with {ar}"))
        else:
            out.append(
                Check(
                    s,
                    "GCC LTO",
                    WARN,
                    f"GCC points-to (flow.backend gcc) needs LTO and gcc-ar: {why if not ok else 'no gcc-ar or ar'}",
                    install_hint("binutils"),
                )  # fmt: skip
            )
    missing = [t for t in ("nm", "ar") if not shutil.which(t)]
    if missing:
        out.append(
            Check(
                s,
                "binutils",
                WARN,
                f"{', '.join(missing)} not found: the link model reads archive members and shared-object exports",
                install_hint("binutils"),
            )  # fmt: skip
        )
    else:
        out.append(Check(s, "binutils", OK, "nm and ar found"))
    return out, found


def coverage_checks(tmp: Path, found: dict[str, Any]) -> list[Check]:
    """Coverage of the changed lines: the reader must exist and the compiler must link a --coverage build."""
    s = "Coverage"
    out: list[Check] = []
    for fam, tool in found.items():
        major = str(_major(tool.version) or "")
        ok, why = _compile([tool.path, "--coverage", "doctor.c", "-o", f"cov-{fam}"], tmp)
        names = [f"gcov-{major}", "gcov"] if fam == "gcc" else [f"llvm-cov-{major}", "llvm-cov"]
        reader = next((shutil.which(n) for n in names if shutil.which(n)), None)
        name = f"{fam} --coverage"
        if not ok:
            rt = fam == "clang" and ("profile" in why or "linker command failed" in why)
            fix = install_hint("clang-rt", major=major) if rt else install_hint(fam)
            what = "the profile runtime (libclang_rt.profile) is missing" if rt else why
            out.append(
                Check(
                    s,
                    name,
                    WARN,
                    f"cannot link a coverage build: {what}. Validation then says the changed "
                    "lines' coverage was not measured",
                    fix,
                )  # fmt: skip
            )
        elif reader is None:
            out.append(
                Check(
                    s,
                    name,
                    WARN,
                    f"builds, but {names[-1]} is not installed to read the counts",
                    install_hint("gcc" if fam == "gcc" else "llvm"),
                )  # fmt: skip
            )
        else:
            out.append(Check(s, name, OK, f"links; read with {reader}"))
    return out


def points_to_checks(project: Any, found: dict[str, Any]) -> list[Check]:
    from weaver.flow.svf import find_wpa

    s = "Points-to"
    stub = project if project is not None else SimpleNamespace(flow=SimpleNamespace(wpa=None))
    svf = find_wpa(stub)
    out: list[Check] = []
    if svf:
        r = run([svf["wpa"], "-help"], env=svf.get("env") or None, timeout=60)
        text = r.stderr_text(2000)
        if r.returncode in (0, 1) and "error while loading" not in text:
            out.append(Check(s, "SVF", OK, f"{svf['wpa']} ({svf.get('source')})"))
        elif "error while loading shared libraries" in text:
            out.append(Check(s, "SVF", INFO, f"{svf['wpa']}: a shared library is missing under its versioned name; "
                             "Weaver links it in .weaver/flow on first use"))  # fmt: skip
        else:
            out.append(Check(s, "SVF", WARN, f"{svf['wpa']} does not run: {text.strip()[:200]}",
                             "pip install --force-reinstall 'weaver[flow]'"))  # fmt: skip
    else:
        gcc_ok = "gcc" in found
        out.append(
            Check(
                s,
                "SVF",
                INFO if gcc_ok else WARN,
                "not installed (optional, AGPL-3.0, runs as a separate process)"
                + ("; GCC's own points-to is used" if gcc_ok else "; with no GCC either, there is no points-to"),
                "pip install 'weaver[flow]'",
            )
        )
    return out


def build_tool_checks() -> list[Check]:
    s = "Build tools"
    out: list[Check] = []
    for t in ("make", "cmake", "ninja", "meson"):
        p = shutil.which(t)
        out.append(Check(s, t, OK if p else INFO, p or "not found (needed only if your build uses it)"))
    g = shutil.which("git")
    out.append(
        Check(s, "git", OK, g)
        if g
        else Check(
            s,
            "git",
            WARN,
            "not found: 'ratchet --base', git snapshots and source revisions need it",
            install_hint("git"),
        )  # fmt: skip
    )
    return out


# ---------------------------------------------------------------------------
# This project
# ---------------------------------------------------------------------------


def _is_shared(link: dict[str, Any]) -> bool:
    out = os.path.basename(link.get("output") or "")
    return "-shared" in link.get("argv", []) or out.endswith(".so") or ".so." in out or out.endswith(".dylib")


def project_checks(project: Any, coverage: dict[str, str] | None = None) -> list[Check]:
    from weaver.capture.compdb import load_compdb
    from weaver.capture.toolid import resolve_executable

    identify = _identify()
    from weaver.coverage import _family
    from weaver.errors import ConfigError
    from weaver.store import Store
    from weaver.util import read_json

    s = f"Project {project.name}"
    out: list[Check] = []
    configured = {"testing": False, "differential-testing": False, "build": False}
    for prof in project.profiles:
        ps = f"{s}, profile {prof.id}"
        if prof.capture is not None:
            for name, real in prof.capture.tools.items():
                if resolve_executable(real) is None:
                    out.append(Check(ps, "capture tool", FAIL, f"'{name}' names {real}, which is not installed"))
        cmds: list[Any] = []
        if not prof.compile_commands.exists():
            if prof.capture is not None:
                out.append(Check(ps, "compile commands", WARN, f"{prof.compile_commands} does not exist yet",
                                 "weaver refresh --capture"))  # fmt: skip
            else:
                out.append(
                    Check(ps, "compile commands", FAIL,
                          f"{prof.compile_commands} does not exist and the profile has no capture command",
                          "add 'capture: {command: ..., tools: {gcc: gcc}}' to the profile (see docs/onboarding.md)")
                )  # fmt: skip
        else:
            try:
                cmds = load_compdb(prof.compile_commands)
            except (ConfigError, ValueError, KeyError) as e:
                out.append(Check(ps, "compile commands", FAIL, f"unreadable: {e}"))
            else:
                n = len({c.file for c in cmds})
                out.append(Check(ps, "compile commands", OK if cmds else FAIL,
                                 f"{len(cmds)} command(s) for {n} source file(s)"))  # fmt: skip
        for comp in list(dict.fromkeys(c.compiler for c in cmds))[:4]:
            d = cmds[0].directory
            tool = identify(comp, d)
            if tool.path is None:
                out.append(Check(ps, "compiler", FAIL, f"{comp} is not on this machine (captured elsewhere?)",
                                 "capture the build again here: weaver refresh --capture"))  # fmt: skip
                continue
            out.append(Check(ps, "compiler", OK, f"{tool.family} {tool.version} ({tool.path})"))
            if tool.family == "clang":
                continue
            sec = prof.secondary_frontend
            if sec is None:
                out.append(
                    Check(ps, "AST reader", FAIL,
                          f"{tool.family} gives no AST Weaver can read, and there is no secondary frontend (no clang "
                          "on PATH, or 'secondary_frontend: false'): every unit is collected without pointer facts",
                          "install clang and remove 'secondary_frontend: false', or name one: "
                          "'secondary_frontend: {compiler: clang-18}'")
                )  # fmt: skip
            else:
                st = identify(sec.compiler)
                how = "the clang on PATH" if sec.auto else f"'{sec.compiler}' from secondary_frontend"
                if st.path is None:
                    out.append(Check(ps, "AST reader", FAIL, f"secondary frontend {how} is not installed",
                                     install_hint("clang")))  # fmt: skip
                else:
                    what = f"clang {st.version}: {how} reads the AST of {tool.family} units"
                    out.append(Check(ps, "AST reader", OK, what))
        v = prof.validation
        configured["build"] |= v.build is not None
        configured["testing"] |= bool(v.tests)
        configured["differential-testing"] |= bool(v.compare)
        if not v.tests and not v.compare:
            out.append(
                Check(ps, "validation", WARN,
                      "no tests and no differential run: validation is compile-only and never runs the program",
                      "weaver tests (detects make check, CTest, Meson), then 'validation.tests' in weaver.yaml")
            )  # fmt: skip
        else:
            what = [f"{len(v.tests)} test command(s)"] if v.tests else []
            what += [f"{len(v.compare)} differential run(s)"] if v.compare else []
            if v.build is None:
                out.append(Check(ps, "validation", WARN, ", ".join(what) + ", but no validation.build: the "
                                 "tests run whatever the workspace already contains"))  # fmt: skip
            else:
                out.append(Check(ps, "validation", OK, "build, " + ", ".join(what)))
            if v.coverage and cmds:
                comp = resolve_executable(cmds[0].compiler, cmds[0].directory)
                fam = _family(comp)[0] if comp else ""
                state = (coverage or {}).get(fam)
                if state == OK:
                    out.append(Check(ps, "coverage", OK, f"{fam} builds with --coverage: validation reports which "
                                     "changed lines the tests ran"))  # fmt: skip
                elif state is not None:
                    out.append(
                        Check(
                            ps,
                            "coverage",
                            WARN,
                            f"{fam} cannot build with --coverage here (see Coverage "
                            "above): validation will say the changed lines' coverage was not measured",
                        )
                    )
                    # fmt: skip
        placeholders = [f"{k}={val}" for k, val in {**prof.target, **prof.platform}.items()
                        if isinstance(val, str) and val.startswith("recorded_")]  # fmt: skip
        if placeholders:
            out.append(Check(ps, "target facts", INFO,
                             f"placeholders from 'weaver init' ({', '.join(placeholders[:3])}): candidate cards "
                             "show them as the target"))  # fmt: skip
        links = prof.compile_commands.parent / "links.json"
        if links.exists() and not project.programs:
            try:
                shared = sorted({os.path.basename(x.get("output") or "") for x in read_json(links) if _is_shared(x)})
            except (OSError, ValueError, AttributeError):
                shared = []
            if shared:
                out.append(
                    Check(ps, "programs", INFO,
                          f"{len(shared)} shared object(s) ({', '.join(shared[:3])}) and no 'programs:': their "
                          "exported functions count as callable from outside, so recipes leave their signatures alone",
                          "declare the programs and their entry points (docs/onboarding.md, step 6)")
                )  # fmt: skip
    for kind in project.acceptance.require:
        if kind in ("testing", "differential-testing") and not configured[kind]:
            what = "tests" if kind == "testing" else "differential runs"
            out.append(
                Check(s, "acceptance", FAIL,
                      f"acceptance requires '{kind}' but no profile configures {what}: every transaction will stay "
                      "provisional", "configure them, or remove the requirement from 'acceptance.require'")
            )  # fmt: skip
    if not project.preservation.get("concurrency"):
        out.append(
            Check(s, "concurrency", INFO,
                  "not declared: 'no concurrent writers' stays unresolved, so scalar-input never becomes eligible",
                  "declare 'preservation: {concurrency: single-threaded}' if true, or the program's tasks")
        )  # fmt: skip
    if project.ai.enabled:
        from weaver.llm.keys import key_status

        status = key_status(project.ai)
        if project.ai.provider == "anthropic":
            import importlib.util

            if importlib.util.find_spec("anthropic") is None:
                out.append(Check(s, "AI", FAIL, "AI is on with provider anthropic, but its SDK is not installed",
                                 "pip install 'weaver[llm]'"))  # fmt: skip
        out.append(
            Check(s, "AI key", WARN, "missing", "set the provider's environment variable, or: weaver ai key")
            if status == "missing"
            else Check(s, "AI key", OK, status)
        )
    store = Store(project.state_dir)
    if not store.inventory_path.exists():
        out.append(Check(s, "analysis", INFO, "not analysed yet", "weaver refresh"))
    else:
        try:
            summ = read_json(store.inventory_path).get("summary", {})
        except (OSError, ValueError):
            summ = {}
        no_ast = summ.get("units_without_ast") or 0
        line = f"{summ.get('findings', '?')} pointer(s) in {summ.get('units', '?')} unit(s)"
        if no_ast:
            out.append(Check(s, "analysis", FAIL, f"{line}; {no_ast} unit(s) have no AST evidence and no pointers",
                             "see the AST reader checks above, then weaver refresh"))  # fmt: skip
        else:
            out.append(Check(s, "analysis", OK, line))
    return out


# ---------------------------------------------------------------------------


def doctor(project: Any = None, project_error: str | None = None, machine: bool = True) -> dict[str, Any]:
    from weaver import __version__

    where = project.root if project is not None else Path.cwd()
    checks: list[Check] = []
    found: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="weaver-doctor-") as t:
        tmp = Path(t)
        if machine:
            checks += system_checks(where)
            comp, found = compiler_checks(tmp)
            checks += comp
            checks += coverage_checks(tmp, found)
            checks += points_to_checks(project, found)
            checks += build_tool_checks()
    if project is not None:
        cov = {c.name.split()[0]: c.status for c in checks if c.section == "Coverage"}
        checks += project_checks(project, cov if machine else None)
    elif project_error:
        checks.append(Check("Project", "weaver.yaml", FAIL, project_error))
    counts = {k: sum(1 for c in checks if c.status == k) for k in (FAIL, WARN, INFO, OK)}
    return {
        "weaver": __version__,
        "platform": _platform(),
        "project": {"name": project.name, "config": str(project.config_path)} if project is not None else None,
        "checks": [c.to_json() for c in checks],
        "counts": counts,
        "ok": counts[FAIL] == 0,
    }


def render(res: dict[str, Any], width: int = 110) -> str:
    import textwrap

    lines = [f"Weaver {res['weaver']} on {res['platform']}"]
    pad = " " * 31
    section = None
    for c in res["checks"]:
        if c["section"] != section:
            section = c["section"]
            lines += ["", section]
        text = f"  {c['status']:<5} {c['name']:<22} {c['detail']}"
        wrap = {"break_on_hyphens": False, "break_long_words": False}
        lines += textwrap.wrap(text, width, subsequent_indent=pad, **wrap) or [text]
        if c["fix"] and c["status"] in (FAIL, WARN, INFO):
            lines += textwrap.wrap(
                f"fix: {c['fix']}", width, initial_indent=pad, subsequent_indent=pad + "     ", break_long_words=False
            )
    no_project = res["project"] is None and not any(c["section"] == "Project" for c in res["checks"])
    if no_project and not res.get("machine_only"):
        lines += ["", "No weaver.yaml here: machine checks only. 'weaver init' or 'weaver serve' sets up a project."]
    n = res["counts"]
    verdict = ": Weaver is ready." if n[FAIL] == 0 and n[WARN] == 0 else "." if n[FAIL] == 0 else ": fix those first."
    lines += ["", f"{n[FAIL]} problem(s), {n[WARN]} warning(s){verdict}"]
    return "\n".join(lines)
