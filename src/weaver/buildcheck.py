"""Is the build that validation runs the build Weaver analysed?

Validation judges a change by building and testing it with the profile's validation commands. If that
build compiles other sources, with other defines or in another dialect, than the capture Weaver
analysed, its tests run code the analysis never saw. cJSON shows it: its Makefile (the capture) leaves
``ENABLE_LOCALES`` off and compiles no tests, while its CMake build (the validation) turns it on and
compiles 22 test programs.

The validation build of the unchanged tree runs through recording shims placed first on ``PATH``, one
per compiler name the builds use. Each shim writes its working directory and arguments to a file of
its own (so parallel compiles never interleave), then runs the real compiler with the same arguments
and exit status. Every recorded compile of a C source is mapped back to the project's file, and its
flags that change what the code means are compared with the analysed commands for that file:

* ``-D``/``-U`` macros, the dialect (``-std``, ``-ansi``), include directories and forced includes
  inside the project or the SDK, target and ABI flags (``-m…``, ``--target``) and flags that change
  the language (``-funsigned-char``, ``-fwrapv``, ``-fshort-enums``, ``-ffreestanding``…);
* which compiler compiled it;
* sources the validation build compiles that no analysed configuration compiled, and analysed sources
  it never compiles.

Warnings, optimisation, debug information, dependency files and output names are not compared. Nothing
here fails a change: a mismatch leaves the ``configuration`` record not evaluated and says what differs
(``acceptance.require: [configuration]`` makes it block). The last result per profile is kept for
``weaver doctor``.

Limits: a build that runs its compiler by absolute path bypasses the shims (the record then says nothing
was observed); a build that finds its objects already up to date compiles nothing (exclude build
outputs from the workspaces with ``project.workspace_exclude``).
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from weaver.config import Profile, Project
from weaver.util import now_iso, read_json, run, write_json

COMPILERS = ("cc", "gcc", "clang", "c89", "c99")
SHIM = """#!/bin/sh
# Weaver build-check shim: records the compile, then runs {real} unchanged
f=$(mktemp {logdir}/c.XXXXXX 2>/dev/null) && {{
  printf '%s\\037%s' "$PWD" {real} > "$f"
  for a in "$@"; do printf '\\037%s' "$a" >> "$f"; done
}}
exec {real} "$@"
"""

# -f flags that change what C code means (not how it is optimised or diagnosed)
LANGUAGE_F = (
    "-fwrapv", "-fno-wrapv", "-ftrapv", "-funsigned-char", "-fsigned-char", "-fno-signed-char",
    "-fno-unsigned-char", "-funsigned-bitfields", "-fsigned-bitfields", "-fshort-enums", "-fno-short-enums",
    "-fshort-wchar", "-fpack-struct", "-fms-extensions", "-fgnu89-inline", "-fno-builtin", "-ffreestanding",
    "-fhosted", "-fopenmp", "-fno-common", "-fcommon", "-fgnu-keywords", "-fno-asm", "-fdollars-in-identifiers",
)  # fmt: skip
STD_ALIASES = {
    "c89": "c90", "iso9899:1990": "c90", "iso9899:199409": "c94", "gnu89": "gnu90",
    "c9x": "c99", "iso9899:1999": "c99", "iso9899:199x": "c99", "gnu9x": "gnu99",
    "c1x": "c11", "iso9899:2011": "c11", "gnu1x": "gnu11",
    "c18": "c17", "iso9899:2017": "c17", "iso9899:2018": "c17", "gnu18": "gnu17",
    "c2x": "c23", "gnu2x": "gnu23",
}  # fmt: skip
PATH_FLAGS = ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros")


def make_shims(bin_dir: Path, logdir: Path, names: list[str]) -> list[str]:
    """One recording shim per compiler name found on PATH (outside ``bin_dir``); the names shimmed."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    logdir.mkdir(parents=True, exist_ok=True)
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if Path(p) != bin_dir)
    made = []
    for name in dict.fromkeys(names):
        real = shutil.which(name, path=path)
        if real is None or "/" in name:
            continue
        s = bin_dir / name
        s.write_text(SHIM.format(real=shlex.quote(real), logdir=shlex.quote(str(logdir))))
        s.chmod(0o755)
        made.append(name)
    return made


