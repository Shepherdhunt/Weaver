"""Borrowed-pointer contract: the target is only read, and no copy of the pointer outlives the borrow.

A *borrowed* pointer refers to an object someone else owns for a limited time.
cFS's software-bus buffers are the model case: ``CFE_SB_ReceiveBuffer`` hands the
application a pointer that must be treated as read-only and is valid only until
the next receive on the same pipe (``cfe_sb.h``).  Code that honours the borrow

* never writes through the pointer or anything derived from it, and
* never stores the pointer (or a derived pointer) anywhere that outlives the
  handling of this message: no global, heap or struct field, no return value.

``check_borrow`` follows the pointer through the analysed program: casts,
``&p->field`` and copies into locals are followed to where the value goes;
arguments to analysed functions are followed into the callee's parameter;
arguments to modelled functions are judged by the reviewed model (``writes``
and ``retains``); anything else is unknown.  Only syntactic evidence and
reviewed models are used, so an alias created through memory would escape the
check - such a store is itself reported as retention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WRITE_ACCESS = {"write", "readwrite"}
READ_ACCESS = {"read", "member-read", "decay", "member-decay", "unevaluated", "member-unevaluated"}


@dataclass
class BorrowResult:
    status: str = "held"  # held | violated | unknown
    reasons: list[dict[str, Any]] = field(default_factory=list)
    visited: list[dict[str, Any]] = field(default_factory=list)  # (function, name, file, line) of each pointer followed

    def add(self, status: str, where: dict[str, Any], text: str) -> None:
        self.reasons.append({"status": status, "text": text, **where})
        if status == "violated":
            self.status = "violated"
        elif self.status == "held":
            self.status = "unknown"

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": self.reasons, "visited": self.visited}


def check_borrow(inventory: dict[str, Any], program: Any, finding: dict[str, Any]) -> BorrowResult:
    return _Walk(inventory, program).run(finding)


class _Walk:
    """Follows one borrowed pointer through the analysed program."""

    def __init__(self, inventory: dict[str, Any], program: Any):
        self.program = program
        self.res = BorrowResult()
        self.work: list[dict[str, Any]] = []
        # parameters by (function, index); pointer variables of each function (its locals and its own
        # parameters) by (file, function, name); names of globals
        self.params: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.vars: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
        self.globals: set[str] = set()
        for f in inventory["findings"]:
            kind = f.get("kind")
            if kind == "parameter":
                self.params.setdefault((f.get("function"), f.get("param_index")), []).append(f)
            if kind in ("parameter", "local"):
                self.vars[(f.get("file"), f.get("function"), f.get("name"))] = f
            elif kind in ("global", "static-global", "extern-decl"):
                self.globals.add(str(f.get("name")))

    def run(self, finding: dict[str, Any]) -> BorrowResult:
        seen: set[str] = set()
        self.work = [finding]
        while self.work:
            f = self.work.pop()
            if f["id"] in seen:
                continue
            seen.add(f["id"])
            self.res.visited.append({k: f.get(k) for k in ("id", "function", "name", "file", "line", "kind")})
            for u in f.get("uses", []):
                where = {
                    "function": f.get("function"),
                    "file": f.get("file"),
                    "line": u.get("line"),
                    "name": f.get("name"),
                }
                self._use(u, f, where)
        return self.res

    def _use(self, u: dict[str, Any], f: dict[str, Any], where: dict[str, Any]) -> None:
        res = self.res
        k, acc, d = u["kind"], u.get("access"), u.get("detail") or {}
        if k in ("deref", "arrow", "subscript"):
            if acc in WRITE_ACCESS or (acc or "").removeprefix("member-") in WRITE_ACCESS:
                res.add("violated", where, f"line {u['line']}: writes through '{f.get('name')}'")
            elif acc == "address" or (acc or "").endswith("address"):
                self._sink(d.get("sink") or {}, f, where, u)
            elif acc not in READ_ACCESS and not (acc or "").startswith("member-"):
                res.add("unknown", where, f"line {u['line']}: {k} with access '{acc}'")
            return
        if k in ("null-test", "compare", "unevaluated"):
            return
        if k == "cast":
            self._sink(d.get("sink") or {}, f, where, u)
        elif k == "call-arg":
            self._sink({"kind": "call-arg", "callee": d.get("callee"), "arg": d.get("arg")}, f, where, u)
        elif k == "copy":
            self._sink({"kind": "copy", "into": d.get("into") or {}}, f, where, u)
        elif k == "return":
            res.add("violated", where, f"line {u['line']}: returned; the caller may keep it")
        elif k in ("address-of-pointer", "reassign"):
            # &p handed to a producer that stores a pointer into p (CFE_SB_ReceiveBuffer), or a new
            # value for the variable: neither affects the borrow of the value being followed
            return
        else:
            res.add("unknown", where, f"line {u['line']}: {k} use")

    def _sink(self, s: dict[str, Any], f: dict[str, Any], where: dict[str, Any], u: dict[str, Any]) -> None:
        res = self.res
        kind = s.get("kind")
        line = u.get("line")
        if kind == "compare":
            return
        if kind in ("deref", "arrow", "subscript"):
            if (s.get("access") or "").removeprefix("member-") in WRITE_ACCESS:
                res.add("violated", where, f"line {line}: writes through a pointer derived from '{f.get('name')}'")
            return
        if kind == "return":
            res.add("violated", where, f"line {line}: a derived pointer is returned")
            return
        if kind == "copy":
            into = s.get("into") or {}
            name = into.get("name")
            if into.get("target") == "variable":
                var = self.vars.get((f.get("file"), f.get("function"), name))
                if var is not None:
                    self.work.append(var)
                elif name in self.globals:
                    res.add("violated", where, f"line {line}: stored into global '{name}'; it outlives the borrow")
                else:
                    res.add("unknown", where, f"line {line}: copied into '{name}', which is not tracked")
                return
            res.add(
                "violated",
                where,
                f"line {line}: stored into {into.get('target') or 'memory'}; the pointer may outlive the borrow",
            )
            return
        if kind == "call-arg":
            self._call(s.get("callee"), s.get("arg"), f, where, line)
            return
        if kind == "indirect-call":
            res.add("unknown", where, f"line {line}: used as a function pointer")
            return
        res.add("unknown", where, f"line {line}: a derived pointer is used as {s.get('parent') or kind}")

    def _call(self, callee: str | None, arg: int | None, f: dict[str, Any], where: dict[str, Any], line: Any) -> None:
        res, program = self.res, self.program
        if callee is None or arg is None:
            res.add("unknown", where, f"line {line}: passed to an indirect call")
            return
        key = f"{f.get('file')}::{f.get('function')}"
        defs = [] if program.models.is_boundary(callee) else program.resolve(key, callee)
        if defs:
            targets = self.params.get((callee, arg), [])
            if not targets:
                res.add(
                    "unknown",
                    where,
                    f"line {line}: passed to {callee}() argument {arg + 1}, which is not an analysed pointer parameter",
                )
            self.work.extend(targets)
            return
        model = program.models.lookup(callee)
        if model is None:
            res.add("unknown", where, f"line {line}: passed to {callee}(), which has no reviewed effect model")
        elif model.writes == "any" or arg in (model.writes or []):
            res.add("violated", where, f"line {line}: {callee}() writes through argument {arg + 1}")
        elif arg in (model.retains or []):
            res.add("violated", where, f"line {line}: {callee}() keeps argument {arg + 1} after returning")
        elif model.calls_back:
            res.add("unknown", where, f"line {line}: {callee}() may run program callbacks")
