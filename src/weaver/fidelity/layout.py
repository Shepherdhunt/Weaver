"""Representative ABI/layout probes built with both compilers.

A probe translation unit includes the unit's own source (so every type it
sees is visible exactly as in production) and defines one array per question
whose size encodes the answer plus one.  Both the production compiler and the
secondary frontend compile it to a target object; the ELF symbol sizes are
compared.  Bit-field layout, packed attributes beyond what sizeof/offsetof
reveal, calling conventions and interrupt/atomic interfaces are *not* covered
here and are reported as such.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from weaver.config import Profile, Project
from weaver.fidelity.elf import symbol_sizes
from weaver.frontend.clang_ast import TranslationUnit, load_unit_ast
from weaver.util import run, write_json

BASE_QUESTIONS = [
    ("sizeof(void *)", "data pointer size"),
    ("sizeof(void (*)(void))", "function pointer size"),
    ("__alignof__(void *)", "data pointer alignment"),
    ("sizeof(short)", "short size"),
    ("sizeof(int)", "int size"),
    ("sizeof(long)", "long size"),
    ("sizeof(long long)", "long long size"),
    ("__alignof__(long long)", "long long alignment"),
    ("sizeof(float)", "float size"),
    ("sizeof(double)", "double size"),
    ("__alignof__(double)", "double alignment"),
    ("sizeof(long double)", "long double size"),
    ("sizeof(_Bool)", "_Bool size"),
    ("((char)-1 < 0)", "plain char is signed"),
    ("sizeof(enum { weaver_probe_e0 })", "small enum size"),
]


def _record_names(tu: TranslationUnit) -> dict[str, str]:
    """RecordDecl id -> spelling usable at file scope ('struct S' or a typedef name)."""
    names: dict[str, str] = {}
    for n in tu.top:
        if n.kind == "RecordDecl" and n.name:
            names[n.id] = f"{n.raw.get('tagUsed', 'struct')} {n.name}"
    for n in tu.top:
        if n.kind != "TypedefDecl" or not n.name:
            continue
        for t in n.walk():
            for key in ("ownedTagDecl", "decl"):
                ref = t.raw.get(key)
                if isinstance(ref, dict) and ref.get("kind") == "RecordDecl" and ref.get("id") not in names:
                    names[ref["id"]] = n.name
    return names


def _questions(tu: TranslationUnit) -> tuple[list[tuple[str, str]], list[str]]:
    qs = list(BASE_QUESTIONS)
    skipped: list[str] = []
    names = _record_names(tu)
    for n in tu.top:
        if n.kind == "RecordDecl" and n.raw.get("completeDefinition") and n.id in names:
            spell = names[n.id]
            qs.append((f"sizeof({spell})", f"sizeof {spell}"))
            qs.append((f"__alignof__({spell})", f"alignof {spell}"))
            for f in n.real_children():
                if f.kind != "FieldDecl" or not f.name:
                    continue
                if f.raw.get("isBitfield"):
                    skipped.append(f"{spell}.{f.name}: bit-field layout not probed")
                    continue
                qs.append((f"__builtin_offsetof({spell}, {f.name})", f"offsetof {spell}.{f.name}"))
        elif n.kind == "EnumDecl" and n.name:
            qs.append((f"sizeof(enum {n.name})", f"sizeof enum {n.name}"))
    return qs, skipped


def _probe_source(main_file: str, qs: list[tuple[str, str]]) -> str:
    lines = [f'#include "{main_file}"', "/* Weaver layout probe: each array size is (value + 1). */"]
    for i, (expr, _) in enumerate(qs):
        lines.append(f"char weaver_probe_{i}[({expr}) + 1] = {{0}};")
    return "\n".join(lines) + "\n"


def _build_both(
    m: dict[str, Any], udir: Path, src: Path, stem: str, prefix: str
) -> tuple[dict[str, int] | None, dict[str, int] | None, str | None]:
    """Compile a probe with the production compiler and the secondary frontend; the probe symbols' sizes."""
    tr = m.get("translation") or {}
    sec_tool = (m.get("secondary_tool") or {}).get("path")
    if not sec_tool or not tr.get("options"):
        return None, None, "no secondary frontend translation recorded"
    prod_o, sec_o = udir / f"{stem}.prod.o", udir / f"{stem}.sec.o"
    prod_cmd = [m["sanitized"]["compiler"], *m["sanitized"]["options"], "-w", "-c", str(src), "-o", str(prod_o)]
    rsp = udir / "secondary.clang.rsp"  # the translated options plus the profile's extra_args, as collected
    sec_opts = [f"@{rsp}"] if rsp.exists() else list(tr["options"])
    sec_cmd = [sec_tool, *sec_opts, "-w", "-c", str(src), "-o", str(sec_o)]
    rp = run(prod_cmd, cwd=m["directory"], timeout=600)
    if not rp.ok:
        return None, None, f"production compiler rejected the probe: {rp.stderr_text(800)}"
    rs = run(sec_cmd, cwd=m["directory"], timeout=600)
    if not rs.ok:
        return None, None, f"secondary frontend rejected the probe: {rs.stderr_text(800)}"
    ps, ss = symbol_sizes(prod_o, prefix), symbol_sizes(sec_o, prefix)
    if ps is None or ss is None:
        return None, None, "object format is not ELF; use a native layout report"
    return ps, ss, None