def compiler_names(project: Project, prof: Profile, analysed: list[Any]) -> list[str]:
    names = list(COMPILERS)
    names += [os.path.basename(c.compiler) for c in analysed]
    if prof.capture is not None:
        names += list(prof.capture.tools)
    return list(dict.fromkeys(n for n in names if n))


def read_log(logdir: Path) -> list[tuple[str, list[str]]]:
    out = []
    for f in sorted(logdir.glob("c.*")):
        parts = f.read_bytes().decode("utf-8", "surrogateescape").split("\x1f")
        if len(parts) >= 2:
            out.append((parts[0], parts[1:]))
    return out


def _excluded(rel: str, project: Project) -> bool:
    """Never copied into a validation workspace: an excluded name, or Weaver's state dir (where
    'weaver init' puts the capture build)."""
    if any(part in set(project.workspace_exclude) for part in Path(rel).parts):
        return True
    state = os.path.relpath(project.state_dir.resolve(), project.root.resolve())
    return not state.startswith("..") and (rel == state or rel.startswith(state + os.sep))


def _norm_path(value: str, directory: str, roots: list[tuple[str, str]], project: Project) -> str | None:
    """A path flag's value, relative to the project when inside it; None for build-tree paths."""
    p = os.path.normpath(os.path.join(directory, value))
    for base, _label in roots:
        if p == base or p.startswith(base + os.sep):
            rel = os.path.relpath(p, base)
            if _excluded(rel, project) or not (project.root / rel).exists():
                return None  # a build directory or generated file: differs between any two builds
            return rel
    return p


def semantic_flags(args: list[str], directory: str, roots: list[tuple[str, str]], project: Project) -> frozenset[str]:
    out: set[str] = set()
    i = 1
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a in ("-D", "-U") and nxt:
            out.add(a + nxt)
            i += 2
            continue
        if a.startswith(("-D", "-U")) and len(a) > 2:
            out.add(a)
        elif a in PATH_FLAGS and nxt:
            v = _norm_path(nxt, directory, roots, project)
            if v is not None:
                out.add(f"{a} {v}")
            i += 2
            continue
        elif joined := next((f for f in ("-isystem", "-iquote", "-idirafter", "-I") if a.startswith(f)), None):
            v = _norm_path(a[len(joined) :], directory, roots, project)
            if v is not None:
                out.add(f"{joined} {v}")
        elif a.startswith("-std="):
            v = a[5:]
            out.add("-std=" + STD_ALIASES.get(v, v))
        elif a == "-ansi":
            out.add("-std=c90")
        elif a in ("-target", "--target") and nxt:
            out.add("--target=" + nxt)
            i += 2
            continue
        elif a.startswith("--target="):
            out.add(a)
        elif a.startswith("-m") and not a.startswith(("-mllvm",)):
            out.add(a)
        elif a == "-pthread" or a in LANGUAGE_F or a.startswith(("-fpack-struct=", "-fno-builtin-")):
            out.add(a)
        i += 1
    return frozenset(out)


def _sources(args: list[str]) -> list[str]:
    from weaver.toolchain.options import parse_driver_args

    pa = parse_driver_args(args)
    if pa.action in ("-E",) or (pa.has_dep_flags and pa.action is None and not pa.output and pa.sources):
        return []
    return [s for s in pa.sources if s.endswith(".c")]


