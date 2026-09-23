"""``weaver report``: what the evidence supports, what is blocked and why, for a scope of the project.

The report is the pilot deliverable of the tracker plan's roadmap ("produce the
inventory and rejection report"): for every pointer in scope it records which
recipes apply, which preconditions fail, and which single precondition would
unlock the most candidates, next to the evidence the verdicts rest on (unit
evidence status, fidelity, coverage, flow evidence per program) and the state
of pinned contracts.
"""

from __future__ import annotations

import collections
import subprocess
from typing import Any, Callable

from weaver.config import Project
from weaver.util import now_iso


def _in_scope(f: dict[str, Any], scope: list[str]) -> bool:
    return not scope or any((f.get("file") or "").startswith(s) for s in scope)


def _git(project: Project) -> dict[str, Any] | None:
    try:
        head = subprocess.run(
            ["git", "-C", str(project.root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        )
        if head.returncode != 0:
            return None
        dirty = subprocess.run(
            ["git", "-C", str(project.root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {"commit": head.stdout.strip(), "modified_files": len(dirty.stdout.splitlines())}
    except (OSError, subprocess.TimeoutExpired):
        return None


def build_report(
    project: Project, scope: list[str] | None = None, log: Callable[[str], None] | None = None
) -> dict[str, Any]:
    from weaver import __version__
    from weaver.analysis.inventory import load_inventory
    from weaver.flow.evidence import flow_status
    from weaver.impact import access_class, check_contracts
    from weaver.recipes import CATALOG, RecipeContext

    scope = scope or []
    inv = load_inventory(project)
    ctx = RecipeContext(project, inv)
    findings = [f for f in inv["findings"] if _in_scope(f, scope)]
    units = [u for u in inv["units"] if _in_scope(u, scope)] if scope else inv["units"]

    rep: dict[str, Any] = {
        "schema": "weaver.report/1",
        "generated_at": now_iso(),
        "weaver": __version__,
        "project": project.name,
        "scope": scope,
        "revision": _git(project),
        "profiles": [p.id for p in project.profiles],
    }
    rep["evidence"] = {
        "units_total": len(inv["units"]),
        "units_in_scope": len(units),
        "evidence_status": dict(collections.Counter(u["evidence_status"] for u in inv["units"])),
        "evidence_status_in_scope": dict(collections.Counter(u["evidence_status"] for u in units)),
        "unexamined": {
            f: v["unexamined_ranges"]
            for f, v in inv["coverage"]["files"].items()
            if v.get("unexamined_ranges") and (not scope or any(f.startswith(s) for s in scope))
        },
        "flow": flow_status(project, inv),
    }
    gcc = _gcc_status(project)
    if gcc:
        rep["evidence"]["gcc_pta"] = gcc
    rep["inventory"] = {
        "findings": len(findings),
        "by_kind": dict(collections.Counter(f["kind"] for f in findings)),
        "by_class": dict(collections.Counter(access_class(f) for f in findings)),
    }

    recipes: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    n = 0
    for f in findings:
        for r in CATALOG.values():
            if not r.applicable(f):
                continue
            res = r.evaluate(ctx, f)
            n += 1
            if log and n % 200 == 0:
                log(f"  evaluated {n} candidate(s)")
            blocked = [p for p in res.preconditions if p.status != "established"]
            rec = recipes.setdefault(
                r.id,
                {
                    "applicable": 0,
                    "eligible": 0,
                    "blocked_by": collections.Counter(),
                    "sole_blocker": collections.Counter(),
                },
            )
            rec["applicable"] += 1
            if res.eligible:
                rec["eligible"] += 1
            for p in blocked:
                rec["blocked_by"][p.id] += 1
            if len(blocked) == 1:
                rec["sole_blocker"][blocked[0].id] += 1
            rows.append(
                {
                    "finding": f["id"],
                    "recipe": r.id,
                    "file": f.get("file"),
                    "line": f.get("line"),
                    "function": f.get("function"),
                    "name": f.get("name"),
                    "kind": f["kind"],
                    "type": f.get("type"),
                    "class": access_class(f),
                    "eligible": res.eligible,
                    "edits": len(res.edits),
                    "blockers": [
                        {"id": p.id, "status": p.status, "evidence": (p.evidence or [""])[0][:300]} for p in blocked
                    ],
                }
            )
    for rec in recipes.values():
        rec["blocked_by"] = dict(rec["blocked_by"].most_common())
        rec["sole_blocker"] = dict(rec["sole_blocker"].most_common())
    rep["recipes"] = recipes
    rep["candidates"] = rows
    rep["contracts"] = [c for c in check_contracts(project, inv) if c.get("status")]
    return rep


def _gcc_status(project: Project) -> dict[str, Any] | None:
    from weaver.store import Store
    from weaver.util import read_json

    out = {}
    for p in project.profiles:
        f = Store(project.state_dir).root / "flow" / p.id / "gcc" / "run.json"
        if f.exists():
            r = read_json(f)
            out[p.id] = {
                "status": r.get("status"),
                "reason": r.get("reason"),
                "deviations": r.get("deviations"),
                "programs": {
                    n: {
                        "status": v.get("status"),
                        "images": {i: x.get("status") for i, x in v.get("images", {}).items()},
                    }
                    for n, v in (r.get("programs") or {}).items()
                },
            }
    return out or None


def render_markdown(rep: dict[str, Any]) -> str:
    L: list[str] = []
    scope = ", ".join(rep["scope"]) or "whole project"
    L.append(f"# Weaver report: {rep['project']} ({scope})")
    L.append("")
    rev = rep.get("revision") or {}
    L.append(
        f"Generated {rep['generated_at']} by Weaver {rep['weaver']}"
        + (f" at commit `{rev.get('commit', '')[:12]}`" if rev else "")
        + (f" ({rev['modified_files']} modified tracked file(s))" if rev.get("modified_files") else "")
        + "."
    )
    L.append("")
    ev = rep["evidence"]
    L.append("## Evidence")
    L.append("")
    L.append(f"- Units analysed: {ev['units_total']} ({ev['units_in_scope']} in scope)")
    L.append("- Evidence status: " + ", ".join(f"{k} {v}" for k, v in sorted(ev["evidence_status"].items())))
    for pid, fs in (ev.get("flow") or {}).items():
        progs = fs.get("programs") or {}
        L.append(
            f"- SVF flow evidence ({pid}): "
            + (
                ", ".join(
                    f"{n} {p.get('status')}" + ("" if p.get("closed", True) else " (open)") for n, p in progs.items()
                )
                or "none"
            )
        )
    for pid, g in (ev.get("gcc_pta") or {}).items():
        L.append(
            f"- GCC IPA points-to ({pid}): "
            + ", ".join(f"{n} {p['status']}" for n, p in g["programs"].items())
            + (
                f"; analysis deviations from production flags: {' '.join(g['deviations'])}"
                if g.get("deviations")
                else ""
            )
        )
    if ev["unexamined"]:
        total = sum(b - a + 1 for rs in ev["unexamined"].values() for a, b in rs)
        L.append(
            f"- Unexamined code in scope: {total} line(s) in {len(ev['unexamined'])} file(s) "
            "(compiled by no analysed configuration)"
        )
    L.append("")
    inv = rep["inventory"]
    L.append("## Pointers")
    L.append("")
    L.append(
        f"{inv['findings']} pointer finding(s): "
        + ", ".join(f"{v} {k}" for k, v in sorted(inv["by_kind"].items(), key=lambda x: -x[1]))
        + "."
    )
    L.append(
        "By what each does to its target: "
        + ", ".join(f"{v} {k}" for k, v in sorted(inv["by_class"].items(), key=lambda x: -x[1]))
        + "."
    )
    L.append("")
    L.append("## Recipes")
    L.append("")
    L.append("| Recipe | Applicable | Eligible | Most common blockers | Would unlock alone |")
    L.append("|---|---|---|---|---|")
    for rid, r in rep["recipes"].items():
        top = ", ".join(f"{k} ({v})" for k, v in list(r["blocked_by"].items())[:4])
        sole = ", ".join(f"{k} ({v})" for k, v in list(r["sole_blocker"].items())[:3]) or "-"
        L.append(f"| `{rid}` | {r['applicable']} | {r['eligible']} | {top or '-'} | {sole} |")
    L.append("")
    elig = [c for c in rep["candidates"] if c["eligible"]]
    L.append(f"### Eligible ({len(elig)})")
    L.append("")
    if elig:
        L.append("| Recipe | Pointer | Where | Edits |")
        L.append("|---|---|---|---|")
        for c in elig:
            L.append(
                f"| `{c['recipe']}` | `{c['name']}` in `{c['function'] or ''}()` "
                f"| `{c['file']}:{c['line']}` | {c['edits']} |"
            )
    else:
        L.append("None.")
    L.append("")
    blocked = [c for c in rep["candidates"] if not c["eligible"]]
    L.append(f"### Blocked ({len(blocked)})")
    L.append("")
    if blocked:
        L.append("| Pointer | Recipe | Blocking preconditions (first evidence) |")
        L.append("|---|---|---|")
        for c in blocked[:400]:
            reasons = "<br>".join(
                f"{'✗' if b['status'] == 'violated' else '?'} {b['id']}: {b['evidence'].replace('|', '/')[:160]}"
                for b in c["blockers"][:4]
            )
            L.append(
                f"| `{c['name']}` in `{c['function'] or c['file']}` (`{c['file']}:{c['line']}`) "
                f"| `{c['recipe']}` | {reasons} |"
            )
        if len(blocked) > 400:
            L.append(f"| ... | | {len(blocked) - 400} more in the JSON report |")
    L.append("")
    if rep["contracts"]:
        L.append("## Contracts")
        L.append("")
        for c in rep["contracts"]:
            L.append(f"- `{c['contract']}` **{c['status']}**: {c['text']}")
        L.append("")
    L.append("## Reading this report")
    L.append("")
    L.append(
        "✗ marks a violated precondition (counter-evidence exists); ? marks one that could not be established from "
        "the available evidence. Unknown is never reported as safe. Eligibility is a proposal: each transaction is "
        "still validated in isolated workspaces before it can be accepted."
    )
    L.append("")
    return "\n".join(L)
