"""Change impact: did someone else's edit change what a pointer does?

A *snapshot* freezes the analysed state of the project: its inventory (pointer
facts keyed by stable IDs), each candidate's recipe verdict, and a copy of the
source tree.  ``impact`` re-reads the current inventory and explains every
difference in pointer behavior, attributed to the changed lines:

* a read-only pointer that is now written through, a pointer whose address now
  escapes, a new identity comparison, new possible targets, a changed type;
* recipe verdicts that changed (a candidate that is now blocked, and why);
* contracts that no longer hold — pinned by users (``weaver contract pin``) or
  implied by accepted transactions (a removed alias must not come back; a
  by-value parameter must stay by value);
* accepted transactions whose edited lines were later modified.

Optionally the configured builds, tests and differential comparisons run on
the snapshot tree and on the current tree.  Severity is ``high`` (behavioral
risk), ``review`` or ``info``; ``weaver check`` fails on ``high``.
"""

from __future__ import annotations

import difflib
import os
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from weaver import SCHEMA_VERSION
from weaver.analysis.inventory import load_inventory
from weaver.analysis.uses import describe_use_parts
from weaver.config import Project
from weaver.errors import WeaverError
from weaver.store import Store
from weaver.util import now_iso, read_json, run, sha256_file, short_hash, write_json

HIGH, REVIEW, INFO = "high", "review", "info"
RANK = {HIGH: 3, REVIEW: 2, INFO: 1, None: 0}
WRITE_ACCESS = {"write", "readwrite", "member-write", "member-readwrite"}
ESCAPE_KINDS = {"call-arg", "copy", "return", "cast", "asm", "address-of-pointer", "other", "indirect-call"}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshot_dir(project: Project, name: str) -> Path:
    return Store(project.state_dir).root / "snapshots" / name


def _copy_tree(project: Project, src: Path, dest: Path) -> None:
    state = project.state_dir.resolve()
    excludes = set(project.workspace_exclude) | {".git"}

    def ignore(d: str, names: list[str]) -> set[str]:
        return {n for n in names if n in excludes or Path(d, n).resolve() == state}

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True, ignore=ignore)


def _verdicts(project: Project, inv: dict[str, Any]) -> dict[str, Any]:
    from weaver.recipes import RecipeContext, recipes_for_finding

    ctx = RecipeContext(project, inv)
    out: dict[str, Any] = {}
    for f in inv["findings"]:
        for r in recipes_for_finding(f):
            res = r.evaluate(ctx, f)
            out.setdefault(f["id"], {})[r.id] = {
                "eligible": res.eligible,
                "blockers": [
                    {"id": b["id"], "status": b["status"], "evidence": b["evidence"][:3]} for b in res.blockers
                ],
            }
    return out


