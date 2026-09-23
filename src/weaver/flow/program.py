"""Whole-program view over function summaries: call resolution, closure, may-modify.

``may_modify`` answers: *can a call to F (including everything F transitively
calls) write any object that parameter k may point to?*  It combines

* AST facts: named writes, writes through pointers, external calls, inline
  assembly and indirect calls in every function of the closure;
* points-to evidence (SVF) for the parameter and for each write's pointer; and
* reviewed effect models for functions outside the analysed program.

The answer is ``no`` only when every write in the closure is shown not to reach
a possible target.  Anything that cannot be decided (no points-to facts for a
write, an unmodelled external call, an unresolved indirect call, unknown memory
in a points-to set) yields ``unknown``; an incomplete analysis never produces
``no`` through an apparent absence of aliases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from weaver.flow.evidence import FlowEvidence
from weaver.flow.models import Models


@dataclass
class ModResult:
    status: str  # no | yes | unknown
    reasons: list[dict[str, Any]] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)
    closure: list[str] = field(default_factory=list)
    targets: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "checked": self.checked,
            "closure": self.closure,
            "targets": self.targets,
            "assumptions": self.assumptions,
        }


class Program:
    def __init__(self, inventory: dict[str, Any], models: Models, unit_programs: dict[str, set[str]] | None = None):
        self.inv = inventory
        self.models = models
        self.funcs: dict[str, dict[str, Any]] = inventory.get("functions", {})
        self.by_name: dict[str, list[str]] = {}
        for k, f in self.funcs.items():
            self.by_name.setdefault(f["name"], []).append(k)
        # unit id -> programs linking it; a call resolves only to definitions linked into the same program
        self.unit_programs = unit_programs or {}

    def programs_of(self, key: str) -> set[str]:
        return {p for u in self.funcs.get(key, {}).get("units", []) for p in self.unit_programs.get(u, set())}

    def resolve(self, caller: str, callee: str | None) -> list[str]:
        """Definitions a direct call may reach (static functions only within shared units)."""
        if not callee:
            return []
        cu = set(self.funcs.get(caller, {}).get("units", []))
        cp = self.programs_of(caller)
        out = []
        for k in self.by_name.get(callee, []):
            f = self.funcs[k]
            if f["static"] and not cu & set(f.get("units", [])):
                continue
            if cp and not cp & self.programs_of(k):
                continue  # linked into a different program: this call cannot reach it
            out.append(k)
        return out

    def callers_of(self, key: str) -> list[tuple[str, dict[str, Any]]]:
        name = self.funcs[key]["name"] if key in self.funcs else key.split("::")[-1]
        if not hasattr(self, "_calls_by_callee"):
            self._calls_by_callee: dict[str, list[tuple[str, dict[str, Any]]]] = {}
            for ck, f in self.funcs.items():
                for c in f["calls"]:
                    self._calls_by_callee.setdefault(c["callee"], []).append((ck, c))
        return [(ck, c) for ck, c in self._calls_by_callee.get(name, []) if key in self.resolve(ck, name)]

    def closure(self, key: str, flows: list[FlowEvidence | None]) -> tuple[list[str], list[dict[str, Any]]]:
        """Functions reachable from ``key`` and the reasons the set may be incomplete."""
        seen: list[str] = []
        unknown: list[dict[str, Any]] = []
        stack = [key]
        while stack:
            k = stack.pop()
            if k in seen or k not in self.funcs:
                continue
            seen.append(k)
            f = self.funcs[k]
            for c in f["calls"]:
                if self.models.is_boundary(c["callee"]):
                    continue  # judged by its reviewed model in may_modify
                targets = self.resolve(k, c["callee"])
                stack.extend(t for t in targets if t not in seen)
            for c in f["indirect_calls"]:
                site = c.get("site") or {}
                resolved: set[str] | None = set()
                for fe in flows:
                    t = fe.indirect_targets(site.get("file"), site.get("line"), site.get("col")) if fe else None
                    if t is None or not t:
                        resolved = None
                        break
                    resolved |= set(t)
                if resolved is None:
                    unknown.append(
                        {
                            "kind": "indirect-call",
                            "function": f["name"],
                            "site": site,
                            "detail": "indirect call whose targets are not resolved by points-to evidence",
                        }
                    )
                    continue
                for name in resolved:
                    defs = self.resolve(k, name)
                    if not defs:
                        unknown.append(
                            {
                                "kind": "indirect-call",
                                "function": f["name"],
                                "site": site,
                                "detail": f"indirect target {name}() is not in the analysed program",
                            }
                        )
                    stack.extend(d for d in defs if d not in seen)
        return seen, unknown


def _matches_designator(w: dict[str, Any], designators: list[dict[str, Any]]) -> bool:
    return any(
        d.get("name") == w.get("name")
        and d.get("decl_file") == w.get("decl_file")
        and d.get("decl_line") == w.get("decl_line")
        for d in designators
    )


def _owned_targets(
    model: Any,
    targets: dict[str, set[int] | None],
    usable: dict[str, FlowEvidence],
    designators: list[dict[str, Any]] | None,
) -> list[str] | None:
    """Names of possible targets that are framework-owned (None when the targets are unknown)."""
    out: list[str] = []
    known = False
    for p, fe in usable.items():
        objs = targets.get(p)
        if objs is None:
            continue
        known = True
        for o in objs:
            d = fe.describe(o)
            if model.owned(d.get("file")):
                out.append(str(d.get("name")))
    if not known and designators is not None:
        known = True
        out.extend(str(d.get("name")) for d in designators if model.owned(d.get("decl_file")))
    return sorted(set(out)) if known else None


def may_modify(
    program: Program,
    key: str,
    index: int,
    flows: dict[str, FlowEvidence | None],
    designators: list[dict[str, Any]] | None,
) -> ModResult:
    """Can a call to ``key`` write an object that parameter ``index`` may point to?

    ``flows`` holds one entry per configuration that compiles the function (None
    when no current, complete flow evidence exists for it); ``designators`` are the
    call-site targets when every call site passes ``&object`` (else None).
    """
    fn = program.funcs[key]
    usable = {p: fe for p, fe in flows.items() if fe is not None and fe.complete}
    res = ModResult(status="no")

    def reason(kind: str, status: str, f: dict[str, Any], site: dict[str, Any] | None, detail: str) -> None:
        res.reasons.append(
            {"kind": kind, "status": status, "function": f["name"], "file": f["file"], "site": site, "detail": detail}
        )
        if status == "yes":
            res.status = "yes"
        elif res.status != "yes":
            res.status = "unknown"

    # Targets per configuration.
    targets: dict[str, set[int] | None] = {}
    for p in flows:
        fe = usable.get(p)
        targets[p] = fe.param_targets(fn["name"], index) if fe else None
        if fe is not None and targets[p] is not None:
            res.targets.extend({**fe.describe(o), "profile": p} for o in sorted(targets[p]))
    have_pts = bool(flows) and all(targets[p] is not None for p in flows)

    closure, unknown = program.closure(key, list(usable.values()) if have_pts else [None])
    res.closure = closure
    for u in unknown:
        reason(u["kind"], "unknown", {"name": u["function"], "file": None}, u.get("site"), u["detail"])

    def check_objects(objs_by_p: dict[str, set[int]], f: dict[str, Any], site: Any, what: str) -> None:
        for p, objs in objs_by_p.items():
            tgt = targets.get(p) or set()
            fe = usable[p]
            if fe.unknown_objects(tgt) or fe.unknown_objects(objs):
                reason("unknown-memory", "unknown", f, site, f"{what}: points-to set includes unknown memory ({p})")
                return
            both = tgt & objs
            if both:
                names = ", ".join(str(fe.describe(o).get("name")) for o in sorted(both))
                reason("write", "yes", f, site, f"{what} may write {names} ({p})")
                return

    for k in closure:
        f = program.funcs[k]
        for w in f["named_writes"]:
            if not w.get("global"):
                continue  # automatic locals and parameters of a callee cannot be a caller's object
            res.checked["named_writes"] = res.checked.get("named_writes", 0) + 1
            what = f"writes '{w['name']}' by name"
            if have_pts:
                objs = {
                    p: fe.objects_for_decl(w.get("decl_file"), w.get("decl_line"), w["name"], True)
                    for p, fe in usable.items()
                }
                if any(not v for v in objs.values()):
                    if designators is not None:
                        if _matches_designator(w, designators):
                            reason("write", "yes", f, w.get("site"), what + " (a target of the parameter)")
                        continue
                    reason("unmapped", "unknown", f, w.get("site"), what + "; object not found in points-to evidence")
                    continue
                check_objects(objs, f, w.get("site"), what)
            elif designators is not None:
                if _matches_designator(w, designators):
                    reason("write", "yes", f, w.get("site"), what + " (a target of the parameter)")
            else:
                reason(
                    "no-points-to",
                    "unknown",
                    f,
                    w.get("site"),
                    what + "; the parameter's targets are unknown without points-to evidence",
                )
        for w in f["pointer_writes"]:
            res.checked["pointer_writes"] = res.checked.get("pointer_writes", 0) + 1
            site = w.get("site")
            if not have_pts or not w.get("lc") or w.get("unknown_base"):
                reason("no-points-to", "unknown", f, site, "write through a pointer; no points-to evidence for it")
                continue
            objs: dict[str, set[int]] = {}
            for p, fe in usable.items():
                pts, n = fe.pts_in_span(w["lvalue"]["file"] if w.get("lvalue") else f["file"], w["lc"])
                if n == 0:
                    reason(
                        "unmapped", "unknown", f, site, f"write through a pointer has no mapped points-to facts ({p})"
                    )
                    objs = {}
                    break
                objs[p] = pts
            if objs:
                check_objects(objs, f, site, "write through a pointer")
        for c in f["calls"]:
            boundary = program.models.is_boundary(c["callee"])
            if program.resolve(k, c["callee"]) and not boundary:
                continue
            res.checked["boundary_calls" if boundary else "external_calls"] = (
                res.checked.get("boundary_calls" if boundary else "external_calls", 0) + 1
            )
            model = program.models.lookup(c["callee"])
            site = c.get("site")
            if model is None:
                reason("external", "unknown", f, site, f"calls {c['callee']}(), which has no reviewed effect model")
                continue
            res.assumptions.extend(f"{c['callee']}(): {a}" for a in model.assumptions)
            if model.calls_back:
                reason("external", "unknown", f, site, f"{c['callee']}() may run program callbacks")
                continue
            if model.writes == "any":
                reason("external", "unknown", f, site, f"{c['callee']}() may write any object")
                continue
            if model.writes_owned:
                owned = _owned_targets(model, targets, usable, designators)
                if owned is None:
                    reason(
                        "external",
                        "unknown",
                        f,
                        site,
                        f"{c['callee']}() writes framework-owned state; the parameter's targets are unknown",
                    )
                    continue
                if owned:
                    reason("write", "yes", f, site, f"{c['callee']}() may write framework-owned {', '.join(owned)}")
                    continue
            for i in model.writes:
                if i >= len(c["args"]):
                    continue
                a = c["args"][i]
                what = f"{c['callee']}() writes through argument {i + 1}"
                if a.get("addr_of"):
                    d = a["addr_of"]
                    fake = {"name": d.get("name"), "decl_file": d.get("decl_file"), "decl_line": d.get("decl_line")}
                    if have_pts:
                        objs = {
                            p: fe.objects_for_decl(
                                d.get("decl_file"), d.get("decl_line"), d.get("name"), bool(d.get("global_"))
                            )
                            for p, fe in usable.items()
                        }
                        if all(objs.values()):
                            check_objects(objs, f, site, what)
                            continue
                    if designators is not None and _matches_designator(fake, designators):
                        reason("write", "yes", f, site, what + f" (&{d.get('name')})")
                    elif designators is None and not d.get("global_", True):
                        pass  # a callee's own local cannot be the caller's object
                    elif designators is None:
                        reason("no-points-to", "unknown", f, site, what + "; parameter targets unknown")
                    continue
                if not have_pts or not a.get("lc"):
                    reason("no-points-to", "unknown", f, site, what + "; no points-to evidence")
                    continue
                objs = {}
                for p, fe in usable.items():
                    pts, n = fe.pts_in_span(f["file"], a["lc"])
                    if n == 0:
                        reason("unmapped", "unknown", f, site, what + f"; argument not mapped ({p})")
                        objs = {}
                        break
                    objs[p] = pts
                if objs:
                    check_objects(objs, f, site, what)
        for a in f["asm"]:
            reason("asm", "unknown", f, a, "inline assembly may access memory")
    if not flows:
        res.assumptions.append("no configuration compiles this function")
    elif not have_pts:
        missing = [p for p in flows if targets.get(p) is None]
        res.assumptions.append(f"no current, complete points-to evidence for: {', '.join(missing)}")
    return res
