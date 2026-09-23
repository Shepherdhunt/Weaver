"""Pointer risk: which pointers deserve attention first, and why.

Every factor is a fact Weaver already establishes for a pointer: how it is used
(casts to and from integers, arithmetic, escapes, writes), what it may point to
(unknown memory, the heap, many objects), whether another task may write its
target, and how strong the evidence is.  Each factor carries a weight; a
pointer's score is the sum of its factors' weights, and its level (high, medium,
low) comes from the score.  The weights are a transparent heuristic for ordering
the work, not a probability of failure: every factor is listed with the line or
the analysis that produced it, so a reviewer can see exactly why a pointer
ranks where it does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from weaver.config import Project
from weaver.impact import access_class

FACTORS: dict[str, dict[str, Any]] = {
    "integer-conversion": {
        "weight": 4,
        "title": "Converted to or from an integer",
        "why": "An address held as an integer escapes every points-to analysis and every bounds check.",
    },
    "concurrent-writer": {
        "weight": 4,
        "title": "Another task may write its target",
        "why": "A concurrent write can change the target between two reads (from the task model).",
    },
    "unknown-target": {
        "weight": 3,
        "title": "May point to unknown memory",
        "why": "Points-to analysis could not identify every object it may point to.",
    },
    "arithmetic": {
        "weight": 3,
        "title": "Pointer arithmetic or subscripts",
        "why": "The bounds of the underlying array are not visible where the pointer moves.",
    },
    "reinterpret-cast": {
        "weight": 3,
        "title": "Cast to an unrelated pointer type",
        "why": "The same memory is read as another type.",
    },
    "escapes": {
        "weight": 2,
        "title": "Its value leaves the function",
        "why": "Passed, copied, stored or returned: other code can reach the target.",
    },
    "writes": {"weight": 2, "title": "Writes through the pointer", "why": "Changes an object other code may use."},
    "heap": {"weight": 2, "title": "Points to heap memory", "why": "Lifetime and ownership are managed by hand."},
    "global": {
        "weight": 2,
        "title": "Global or static pointer",
        "why": "Shared by every function that can see it.",
    },
    "function-pointer": {
        "weight": 2,
        "title": "Function pointer",
        "why": "The code it calls is decided at run time.",
    },
    "many-targets": {
        "weight": 2,
        "title": "May point to many objects",
        "why": "Five or more possible targets: aliasing is hard to reason about.",
    },
    "reassigned": {"weight": 1, "title": "Reassigned", "why": "Its target changes over time."},
    "null-tested": {"weight": 1, "title": "Null-tested", "why": "The code expects it may be null."},
    "compared": {"weight": 1, "title": "Identity compared", "why": "Behaviour depends on which object it is."},
    "weak-evidence": {
        "weight": 1,
        "title": "Weaker evidence",
        "why": "Facts come from a secondary frontend that was not fully checked against the production compiler.",
    },
    "no-flow-evidence": {
        "weight": 1,
        "title": "No points-to evidence",
        "why": "Its targets are not known yet (run the points-to analysis).",
    },
}
LEVELS = (("high", 7), ("medium", 4), ("low", 0))
RISK_NOTE = (
    "A score adds up the weights of the factors established for the pointer (high: 7 or more, medium: 4 to 6). "
    "It orders the work; it is not a probability of failure."
)
UNKNOWN_KINDS = {"dummy", "other", "unknown", "blackhole"}
MANY = 5


def level(score: int) -> str:
    return next(name for name, lo in LEVELS if score >= lo)


def pointer_risk(
    f: dict[str, Any], targets: list[list[dict[str, Any]]] | None, verdicts: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Score one finding.  ``targets``: its points-to targets per program (None: no evidence)."""
    found: dict[str, list[str]] = {}

    def add(fid: str, evidence: str) -> None:
        found.setdefault(fid, []).append(evidence)

    uses = f.get("uses", [])
    for u in uses:
        k, d, ln = u["kind"], u.get("detail") or {}, f"line {u.get('line')}"
        if k == "cast" and d.get("cast_kind") in ("PointerToIntegral", "IntegralToPointer"):
            add("integer-conversion", f"{ln}: {d.get('cast_kind')}")
        elif k == "cast" and d.get("cast_kind") == "BitCast" and d.get("explicit"):
            add("reinterpret-cast", f"{ln}: to {d.get('to')}")
        elif k in ("arith", "arith-update"):
            add("arithmetic", f"{ln}: '{d.get('op')}'")
        elif k == "subscript":
            add("arithmetic", f"{ln}: subscript")
        elif k == "reassign":
            add("reassigned", ln)
        elif k == "null-test" or (k == "compare" and d.get("null")):
            add("null-tested", ln)
        elif k == "compare":
            add("compared", f"{ln}: '{d.get('op')}'")
        if k in ("deref", "arrow", "subscript") and str(u.get("access") or "").endswith("write"):
            add("writes", ln)
    if access_class(f) == "escapes":
        esc = next((u for u in uses if u["kind"] in ("call-arg", "copy", "return", "cast", "asm", "other")), None)
        add("escapes", f"line {esc.get('line')}: {esc['kind']}" if esc else "escapes")
    if f.get("kind") in ("global", "static-global", "extern-decl"):
        add("global", f"{f.get('kind')} declared at line {f.get('line')}")
    if f.get("function_pointer"):
        add("function-pointer", str(f.get("type")))
    if f.get("evidence_status") in ("secondary-partial", "secondary-unchecked", "unsupported"):
        add("weak-evidence", str(f.get("evidence_status")))
    if targets is None:
        if f.get("kind") not in ("field", "typedef"):
            add("no-flow-evidence", "no current points-to run covers this pointer")
    else:
        allt = [t for ts in targets for t in ts]
        unknown = [t for t in allt if t.get("kind") in UNKNOWN_KINDS]
        if unknown:
            add("unknown-target", f"{len(unknown)} unidentified object(s)")
        heap = sorted({t.get("name") or f"#{t.get('id')}" for t in allt if t.get("kind") == "heap"})
        if heap:
            add("heap", ", ".join(heap[:4]))
        named = {t.get("id") for t in allt}
        if len(named) >= MANY:
            add("many-targets", f"{len(named)} possible targets")
    for rid, r in (verdicts or {}).items():
        for p in r.get("preconditions", []):
            if p.get("id", "").endswith("no-concurrent-writers") and p.get("status") == "violated":
                add("concurrent-writer", (p.get("evidence") or [f"{rid}: violated"])[0])
    factors = [
        {"id": k, "weight": FACTORS[k]["weight"], "title": FACTORS[k]["title"], "evidence": v[:3], "count": len(v)}
        for k, v in found.items()
    ]
    factors.sort(key=lambda x: (-x["weight"], x["id"]))
    score = sum(x["weight"] for x in factors)
    return {"score": score, "level": level(score), "factors": factors}