def save_snapshot(project: Project, name: str | None = None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    from weaver.toolchain.collect import _source_revision

    inv = load_inventory(project)
    stale = [f for f, h in inv["files"].items() if (project.root / f).exists() and sha256_file(project.root / f) != h]
    if stale:
        raise WeaverError(f"the inventory is stale for {len(stale)} file(s) (e.g. {stale[0]}); run 'weaver refresh'")
    name = name or time.strftime("snap-%Y%m%d-%H%M%S")
    d = snapshot_dir(project, name)
    d.mkdir(parents=True, exist_ok=True)
    _copy_tree(project, project.root, d / "tree")
    write_json(d / "inventory.json", inv)
    write_json(d / "verdicts.json", _verdicts(project, inv))
    meta = {
        "schema": f"weaver.snapshot/{SCHEMA_VERSION}",
        "name": name,
        "created_at": now_iso(),
        "source": source or {"kind": "working-tree", "revision": _source_revision(project.root)},
        "inventory_generated_at": inv["generated_at"],
        "findings": len(inv["findings"]),
    }
    write_json(d / "meta.json", meta)
    return meta


def snapshot_from_git(project: Project, rev: str, name: str | None = None, log: Any = print) -> dict[str, Any]:
    """Analyse the project as it was at git revision ``rev`` and store it as a snapshot."""
    import copy

    from weaver.capture.compdb import load_compdb, write_compdb
    from weaver.pipeline import refresh
    from weaver.validate import Remapper

    r = run(["git", "-C", str(project.root), "rev-parse", "--verify", f"{rev}^{{commit}}"], timeout=30)
    if not r.ok:
        raise WeaverError(f"not a git revision: {rev}")
    commit = r.stdout.decode().strip()
    name = name or f"git-{commit[:12]}"
    d = snapshot_dir(project, name)
    if d.exists():
        shutil.rmtree(d)
    tree = d / "tree"
    tree.mkdir(parents=True)
    arch = run(["git", "-C", str(project.root), "archive", "--format=tar", commit], timeout=600)
    if not arch.ok:
        raise WeaverError(f"git archive failed: {arch.stderr_text(500)}")
    x = run(["tar", "-x", "-C", str(tree)], input=arch.stdout, timeout=600)
    if not x.ok:
        raise WeaverError(f"cannot unpack {commit}: {x.stderr_text(500)}")
    # A clone of the project rooted at the old tree, with its own state directory and
    # each profile's compile database remapped onto the old tree.
    clone = copy.deepcopy(project)
    clone.root = tree.resolve()
    clone.state_dir = (d / "state").resolve()
    remap = Remapper(project.root, clone.root)
    for prof in clone.profiles:
        cmds = load_compdb(prof.compile_commands)
        entries = [
            {
                "directory": remap(c.directory),
                "file": remap(c.file),
                "arguments": [remap(a) for a in c.arguments],
                **({"output": c.output} if c.output else {}),
            }
            for c in cmds
        ]
        out = clone.state_dir / "compdb" / prof.id / "compile_commands.json"
        write_compdb(out, entries)
        links = prof.link_manifest or prof.compile_commands.parent / "links.json"
        if links.exists():
            shutil.copy(links, out.parent / "links.json")
        prof.compile_commands = out
        prof.link_manifest = None
        for dname in {os.path.dirname(e["directory"]) for e in entries} | {e["directory"] for e in entries}:
            os.makedirs(dname, exist_ok=True)
    log(f"analysing {commit[:12]} in {tree}")
    refresh(clone, log=log)
    inv = load_inventory(clone)
    write_json(d / "inventory.json", inv)
    write_json(d / "verdicts.json", _verdicts(clone, inv))
    meta = {
        "schema": f"weaver.snapshot/{SCHEMA_VERSION}",
        "name": name,
        "created_at": now_iso(),
        "source": {"kind": "git", "commit": commit, "rev": rev},
        "inventory_generated_at": inv["generated_at"],
        "findings": len(inv["findings"]),
    }
    write_json(d / "meta.json", meta)
    return meta


def list_snapshots(project: Project) -> list[dict[str, Any]]:
    root = Store(project.state_dir).root / "snapshots"
    if not root.exists():
        return []
    out = [read_json(p / "meta.json") for p in root.iterdir() if (p / "meta.json").exists()]
    return sorted(out, key=lambda m: m["created_at"])


def load_snapshot(project: Project, name: str) -> dict[str, Any]:
    d = snapshot_dir(project, name)
    if not (d / "meta.json").exists():
        known = ", ".join(m["name"] for m in list_snapshots(project)) or "none"
        raise WeaverError(f"no snapshot {name!r} (known: {known}); create one with 'weaver snapshot save'")
    return {
        "meta": read_json(d / "meta.json"),
        "inventory": read_json(d / "inventory.json"),
        "verdicts": read_json(d / "verdicts.json") if (d / "verdicts.json").exists() else {},
        "tree": d / "tree",
    }


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

EXPECTATIONS = {
    "read-only": "the pointer is never used to write its target",
    "no-escape": "the pointer's value never leaves the function (no copy, argument, return, cast)",
    "no-identity": "the pointer is never compared",
    "no-reassign": "the pointer is never reassigned",
    "not-null-tested": "the pointer is never null-tested",
    "borrowed": "the target is only read and no copy of the pointer outlives the call chain "
    "(followed into analysed callees and through reviewed models)",
}


def load_contracts(project: Project) -> list[dict[str, Any]]:
    p = project.contracts_path
    if p is None or not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    return list(raw.get("contracts") or [])


def pin_contract(project: Project, finding_id: str, expect: list[str], reason: str = "") -> dict[str, Any]:
    from weaver.analysis.inventory import find_finding

    bad = [e for e in expect if e not in EXPECTATIONS]
    if bad:
        raise WeaverError(f"unknown expectation(s) {bad}; known: {', '.join(EXPECTATIONS)}")
    inv = load_inventory(project)
    f = find_finding(inv, finding_id)
    violations, unknown = _contract_check({"expect": expect}, f, project, inv)
    if violations or unknown:
        raise WeaverError("the pointer does not currently satisfy the expectations: " + "; ".join(violations + unknown))
    contracts = load_contracts(project)
    c = {
        "id": "C-" + short_hash(f["id"], sorted(expect), length=8),
        "finding": f["id"],
        "subject": {k: f.get(k) for k in ("kind", "name", "function", "file", "type")},
        "expect": sorted(expect),
        "reason": reason,
        "created": now_iso(),
    }
    contracts = [x for x in contracts if x.get("id") != c["id"]] + [c]
    assert project.contracts_path is not None
    project.contracts_path.write_text(
        yaml.safe_dump({"schema": "weaver.contracts/1", "contracts": contracts}, sort_keys=False)
    )
    return c


def access_class(f: dict[str, Any]) -> str:
    """Summary color class used by reports and the web map."""
    uses = f.get("uses", [])
    kinds = {u["kind"] for u in uses}
    if kinds & ESCAPE_KINDS:
        return "escapes"
    if any(u.get("access") in WRITE_ACCESS for u in uses if u["kind"] in ("deref", "arrow", "subscript")):
        return "writes"
    if kinds & {"reassign", "arith-update"}:
        return "reassigned"
    if not uses:
        return "unused"
    return "read-only"


def _contract_check(
    c: dict[str, Any], f: dict[str, Any], project: Project, inv: dict[str, Any], cache: dict[str, Any] | None = None
) -> tuple[list[str], list[str]]:
    """(violations, unresolved) of a contract on the current inventory."""
    out = _contract_violations(c, f)
    unknown: list[str] = []
    if "borrowed" in c["expect"]:
        from weaver.analysis.borrow import check_borrow

        cache = cache if cache is not None else {}
        if "program" not in cache:
            from weaver.recipes import RecipeContext

            cache["program"] = RecipeContext(project, inv).program
        r = check_borrow(inv, cache["program"], f)
        for x in r.reasons:
            text = f"borrowed: {x.get('function')}() line {x.get('line')}: {x['text']}"
            (out if x["status"] == "violated" else unknown).append(text)
    return out, unknown


def _contract_violations(c: dict[str, Any], f: dict[str, Any]) -> list[str]:
    out = []
    for u in f.get("uses", []):
        k, a = u["kind"], u.get("access")
        d = describe_use_parts(k, a, u.get("detail") or {})
        if "read-only" in c["expect"] and k in ("deref", "arrow", "subscript") and a in WRITE_ACCESS:
            out.append(f"read-only: line {u['line']}: {d}")
        if "no-escape" in c["expect"] and k in ESCAPE_KINDS:
            out.append(f"no-escape: line {u['line']}: {d}")
        if "no-identity" in c["expect"] and k == "compare":
            out.append(f"no-identity: line {u['line']}: {d}")
        if "no-reassign" in c["expect"] and k in ("reassign", "arith-update"):
            out.append(f"no-reassign: line {u['line']}: {d}")
        if "not-null-tested" in c["expect"] and k == "null-test":
            out.append(f"not-null-tested: line {u['line']}: {d}")
    return out


def _implied_contracts(project: Project) -> list[dict[str, Any]]:
    """Contracts implied by accepted (and not reverted) transactions."""
    from weaver.ledger import Ledger

    out = []
    for t in Ledger(project).all():
        if t["state"] != "accepted":
            continue
        f = t["finding"]
        rc = t["candidate"].get("recheck") or {}
        if t["recipe"] == "local-alias":
            out.append(
                {
                    "id": f"{t['id']}/no-alias",
                    "txn": t["id"],
                    "kind": "no-reintroduced-alias",
                    "function": f.get("function"),
                    "file": f.get("file"),
                    "target": rc.get("target_name"),
                    "text": f"{t['id']} replaced '{f.get('name')}' with direct access to "
                    f"'{rc.get('target_name')}' in {f.get('function')}()",
                }
            )
        elif t["recipe"] == "scalar-input":
            out.append(
                {
                    "id": f"{t['id']}/by-value",
                    "txn": t["id"],
                    "kind": "by-value-parameter",
                    "function": rc.get("function"),
                    "index": rc.get("param_index"),
                    "text": f"{t['id']} made parameter {int(rc.get('param_index') or 0) + 1} of "
                    f"{rc.get('function')}() a value parameter",
                }
            )
    return out


def check_contracts(project: Project, inv: dict[str, Any]) -> list[dict[str, Any]]:
    from weaver.frontend.typestr import safe_parse

    results = []
    by_id = {f["id"]: f for f in inv["findings"]}
    cache: dict[str, Any] = {}
    for c in load_contracts(project):
        f = by_id.get(c["finding"])
        if f is None:
            results.append(
                {
                    "contract": c["id"],
                    "status": "subject-gone",
                    "severity": REVIEW,
                    "text": f"pinned pointer {c['subject'].get('name')} in "
                    f"{c['subject'].get('function') or c['subject'].get('file')} no longer exists",
                }
            )
            continue
        v, unk = _contract_check(c, f, project, inv, cache)
        status = "violated" if v else "unknown" if unk else "held"
        results.append(
            {
                "contract": c["id"],
                "status": status,
                "severity": HIGH if v else REVIEW if unk else None,
                "text": f"{c['subject'].get('name')} in {c['subject'].get('function')}(): "
                f"{', '.join(c['expect'])}" + (": " + "; ".join(v + unk) if v or unk else ""),
                "finding": f["id"],
            }
        )
    for c in _implied_contracts(project):
        if c["kind"] == "no-reintroduced-alias":
            back = [
                f
                for f in inv["findings"]
                if f.get("function") == c["function"]
                and f.get("file") == c["file"]
                and any((t.get("object") or {}).get("name") == c["target"] for t in f.get("possible_targets", []))
            ]
            results.append(
                {
                    "contract": c["id"],
                    "status": "violated" if back else "held",
                    "severity": HIGH if back else None,
                    "text": c["text"]
                    + (
                        "; a pointer to it is back: " + ", ".join(f"'{f['name']}' line {f['line']}" for f in back)
                        if back
                        else ""
                    ),
                }
            )
        else:
            decls = [d for d in inv.get("function_decls", []) if d["name"] == c["function"]]
            bad = []
            for d in decls:
                if c["index"] is not None and c["index"] < len(d["params"]):
                    t = safe_parse(d["params"][c["index"]].get("canonical_type"))
                    if t is not None and t.kind == "pointer":
                        bad.append(f"{d['file']}:{d['line']}")
            results.append(
                {
                    "contract": c["id"],
                    "status": "violated" if bad else "held",
                    "severity": HIGH if bad else None,
                    "text": c["text"] + ("; it is a pointer again at " + ", ".join(bad) if bad else ""),
                }
            )
    return results


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _file_hunks(old: Path, new: Path) -> list[dict[str, Any]]:
    a = old.read_text(errors="replace").splitlines() if old.exists() else []
    b = new.read_text(errors="replace").splitlines() if new.exists() else []
    hunks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag != "equal":
            hunks.append(
                {"tag": tag, "old": [i1 + 1, i2], "new": [j1 + 1, j2], "before": a[i1:i2][:12], "after": b[j1:j2][:12]}
            )
    return hunks


def _use_key(u: dict[str, Any]) -> tuple[str, str, str]:
    d = u.get("detail") or {}
    detail = d.get("callee") or (d.get("into") or {}).get("name") or d.get("op") or d.get("cast_kind") or ""
    return u["kind"], u.get("access") or "", str(detail)


def _targets(f: dict[str, Any]) -> set[str]:
    out = set()
    for t in f.get("possible_targets", []):
        obj = t.get("object") or {}
        out.add(f"{t.get('source')}:{obj.get('name') or t.get('name') or t.get('callee') or ''}")
    return out


def _source_line(root: Path, rel: str | None, line: int | None) -> str | None:
    if not rel or not line:
        return None
    try:
        lines = (root / rel).read_text(errors="replace").splitlines()
        return lines[line - 1].strip() if 0 < line <= len(lines) else None
    except OSError:
        return None


def _in_hunks(hunks: list[dict[str, Any]], line: int | None) -> bool:
    return bool(line) and any(h["new"][0] <= line <= max(h["new"][1], h["new"][0]) for h in hunks)


def diff(project: Project, base: dict[str, Any], cur: dict[str, Any], cur_verdicts: dict[str, Any]) -> dict[str, Any]:
    tree: Path = base["tree"]
    binv = base["inventory"]
    files = sorted(set(binv["files"]) | set(cur["files"]))
    changed_files: dict[str, list[dict[str, Any]]] = {}
    for rel in files:
        old, new = tree / rel, project.root / rel
        if old.exists() and new.exists() and sha256_file(old) == sha256_file(new):
            continue
        changed_files[rel] = _file_hunks(old, new)

    bf = {f["id"]: f for f in binv["findings"]}
    cf = {f["id"]: f for f in cur["findings"]}
    changes: list[dict[str, Any]] = []

    def add(sev: str, f: dict[str, Any], aspect: str, text: str, **extra: Any) -> None:
        changes.append(
            {
                "severity": sev,
                "finding": f["id"],
                "name": f.get("name"),
                "kind": f.get("kind"),
                "function": f.get("function"),
                "file": f.get("file"),
                "aspect": aspect,
                "text": text,
                **extra,
            }
        )

    for fid in sorted(set(cf) - set(bf)):
        f = cf[fid]
        cls = access_class(f)
        add(
            REVIEW if cls in ("escapes", "writes") else INFO,
            f,
            "added",
            f"new {f['kind']} pointer '{f.get('name')}' ({f.get('type')}) in {f.get('function') or f.get('file')}"
            f"{'()' if f.get('function') else ''} at line {f.get('line')}, {cls}",
            line=f.get("line"),
            source=_source_line(project.root, f.get("file"), f.get("line")),
            caused_by_change=_in_hunks(changed_files.get(f.get("file") or "", []), f.get("line")),
        )
    for fid in sorted(set(bf) - set(cf)):
        f = bf[fid]
        add(INFO, f, "removed", f"pointer '{f.get('name')}' in {f.get('function') or f.get('file')} no longer exists")

    for fid in sorted(set(bf) & set(cf)):
        a, b = bf[fid], cf[fid]
        hunks = changed_files.get(b.get("file") or "", [])
        if a.get("canonical_type") != b.get("canonical_type") or a.get("type") != b.get("type"):
            add(HIGH, b, "type", f"type changed from '{a.get('type')}' to '{b.get('type')}'", line=b.get("line"))
        ca, cb = access_class(a), access_class(b)
        ua = {_use_key(u) for u in a.get("uses", [])}
        new_uses = [u for u in b.get("uses", []) if _use_key(u) not in ua]
        cur = {_use_key(u) for u in b.get("uses", [])}
        gone: dict[tuple[str, str, str], dict[str, Any]] = {}
        for u in a.get("uses", []):
            if _use_key(u) not in cur:
                gone.setdefault(_use_key(u), u)  # described from the baseline's own record
        for u in new_uses:
            k, acc = u["kind"], u.get("access")
            d = describe_use_parts(k, acc, u.get("detail") or {})
            if k in ("deref", "arrow", "subscript") and acc in WRITE_ACCESS and ca == "read-only":
                sev, why = HIGH, "was read-only; its target is now written"
            elif k in ESCAPE_KINDS and ca != "escapes":
                sev, why = HIGH, "its value now leaves the function"
            elif k == "compare":
                sev, why = REVIEW, "its identity is now observed"
            elif k in ("reassign", "arith-update"):
                sev, why = REVIEW, "it is now reassigned"
            else:
                sev, why = INFO, "new use"
            add(
                sev,
                b,
                "use",
                f"line {u['line']}: {d} — {why}",
                line=u["line"],
                source=_source_line(project.root, b.get("file"), u["line"]),
                caused_by_change=_in_hunks(hunks, u["line"]),
            )
        for key in sorted(gone):
            u = gone[key]
            what = describe_use_parts(u["kind"], u.get("access"), u.get("detail") or {})
            add(INFO, b, "use-removed", f"no longer {what}")
        if ca != cb:
            add(
                HIGH if (ca == "read-only" and cb in ("writes", "escapes")) else REVIEW,
                b,
                "access-class",
                f"access class changed: {ca} → {cb}",
            )
        ta, tb = _targets(a), _targets(b)
        if tb - ta:
            add(REVIEW, b, "targets", f"new possible targets: {', '.join(sorted(tb - ta))}")
        if a.get("evidence_status") != b.get("evidence_status"):
            add(REVIEW, b, "evidence", f"evidence {a.get('evidence_status')} → {b.get('evidence_status')}")

    # recipe verdicts
    bv = base.get("verdicts") or {}
    for fid, recs in cur_verdicts.items():
        for rid, v in recs.items():
            old = (bv.get(fid) or {}).get(rid)
            if old is None or fid not in cf:
                continue
            f = cf[fid]
            if old["eligible"] and not v["eligible"]:
                why = "; ".join(f"{b['id']}: {(b['evidence'] or [''])[0]}" for b in v["blockers"])
                add(REVIEW, f, "verdict", f"{rid} was eligible and is now blocked — {why}")
            elif not old["eligible"] and v["eligible"]:
                add(INFO, f, "verdict", f"{rid} is now eligible")

    # accepted transactions whose edited lines changed later
    txn_notes = _transaction_overlaps(project, base, changed_files)
    return {"changed_files": changed_files, "changes": changes, "transactions": txn_notes}


def _transaction_overlaps(project: Project, base: dict[str, Any], changed: dict[str, Any]) -> list[dict[str, Any]]:
    from weaver.ledger import Ledger
    from weaver.rewrite import Edit, OffsetMap

    out = []
    for t in Ledger(project).all():
        if t["state"] != "accepted":
            continue
        post = (t.get("acceptance") or {}).get("post_hashes", {})
        strength = (t.get("acceptance") or {}).get("strength")
        weak = (
            "; it was accepted on compile and re-check evidence only, so no test has ever exercised it"
            if strength == "compile-only"
            else ""
        )
        for rel, h in post.items():
            if rel not in changed:
                continue
            bt = base["tree"] / rel
            if not bt.exists() or sha256_file(bt) != h:
                out.append(
                    {
                        "txn": t["id"],
                        "file": rel,
                        "severity": REVIEW,
                        "text": f"{rel} changed after {t['id']} and the snapshot does not contain its exact result; "
                        "re-run its validation plan" + weak,
                        "strength": strength,
                    }
                )
                continue
            edits = [Edit.from_json(e) for e in t["candidate"]["edits"] if e["file"] == rel]
            om = OffsetMap(edits)
            text = bt.read_text(errors="replace")
            lines = set()
            for e in edits:
                lo, hi = om.replaced_range(e)
                lines.update(range(text.count("\n", 0, lo) + 1, text.count("\n", 0, max(hi, lo)) + 2))
            hit = [h for h in changed[rel] if any(h["old"][0] <= ln <= max(h["old"][1], h["old"][0]) for ln in lines)]
            if hit:
                out.append(
                    {
                        "txn": t["id"],
                        "file": rel,
                        "severity": HIGH,
                        "text": f"lines edited by {t['id']} ({t['recipe']} on '{t['finding'].get('name')}') were "
                        f"modified afterwards: " + ", ".join(f"{h['old'][0]}-{h['old'][1]}" for h in hit) + weak,
                        "strength": strength,
                    }
                )
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def impact(project: Project, since: str, revalidate: bool = False, log: Any = None) -> dict[str, Any]:
    from weaver.validate import configured_strength

    base = load_snapshot(project, since)
    cur = load_inventory(project)
    stale = [f for f, h in cur["files"].items() if (project.root / f).exists() and sha256_file(project.root / f) != h]
    cur_verdicts = _verdicts(project, cur)
    rep = diff(project, base, cur, cur_verdicts)
    rep["contracts"] = check_contracts(project, cur)
    rep["base"] = base["meta"]
    rep["generated_at"] = now_iso()
    rep["stale_inventory"] = stale
    rep["validation"] = configured_strength(project)
    if revalidate:
        rep["revalidation"] = revalidate_against(project, base["tree"], log=log)
    sev = (
        [c["severity"] for c in rep["changes"]]
        + [t["severity"] for t in rep["transactions"]]
        + [c["severity"] for c in rep["contracts"] if c.get("severity")]
    )
    if rep.get("revalidation") and any(r["outcome"] == "failed" for r in rep["revalidation"]):
        sev.append(HIGH)
    if stale:
        sev.append(REVIEW)
    rep["risk"] = max(sev, key=lambda s: RANK[s]) if sev else "none"
    rep["summary"] = {
        "changed_files": len(rep["changed_files"]),
        "high": sum(1 for s in sev if s == HIGH),
        "review": sum(1 for s in sev if s == REVIEW),
        "info": sum(1 for s in sev if s == INFO),
        "contracts_violated": sum(1 for c in rep["contracts"] if c["status"] == "violated"),
    }
    write_json(Store(project.state_dir).root / "impact" / f"{since}.json", rep)
    return rep


def revalidate_against(project: Project, tree: Path, log: Any = None) -> list[dict[str, Any]]:
    """Run each profile's build, tests and comparisons on the snapshot tree and on the current tree."""
    from weaver.validate import _compare_tests, _run_spec, parse_test_outcomes

    work = Store(project.state_dir).root / "impact" / "work"
    base_ws, cur_ws = work / "baseline", work / "current"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(tree, base_ws, symlinks=True)
    _copy_tree(project, project.root, cur_ws)
    records = []
    for prof in project.profiles:
        v = prof.validation
        ok = {"baseline": True, "current": True}
        if v.build:
            for label, ws in (("baseline", base_ws), ("current", cur_ws)):
                argv, cwd, env = v.build.render(workspace=str(ws), root=str(project.root))
                r = run(argv, cwd=cwd, env=env, timeout=v.build.timeout)
                ok[label] = r.ok
            records.append(
                {
                    "kind": "build",
                    "name": f"{prof.id}:build",
                    "outcome": "passed" if ok["current"] else ("failed" if ok["baseline"] else "not-evaluated"),
                    "detail": f"baseline {'ok' if ok['baseline'] else 'failed'}, current "
                    f"{'ok' if ok['current'] else 'failed'}",
                }
            )
        for t in v.tests:
            if not ok["current"]:
                continue
            ra = _run_spec(t, base_ws, project) if ok["baseline"] else None
            rb = _run_spec(t, cur_ws, project)
            per_a = parse_test_outcomes(ra.stdout_text(None)) if ra is not None else {}
            per_b = parse_test_outcomes(rb.stdout_text(None))
            rec: dict[str, Any] = {"kind": "testing", "name": f"{prof.id}:{t.name}"}
            if per_a and per_b:
                outcome, detail, extra = _compare_tests(per_a, per_b)
                rec.update(outcome=outcome.value, detail=detail.replace("with the patch", "now"), **extra)
            elif rb.ok:
                rec.update(outcome="passed", detail="exit 0")
            elif ra is not None and not ra.ok:
                rec.update(outcome="not-evaluated", detail=f"exit {rb.returncode}; also fails on the snapshot")
            else:
                rec.update(outcome="failed", detail=f"exit {rb.returncode}; passes on the snapshot")
            records.append(rec)
        for t in v.compare:
            if not (ok["current"] and ok["baseline"]):
                continue
            ra, rb = _run_spec(t, base_ws, project), _run_spec(t, cur_ws, project)
            same = ra.returncode == rb.returncode and ra.stdout == rb.stdout
            rec = {
                "kind": "differential-testing",
                "name": f"{prof.id}:{t.name}",
                "outcome": "passed" if same else "failed",
                "detail": "identical exit status and stdout" if same else "behavior differs from the snapshot",
            }
            if not same:
                rec["diff"] = "".join(
                    difflib.unified_diff(
                        ra.stdout_text(4000).splitlines(keepends=True),
                        rb.stdout_text(4000).splitlines(keepends=True),
                        "snapshot",
                        "current",
                    )
                )
            records.append(rec)
        if log:
            log(f"[{prof.id}] revalidation: " + ", ".join(f"{r['name']} {r['outcome']}" for r in records))
    shutil.rmtree(work, ignore_errors=True)
    return records


def render_report(rep: dict[str, Any]) -> str:
    lines = [
        f"Change impact since snapshot '{rep['base']['name']}' ({rep['base']['created_at']}): risk "
        f"{rep['risk'].upper()}",
        f"  {rep['summary']['changed_files']} changed file(s); {rep['summary']['high']} high, "
        f"{rep['summary']['review']} review, {rep['summary']['info']} info; "
        f"{rep['summary']['contracts_violated']} contract(s) violated",
    ]
    if rep.get("stale_inventory"):
        lines.append(f"  WARNING: inventory is stale for {len(rep['stale_inventory'])} file(s); run 'weaver refresh'")
    if (rep.get("validation") or {}).get("level") == "compile-only":
        lines.append(
            "  NOTE: no tests are configured, so this report rests on pointer facts and contracts only; "
            "--revalidate can rebuild but cannot run anything"
        )
    for rel, hunks in rep["changed_files"].items():
        lines.append(f"  changed {rel}: " + ", ".join(f"lines {h['new'][0]}-{h['new'][1]}" for h in hunks))
    order = sorted(rep["changes"], key=lambda c: -RANK[c["severity"]])
    for c in order:
        where = f"{c.get('function') or c.get('file')}" + ("()" if c.get("function") else "")
        lines.append(f"  [{c['severity'].upper():6}] {c['name']} in {where}: {c['text']}")
        if c.get("source"):
            lines.append(f"           {c['source']}")
    for t in rep["transactions"]:
        lines.append(f"  [{t['severity'].upper():6}] {t['text']}")
    for c in rep["contracts"]:
        mark = {"held": "ok", "violated": "VIOLATED", "subject-gone": "gone", "unknown": "UNKNOWN"}.get(
            c["status"], c["status"]
        )
        lines.append(f"  contract {c['contract']}: {mark} — {c['text']}")
    for r in rep.get("revalidation", []):
        lines.append(f"  revalidation {r['name']}: {r['outcome']} ({r['detail']})")
        if r.get("diff"):
            lines.extend("      " + x for x in r["diff"].splitlines()[:20])
    return "\n".join(lines)