def compare(project: Project, analysed: list[Any], recorded: list[tuple[str, list[str]]], ws: Path) -> dict[str, Any]:
    """Compare the analysed compile commands with the compiles a validation build of ``ws`` recorded."""
    from weaver.capture.shims import BUILD_SYSTEM_PROBES
    from weaver.util import is_within

    root, wsr = str(project.root.resolve()), str(ws.resolve())
    a_files: dict[str, dict[frozenset[str], str]] = {}
    for c in analysed:
        if not is_within(os.path.realpath(c.file), root):
            continue  # compiled from outside the project: validation copies only the project
        rel = os.path.relpath(os.path.realpath(c.file), root)
        flags = semantic_flags(c.expanded, c.directory, [(root, "project")], project)
        a_files.setdefault(rel, {})[flags] = os.path.realpath(c.compiler)
    v_files: dict[str, dict[frozenset[str], str]] = {}
    observed = 0
    for cwd, args in recorded:
        for src in _sources(args):
            p = os.path.realpath(os.path.join(cwd, src))
            if not is_within(p, wsr):
                continue
            rel = os.path.relpath(p, wsr)
            if any(fnmatch.fnmatch("/" + rel, pat) for pat in BUILD_SYSTEM_PROBES):
                continue
            observed += 1
            if _excluded(rel, project) or not (project.root / rel).exists():
                continue  # generated in the validation build tree
            # a build that writes the project's own path into its commands still means the same files
            flags = semantic_flags(args, cwd, [(wsr, "workspace"), (root, "project")], project)
            v_files.setdefault(rel, {})[flags] = os.path.realpath(args[0])
    unanalysed = sorted(set(v_files) - set(a_files))
    uncompiled = sorted(f for f in set(a_files) - set(v_files) if not _excluded(f, project))
    groups: dict[tuple[tuple[str, ...], tuple[str, ...], str], list[str]] = {}
    for rel in sorted(set(v_files) & set(a_files)):
        for vflags, vcomp in v_files[rel].items():
            if any(vflags == af and vcomp == ac for af, ac in a_files[rel].items()):
                continue
            af, ac = min(a_files[rel].items(), key=lambda x: (len(x[0] ^ vflags), x[1] != vcomp))
            comp = "" if ac == vcomp else f"{os.path.basename(vcomp)} instead of {os.path.basename(ac)}"
            key = (tuple(sorted(vflags - af)), tuple(sorted(af - vflags)), comp)
            groups.setdefault(key, []).append(rel)
    differences = [
        {"validation_only": list(k[0]), "analysed_only": list(k[1]), "compiler": k[2], "files": sorted(set(v))}
        for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]
    return {
        "observed": observed,
        "files_validated": len(v_files),
        "files_analysed": len(a_files),
        "unanalysed": unanalysed,
        "uncompiled": uncompiled,
        "differences": differences,
        "same": observed > 0 and not unanalysed and not uncompiled and not differences,
    }


def _names(files: list[str], n: int = 3) -> str:
    return ", ".join(files[:n]) + (f" and {len(files) - n} more" if len(files) > n else "")


def _per_flag(res: dict[str, Any], side: str) -> list[tuple[str, int]]:
    count: dict[str, set[str]] = {}
    for d in res["differences"]:
        for flag in d[side]:
            count.setdefault(flag, set()).update(d["files"])
    return sorted(((f, len(v)) for f, v in count.items()), key=lambda x: (-x[1], x[0]))