def _targets(ctx: Any, inv: dict[str, Any], f: dict[str, Any]) -> list[list[dict[str, Any]]] | None:
    from weaver.flow.evidence import declared_targets, load_flow

    if not ctx.project.flow.uses("svf"):
        return None
    units = sorted({o["unit"] for o in f.get("occurrences", []) if o.get("unit")})
    flows = ctx.flows_for_units(units)
    if not flows:
        flows = {p: load_flow(ctx.project, p, inv) for p in sorted({o["profile"] for o in f.get("occurrences", [])})}
    out = []
    for fe in flows.values():
        if fe is None:
            continue
        t = declared_targets(fe, f)
        if t is not None:
            out.append(t)
    return out or None


def module_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")


def report(
    project: Project,
    inv: dict[str, Any],
    in_scope: Callable[[str], bool] = lambda _path: True,
    ctx: Any = None,
    verdicts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every pointer in scope, scored and ranked, with totals per function, file and module."""
    if ctx is None:
        from weaver.recipes import RecipeContext

        ctx = RecipeContext(project, inv)
    rows = []
    for f in inv["findings"]:
        # a typedef is not an object; an extern declaration is the same object as its definition
        if f.get("kind") in ("typedef", "extern-decl") or not in_scope(f.get("file") or ""):
            continue
        r = pointer_risk(f, _targets(ctx, inv, f), (verdicts or {}).get(f["id"]))
        rows.append(
            {
                "id": f["id"],
                "name": f.get("name"),
                "kind": f.get("kind"),
                "type": f.get("type"),
                "file": f.get("file"),
                "line": f.get("line"),
                "function": f.get("function"),
                **r,
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["file"] or "", r["line"] or 0))

    def group(key: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
        acc: dict[str, dict[str, Any]] = {}
        for r in rows:
            g = acc.setdefault(key(r), {"name": key(r), "score": 0, "pointers": 0, "high": 0, "medium": 0, "low": 0})
            g["score"] += r["score"]
            g["pointers"] += 1
            g[r["level"]] += 1
        return sorted(acc.values(), key=lambda g: (-g["score"], g["name"]))

    factor_counts = Counter(x["id"] for r in rows for x in r["factors"])
    return {
        "levels": {name: lo for name, lo in LEVELS},
        "factors": {k: {**v, "pointers": factor_counts.get(k, 0)} for k, v in FACTORS.items()},
        "summary": {
            "pointers": len(rows),
            **{name: sum(1 for r in rows if r["level"] == name) for name, _ in LEVELS},
        },
        "pointers": rows,
        "functions": group(lambda r: f"{r['file']}::{r['function'] or '(file scope)'}"),
        "files": group(lambda r: r["file"] or "?"),
        "modules": group(lambda r: module_of(r["file"] or "?")),
    }
