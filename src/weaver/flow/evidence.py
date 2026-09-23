"""Queries over flow evidence: one ``flow.json`` per program (see ``weaver.link``)."""

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
        self.globals_by_name: dict[str, list[dict[str, Any]]] = {}
        for o in data["objects"]:
            loc = o.get("loc") or {}
            self.decl_objects.setdefault((loc.get("file"), loc.get("line"), o.get("name")), []).append(o["id"])
            if o.get("kind") == "global" and o.get("name"):
                self.globals_by_name.setdefault(o["name"], []).append(o)
        self.indirect = {(c["file"], c["line"], c["col"]): c for c in data.get("indirect_calls", [])}

    # -- status ----------------------------------------------------------
    @property
    def complete(self) -> bool:
        """Complete and closed: every caller of every function is inside the analysed program."""
        return self.run.get("status") == "complete" and self.run.get("closed", True)

    @property
    def program(self) -> str | None:
        return self.run.get("program")

    @property
    def evidence_status(self) -> str:
        return self.run.get("evidence_status", "unsupported")

    def provenance(self) -> dict[str, Any]:
        svf = self.run.get("svf") or {}
        return {
            "profile": self.run.get("profile"),
            "program": self.run.get("program"),
            "closed": self.run.get("closed", True),
            "status": self.run.get("status"),
            "evidence_status": self.evidence_status,
            "svf": svf.get("source"),
            "wpa_sha256": svf.get("wpa_sha256"),
            "options": (self.run.get("job") or {}).get("argv", [])[1:-1],
            "program_sha256": (self.run.get("program_bc") or {}).get("sha256"),
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

    def objects_for_decl(self, file: str | None, line: int | None, name: str | None, global_: bool = False) -> set[int]:
        """Objects declared at a source position; for globals, also by name.

        A write names a global through the declaration visible in its unit (often
        an ``extern`` in a header), while SVF places the object at its definition.
        Within one program a global with external linkage has one definition, so
        a unique global object of that name is it; several (file-scope statics
        of the same name) are narrowed by file, and otherwise all are returned.
        """
        hit = set(self.decl_objects.get((file, line, name), []))
        if hit or not global_ or not name:
            return hit
        cands = self.globals_by_name.get(name, [])
        if len(cands) > 1:
            same = [o for o in cands if (o.get("loc") or {}).get("file") == file]
            cands = same or cands
        return {o["id"] for o in cands}

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


def _program_dirs(project: Project, profile_id: str) -> dict[str, Any]:
    d = Store(project.state_dir).root / "flow" / profile_id / "programs"
    if not d.is_dir():
        return {}
    return {p.name: p for p in sorted(d.iterdir()) if (p / "run.json").exists()}


def _load_program(d: Any, inventory: dict[str, Any] | None, profile_id: str) -> FlowEvidence | None:
    if not (d / "flow.json").exists():
        return None
    run = read_json(d / "run.json")
    if inventory is not None:
        current = {u["unit_id"]: u["file_sha256"] for u in inventory["units"] if u["profile"] == profile_id}
        for i in run.get("inputs", []):
            if current.get(i["unit"]) != i["file_sha256"]:
                return None  # stale: evidence no longer describes the sources
    return FlowEvidence(read_json(d / "flow.json"), run)


def load_flows(
    project: Project, profile_id: str, inventory: dict[str, Any] | None = None
) -> dict[str, FlowEvidence | None]:
    """Program name -> its current flow evidence (None when absent, failed or stale)."""
    return {name: _load_program(d, inventory, profile_id) for name, d in _program_dirs(project, profile_id).items()}


def load_flow(
    project: Project, profile_id: str, inventory: dict[str, Any] | None = None, program: str | None = None
) -> FlowEvidence | None:
    """One program's flow evidence; without ``program``, the profile's only program."""
    dirs = _program_dirs(project, profile_id)
    if program is None:
        if len(dirs) != 1:
            return None
        program = next(iter(dirs))
    from weaver.flow.svf import _safe

    d = dirs.get(program) or dirs.get(_safe(program))
    return _load_program(d, inventory, profile_id) if d is not None else None


def flow_status(project: Project, inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {}
    for p in project.profiles:
        d = Store(project.state_dir).root / "flow" / p.id
        run = read_json(d / "run.json") if (d / "run.json").exists() else None
        flows = load_flows(project, p.id, inventory)
        progs = (run or {}).get("programs") or {}
        out[p.id] = {
            "run": {k: run.get(k) for k in ("status", "reason", "started_at", "duration_s")} if run else None,
            "programs": {
                n: {**v, "current": flows.get(n) is not None or flows.get(_safe_name(n)) is not None}
                for n, v in progs.items()
            },
            "current": bool(flows) and all(fe is not None for fe in flows.values()),
            "complete": bool(flows)
            and all(fe is not None and fe.run.get("status") == "complete" for fe in flows.values()),
            "gcc": _gcc_status(project, p.id, inventory),
        }
    return out


def _gcc_status(project: Project, profile_id: str, inventory: dict[str, Any] | None) -> dict[str, Any] | None:
    """The GCC points-to job's state for a profile: run status and how many image solutions are current."""
    from weaver.flow.gcc_pta import load_gcc_pta

    f = Store(project.state_dir).root / "flow" / profile_id / "gcc" / "run.json"
    if not f.exists():
        return None
    run = read_json(f)
    images = sum(len(p.get("images") or {}) for p in (run.get("programs") or {}).values())
    current = load_gcc_pta(project, profile_id, inventory=inventory) if run.get("status") != "unavailable" else {}
    return {
        "status": run.get("status"),
        "reason": run.get("reason"),
        "images": images,
        "current": len(current),
        "complete": run.get("status") == "complete" and images > 0 and len(current) == images,
    }


def _safe_name(name: str) -> str:
    from weaver.flow.svf import _safe

    return _safe(name)
