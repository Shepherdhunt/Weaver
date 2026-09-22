"""Candidate cards (pointer-tracker plan §6).

The card is the unit of review: it states the candidate and its revision, the
pointer's role and evidence, what is affected, the recipe and required target
capabilities, which preconditions are established or not, the behavior to
preserve, the patch and validation plan, the actual results with their limits,
and the decision and rollback checkpoint.  No confidence percentages.
"""

from __future__ import annotations

import textwrap
from typing import Any

MARK = {"established": "[x]", "violated": "[!]", "unresolved": "[?]"}


def _wrap(s: str, indent: str = "    ") -> str:
    return textwrap.fill(s, width=100, initial_indent=indent, subsequent_indent=indent)


def render_card(txn: dict[str, Any]) -> str:
    f = txn["finding"]
    c = txn["candidate"]
    rev = txn.get("source_revision") or {}
    out: list[str] = []
    add = out.append

    add(f"=== {txn['id']}  [{txn['state'].upper()}] ===")
    add("Candidate and source/configuration revision:")
    add(
        f"    {txn['finding_id']}: {f.get('kind')} '{f.get('name')}' in {f.get('function') or '<file scope>'}() "
        f"at {f.get('file')}:{f.get('line')}:{f.get('col')}"
    )
    if rev.get("vcs"):
        add(
            f"    revision {rev.get('commit', '?')[:12]}{' (dirty)' if rev.get('dirty') else ''}; "
            f"file sha256 {next(iter(c['file_hashes'].values()))[:12]}"
        )
    for u in c.get("units", []):
        add(
            f"    configuration {u['profile']} / unit {u['unit']}: declaration {u['declaration']}"
            + (f", {u['uses']} use(s)" if "uses" in u else "")
            + (f"; note: {u['note']}" if u.get("note") else "")
        )

    add("Current pointer role and evidence:")
    add(f"    type {f.get('type')!r}; uses {f.get('use_summary') or {}}; evidence {f.get('evidence_status')}")

    aff = c.get("affected", {})
    add("Objects, aliases, files, interfaces, and callers affected:")
    add(
        f"    objects {aff.get('objects')}; files {aff.get('files')}; functions {aff.get('functions')}; "
        f"interfaces {aff.get('interfaces') or 'none'}"
    )

    add("Proposed recipe and target capabilities:")
    add(
        f"    {c['recipe']} v{c['recipe_version']}; CLite capabilities required: "
        f"{c.get('capabilities_required') or 'none (C-to-C simplification)'}"
    )

    add("Preconditions established / unresolved:")
    for p in c["preconditions"]:
        add(f"    {MARK.get(p['status'], '[ ]')} {p['id']}: {p['description']}")
        for ev in p.get("evidence", [])[:6]:
            add(f"          - {ev}")
        if p.get("resolve_by"):
            add(f"          > resolve by: {p['resolve_by']}")

    add("Behavior and resource requirements to preserve:")
    pres = txn.get("preservation_contract") or {}
    add(f"    contract: {pres.get('behaviors') or 'not recorded in weaver.yaml'}")
    if pres.get("interfaces"):
        add(f"    interfaces: {pres['interfaces']}")
    add(_wrap("argument: " + c.get("preservation_argument", "")))

    add("Patch and validation plan:")
    patch = txn.get("patch")
    if patch:
        for line in patch["diff"].rstrip("\n").splitlines():
            add("    " + line)
    else:
        add("    no patch (candidate is blocked)")
    for step in c.get("validation_plan", []):
        add(f"    - {step}")

    add("Results, assumptions, bounds, and remaining limitations:")
    val = txn.get("validation")
    if not val:
        add("    not validated yet")
    elif val.get("error"):
        add(f"    validation could not run: {val['error']}")
    else:
        for r in val["records"]:
            add(f"    {r['outcome']:>13}  {r['kind']:<22} {r['name']}: {r['detail'][:160]}")
        j = val.get("judgement") or {}
        if j.get("reasons"):
            add(f"    judgement: {j['state']} — " + "; ".join(j["reasons"]))
    add(
        "    limits: testing and differential runs cover only the exercised inputs; the mechanical re-check "
        "covers only the analysed configurations; no universal equivalence proof is claimed."
    )
    for n in c.get("notes", []):
        add(f"    note: {n}")

    add("Decision and rollback checkpoint:")
    add(f"    state {txn['state']}; history: " + " -> ".join(h["state"] for h in txn.get("history", [])))
    acc = txn.get("acceptance")
    if acc:
        add(f"    checkpoint {acc['checkpoint']} (revert with 'weaver revert {txn['id']}')")
    elif patch:
        add("    not applied; the working tree is unchanged")
    return "\n".join(out)