def _macro_source(main_file: str, names: list[str]) -> str:
    lines = [
        f'#include "{main_file}"',
        "/* Weaver macro probe: size, signedness and each byte of the value, as (answer + 1) array sizes. */",
    ]
    for i, n in enumerate(names):
        lines.append(f"char weaver_mv_{i}_z[sizeof({n}) + 1] = {{0}};")
        lines.append(f"char weaver_mv_{i}_s[((0 ? ({n}) : -1) < 0) + 1] = {{0}};")
        lines += [
            f"char weaver_mv_{i}_b{k}[(((unsigned long long)({n}) >> {8 * k}) & 0xff) + 1] = {{0}};" for k in range(8)
        ]
    return "\n".join(lines) + "\n"


def macro_values(m: dict[str, Any], udir: Path, names: list[str], limit: int = 24) -> dict[str, bool]:
    """Object-like macros spelled differently by the two compilers: which have the same value?

    Each macro is used as an integer constant expression with both compilers; its size, signedness and
    all eight bytes of its value must agree.  A macro that is not an integer constant expression (a
    string, a float, a function-like macro, anything naming a variable) is absent from the result."""
    names = names[:limit]
    if not names:
        return {}

    def attempt(batch: list[str], stem: str) -> dict[str, bool] | None:
        src = udir / f"{stem}.c"
        src.write_text(_macro_source(m["file"], batch))
        ps, ss, err = _build_both(m, udir, src, stem, "weaver_mv_")
        if err:
            return None
        out = {}
        for i, n in enumerate(batch):
            keys = [f"weaver_mv_{i}_{s}" for s in ("z", "s", *(f"b{k}" for k in range(8)))]
            out[n] = all(ps.get(k) is not None and ps.get(k) == ss.get(k) for k in keys)  # type: ignore[union-attr]
        return out

    together = attempt(names, "macros.probe")
    if together is not None:
        return together
    res: dict[str, bool] = {}
    for i, n in enumerate(names):  # one of them is not a constant: find the ones that are
        one = attempt([n], f"macros.probe{i}")
        if one is not None:
            res.update(one)
    return res


def layout_probe(project: Project, profile: Profile, m: dict[str, Any], udir: Path) -> dict[str, Any]:
    tu = load_unit_ast(m, udir, str(project.root))
    if tu is None:
        return {"status": "unavailable", "reason": "no AST to enumerate record types"}
    qs, skipped = _questions(tu)
    src = udir / "layout.probe.c"
    src.write_text(_probe_source(m["file"], qs))
    ps, ss, err = _build_both(m, udir, src, "layout", "weaver_probe_")
    if err or ps is None or ss is None:
        return {"status": "unavailable", "reason": err}
    values, mismatches = [], []
    for i, (_expr, desc) in enumerate(qs):
        a, b = ps.get(f"weaver_probe_{i}"), ss.get(f"weaver_probe_{i}")
        va = a - 1 if a is not None else None
        vb = b - 1 if b is not None else None
        values.append({"question": desc, "production": va, "secondary": vb})
        if va != vb:
            mismatches.append(f"{desc}: production {va}, secondary {vb}")
    out = {
        "status": "compared",
        "questions": len(qs),
        "mismatches": mismatches,
        "not_probed": skipped,
        "not_covered": ["bit-field layout", "calling conventions for aggregates", "interrupt and atomic interfaces"],
    }
    write_json(udir / "layout.values.json", values)
    return out
