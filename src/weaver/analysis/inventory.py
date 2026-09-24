"""Build the semantic pointer inventory from collected ASTs (pointer-tracker plan §3).

Each finding has a stable ID derived from its file, enclosing scope, name and
kind (not from AST-internal ids or line numbers), and is tied to the source
revision and configurations through the recorded file hash and unit list.
Facts extracted here are compiler-established syntax facts (types, locations,
use shapes); anything that needs flow, alias or lifetime reasoning is left as
explicit ``unknown`` for later analyses.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from weaver import SCHEMA_VERSION
from weaver.analysis import functions as fn_summaries
from weaver.analysis.uses import classify_ref
from weaver.config import Project
from weaver.evidence import EvidenceStatus
from weaver.frontend.clang_ast import Node, TranslationUnit, load_unit_ast
from weaver.frontend.typestr import CType, contains_pointer, resolve_typedefs, safe_parse
from weaver.store import Store
from weaver.toolchain.collect import unit_manifests
from weaver.util import now_iso, read_json, rel_or_abs, sha256_file, short_hash, write_json

ALLOCATORS = {
    "malloc",
    "calloc",
    "realloc",
    "aligned_alloc",
    "strdup",
    "strndup",
    "posix_memalign",
    "OS_HeapAlloc",
    "CFE_ES_GetPoolBuf",
    "CFE_SB_AllocateMessageBuffer",
}
DEALLOCATORS = {"free", "CFE_ES_PutPoolBuf", "CFE_SB_ReleaseMessageBuffer"}
MEMORY_FUNCS = {
    "memcpy",
    "memmove",
    "memset",
    "memcmp",
    "strcpy",
    "strncpy",
    "strcat",
    "strncat",
    "strlen",
    "strcmp",
    "strncmp",
    "strchr",
    "strrchr",
    "strstr",
    "sprintf",
    "snprintf",
    "CFE_PSP_MemCpy",
    "CFE_PSP_MemSet",
    "OS_MemCpy",
    "OS_MemSet",
}


def finding_id(*parts: Any) -> str:
    return "P-" + short_hash(*parts, length=10)


class _TypeInfo:
    def __init__(self, tu: TranslationUnit):
        self.tu = tu
        self._cache: dict[str, CType | None] = {}

    def parse(self, s: str | None) -> CType | None:
        if s is None:
            return None
        if s not in self._cache:
            self._cache[s] = resolve_typedefs(safe_parse(s), self.tu.typedefs)
        return self._cache[s]

    def of(self, n: Node) -> CType | None:
        return self.parse(n.canonical_type)

    def is_pointer(self, n: Node) -> bool:
        t = self.of(n)
        return t is not None and t.kind in ("pointer", "block")

    def describe(self, n: Node) -> dict[str, Any]:
        qt = n.qual_type
        ct = n.canonical_type
        t = self.of(n)
        direct = safe_parse(qt)
        info: dict[str, Any] = {
            "type": qt,
            "canonical_type": ct if ct != qt else None,
            "typedef_hidden": bool(
                t is not None
                and t.kind in ("pointer", "block")
                and not (direct is not None and direct.kind in ("pointer", "block"))
            ),
            "type_parsed": t is not None,
        }
        if t is not None and t.kind in ("pointer", "block"):
            depth, cur = 0, t
            while cur is not None and cur.kind in ("pointer", "block"):
                depth += 1
                cur = resolve_typedefs(cur.inner, self.tu.typedefs)
            info.update(
                pointer_depth=depth,
                function_pointer=t.is_function_pointer,
                pointer_quals=sorted(t.quals),
                pointee=t.inner.spell() if t.inner else None,
                pointee_quals=sorted(t.inner.quals) if t.inner else [],
                pointee_atomic=bool(t.inner and t.inner.atomic),
            )
        elif t is not None and t.kind == "array":
            info.update(array_of_pointers=contains_pointer(t, self.tu.typedefs))
        return info


def _function_key(fn: Node, file_rel: str) -> str:
    return f"{file_rel}::{fn.name}"


def _decl_record(n: Node, root: str, tinfo: _TypeInfo) -> dict[str, Any]:
    loc = n.loc.file_loc
    span = n.expansion_span()
    return {
        "name": n.name,
        "file": rel_or_abs(loc.file, root) if loc.file else None,
        "line": loc.line,
        "col": loc.col,
        "offset": loc.offset,
        "decl_span": [span[1], span[2]] if span else None,
        "in_macro": n.loc.in_macro or n.begin.in_macro or n.end.in_macro,
        "storage": n.storage_class,
        **tinfo.describe(n),
    }


def _possible_targets(decl: Node, uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flow-insensitive, intraprocedural candidate targets (hypotheses, not a points-to set)."""
    from weaver.analysis.uses import _describe_rhs

    out: list[dict[str, Any]] = []
    init = decl.child(0) if decl.raw.get("init") else None
    if init is not None:
        out.append({"via": "initializer", **_describe_rhs(init)})
    for u in uses:
        if u["kind"] == "reassign":
            out.append({"via": "assignment", "line": u["line"], **u.get("detail", {}).get("from", {})})
        if u["kind"] in ("arith-update", "address-of-pointer"):
            out.append({"via": u["kind"], "line": u["line"], "source": "unknown"})
    if decl.kind == "ParmVarDecl":
        out.append({"via": "caller-argument", "source": "unknown"})
    return out


