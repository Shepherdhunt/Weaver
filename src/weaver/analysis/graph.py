"""Normalized, source-linked evidence graph (compiler plan §10).

Nodes: profiles, files, functions, pointer-bearing declarations, and address-
taken objects.  Edges: possible targets, address-taking, copies, escapes to
callees, returns, dereferences, and direct/indirect calls.  Every edge carries
provenance (profile, unit, file hash, producing tool, evidence status and fact
kind).  Unresolved targets and effects are explicit ``unknown`` nodes; nothing
absent from the graph is a claim that it does not exist.
"""

from __future__ import annotations

from typing import Any

from weaver import SCHEMA_VERSION
from weaver.analysis.inventory import load_inventory
from weaver.config import Project
from weaver.evidence import FactKind
from weaver.store import Store
from weaver.toolchain.collect import MANIFEST
from weaver.util import now_iso, read_json

UNKNOWN = "unknown"


def build_graph(project: Project) -> dict[str, Any]:
    inv = load_inventory(project)
    store = Store(project.state_dir)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    producers: dict[str, dict[str, Any]] = {}
    for u in inv["units"]:
        p = store.unit_dir(u["profile"], u["unit_id"]) / MANIFEST
        if p.exists():
            m = read_json(p)
            key = m.get("ast_artifact")
            art = m["artifacts"].get(key) if key else None
            producers[u["unit_id"]] = art["tool"] if art else None
        nodes.setdefault(f"profile:{u['profile']}", {"id": f"profile:{u['profile']}", "type": "profile"})
        nodes.setdefault(
            f"unit:{u['unit_id']}",
            {
                "id": f"unit:{u['unit_id']}",
                "type": "unit",
                "file": u["file"],
                "profile": u["profile"],
                "evidence_status": u["evidence_status"],
                "file_sha256": u["file_sha256"],
            },
        )
        edges.append({"src": f"unit:{u['unit_id']}", "dst": f"profile:{u['profile']}", "type": "configured-by"})
    nodes[UNKNOWN] = {
        "id": UNKNOWN,
        "type": "unknown",
        "note": "an unresolved target, callee or effect; never evidence of absence",
    }

    for f, h in inv["files"].items():
        nodes[f"file:{f}"] = {"id": f"file:{f}", "type": "file", "path": f, "sha256": h}

    def prov(f: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "profile": o["profile"],
                "unit": o["unit"],
                "evidence_status": o["evidence_status"],
                "file": f.get("file"),
                "file_sha256": f.get("file_sha256"),
                "producer": producers.get(o["unit"]),
                "fact_kind": FactKind.COMPILER.value,
            }
            for o in f.get("occurrences", [])
        ]

    by_scope_name: dict[tuple[str | None, str | None, str | None], str] = {}
    for f in inv["findings"]:
        by_scope_name[(f.get("file"), f.get("function"), f.get("name"))] = f["id"]

    for fkey, per_unit in inv["operations"].items():
        file, _, fn = fkey.partition("::")
        ops = next(iter(per_unit.values()))
        nid = f"function:{fkey}"
        nodes[nid] = {
            "id": nid,
            "type": "function",
            "name": fn,
            "file": file,
            "line": ops.get("line"),
            "operations": ops["counts"],
            "units": sorted(per_unit),
        }
        edges.append({"src": nid, "dst": f"file:{file}", "type": "defined-in"})
        for callee, n in ops.get("calls", {}).items():
            edges.append(
                {
                    "src": nid,
                    "dst": f"callee:{callee}",
                    "type": "calls",
                    "count": n,
                    "fact_kind": FactKind.COMPILER.value,
                }
            )
            nodes.setdefault(f"callee:{callee}", {"id": f"callee:{callee}", "type": "callee", "name": callee})
        if ops["counts"].get("indirect-call"):
            edges.append(
                {
                    "src": nid,
                    "dst": UNKNOWN,
                    "type": "calls-indirect",
                    "count": ops["counts"]["indirect-call"],
                    "note": "targets unresolved",
                }
            )

    for f in inv["findings"]:
        nid = f["id"]
        nodes[nid] = {
            "id": nid,
            "type": "pointer-declaration",
            "kind": f["kind"],
            "name": f.get("name"),
            "c_type": f.get("type"),
            "file": f.get("file"),
            "line": f.get("line"),
            "col": f.get("col"),
            "function": f.get("function"),
            "record": f.get("record"),
            "evidence_status": f.get("evidence_status"),
            "typedef_hidden": f.get("typedef_hidden"),
            "function_pointer": f.get("function_pointer"),
            "use_summary": f.get("use_summary"),
        }
        p = prov(f)
        if f.get("file"):
            edges.append({"src": nid, "dst": f"file:{f['file']}", "type": "declared-in", "provenance": p})
        if f.get("function"):
            fk = f"function:{f['file']}::{f['function']}"
            if fk in nodes:
                edges.append({"src": nid, "dst": fk, "type": "scoped-to"})
        for t in f.get("possible_targets", []):
            dst = UNKNOWN
            src_kind = t.get("source")
            if (
                src_kind in ("address-of", "array-decay", "function")
                and (t.get("object") or {}).get("kind") == "variable"
            ):
                obj = t["object"]
                dst = f"object:{f.get('file')}::{f.get('function') or ''}::{obj.get('name')}"
                nodes.setdefault(
                    dst,
                    {
                        "id": dst,
                        "type": "object",
                        "name": obj.get("name"),
                        "decl_kind": obj.get("decl_kind"),
                        "file": f.get("file"),
                    },
                )
            elif src_kind == "variable":
                other = by_scope_name.get((f.get("file"), f.get("function"), t.get("name"))) or by_scope_name.get(
                    (f.get("file"), None, t.get("name"))
                )
                dst = other or UNKNOWN
            elif src_kind == "null":
                dst = "null"
                nodes.setdefault("null", {"id": "null", "type": "null"})
            edges.append(
                {
                    "src": nid,
                    "dst": dst,
                    "type": "may-point-to",
                    "via": t.get("via"),
                    "source": src_kind,
                    "line": t.get("line"),
                    "fact_kind": FactKind.HYPOTHESIS.value,
                    "note": "flow-insensitive, intraprocedural; not a points-to proof",
                    "provenance": p,
                }
            )
        for u in f.get("uses", []):
            d = u.get("detail") or {}
            base = {
                "src": nid,
                "line": u.get("line"),
                "col": u.get("col"),
                "in_macro": u.get("in_macro"),
                "fact_kind": FactKind.COMPILER.value,
                "provenance": p,
            }
            k = u["kind"]
            if k in ("deref", "arrow", "subscript"):
                edges.append({**base, "dst": nid, "type": f"{k}-{u.get('access') or 'other'}"})
            elif k == "call-arg":
                callee = d.get("callee")
                dst = f"callee:{callee}" if callee else UNKNOWN
                nodes.setdefault(dst, {"id": dst, "type": "callee", "name": callee} if callee else nodes[UNKNOWN])
                edges.append({**base, "dst": dst, "type": "escapes-to-call", "arg": d.get("arg")})
            elif k == "copy":
                into = d.get("into") or {}
                other = (
                    by_scope_name.get((f.get("file"), f.get("function"), into.get("name")))
                    if into.get("target") == "variable"
                    else None
                )
                edges.append({**base, "dst": other or UNKNOWN, "type": "copied-into", "into": into.get("target")})
            elif k in ("return", "cast", "asm", "address-of-pointer", "other", "indirect-call"):
                edges.append({**base, "dst": UNKNOWN, "type": f"escape-{k}", "detail": d or None})
            elif k in ("compare", "null-test", "arith", "arith-update", "reassign", "unevaluated"):
                edges.append({**base, "dst": nid, "type": k, "detail": d or None})

    return {
        "schema": f"weaver.graph/{SCHEMA_VERSION}",
        "generated_at": now_iso(),
        "inventory_generated_at": inv["generated_at"],
        "profiles": inv["profiles"],
        "nodes": list(nodes.values()),
        "edges": edges,
        "limits": [
            "possible targets are flow-insensitive, intraprocedural hypotheses",
            "calls through function pointers and external/library effects are unresolved (see 'unknown')",
            "only analysed configurations are represented; see inventory coverage for unexamined code",
        ],
    }
