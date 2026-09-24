"""Transaction ledger: one reviewable transaction at a time (pointer-tracker plan §6).

States: discovered -> analyzed -> blocked | proposed -> validated | provisional
| rejected -> accepted | skipped -> reverted.  Every transition is appended to
``ledger.jsonl``; each transaction's full record (candidate card, patch,
validation results bound to the patch and source hashes, acceptance
checkpoint) lives in ``transactions/<id>.json``.

Acceptance writes the patch only if the sources still match the validated
revision, keeps a checkpoint of the pre-image, and never touches unrelated
files.  Reverting restores the pre-image when the file is unchanged since
acceptance, or three-way merges the inverse change when there were later
unrelated edits, refusing on conflict.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from weaver import SCHEMA_VERSION
from weaver.analysis.inventory import find_finding, load_inventory
from weaver.config import Project
from weaver.errors import StaleEvidenceError, WeaverError
from weaver.evidence import TxnState
from weaver.recipes import CATALOG, RecipeContext, recipes_for_finding
from weaver.rewrite import Edit, apply_edits, unified_diff
from weaver.store import Store
from weaver.toolchain.collect import _source_revision
from weaver.util import (
    append_jsonl,
    atomic_write_bytes,
    now_iso,
    read_json,
    read_jsonl,
    run,
    sha256_bytes,
    sha256_file,
    short_hash,
    write_json,
)

OPEN_STATES = {TxnState.PROPOSED.value, TxnState.VALIDATED.value, TxnState.PROVISIONAL.value}
FINDING_KEYS = (
    "kind", "name", "function", "file", "line", "col", "type", "use_summary", "evidence_status", "occurrences",
)  # fmt: skip


class Ledger:
    def __init__(self, project: Project):
        self.project = project
        self.store = Store(project.state_dir)

    # -- persistence ----------------------------------------------------
    def load(self, txn_id: str) -> dict[str, Any]:
        p = self.store.txn_path(txn_id)
        if not p.exists():
            matches = sorted(q.stem for q in self.store.txn_dir().glob(f"{txn_id}*.json"))
            if len(matches) != 1:
                raise WeaverError(f"no unique transaction {txn_id!r}" + (f": {matches}" if matches else ""))
            p = self.store.txn_path(matches[0])
        return read_json(p)

    def save(self, txn: dict[str, Any]) -> None:
        write_json(self.store.txn_path(txn["id"]), txn)

    def transition(self, txn: dict[str, Any], state: TxnState | str, note: str = "") -> None:
        state = TxnState(state).value
        txn["state"] = state
        txn.setdefault("history", []).append({"state": state, "at": now_iso(), "note": note})
        self.save(txn)
        append_jsonl(
            self.store.ledger_path,
            {"txn": txn["id"], "finding": txn["finding_id"], "state": state, "at": now_iso(), "note": note},
        )

    def all(self) -> list[dict[str, Any]]:
        return sorted(
            (read_json(p) for p in self.store.txn_dir().glob("*.json")), key=lambda t: (t["created_at"], t["id"])
        )

    def events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.store.ledger_path)

    # -- operations ---------------------------------------------------------
    def propose(self, finding_id: str, recipe_id: str | None = None) -> dict[str, Any]:
        inv = load_inventory(self.project)
        finding = find_finding(inv, finding_id)
        candidates = [CATALOG[recipe_id]] if recipe_id else recipes_for_finding(finding)
        if not candidates:
            raise WeaverError(
                f"no recipe in the catalog applies to a {finding['kind']} finding (available: {', '.join(CATALOG)})"
            )
        recipe = candidates[0]
        if not recipe.applicable(finding):
            raise WeaverError(f"recipe {recipe.id} does not apply to a {finding['kind']} finding")
        ctx = RecipeContext(self.project, inv)
        result = recipe.evaluate(ctx, finding)
        cand = result.to_json()
        txn_id = "T-" + short_hash(finding["id"], recipe.id, cand["file_hashes"], time.time_ns(), length=8)
        txn: dict[str, Any] = {
            "schema": f"weaver.txn/{SCHEMA_VERSION}",
            "id": txn_id,
            "created_at": now_iso(),
            "finding_id": finding["id"],
            "finding": {k: finding.get(k) for k in FINDING_KEYS},
            "recipe": recipe.id,
            "recipe_version": recipe.version,
            "candidate": cand,
            "source_revision": _source_revision(self.project.root),
            "preservation_contract": self.project.preservation,
            "clite_model": self.project.clite,
            "history": [],
        }
        self.transition(txn, TxnState.DISCOVERED, "finding selected")
        self.transition(txn, TxnState.ANALYZED, f"recipe {recipe.id} v{recipe.version} evaluated")
        return self._open(txn)

    def propose_patch(
        self,
        diff_text: str | bytes,
        removes: list[str] | None = None,
        title: str = "",
        origin: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open a transaction for a hand-written or AI-drafted unified diff (``weaver.patch``)."""
        from weaver.patch import RECIPE, VERSION, candidate_from_patch, origin_label

        inv = load_inventory(self.project)
        origin = origin or {"kind": "manual"}
        cand = candidate_from_patch(self.project, inv, diff_text, list(removes or []), title, origin)
        targets = cand["recheck"]["targets"]
        if targets:
            f = find_finding(inv, targets[0])
            finding = {k: f.get(k) for k in FINDING_KEYS}
        else:
            first = sorted(cand["first_line"].items())[:1]
            finding = {
                "kind": "patch",
                "name": title or "change",
                "function": None,
                "file": first[0][0] if first else None,
                "line": first[0][1] if first else None,
            }
        txn_id = "T-" + short_hash("patch", cand["file_hashes"], title, time.time_ns(), length=8)
        txn: dict[str, Any] = {
            "schema": f"weaver.txn/{SCHEMA_VERSION}",
            "id": txn_id,
            "created_at": now_iso(),
            "finding_id": cand["finding_id"],
            "finding": finding,
            "recipe": RECIPE,
            "recipe_version": VERSION,
            "title": title,
            "origin": origin,
            "candidate": cand,
            "source_revision": _source_revision(self.project.root),
            "preservation_contract": self.project.preservation,
            "clite_model": self.project.clite,
            "history": [],
        }
        self.transition(txn, TxnState.DISCOVERED, f"{origin_label(origin)} submitted")
        self.transition(txn, TxnState.ANALYZED, f"{len(cand['edits'])} edit(s) in {len(cand['file_hashes'])} file(s)")
        return self._open(txn)

    def _open(self, txn: dict[str, Any]) -> dict[str, Any]:
        """Blocked, or proposed with its patch rendered from the edits."""
        cand = txn["candidate"]
        if not cand["eligible"]:
            blockers = [p for p in cand["preconditions"] if p["status"] != "established"]
            self.transition(
                txn, TxnState.BLOCKED, "; ".join(f"{b['id']} {b['status']}" for b in blockers) or "no edits"
            )
            return txn
        edits = [Edit.from_json(e) for e in cand["edits"]]
        changes = apply_edits(self.project.root, edits, cand["file_hashes"])
        diff = unified_diff(changes)
        txn["patch"] = {
            "diff": diff,
            "sha256": sha256_bytes((diff + json.dumps(cand["edits"], sort_keys=True)).encode()),
            "post_hashes": {f: sha256_bytes(new) for f, (_, new) in changes.items()},
        }
        self.transition(txn, TxnState.PROPOSED, f"{len(edits)} edit(s) in {len(changes)} file(s)")
        return txn

    def validate(self, txn_id: str, keep: bool = False) -> dict[str, Any]:
        from weaver.validate import judge, run_validation, validation_strength

        txn = self.load(txn_id)
        if txn["state"] not in OPEN_STATES | {TxnState.REJECTED.value}:
            raise WeaverError(f"{txn['id']} is {txn['state']}; only proposed transactions can be validated")
        try:
            val = run_validation(self.project, txn, keep=keep)
        except StaleEvidenceError as e:
            txn["validation"] = {"at": now_iso(), "error": str(e)}
            self.transition(txn, TxnState.BLOCKED, f"stale: {e}")
            return txn
        state, reasons = judge(val["records"], self.project.acceptance.require)
        val["judgement"] = {"state": state, "reasons": reasons, "policy": self.project.acceptance.require}
        val["strength"] = validation_strength(val["records"])
        txn["validation"] = val
        self.transition(
            txn,
            state,
            "; ".join(reasons)
            if reasons
            else f"{sum(r['outcome'] == 'passed' for r in val['records'])} check(s) passed",
        )
        return txn

    def accept(self, txn_id: str) -> dict[str, Any]:
        txn = self.load(txn_id)
        pol = self.project.acceptance
        allowed = {TxnState.VALIDATED.value} | ({TxnState.PROVISIONAL.value} if pol.allow_provisional else set())
        if txn["state"] not in allowed:
            raise WeaverError(f"{txn['id']} is {txn['state']}; acceptance policy allows {sorted(allowed)}")
        val = txn.get("validation") or {}
        if val.get("patch_sha256") != txn["patch"]["sha256"]:
            raise StaleEvidenceError("validation results are not bound to this patch; re-validate")
        cand = txn["candidate"]
        for f, h in cand["file_hashes"].items():
            if sha256_file(self.project.root / f) != h:
                raise StaleEvidenceError(f"{f} changed since validation; re-run collect/inventory and propose again")
        edits = [Edit.from_json(e) for e in cand["edits"]]
        changes = apply_edits(self.project.root, edits, cand["file_hashes"])
        ckpt = self.store.checkpoint_dir(txn["id"])
        for f, (old, new) in changes.items():
            (ckpt / "pre").mkdir(parents=True, exist_ok=True)
            (ckpt / "post").mkdir(parents=True, exist_ok=True)
            safe = f.replace("/", "__")
            (ckpt / "pre" / safe).write_bytes(old)
            (ckpt / "post" / safe).write_bytes(new)
        for f, (_, new) in changes.items():
            atomic_write_bytes(self.project.root / f, new)
        txn["acceptance"] = {
            "at": now_iso(),
            "pre_hashes": cand["file_hashes"],
            "post_hashes": {f: sha256_bytes(new) for f, (_, new) in changes.items()},
            "checkpoint": str(ckpt),
            "policy": {"require": pol.require, "allow_provisional": pol.allow_provisional},
            "strength": val.get("strength", "compile-only"),
        }
        note = "patch applied; affected evidence is now stale and must be re-collected before the next selection"
        if txn["acceptance"]["strength"] != "behavioural":
            note += "; accepted without running the program (no test or differential run passed)"
        self.transition(txn, TxnState.ACCEPTED, note)
        return txn

    def skip(self, txn_id: str, reason: str = "") -> dict[str, Any]:
        txn = self.load(txn_id)
        if txn["state"] in (TxnState.ACCEPTED.value, TxnState.REVERTED.value):
            raise WeaverError(f"{txn['id']} is {txn['state']}; use revert instead")
        self.transition(txn, TxnState.SKIPPED, reason or "skipped by user")
        return txn

    def revert(self, txn_id: str) -> dict[str, Any]:
        txn = self.load(txn_id)
        if txn["state"] != TxnState.ACCEPTED.value:
            raise WeaverError(f"{txn['id']} is {txn['state']}; only accepted transactions can be reverted")
        acc = txn["acceptance"]
        ckpt = Path(acc["checkpoint"])
        plans: dict[str, bytes] = {}
        for f, post_h in acc["post_hashes"].items():
            safe = f.replace("/", "__")
            pre = (ckpt / "pre" / safe).read_bytes()
            post = (ckpt / "post" / safe).read_bytes()
            cur_path = self.project.root / f
            cur = cur_path.read_bytes()
            if sha256_bytes(cur) == post_h:
                plans[f] = pre
                continue
            merged = _merge_inverse(cur, post, pre, ckpt)
            if merged is None:
                raise WeaverError(
                    f"{f} changed since acceptance and the inverse patch conflicts; "
                    "revert manually from the checkpoint in " + str(ckpt)
                )
            plans[f] = merged
        for f, data in plans.items():
            atomic_write_bytes(self.project.root / f, data)
        txn["reversion"] = {"at": now_iso(), "files": sorted(plans)}
        self.transition(txn, TxnState.REVERTED, "pre-image restored; re-collect affected evidence")
        return txn


def _merge_inverse(current: bytes, post: bytes, pre: bytes, scratch: Path) -> bytes | None:
    """Three-way merge: apply (post -> pre) onto current.  None on conflict."""
    if shutil.which("git") is None:
        return None
    d = scratch / "merge"
    d.mkdir(parents=True, exist_ok=True)
    (d / "current").write_bytes(current)
    (d / "base").write_bytes(post)
    (d / "other").write_bytes(pre)
    r = run(["git", "merge-file", "-p", str(d / "current"), str(d / "base"), str(d / "other")], timeout=60)
    shutil.rmtree(d, ignore_errors=True)
    return r.stdout if r.returncode == 0 else None