def analyze_unit(manifest: dict[str, Any], unit_dir: str, root: str) -> dict[str, Any]:
    tu = load_unit_ast(manifest, Path(unit_dir), root)
    base = {
        "unit_id": manifest["unit_id"],
        "profile": manifest["profile"],
        "file": manifest["file_rel"],
        "evidence_status": manifest.get("ast_evidence_status", EvidenceStatus.UNSUPPORTED.value),
    }
    return analyze_tu(tu, base, root)


def analyze_tu(tu: TranslationUnit | None, base: dict[str, Any], root: str) -> dict[str, Any]:
    """Pointer facts of one loaded translation unit (``root``: what paths are made relative to)."""
    if tu is None:
        return {
            **base,
            "analyzed": False,
            "findings": [],
            "operations": {},
            "records": [],
            "files": {},
            "functions": {},
            "function_decls": [],
            "function_refs": [],
        }
    tinfo = _TypeInfo(tu)
    findings: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    operations: dict[str, Any] = {}
    files: dict[str, str] = {}
    functions: dict[str, Any] = {}
    function_decls: list[dict[str, Any]] = []
    function_refs: list[dict[str, Any]] = fn_summaries.file_scope_function_refs(tu, root)

    def fid_file(n: Node) -> str:
        f = n.loc.file_loc.file or "?"
        r = rel_or_abs(f, root)
        if r not in files and os.path.exists(f):
            files[r] = sha256_file(f)
        return r

    def uses_of(decl: Node) -> list[dict[str, Any]]:
        return [classify_ref(r).to_json() for r in tu.refs_to(decl.id)]

    for top in tu.top:
        k = top.kind
        if k == "VarDecl" and contains_pointer(tinfo.of(top), tu.typedefs):
            f = fid_file(top)
            static = top.storage_class == "static"
            kind = "static-global" if static else ("extern-decl" if top.storage_class == "extern" else "global")
            fid = finding_id(kind, f if static else "", top.name)
            u = uses_of(top)
            findings.append(
                {
                    "id": fid,
                    "kind": kind,
                    "function": None,
                    **_decl_record(top, root, tinfo),
                    "uses": u,
                    "use_summary": summarize_json(u),
                    "possible_targets": _possible_targets(top, u),
                }
            )
        elif k == "TypedefDecl" and contains_pointer(tinfo.of(top), tu.typedefs):
            f = fid_file(top)
            findings.append(
                {
                    "id": finding_id("typedef", f, top.name),
                    "kind": "typedef",
                    "function": None,
                    **_decl_record(top, root, tinfo),
                    "uses": [],
                }
            )
        elif k == "RecordDecl" and top.raw.get("completeDefinition"):
            f = fid_file(top)
            anon = f"(anonymous at {f}:{top.loc.file_loc.line})"
            rec_name = f"{top.raw.get('tagUsed', 'struct')} {top.name or anon}"
            ptr_fields = []
            for fld in top.walk():
                if fld.kind == "FieldDecl" and contains_pointer(tinfo.of(fld), tu.typedefs):
                    owner = fld.enclosing("RecordDecl")
                    owner_name = (
                        f"{owner.raw.get('tagUsed', 'struct')} {owner.name or '(anonymous)'}" if owner else rec_name
                    )
                    findings.append(
                        {
                            "id": finding_id("field", f, owner_name, fld.name),
                            "kind": "field",
                            "function": None,
                            "record": owner_name,
                            **_decl_record(fld, root, tinfo),
                            "uses": [],
                        }
                    )
                    ptr_fields.append(fld.name)
            if ptr_fields:
                records.append(
                    {"record": rec_name, "file": f, "line": top.loc.file_loc.line, "pointer_fields": ptr_fields}
                )
        elif k == "FunctionDecl":
            f = fid_file(top)
            function_decls.append(fn_summaries.declaration_record(top, tu, root))
            fn_type = safe_parse(top.canonical_type)
            static = top.storage_class == "static"
            has_body = any(c.kind == "CompoundStmt" for c in top.real_children())
            if fn_type is not None and fn_type.kind == "function":
                ret = resolve_typedefs(fn_type.inner, tu.typedefs)
                if ret is not None and ret.kind in ("pointer", "block"):
                    findings.append(
                        {
                            "id": finding_id("return", f if static else "", top.name),
                            "kind": "return",
                            "function": top.name,
                            "name": top.name,
                            "file": f,
                            "line": top.loc.file_loc.line,
                            "col": top.loc.file_loc.col,
                            "offset": top.loc.file_loc.offset,
                            "type": ret.spell(),
                            "definition": has_body,
                            "uses": [],
                        }
                    )
            if not has_body:
                continue
            fkey = _function_key(top, f)
            ordinals: dict[str, int] = {}
            for idx, prm in enumerate(c for c in top.real_children() if c.kind == "ParmVarDecl"):
                if contains_pointer(tinfo.of(prm), tu.typedefs):
                    u = uses_of(prm)
                    findings.append(
                        {
                            "id": finding_id("parameter", f, top.name, prm.name or f"#{idx}"),
                            "kind": "parameter",
                            "function": top.name,
                            "param_index": idx,
                            **_decl_record(prm, root, tinfo),
                            "uses": u,
                            "use_summary": summarize_json(u),
                            "possible_targets": _possible_targets(prm, u),
                        }
                    )
            for n in top.walk():
                if n.kind == "VarDecl" and contains_pointer(tinfo.of(n), tu.typedefs):
                    ordinal = ordinals.get(n.name or "", 0)
                    ordinals[n.name or ""] = ordinal + 1
                    kind = (
                        "static-local"
                        if n.storage_class == "static"
                        else ("extern-decl" if n.storage_class == "extern" else "local")
                    )
                    u = uses_of(n)
                    findings.append(
                        {
                            "id": finding_id(kind, f, top.name, n.name, ordinal),
                            "kind": kind,
                            "function": top.name,
                            "ordinal": ordinal,
                            **_decl_record(n, root, tinfo),
                            "uses": u,
                            "use_summary": summarize_json(u),
                            "possible_targets": _possible_targets(n, u),
                        }
                    )
            operations[fkey] = _operations(top, tinfo, tu, root)
            summary = fn_summaries.summarize_function(top, tu, root)
            functions[fkey] = summary
            function_refs.extend(summary["function_refs"])
    return {
        **base,
        "analyzed": True,
        "findings": findings,
        "operations": operations,
        "records": records,
        "files": files,
        "functions": functions,
        "function_decls": function_decls,
        "function_refs": function_refs,
    }