def describe(res: dict[str, Any]) -> str:
    if res.get("skipped"):
        return res["skipped"]
    if res.get("build_failed") is not None:
        lines = [ln for ln in res["build_failed"].strip().splitlines() if ln.strip()]
        return "the validation build failed, so what it compiles was not compared" + (
            f": {lines[-1][:300]}" if lines else ""
        )
    if not res.get("observed"):
        return (
            "not observed: the validation build ran no compiler through PATH (it may name its compiler by "
            "absolute path, or find everything already built)"
        )
    if res["same"]:
        return f"the validation build compiles the {res['files_validated']} analysed source(s) as analysed"
    parts = []
    if res["unanalysed"]:
        parts.append(
            f"it compiles {len(res['unanalysed'])} source(s) no analysed configuration compiled "
            f"({_names(res['unanalysed'])}), so the tests run code Weaver never analysed"
        )
    if res["differences"]:
        n = len({f for d in res["differences"] for f in d["files"]})
        what = []
        for side, label in (("validation_only", "only in the validation build"), ("analysed_only", "only as analysed")):
            flags = _per_flag(res, side)
            if flags:
                shown = ", ".join(f"{f} ({c})" if c > 1 else f for f, c in flags[:6])
                what.append(f"{shown}{' and more' if len(flags) > 6 else ''} {label}")
        comps = sorted({d["compiler"] for d in res["differences"] if d["compiler"]})
        what += [f"compiled by {c}" for c in comps]
        parts.append(f"it compiles {n} analysed source(s) differently: " + "; ".join(what))
    if res["uncompiled"]:
        parts.append(
            f"it never compiles {len(res['uncompiled'])} analysed source(s) ({_names(res['uncompiled'])}); "
            "if they are part of the build, their objects were probably copied already built (exclude build "
            "outputs with project.workspace_exclude)"
        )
    return "the validation build differs from the analysed build: " + "; ".join(parts)


def record_path(project: Project, prof: Profile) -> Path:
    from weaver.store import Store

    return Store(project.state_dir).root / "validation" / prof.id / "build-config.json"


def save(project: Project, prof: Profile, res: dict[str, Any], source: str) -> None:
    from weaver.util import sha256_file

    cdb = sha256_file(prof.compile_commands) if prof.compile_commands.exists() else None
    write_json(record_path(project, prof), {"at": now_iso(), "source": source, "compdb": cdb, "result": res})


def is_current(project: Project, prof: Profile, rec: dict[str, Any]) -> bool:
    """Was the comparison made against the compile commands analysed now?"""
    from weaver.util import sha256_file

    return prof.compile_commands.exists() and rec.get("compdb") == sha256_file(prof.compile_commands)


def last(project: Project, prof: Profile) -> dict[str, Any] | None:
    p = record_path(project, prof)
    try:
        return read_json(p) if p.exists() else None
    except (OSError, ValueError):
        return None


def build_env(
    project: Project, prof: Profile, analysed: list[Any], work: Path, path: str | None = None
) -> tuple[dict[str, str], Path]:
    """PATH (the build command's own, else Weaver's) with recording shims first, and where records go."""
    logdir = work / "build-check-log"
    if logdir.exists():
        shutil.rmtree(logdir)
    shims = work / "build-check-shims"
    make_shims(shims, logdir, compiler_names(project, prof, analysed))
    return {"PATH": os.pathsep.join([str(shims), path or os.environ.get("PATH", "")])}, logdir


def check_now(project: Project, prof: Profile, log: Any = None) -> dict[str, Any]:
    """Run the profile's validation build once, in a scratch copy of the project, and compare."""
    from weaver.capture.compdb import load_compdb
    from weaver.store import Store
    from weaver.validate import make_workspace

    say = log or (lambda _m: None)
    if prof.validation.build is None:
        return {"observed": 0, "skipped": "the profile has no validation build"}
    analysed = load_compdb(prof.compile_commands)
    work = Store(project.state_dir).root / "validation" / prof.id / "build-check"
    ws = make_workspace(project, work / "tree")
    try:
        argv, cwd, spec_env = prof.validation.build.render(workspace=str(ws), root=str(project.root))
        env, logdir = build_env(project, prof, analysed, work, spec_env.get("PATH"))
        say(f"[{prof.id}] running the validation build once: {' '.join(argv)}")
        r = run(argv, cwd=cwd, env={**spec_env, **env}, timeout=prof.validation.build.timeout)
        res = compare(project, analysed, read_log(logdir), ws)
        if not r.ok:
            res["build_failed"] = r.stderr_text(800)
        save(project, prof, res, "weaver doctor --build")
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)
