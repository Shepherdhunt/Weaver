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

    is_patch = c["recipe"] == "patch"
    add(f"=== {txn['id']}  [{txn['state'].upper()}] ===")
    add("Candidate and source/configuration revision:")
    if is_patch:
        from weaver.patch import origin_label

        add(f"    {txn.get('title') or 'untitled change'}: {origin_label(txn.get('origin'))}")
        names = (c.get("recheck") or {}).get("names", {})
        add(
            "    claims to remove: " + ", ".join(f"{k} '{v}'" for k, v in names.items())
            if names
            else "    names no pointer to remove: every change to pointer facts is reported"
        )
    else:
        add(
            f"    {txn['finding_id']}: {f.get('kind')} '{f.get('name')}' in {f.get('function') or '<file scope>'}() "
            f"at {f.get('file')}:{f.get('line')}:{f.get('col')}"
        )
    if rev.get("vcs") and c["file_hashes"]:
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

    if not is_patch or f.get("kind") != "patch":
        add("Current pointer role and evidence:")
        add(f"    type {f.get('type')!r}; uses {f.get('use_summary') or {}}; evidence {f.get('evidence_status')}")

    aff = c.get("affected", {})
    add("Objects, aliases, files, interfaces, and callers affected:")
    add(
        f"    objects {aff.get('objects')}; files {aff.get('files')}; functions {aff.get('functions')}; "
        f"interfaces {aff.get('interfaces') or 'none'}"
    )

    add("Proposed recipe and target capabilities:")
    if is_patch:
        add("    no recipe: the pointer facts of every affected unit are compared before and after the patch")
    else:
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
            fx = r.get("facts")
            if fx:
                for t in fx.get("targets", []):
                    add(f"          target {t['finding']} '{t['name']}': {t['status']}")
                for label, items in (("removed", fx.get("removed", [])), ("added", fx.get("added", []))):
                    for x in items[:12]:
                        add(f"          {label}: {x['text']}")
                for x in fx.get("review", [])[:20]:
                    add(f"          REVIEW [{x['severity']}] {x.get('name') or ''} {x['text']}".replace("  ", " "))
        j = val.get("judgement") or {}
        if j.get("reasons"):
            add(f"    judgement: {j['state']} — " + "; ".join(j["reasons"]))
        strength = val.get("strength")
        if strength == "compile-only":
            add(
                "    strength: compile-only — the patch builds and re-checks, but no test or differential run "
                "executed it; behaviour is not validated"
            )
        elif strength:
            add(f"    strength: {strength} — a test or differential run passed on the patched tree")
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
        add(
            f"    checkpoint {acc['checkpoint']} (revert with 'weaver revert {txn['id']}')"
            + (f"; accepted on {acc['strength']} evidence" if acc.get("strength") else "")
        )
    elif patch:
        add("    not applied; the working tree is unchanged")
    return "\n".join(out)
