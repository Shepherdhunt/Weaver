"""Queries over one profile's flow evidence (``flow.json``)."""

from __future__ import annotations

from typing import Any

from weaver.config import Project
from weaver.store import Store
from weaver.util import read_json

UNKNOWN_KINDS = {"dummy", "other", "field-or-internal"}


class FlowEvidence:
    def __init__(self, data: dict[str, Any], run: dict[str, Any]):
        self.data = data
        self.run = run
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.objects = {o["id"]: o for o in data["objects"]}
        self.by_loc: dict[tuple[str, int, int], list[int]] = {}
        self.by_line: dict[tuple[str, int], list[int]] = {}
        self.args: dict[tuple[str, int], list[int]] = {}
        for n in data["nodes"]:
            loc = n.get("loc") or {}
            if loc.get("kind") == "inst" and loc.get("file"):
                self.by_loc.setdefault((loc["file"], loc["line"], loc["col"]), []).append(n["id"])
                self.by_line.setdefault((loc["file"], loc["line"]), []).append(n["id"])
            elif loc.get("kind") == "arg":
                self.args.setdefault((loc.get("function"), loc.get("arg_index")), []).append(n["id"])
        self.decl_objects: dict[tuple[str | None, int | None, str | None], list[int]] = {}
        for o in data["objects"]:
            loc = o.get("loc") or {}
            self.decl_objects.setdefault((loc.get("file"), loc.get("line"), o.get("name")), []).append(o["id"])
        self.indirect = {(c["file"], c["line"], c["col"]): c for c in data.get("indirect_calls", [])}

    # -- status ----------------------------------------------------------
    @property
    def complete(self) -> bool:
        return self.run.get("status") == "complete"

    @property
    def evidence_status(self) -> str:
        return self.run.get("evidence_status", "unsupported")

    def provenance(self) -> dict[str, Any]:
        svf = self.run.get("svf") or {}
        return {
            "profile": self.run.get("profile"),
            "status": self.run.get("status"),
            "evidence_status": self.evidence_status,
            "svf": svf.get("source"),
            "wpa_sha256": svf.get("wpa_sha256"),
            "options": (self.run.get("job") or {}).get("argv", [])[1:-1],
            "program_sha256": (self.run.get("program") or {}).get("sha256"),
            "fact_kind": "pointer-analysis",
        }

    # -- queries ---------------------------------------------------------
    def pts(self, ids: list[int]) -> set[int]:
        out: set[int] = set()
        for i in ids:
            out.update(self.nodes.get(i, {}).get("pts", []))
        return out

    def param_targets(self, function: str, index: int) -> set[int] | None:
        ids = self.args.get((function, index))
        return self.pts(ids) if ids else None

    def pts_in_span(self, file: str, lc: list[int] | tuple[int, int, int, int]) -> tuple[set[int], int]:
        """Union of points-to sets of instruction nodes located inside a source span."""
        l0, c0, l1, c1 = lc
        ids: list[int] = []
        for line in range(l0, l1 + 1):
            for nid in self.by_line.get((file, line), []):
                loc = self.nodes[nid]["loc"]
                if (line, loc.get("col") or 0) < (l0, c0) or (line, loc.get("col") or 0) > (l1, c1):
                    continue
                ids.append(nid)
        return self.pts(ids), len(ids)

    def objects_for_decl(self, file: str | None, line: int | None, name: str | None) -> set[int]:
        return set(self.decl_objects.get((file, line, name), []))

    def indirect_targets(self, file: str | None, line: int | None, col: int | None) -> list[str] | None:
        c = self.indirect.get((file, line, col))
        return c["targets"] if c else None

    def unknown_objects(self, objs: set[int]) -> set[int]:
        return {o for o in objs if self.objects.get(o, {}).get("kind") in UNKNOWN_KINDS or o not in self.objects}

    def describe(self, oid: int) -> dict[str, Any]:
        o = self.objects.get(oid)
        if o is None:
            return {"id": oid, "kind": "unknown"}
        loc = o.get("loc") or {}
        return {
            "id": oid,
            "name": o.get("name"),
            "kind": o.get("kind"),
            "file": loc.get("file"),
            "line": loc.get("line"),
        }


def load_flow(project: Project, profile_id: str, inventory: dict[str, Any] | None = None) -> FlowEvidence | None:
    """Load a profile's flow evidence if it exists and describes the current sources."""
    d = Store(project.state_dir).root / "flow" / profile_id
    if not (d / "flow.json").exists() or not (d / "run.json").exists():
        return None
    run = read_json(d / "run.json")
    if inventory is not None:
        current = {u["unit_id"]: u["file_sha256"] for u in inventory["units"] if u["profile"] == profile_id}
        for i in run.get("inputs", []):
            if current.get(i["unit"]) != i["file_sha256"]:
                return None  # stale: evidence no longer describes the sources
        if (
            set(current)
            - {i["unit"] for i in run.get("inputs", [])}
            - {m.get("unit") for m in run.get("missing_units", [])}
        ):
            # new units since the run: treat as incomplete rather than silently partial
            run = {**run, "status": "incomplete", "reason": "units added since the flow run"}
    return FlowEvidence(read_json(d / "flow.json"), run)


def flow_status(project: Project, inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {}
    for p in project.profiles:
        d = Store(project.state_dir).root / "flow" / p.id
        run = read_json(d / "run.json") if (d / "run.json").exists() else None
        fe = load_flow(project, p.id, inventory)
        out[p.id] = {
            "run": {k: run.get(k) for k in ("status", "reason", "evidence_status", "started_at", "duration_s")}
            if run
            else None,
            "current": fe is not None,
            "complete": bool(fe and fe.complete),
        }
    return out