def summarize_json(uses: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in uses:
        key = u["kind"] + (f":{u['access']}" if u.get("access") else "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _conversion_operand(e: Node | None, tu: TranslationUnit, root: str) -> dict[str, Any]:
    """Describe the pointer converted to an integer: which object's address, or which variable's content."""
    out: dict[str, Any] = {"operand_lc": fn_summaries.expansion_span_lc(e) if e is not None else None}
    if e is None:
        return out
    if "(*)" in (e.canonical_type or "") and "(*)(" in (e.canonical_type or "").replace(" ", ""):
        out["operand_function"] = True  # a function pointer exposes no data object
        return out
    op = fn_summaries.strip(e)
    if op is not None and op.kind == "UnaryOperator" and op.opcode == "&":
        base = op.child(0)
        while base is not None and base.kind in (
            "MemberExpr",
            "ArraySubscriptExpr",
            "ParenExpr",
            "ImplicitCastExpr",
            "CStyleCastExpr",
        ):
            base = base.child(0)
        if base is not None and base.kind == "IntegerLiteral":
            out["operand_null_based"] = True  # offsetof-style '&((T *)0)->f': no object
        else:
            out["operand"] = fn_summaries.designator(op.child(0), root, tu)
        return out
    base = op
    while base is not None and base.kind in (
        "MemberExpr",
        "ArraySubscriptExpr",
        "ParenExpr",
        "ImplicitCastExpr",
        "CStyleCastExpr",
    ):
        base = base.child(0)
    if base is not None and base.kind == "DeclRefExpr":
        out["operand_loads"] = fn_summaries.designator(base, root, tu)  # the variable whose content is converted
    return out


def _operations(fn: Node, tinfo: _TypeInfo, tu: TranslationUnit | None = None, root: str = "") -> dict[str, Any]:
    counts: dict[str, int] = {}
    sites: list[dict[str, Any]] = []
    calls: dict[str, int] = {}

    def add(kind: str, n: Node, **detail: Any) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        loc = n.begin.file_loc
        sites.append({"kind": kind, "line": loc.line, "col": loc.col, "in_macro": n.begin.in_macro, **detail})

    for n in fn.walk():
        k, op = n.kind, n.opcode
        if k == "UnaryOperator" and op == "&":
            add("address-of", n)
        elif k == "UnaryOperator" and op == "*":
            add("dereference", n)
        elif k == "MemberExpr" and n.raw.get("isArrow"):
            add("arrow", n, field=n.name)
        elif k == "ArraySubscriptExpr":
            base = n.child(0)
            if base is not None and not (base.kind == "ImplicitCastExpr" and base.cast_kind == "ArrayToPointerDecay"):
                add("pointer-subscript", n)
        elif k == "BinaryOperator" and op in ("+", "-", "==", "!=", "<", ">", "<=", ">="):
            ops = [c for c in n.real_children()]
            if any(tinfo.is_pointer(c) for c in ops):
                add("pointer-arithmetic" if op in ("+", "-") else "pointer-comparison", n, op=op)
        elif k == "CompoundAssignOperator" and tinfo.is_pointer(n):
            add("pointer-arithmetic", n, op=op)
        elif k == "UnaryOperator" and op in ("++", "--") and tinfo.is_pointer(n):
            add("pointer-arithmetic", n, op=op)
        elif k in ("CStyleCastExpr", "ImplicitCastExpr"):
            ck = n.cast_kind
            if ck in ("IntegralToPointer", "PointerToIntegral"):
                extra: dict[str, Any] = {}
                if ck == "PointerToIntegral" and tu is not None:
                    # what is converted (a converted address is exposed; see weaver.flow.tasks)
                    extra.update(_conversion_operand(n.child(0), tu, root))
                add("integer-pointer-conversion", n, cast_kind=ck, explicit=k == "CStyleCastExpr", **extra)
            elif ck == "BitCast" and k == "CStyleCastExpr":
                add("pointer-reinterpret-cast", n, to=n.qual_type)
            elif ck == "ArrayToPointerDecay":
                p = n.parent
                src = n.child(0)
                if src is not None and src.kind == "StringLiteral":
                    add("string-literal", n)
                elif not (p is not None and p.kind == "ArraySubscriptExpr" and n.index == 0):
                    add("array-decay", n)
            elif ck == "FunctionToPointerDecay":
                p = n.parent
                if not (p is not None and p.kind == "CallExpr" and n.index == 0):
                    add("function-address", n)
        elif k == "CallExpr":
            callee = n.child(0)
            c = callee
            while c is not None and c.kind in ("ImplicitCastExpr", "ParenExpr"):
                c = c.child(0)
            name = None
            if (
                c is not None
                and c.kind == "DeclRefExpr"
                and c.raw.get("referencedDecl", {}).get("kind") == "FunctionDecl"
            ):
                name = c.raw["referencedDecl"].get("name")
            if name is not None:
                calls[name] = calls.get(name, 0) + 1
            if name is None:
                add("indirect-call", n)
            elif name in ALLOCATORS:
                add("allocation", n, callee=name)
            elif name in DEALLOCATORS:
                add("release", n, callee=name)
            elif name in MEMORY_FUNCS:
                add("memory-library-call", n, callee=name)
            if any(tinfo.is_pointer(a) for a in n.real_children()[1:]):
                add("call-with-pointer-argument", n, callee=name)
        elif k == "ReturnStmt":
            v = n.child(0)
            if v is not None and tinfo.is_pointer(v):
                add("return-pointer", n)
        elif k in ("GCCAsmStmt", "MSAsmStmt"):
            add("inline-assembly", n)
    loc = fn.loc.file_loc
    return {
        "counts": dict(sorted(counts.items())),
        "sites": sites,
        "calls": dict(sorted(calls.items())),
        "line": loc.line,
    }


# ---------------------------------------------------------------------------


def _analyze_star(args: tuple[dict[str, Any], str, str]) -> dict[str, Any]:
    return analyze_unit(*args)


def build_inventory(project: Project, profile_ids: list[str] | None = None, jobs: int | None = None) -> dict[str, Any]:
    from weaver.analysis.coverage import compute_coverage

    store = Store(project.state_dir)
    manifests = unit_manifests(project, profile_ids)
    root = str(project.root)
    unit_dirs = {m["unit_id"]: store.unit_dir(m["profile"], m["unit_id"]) for m in manifests}
    work = [(m, str(unit_dirs[m["unit_id"]]), root) for m in manifests]
    jobs = jobs or min(8, os.cpu_count() or 2)
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(_analyze_star, work))
    else:
        results = [_analyze_star(w) for w in work]

    fidelity = _load_fidelity(store, project, manifests)
    merged: dict[str, dict[str, Any]] = {}
    files: dict[str, str] = {}
    operations: dict[str, Any] = {}
    records: dict[str, Any] = {}
    functions: dict[str, Any] = {}
    function_decls: dict[tuple[Any, Any], Any] = {}
    function_refs: dict[tuple[Any, ...], Any] = {}
    units = []
    for r, m in zip(results, manifests):
        for fk, summ in r.get("functions", {}).items():
            _merge_function(functions, fk, summ, r["unit_id"], r["profile"])
        for d in r.get("function_decls", []):
            key = (d["file"], d["offset"])
            cur = function_decls.setdefault(key, {**d, "units": []})
            cur["units"].append(r["unit_id"])
        for ref in r.get("function_refs", []):
            function_refs.setdefault((ref.get("name"), ref.get("file"), ref.get("offset")), ref)
        status = fidelity.get(m["unit_id"], r["evidence_status"])
        units.append(
            {
                "unit_id": r["unit_id"],
                "profile": r["profile"],
                "file": r["file"],
                "analyzed": r["analyzed"],
                "evidence_status": status,
                "file_sha256": m["file_sha256"],
            }
        )
        files.update(r["files"])
        for fk, ops in r["operations"].items():
            operations.setdefault(fk, {})[r["unit_id"]] = ops
        for rec in r["records"]:
            records.setdefault(f"{rec['file']}::{rec['record']}", rec)
        for f in r["findings"]:
            key = f["id"]
            occ = {
                "unit": r["unit_id"],
                "profile": r["profile"],
                "evidence_status": status,
                "offset": f.get("offset"),
                "line": f.get("line"),
            }
            if (
                key in merged
                and merged[key].get("offset") != f.get("offset")
                and f.get("file") == merged[key].get("file")
            ):
                # Same logical key, different declaration: disambiguate honestly.
                key = f"{key}~{f.get('line')}"
                f = {**f, "id": key}
            if key not in merged:
                merged[key] = {**f, "occurrences": [occ], "uses": list(f.get("uses", []))}
            else:
                cur = merged[key]
                cur["occurrences"].append(occ)
                seen = {(u["offset"], u["kind"], u.get("access")) for u in cur["uses"]}
                for u in f.get("uses", []):
                    if (u["offset"], u["kind"], u.get("access")) not in seen:
                        cur["uses"].append(u)
                cur["use_summary"] = summarize_json(cur["uses"])

    findings = sorted(merged.values(), key=lambda f: (f.get("file") or "", f.get("offset") or 0, f["id"]))
    for f in findings:
        f["file_sha256"] = files.get(f.get("file") or "")
        f["evidence_status"] = _weakest([o["evidence_status"] for o in f["occurrences"]])
    coverage = compute_coverage(project.root, manifests, unit_dirs)
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    op_totals: dict[str, int] = {}
    for per_unit in operations.values():
        first = next(iter(per_unit.values()))
        for k, v in first["counts"].items():
            op_totals[k] = op_totals.get(k, 0) + v
    inv = {
        "schema": f"weaver.inventory/{SCHEMA_VERSION}",
        "generated_at": now_iso(),
        "root": root,
        "profiles": [p.id for p in project.select_profiles(profile_ids)],
        "units": units,
        "files": files,
        "findings": findings,
        "pointer_bearing_records": sorted(records.values(), key=lambda r: (r["file"], r["line"] or 0)),
        "operations": operations,
        "functions": functions,
        "function_decls": sorted(function_decls.values(), key=lambda d: (d["file"] or "", d["offset"] or 0)),
        "function_refs": sorted(function_refs.values(), key=lambda d: (d.get("file") or "", d.get("offset") or 0)),
        "coverage": coverage,
        "summary": {
            "findings": len(findings),
            "by_kind": dict(sorted(by_kind.items())),
            "operations": dict(sorted(op_totals.items())),
            "units": len(units),
            "units_without_ast": sum(1 for u in units if not u["analyzed"]),
            "unparsed_files": len(coverage["unparsed_files"]),
            "unexamined_lines": coverage["totals"]["unexamined_lines"],
        },
    }
    write_json(store.inventory_path, inv)
    return inv


def _merge_function(functions: dict[str, Any], key: str, summ: dict[str, Any], unit: str, profile: str) -> None:
    """Union a function's summary across configurations (conservative: every configuration's effects)."""
    cur = functions.get(key)
    if cur is None:
        functions[key] = {**summ, "units": [unit], "profiles": [profile]}
        return
    cur["units"].append(unit)
    if profile not in cur["profiles"]:
        cur["profiles"].append(profile)
    for field in ("calls", "indirect_calls", "named_writes", "pointer_writes", "function_refs", "asm", "constructs"):
        items = cur.setdefault(field, [])
        seen = {json.dumps(x, sort_keys=True) for x in items}
        for x in summ.get(field, []):
            k = json.dumps(x, sort_keys=True)
            if k not in seen:
                seen.add(k)
                items.append(x)


def _weakest(statuses: list[str]) -> str:
    from weaver.evidence import EVIDENCE_RANK

    vals = [EvidenceStatus(s) for s in statuses]
    return min(vals, key=lambda s: EVIDENCE_RANK[s]).value if vals else EvidenceStatus.UNSUPPORTED.value


def _load_fidelity(store: Store, project: Project, manifests: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in manifests:
        p = store.fidelity_dir(m["profile"]) / f"{m['unit_id']}.json"
        if p.exists() and (m.get("ast_artifact") or "").startswith("secondary."):
            rec = read_json(p)
            if rec.get("file_sha256") == m["file_sha256"] and rec.get("cache_key") == m.get("cache_key"):
                out[m["unit_id"]] = rec["evidence_status"]
    return out


def load_inventory(project: Project) -> dict[str, Any]:
    from weaver.errors import WeaverError

    p = Store(project.state_dir).inventory_path
    if not p.exists():
        raise WeaverError("no inventory yet: run 'weaver inventory' after 'weaver collect'")
    return read_json(p)


def find_finding(inv: dict[str, Any], fid: str) -> dict[str, Any]:
    from weaver.errors import WeaverError

    matches = [f for f in inv["findings"] if f["id"] == fid or f["id"].startswith(fid)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise WeaverError(f"no finding {fid!r}")
    raise WeaverError(f"ambiguous finding prefix {fid!r}: {[m['id'] for m in matches[:5]]}")
