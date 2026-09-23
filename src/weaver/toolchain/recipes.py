"""Collection recipes (compiler plan §§4-5).

Each recipe is a command template run from the unit's recorded working
directory with the unit's preserved options, writing only into an isolated
collection directory.  Options a recipe adds beyond the production command are
recorded as *deviations*.  Frontend inspection interfaces (``-Xclang ...``) are
marked so that their output format is treated as version-specific.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CLANG_DRIVER_DOC = "https://clang.llvm.org/docs/CommandGuide/clang.html"
GCC_DEV_DOC = "https://gcc.gnu.org/onlinedocs/gcc/Developer-Options.html"
GCC_PREPROC_DOC = "https://gcc.gnu.org/onlinedocs/gcc/Preprocessor-Options.html"


@dataclass
class Invocation:
    argv: list[str]
    outputs: list[Path]
    stdout_to: Path | None = None
    stderr_to: Path | None = None
    collect_globs: list[str] = field(default_factory=list)


@dataclass
class Recipe:
    name: str
    families: tuple[str, ...]
    description: str
    default: bool
    deviations: list[str]
    frontend_interface: bool
    doc: str
    build: Callable[[str, list[str], str, Path, str], Invocation]
    # Content check used by capability probes: (outputs, stderr) -> (ok, detail)
    probe_check: Callable[[list[Path], str], tuple[bool, str]]


def _exists_nonempty(paths: list[Path]) -> bool:
    return bool(paths) and all(p.exists() and p.stat().st_size > 0 for p in paths)


def _read(p: Path, limit: int = 50_000_000) -> str:
    with open(p, "rb") as f:
        return f.read(limit).decode(errors="replace")


# --------------------------------------------------------------------------
# Probe content checks (use the fixture in toolchain/probe.py)
# --------------------------------------------------------------------------


def _check_preprocess(outs: list[Path], _: str) -> tuple[bool, str]:
    if not _exists_nonempty(outs):
        return False, "no preprocessed output"
    t = _read(outs[0])
    if "weaver_probe_fn" not in t:
        return False, "fixture function missing from preprocessed output"
    if not re.search(r'^#(line)? *\d+ "', t, re.M):
        return False, "no line markers in preprocessed output"
    return True, "line markers and fixture code present"


def _check_macros(outs: list[Path], _: str) -> tuple[bool, str]:
    if not _exists_nonempty(outs):
        return False, "no macro dump"
    n = sum(1 for line in _read(outs[0]).splitlines() if line.startswith("#define "))
    return (n >= 10, f"{n} macro definitions")


def _check_deps(outs: list[Path], _: str) -> tuple[bool, str]:
    if not _exists_nonempty(outs):
        return False, "no dependency file"
    t = _read(outs[0])
    ok = "probe.c" in t and "stddef.h" in t
    return ok, "fixture and system header listed" if ok else "dependency file lacks fixture or system header"


def _check_ast(outs: list[Path], _: str) -> tuple[bool, str]:
    if not _exists_nonempty(outs):
        return False, "no AST output"
    try:
        tu = json.loads(_read(outs[0]))
    except json.JSONDecodeError as e:
        return False, f"AST output is not JSON: {e}"
    if tu.get("kind") != "TranslationUnitDecl":
        return False, "root is not a TranslationUnitDecl"
    found = {"fn": False, "ptr": False, "rec": False}

    def walk(n: dict) -> None:
        k = n.get("kind")
        if k == "FunctionDecl" and n.get("name") == "weaver_probe_fn":
            found["fn"] = True
        if k == "VarDecl" and n.get("name") == "p" and n.get("type", {}).get("qualType") == "int *":
            found["ptr"] = True
        if k == "RecordDecl" and n.get("name") == "weaver_probe_rec":
            found["rec"] = True
        for c in n.get("inner", ()):
            if isinstance(c, dict):
                walk(c)

    walk(tu)
    missing = [k for k, v in found.items() if not v]
    return (not missing, "function, pointer variable and record found" if not missing else f"missing: {missing}")


def _check_ll(outs: list[Path], _: str) -> tuple[bool, str]:
    if not _exists_nonempty(outs):
        return False, "no IR"
    t = _read(outs[0])
    need = ["target datalayout", "target triple", "weaver_probe_fn"]
    missing = [n for n in need if n not in t]
    if missing:
        return False, f"IR lacks {missing}"
    triple = re.search(r'target triple = "([^"]+)"', t)
    return True, f"triple {triple.group(1) if triple else '?'}"


def _check_bc(outs: list[Path], _: str) -> tuple[bool, str]:
    if not _exists_nonempty(outs):
        return False, "no bitcode"
    head = outs[0].read_bytes()[:4]
    ok = head in (b"BC\xc0\xde", b"\xde\xc0\x17\x0b")
    return ok, "bitcode magic present" if ok else f"unexpected header {head!r}"


def _check_layouts(outs: list[Path], _: str) -> tuple[bool, str]:
    if not _exists_nonempty(outs):
        return False, "no layout dump"
    t = _read(outs[0])
    ok = "weaver_probe_rec" in t and "Record Layout" in t
    return ok, "fixture record layout dumped" if ok else "fixture record layout missing"


def _check_passes(outs: list[Path], stderr: str) -> tuple[bool, str]:
    text = stderr + "".join(_read(p) for p in outs if p.exists())
    ok = bool(re.search(r"tree-\w+|ipa-\w+|rtl-\w+", text))
    return ok, "pass list emitted" if ok else "no pass list in output"


def _check_gcc_dumps(outs: list[Path], _: str) -> tuple[bool, str]:
    names = sorted(p.name for p in outs if p.exists())
    gimple = [p for p in outs if p.name.endswith(".gimple")]
    if not gimple or "weaver_probe_fn" not in _read(gimple[0]):
        return False, f"no GIMPLE dump for fixture (files: {names})"
    kinds = sorted({n.rsplit(".", 1)[-1] for n in names if re.search(r"\.\d+[tir]\.", n)})
    absent = [k for k in ("original", "gimple", "cfg", "ssa", "alias", "cgraph", "expand") if k not in kinds]
    detail = f"dumps: {kinds}"
    if absent:
        detail += f"; not produced in this configuration: {absent} (pass unavailable or not executed)"
    return True, detail


def _check_contains_fn(suffix: str) -> Callable[[list[Path], str], tuple[bool, str]]:
    def check(outs: list[Path], _: str) -> tuple[bool, str]:
        cand = [p for p in outs if p.name.endswith(suffix) and p.exists()]
        if not cand:
            return False, f"no {suffix} output"
        ok = "weaver_probe_fn" in _read(cand[0])
        return ok, f"{suffix} mentions fixture function" if ok else f"{suffix} lacks fixture function"

    return check


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _b_preprocess(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.i"
    return Invocation([cc, *opts, "-E", src, "-o", str(o)], [o])


def _b_macros(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.macros.txt"
    return Invocation([cc, *opts, "-E", "-dM", src, "-o", str(o)], [o])


def _b_deps(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.d"
    # -M (not -MM/-MMD): system and SDK headers are dependencies too.
    return Invocation([cc, *opts, "-M", "-MF", str(o), src], [o])


def _b_ast(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.ast.json"
    return Invocation(
        [cc, *opts, "-fsyntax-only", "-Xclang", "-ast-dump=json", src],
        [o],
        stdout_to=o,
        stderr_to=out / f"{stem}.ast.stderr.txt",
    )


def _b_ll(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.ll"
    return Invocation([cc, *opts, "-g", "-fno-discard-value-names", "-S", "-emit-llvm", src, "-o", str(o)], [o])


def _b_ll_frontend(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.frontend.ll"
    return Invocation(
        [
            cc,
            *opts,
            "-g",
            "-fno-discard-value-names",
            "-Xclang",
            "-disable-llvm-passes",
            "-S",
            "-emit-llvm",
            src,
            "-o",
            str(o),
        ],
        [o],
    )


def _b_bc(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.bc"
    return Invocation([cc, *opts, "-g", "-fno-discard-value-names", "-c", "-emit-llvm", src, "-o", str(o)], [o])


def _b_flow_bc(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    # Frontend IR before LLVM passes (artifact plan §4): production -O options still
    # select preprocessing and frontend behaviour, while loads/stores of named
    # locals keep their debug locations for source mapping.
    o = out / f"{stem}.flow.bc"
    return Invocation(
        [
            cc,
            *opts,
            "-g",
            "-fno-discard-value-names",
            "-Xclang",
            "-disable-llvm-passes",
            "-c",
            "-emit-llvm",
            src,
            "-o",
            str(o),
        ],
        [o],
    )


def _b_layouts(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.layouts.txt"
    return Invocation([cc, *opts, "-fsyntax-only", "-Xclang", "-fdump-record-layouts-complete", src], [o], stdout_to=o)


def _b_passes(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.probe.o"
    return Invocation(
        [cc, *opts, "-c", "-fdump-passes", src, "-o", str(o)],
        [],
        stderr_to=out / f"{stem}.passes.txt",
    )


GCC_DUMP_FLAGS = [
    "-fdump-tree-original",
    "-fdump-tree-gimple",
    "-fdump-tree-cfg",
    "-fdump-tree-ssa",
    "-fdump-tree-alias",
    "-fdump-ipa-cgraph",
    "-fdump-rtl-expand",
]


def _b_gcc_dumps(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.analysis.o"
    base = f"{stem}.dump"
    return Invocation(
        [cc, *opts, "-g", "-c", src, "-o", str(o), "-dumpdir", str(out) + "/", "-dumpbase", base, *GCC_DUMP_FLAGS],
        [],
        # Dump names carry pass numbers that vary by version: never hard-code them.
        collect_globs=[f"{base}.*"],
    )


def _b_stack(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.stack.o"
    return Invocation([cc, *opts, "-g", "-fstack-usage", "-c", src, "-o", str(o)], [out / f"{stem}.stack.su"])


def _b_asm(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.s"
    return Invocation([cc, *opts, "-g", "-S", src, "-o", str(o)], [o])


def _b_callgraph(cc: str, opts: list[str], src: str, out: Path, stem: str) -> Invocation:
    o = out / f"{stem}.cgi.o"
    return Invocation([cc, *opts, "-fcallgraph-info=su,da", "-c", src, "-o", str(o)], [out / f"{stem}.cgi.ci"])


BOTH = ("clang", "gcc")

RECIPES: dict[str, Recipe] = {
    r.name: r
    for r in [
        Recipe(
            "preprocess",
            BOTH,
            "preprocessed source with line markers",
            True,
            [],
            False,
            CLANG_DRIVER_DOC,
            _b_preprocess,
            _check_preprocess,
        ),
        Recipe(
            "macros",
            BOTH,
            "final macro definitions (-E -dM)",
            True,
            [],
            False,
            GCC_PREPROC_DOC,
            _b_macros,
            _check_macros,
        ),
        Recipe(
            "deps",
            BOTH,
            "dependencies including system headers (-M)",
            True,
            [],
            False,
            GCC_PREPROC_DOC,
            _b_deps,
            _check_deps,
        ),
        Recipe(
            "ast_json",
            ("clang",),
            "Clang JSON AST (frontend debug interface)",
            True,
            [],
            True,
            "https://clang.llvm.org/docs/IntroductionToTheClangAST.html",
            _b_ast,
            _check_ast,
        ),
        Recipe(
            "llvm_ir",
            ("clang",),
            "textual LLVM IR at production optimization",
            False,
            ["-g", "-fno-discard-value-names"],
            False,
            CLANG_DRIVER_DOC,
            _b_ll,
            _check_ll,
        ),
        Recipe(
            "llvm_ir_frontend",
            ("clang",),
            "frontend IR before LLVM passes",
            False,
            ["-g", "-fno-discard-value-names", "-Xclang -disable-llvm-passes"],
            True,
            CLANG_DRIVER_DOC,
            _b_ll_frontend,
            _check_ll,
        ),
        Recipe(
            "bitcode",
            ("clang",),
            "LLVM bitcode for LLVM analysis tools",
            False,
            ["-g", "-fno-discard-value-names"],
            False,
            CLANG_DRIVER_DOC,
            _b_bc,
            _check_bc,
        ),
        Recipe(
            "flow_bitcode",
            ("clang",),
            "frontend LLVM bitcode for points-to analysis (SVF)",
            False,
            ["-g", "-fno-discard-value-names", "-Xclang -disable-llvm-passes"],
            True,
            CLANG_DRIVER_DOC,
            _b_flow_bc,
            _check_bc,
        ),
        Recipe(
            "record_layouts",
            ("clang",),
            "record layout diagnostics",
            False,
            [],
            True,
            CLANG_DRIVER_DOC,
            _b_layouts,
            _check_layouts,
        ),
        Recipe(
            "gcc_passes",
            ("gcc",),
            "pass list for the selected configuration (-fdump-passes)",
            False,
            [],
            False,
            GCC_DEV_DOC,
            _b_passes,
            _check_passes,
        ),
        Recipe(
            "gcc_dumps",
            ("gcc",),
            "GENERIC/GIMPLE/SSA/alias/cgraph/RTL dumps",
            False,
            ["-g"],
            False,
            GCC_DEV_DOC,
            _b_gcc_dumps,
            _check_gcc_dumps,
        ),
        Recipe(
            "stack_usage",
            ("gcc", "clang"),
            "per-function stack usage (-fstack-usage)",
            False,
            ["-g"],
            False,
            "https://gcc.gnu.org/onlinedocs/gcc/Developer-Options.html",
            _b_stack,
            _check_contains_fn(".su"),
        ),
        Recipe(
            "assembly",
            BOTH,
            "assembly listing",
            False,
            ["-g"],
            False,
            CLANG_DRIVER_DOC,
            _b_asm,
            _check_contains_fn(".s"),
        ),
        Recipe(
            "callgraph_info",
            ("gcc",),
            "callgraph with stack/dynamic-allocation info",
            False,
            [],
            False,
            "https://gcc.gnu.org/onlinedocs/gcc/Developer-Options.html",
            _b_callgraph,
            _check_contains_fn(".ci"),
        ),
    ]
}

# Recipes run against the secondary (analysis) Clang for a non-Clang profile.
SECONDARY_DEFAULT = ["preprocess", "macros", "deps", "ast_json"]


def default_recipes(family: str) -> list[str]:
    return [r.name for r in RECIPES.values() if r.default and family in r.families]


def recipes_for(family: str, selection: list[str] | str) -> list[str]:
    if selection == "default":
        return default_recipes(family)
    if selection == "all":
        return [r.name for r in RECIPES.values() if family in r.families]
    names = selection if isinstance(selection, list) else [s.strip() for s in selection.split(",") if s.strip()]
    out = []
    for n in names:
        if n not in RECIPES:
            raise KeyError(f"unknown recipe {n!r}; known: {', '.join(RECIPES)}")
        if family in RECIPES[n].families:
            out.append(n)
    return out
