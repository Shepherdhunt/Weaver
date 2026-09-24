"""Secondary-frontend fidelity checks (compiler plan §§6, 9).

For a profile whose production compiler is not Clang, Weaver parses the source
with a separately labelled Clang frontend.  Before that evidence may support a
rewrite, each unit is compared with the production compiler on:

* final macro definitions that project code can observe (secondary-only
  wrappers that forward every argument unchanged to the fortified variant of
  the same library function are recorded, not counted; see
  ``weaver.frontend.wrappers``),
* the set of included project and SDK headers,
* the conditional branches active in each project file, and
* representative ABI/layout probes (sizes, alignments, member offsets, char
  signedness, pointer sizes) read from *target objects* built by both
  compilers.

The result is ``secondary-checked`` only when all comparisons agree;
otherwise ``secondary-partial`` with the specific differences, or
``unsupported`` when the secondary frontend could not parse the unit.
Passing representative probes does not establish unrestricted compiler
equivalence, and compatibility findings stay separate from transformation
verification.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from weaver.capture.compdb import load_compdb
from weaver.config import Profile, Project
from weaver.evidence import EvidenceStatus
from weaver.fidelity.layout import layout_probe, macro_values
from weaver.frontend.lexer import lex
from weaver.frontend.preproc import active_lines, conditional_segments
from weaver.frontend.wrappers import transparent_wrappers
from weaver.store import Store
from weaver.toolchain.collect import MANIFEST, parse_depfile, read_macros
from weaver.util import is_within, now_iso, read_json, rel_or_abs, write_json

# Macros whose values identify the compiler rather than the program's semantics.
IDENTITY_PREFIXES = (
    "__clang",
    "__GNUC",
    "__VERSION__",
    "__GCC_",
    "__llvm",
    "__apple_build_version__",
    "__STDC_HOSTED__",
    "__OPTIMIZE",
    "__NO_INLINE__",
    "__GXX_ABI_VERSION",
    "__GCC_IEC_",
    "__GCC_ASM_FLAG_OUTPUTS__",
    "__FINITE_MATH_ONLY__",
    "__REGISTER_PREFIX__",
    "__USER_LABEL_PREFIX__",
    "__OBJC",
    "__BLOCKS__",
    "__MEMORY_SCOPE",
    "__ATOMIC_",
    "__SEG_",
    "__CET__",
    "__SSP",
    "__PIC__",
    "__pic__",
    "__PIE__",
    "__pie__",
    "__FLT_EVAL_METHOD",
    "__DECIMAL",
    "__DEC",
    "__HAVE_SPECULATION_SAFE_VALUE",
    "__GCC_HAVE",
    "__CHAR_UNSIGNED__",
    "__WCHAR_UNSIGNED__",
)


CONDITIONAL_DIRECTIVES = {"if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "undef"}


class _Idents:
    """Identifiers of project files, split by where they occur."""

    def __init__(self) -> None:
        self.cache: dict[str, tuple[set[str], set[str]]] = {}

    def _load(self, path: str) -> tuple[set[str], set[str]]:
        if path not in self.cache:
            code: set[str] = set()
            cond: set[str] = set()
            try:
                lx = lex(Path(path).read_bytes())
            except OSError:
                self.cache[path] = (code, cond)
                return self.cache[path]
            for t in lx.tokens:
                if t.kind != "ident":
                    continue
                (cond if t.directive in CONDITIONAL_DIRECTIVES else code).add(t.text)
            self.cache[path] = (code, cond)
        return self.cache[path]

    def of(self, path: str) -> set[str]:
        """Identifiers used in code or macro definitions (their value can reach compiled code)."""
        return self._load(path)[0]

    def conditional(self, path: str) -> set[str]:
        """Identifiers used in conditional directives (their effect is which lines are compiled)."""
        return self._load(path)[1]


def _tokens(defn: str | None) -> tuple[str, ...] | None:
    """A macro definition as a token sequence, up to spacing and parameter names.

    Neither changes what an invocation expands to: ``offsetof(TYPE, MEMBER)`` and
    ``offsetof(t, d)`` with the same body modulo renaming are the same macro.
    """
    if defn is None:
        return None
    head = ""
    body = defn
    rename: dict[str, str] = {}
    if defn.startswith("(fn)("):
        close = defn.find(")", 4)
        params = [x.strip() for x in defn[5:close].split(",") if x.strip()]
        for i, prm in enumerate(params):
            if prm == "...":
                continue
            rename[prm.removesuffix("...").strip()] = f"${i}"
        head = f"(fn){len(params)}" + ("..." if params and params[-1].endswith("...") else "")
        body = defn[close + 1 :]
    return (head, *(rename.get(t.text, t.text) if t.kind == "ident" else t.text for t in lex(body).tokens))


def _deps(unit_dir: Path, name: str, directory: str) -> list[str]:
    p = unit_dir / name
    if not p.exists():
        return []
    return [os.path.realpath(d) for d in parse_depfile(p.read_text(errors="replace"), directory)]


def _builtin_dir(d: str) -> bool:
    return ("/lib/gcc" in d and "/include" in d) or "/lib/clang/" in d or ("/lib/llvm" in d and "/include" in d)


def check_unit(project: Project, profile: Profile, m: dict[str, Any], idents: _Idents, layout: bool) -> dict[str, Any]:
    store = Store(project.state_dir)
    udir = store.unit_dir(profile.id, m["unit_id"])
    root = str(project.root)
    findings: list[str] = []
    notes: list[str] = []
    rec: dict[str, Any] = {
        "unit_id": m["unit_id"],
        "file": m["file_rel"],
        "file_sha256": m["file_sha256"],
        "cache_key": m.get("cache_key"),
        "checked_at": now_iso(),
    }
    sec_ast = m["artifacts"].get("secondary.ast_json")
    if not sec_ast or sec_ast["status"] != "ok":
        err = (
            (udir / "secondary.ast.stderr.txt").read_text(errors="replace")[:1500]
            if (udir / "secondary.ast.stderr.txt").exists()
            else "no secondary AST collected"
        )
        return {
            **rec,
            "evidence_status": EvidenceStatus.UNSUPPORTED.value,
            "findings": [f"secondary frontend could not parse the unit: {err.strip()}"],
        }

    # project identifiers visible to this unit
    prod_deps = _deps(udir, "unit.d", m["directory"])
    sec_deps = _deps(udir, "secondary.d", m["directory"])
    proj_files = [os.path.realpath(m["file"])] + [d for d in prod_deps if is_within(d, root)]
    visible: set[str] = set()
    conditional: set[str] = set()
    for f in proj_files:
        visible |= idents.of(f)
        conditional |= idents.conditional(f)

    # 1. translation log
    tr = m.get("translation") or {}
    unresolved = [x for x in tr.get("log", []) if "UNRESOLVED" in x.get("reason", "")]
    for x in unresolved:
        findings.append(f"option {' '.join(x['option'])} was not translated; semantic effect unresolved")

    # 2. macros
    pm, sm = read_macros(udir / "unit.macros.txt"), read_macros(udir / "secondary.macros.txt")
    wrappers = transparent_wrappers(pm, sm) if pm and sm else {}
    diff_relevant, diff_other, forwarding, spacing, conditional_only = [], [], [], [], []
    relevant_names: list[str] = []
    for name in sorted(set(pm) | set(sm)):
        a, b = pm.get(name), sm.get(name)
        if a == b:
            continue
        desc = f"{name}: production={a!r} secondary={b!r}"
        if name in wrappers:
            if name in visible:
                forwarding.append(f"{name} -> {wrappers[name]}")
            continue
        if _tokens(a) == _tokens(b):
            if name in visible or name in conditional:
                spacing.append(name)
            continue
        if name in visible:
            diff_relevant.append(desc)
            relevant_names.append(name)
        elif name in conditional:
            # Only tested by #if/#ifdef in project files: its whole effect is which lines are
            # compiled, and the active-code comparison below checks exactly that.
            conditional_only.append(desc)
        elif not name.startswith(IDENTITY_PREFIXES):
            diff_other.append(desc)
    same_value: list[str] = []
    if layout:
        # GCC's limits.h writes INT_MIN as (-INT_MAX - 1), Clang's as (-__INT_MAX__ -1): different text,
        # the same constant.  Compile each object-like one with both compilers and compare the values.
        objlike = [n for n in relevant_names if not (pm.get(n) or "").startswith("(fn)") and n in pm and n in sm]
        same = macro_values(m, udir, objlike)
        same_value = [n for n in objlike if same.get(n)]
        diff_relevant = [d for d, n in zip(diff_relevant, relevant_names) if n not in same_value]
    findings += [f"macro observable by project code differs: {d}" for d in diff_relevant]
    if same_value:
        notes.append(
            "macros spelled differently with the same value under both compilers (size, signedness and "
            f"value compared): {', '.join(same_value)}"
        )
    if forwarding:
        notes.append(
            "secondary-only forwarding wrappers (every argument passed once, unchanged, to the fortified "
            f"variant of the same library function; arguments read as plain source text): {', '.join(forwarding)}"
        )
    if spacing:
        notes.append(f"macros that differ only in spacing or parameter names: {', '.join(spacing[:20])}")
    if conditional_only:
        notes.append(
            f"{len(conditional_only)} differing macro(s) are only tested in conditional directives; "
            "their effect is covered by the active-code comparison"
        )
    rec["macro_differences"] = {
        "relevant": diff_relevant,
        "forwarding": forwarding,
        "spacing_only": spacing,
        "same_value": same_value,
        "conditional_only": conditional_only,
        "not_referenced": diff_other[:200],
        "not_referenced_count": len(diff_other),
    }

    # 3. includes
    pp = {d for d in prod_deps if is_within(d, root)}
    sp = {d for d in sec_deps if is_within(d, root)}
    if pp != sp:
        findings.append(
            "project headers differ: only production "
            f"{sorted(rel_or_abs(x, root) for x in pp - sp)}, only secondary "
            f"{sorted(rel_or_abs(x, root) for x in sp - pp)}"
        )
    psys = {d for d in prod_deps if not is_within(d, root) and not _builtin_dir(os.path.dirname(d))}
    ssys = {d for d in sec_deps if not is_within(d, root) and not _builtin_dir(os.path.dirname(d))}
    if psys != ssys:
        findings.append(
            f"SDK/system headers differ beyond builtin-header substitution: only production "
            f"{sorted(psys - ssys)[:10]}, only secondary {sorted(ssys - psys)[:10]}"
        )
    if not prod_deps or not sec_deps:
        notes.append("dependency lists unavailable; include comparison incomplete")
        findings.append("include comparison could not be performed")

    # 4. active branches in project files
    pi, si = udir / "unit.i", udir / "secondary.i"
    if pi.exists() and si.exists():
        pa, sa = active_lines(pi, m["directory"]), active_lines(si, m["directory"])
        for f in sorted(set(pa) | set(sa)):
            if f.startswith("<") or not is_within(f, root):
                continue
            a, b = pa.get(f, set()), sa.get(f, set())
            if a == b:
                continue
            try:
                segs = conditional_segments(f)
            except OSError:
                segs = [(ln, ln) for ln in sorted(a | b)]
            only_p, only_s = [], []
            for lo, hi in segs:
                in_a = any(lo <= ln <= hi for ln in a)
                in_b = any(lo <= ln <= hi for ln in b)
                if in_a != in_b:
                    (only_p if in_a else only_s).append(f"{lo}-{hi}" if hi > lo else str(lo))
            if only_p or only_s:
                findings.append(
                    f"active conditional groups differ in {rel_or_abs(f, root)}: only production lines "
                    f"{only_p[:12]}, only secondary lines {only_s[:12]}"
                )
    else:
        findings.append("preprocessed output missing; active-branch comparison not performed")

    # 5. layout probes
    if layout:
        lay = layout_probe(project, profile, m, udir)
        rec["layout"] = lay
        if lay.get("status") != "compared":
            findings.append(f"layout probe unavailable: {lay.get('reason')}")
        elif lay["mismatches"]:
            findings += [f"layout differs: {x}" for x in lay["mismatches"]]
    else:
        findings.append("layout probes not run (--no-layout)")

    rec["findings"] = findings
    rec["notes"] = notes
    rec["evidence_status"] = (EvidenceStatus.SECONDARY_PARTIAL if findings else EvidenceStatus.SECONDARY_CHECKED).value
    return rec


_WORKER_IDENTS: _Idents | None = None


def _check_star(args: tuple[Project, Profile, dict[str, Any], bool]) -> dict[str, Any]:
    global _WORKER_IDENTS
    if _WORKER_IDENTS is None:
        _WORKER_IDENTS = _Idents()
    project, profile, m, layout = args
    return check_unit(project, profile, m, _WORKER_IDENTS, layout)


def run_fidelity(project: Project, profile: Profile, layout: bool = True, jobs: int | None = None) -> dict[str, Any]:
    from concurrent.futures import ProcessPoolExecutor

    store = Store(project.state_dir)
    cmds = load_compdb(profile.compile_commands)
    work = []
    for c in cmds:
        p = store.unit_dir(profile.id, c.unit_id(profile.id)) / MANIFEST
        if not p.exists():
            continue
        m = read_json(p)
        if not (m.get("ast_artifact") or "").startswith("secondary.") and "secondary.ast_json" not in m.get(
            "artifacts", {}
        ):
            continue
        work.append((project, profile, m, layout))
    jobs = jobs or min(8, os.cpu_count() or 2)
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            units = list(ex.map(_check_star, work, chunksize=4))
    else:
        units = [_check_star(w) for w in work]
    for rec in units:
        write_json(store.fidelity_dir(profile.id) / f"{rec['unit_id']}.json", rec)
    summary = {
        "profile": profile.id,
        "checked_at": now_iso(),
        "units": units,
        "note": ""
        if units
        else "no units with secondary-frontend evidence (production compiler is Clang, "
        "or no secondary_frontend configured, or collect not run)",
        "limits": "representative checks only; they do not establish unrestricted compiler equivalence",
    }
    write_json(store.fidelity_dir(profile.id) / "summary.json", summary)
    return summary
