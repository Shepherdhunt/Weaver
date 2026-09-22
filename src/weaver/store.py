"""Layout of the state directory (``.weaver`` by default)."""

from __future__ import annotations

from pathlib import Path


class Store:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir)

    def _d(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # evidence -----------------------------------------------------------
    def unit_dir(self, profile: str, unit_id: str) -> Path:
        return self._d("evidence", profile, unit_id)

    def evidence_root(self, profile: str) -> Path:
        return self._d("evidence", profile)

    def profile_dir(self, profile: str) -> Path:
        return self._d("profiles", profile)

    def capture_dir(self) -> Path:
        return self._d("capture")

    def probe_dir(self, profile: str) -> Path:
        return self._d("probes", profile)

    def fidelity_dir(self, profile: str) -> Path:
        return self._d("fidelity", profile)

    # analysis -----------------------------------------------------------
    def analysis_dir(self) -> Path:
        return self._d("analysis")

    @property
    def inventory_path(self) -> Path:
        return self.analysis_dir() / "inventory.json"

    # transactions ---------------------------------------------------------
    def txn_dir(self) -> Path:
        return self._d("transactions")

    def txn_path(self, txn_id: str) -> Path:
        return self.txn_dir() / f"{txn_id}.json"

    @property
    def ledger_path(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / "ledger.jsonl"

    def checkpoint_dir(self, txn_id: str) -> Path:
        return self._d("checkpoints", txn_id)

    def workspace_dir(self, txn_id: str) -> Path:
        return self._d("workspaces", txn_id)
