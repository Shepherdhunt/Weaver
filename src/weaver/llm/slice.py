"""Focused evidence slice for one pointer (compiler plan §10).

The slice gives the planner the pointer's declaration and source span, its
uses with source text, flow-insensitive possible targets, callers, relevant
configurations and evidence status, the producing toolchain, unexamined code
near it, and every applicable recipe's precondition evaluation.  Everything is
source-linked so the explanation can cite lines rather than infer safety from
a large raw dump.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from weaver.analysis.coverage import unexamined_in_range
from weaver.analysis.inventory import find_finding, load_inventory
from weaver.config import Project
from weaver.recipes import RecipeContext, recipes_for_finding
from weaver.store import Store
from weaver.toolchain.collect import MANIFEST
from weaver.util import read_json

MAX_EXCERPT_LINES = 120


def source_lines(root: Path, rel: str, lo: int, hi: int) -> list[str]:
    try:
        lines = (root / rel).read_bytes().decode("latin-1").splitlines()
    except OSError:
        return []
    lo = max(1, lo)
    hi = min(len(lines), hi)
    return [f"{n:5d}| {lines[n - 1]}" for n in range(lo, hi + 1)]


def function_extent(inv: dict[str, Any], rel: str, function: str | None) -> tuple[int, int] | None:
    """Approximate line extent of a function from its operation sites and declarations."""
    if not function:
        return None
    ops = inv["operations"].get(f"{rel}::{function}")
    lines: list[int] = []
    if ops:
        first = next(iter(ops.values()))
        if first.get("line"):
            lines.append(first["line"])
        lines += [s["line"] for s in first["sites"] if s.get("line")]
    for f in inv["findings"]:
        if f.get("file") == rel and f.get("function") == function:
            lines.append(f.get("line") or 0)
            lines += [u["line"] for u in f.get("uses", []) if u.get("line")]
    lines = [x for x in lines if x]
    if not lines:
        return None
    return min(lines), max(lines) + 2


def callers_of(inv: dict[str, Any], function: str) -> list[str]:
    out = []
    for fkey, per_unit in inv["operations"].items():
        ops = next(iter(per_unit.values()))
        if function in ops.get("calls", {}):
            out.append(fkey)
    return sorted(out)


def build_slice(project: Project, finding_id: str, inv: dict[str, Any] | None = None) -> dict[str, Any]:
    inv = inv or load_inventory(project)
    f = find_finding(inv, finding_id)
    rel = f.get("file") or ""
    ext = function_extent(inv, rel, f.get("function"))
    if ext is None and f.get("line"):
        ext = (max(1, f["line"] - 5), f["line"] + 5)
    lo, hi = ext if ext else (0, -1)
    excerpt = source_lines(project.root, rel, lo, min(hi, lo + MAX_EXCERPT_LINES)) if ext else []

    store = Store(project.state_dir)
    configs = []
    for o in f.get("occurrences", []):
        p = store.unit_dir(o["profile"], o["unit"]) / MANIFEST
        m = read_json(p) if p.exists() else {}
        key = m.get("ast_artifact")
        configs.append(
            {
                "profile": o["profile"],
                "unit": o["unit"],
                "evidence_status": o["evidence_status"],
                "production_compiler": {
                    k: (m.get("production_tool") or {}).get(k) for k in ("family", "version", "target", "sha256")
                },
                "ast_producer": (m.get("artifacts", {}).get(key) or {}).get("tool") if key else None,
                "profile_target": project.profile(o["profile"]).target,
                "profile_platform": project.profile(o["profile"]).platform,
            }
        )

    ctx = RecipeContext(project, inv)
    recipes = []
    for r in recipes_for_finding(f):
        res = r.evaluate(ctx, f)
        recipes.append(
            {
                "recipe": r.id,
                "version": r.version,
                "title": r.title,
                "eligible": res.eligible,
                "preconditions": [p.to_json() for p in res.preconditions],
                "capabilities_required": res.capabilities_required,
                "preservation_argument": res.preservation_argument if res.eligible else None,
                "edit_count": len(res.edits),
            }
        )

    uses = []
    all_lines = (
        (project.root / rel).read_bytes().decode("latin-1").splitlines()
        if rel and (project.root / rel).exists()
        else []
    )
    for u in f.get("uses", []):
        ln = u.get("line") or 0
        uses.append({**u, "source": all_lines[ln - 1].strip() if 0 < ln <= len(all_lines) else None})

    return {
        "finding": {
            k: f.get(k)
            for k in (
                "id",
                "kind",
                "name",
                "type",
                "canonical_type",
                "typedef_hidden",
                "function_pointer",
                "pointer_depth",
                "pointer_quals",
                "pointee",
                "pointee_quals",
                "function",
                "record",
                "file",
                "line",
                "col",
                "in_macro",
                "storage",
                "evidence_status",
                "file_sha256",
            )
        },
        "source_excerpt": {"file": rel, "lines": excerpt},
        "uses": uses,
        "possible_targets": f.get("possible_targets", []),
        "possible_targets_note": "flow-insensitive, intraprocedural hypotheses; not a points-to proof",
        "callers": callers_of(inv, f["function"]) if f.get("kind") == "parameter" and f.get("function") else None,
        "configurations": configs,
        "unexamined_code_near": unexamined_in_range(inv["coverage"], rel, lo, hi) if ext else [],
        "recipes": recipes,
        "preservation_contract": project.preservation,
        "clite_model": project.clite,
        "acceptance_policy": {
            "require": project.acceptance.require,
            "allow_provisional": project.acceptance.allow_provisional,
            "min_evidence": project.acceptance.min_evidence,
        },
    }
