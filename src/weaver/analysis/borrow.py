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
    res = BorrowResult()
    params: dict[tuple[str, int], list[dict[str, Any]]] = {}
    locals_: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
    for f in inventory["findings"]:
        if f.get("kind") == "parameter":
            params.setdefault((f.get("function"), f.get("param_index")), []).append(f)
        elif f.get("kind") == "local":
            locals_[(f.get("file"), f.get("function"), f.get("name"))] = f
    seen: set[str] = set()
    work = [finding]
    while work:
        f = work.pop()
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        res.visited.append({k: f.get(k) for k in ("id", "function", "name", "file", "line", "kind")})
        for u in f.get("uses", []):
            where = {"function": f.get("function"), "file": f.get("file"), "line": u.get("line"), "name": f.get("name")}
            _use(u, f, where, res, program, params, locals_, work)
    return res


def _use(
    u: dict[str, Any],
    f: dict[str, Any],
    where: dict[str, Any],
    res: BorrowResult,
    program: Any,
    params: dict[tuple[str, int], list[dict[str, Any]]],
    locals_: dict[tuple[str | None, str | None, str | None], dict[str, Any]],
    work: list[dict[str, Any]],
) -> None:
    k, acc, d = u["kind"], u.get("access"), u.get("detail") or {}
    if k in ("deref", "arrow", "subscript"):
        if acc in WRITE_ACCESS or (acc or "").removeprefix("member-") in WRITE_ACCESS:
            res.add("violated", where, f"line {u['line']}: writes through '{f.get('name')}'")
        elif acc == "address" or (acc or "").endswith("address"):
            _sink(d.get("sink") or {}, f, where, res, program, params, locals_, work, u)
        elif acc not in READ_ACCESS and not (acc or "").startswith("member-"):
            res.add("unknown", where, f"line {u['line']}: {k} with access '{acc}'")
        return
    if k in ("null-test", "compare", "unevaluated"):
        return
    if k == "cast":
        _sink(d.get("sink") or {}, f, where, res, program, params, locals_, work, u)
        return
    if k == "call-arg":
        _sink(
            {"kind": "call-arg", "callee": d.get("callee"), "arg": d.get("arg")},
            f,
            where,
            res,
            program,
            params,
            locals_,
            work,
            u,
        )
        return
    if k == "copy":
        _sink({"kind": "copy", "into": d.get("into") or {}}, f, where, res, program, params, locals_, work, u)
        return
    if k == "return":
        res.add("violated", where, f"line {u['line']}: returned; the caller may keep it")
        return
    if k == "address-of-pointer":
        # &p handed to a producer that stores the borrowed pointer into p (e.g. CFE_SB_ReceiveBuffer)
        return
    if k == "reassign":
        return  # a new value for the variable; the borrow of the old value is not affected
    res.add("unknown", where, f"line {u['line']}: {k} use")


def _sink(
    s: dict[str, Any],
    f: dict[str, Any],
    where: dict[str, Any],
    res: BorrowResult,
    program: Any,
    params: dict[tuple[str, int], list[dict[str, Any]]],
    locals_: dict[tuple[str | None, str | None, str | None], dict[str, Any]],
    work: list[dict[str, Any]],
    u: dict[str, Any],
) -> None:
    kind = s.get("kind")
    line = u.get("line")
    if kind in ("compare",):
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
        if into.get("target") == "variable":
            local = locals_.get((f.get("file"), f.get("function"), into.get("name")))
            if local is not None:
                work.append(local)
                return
            res.add("unknown", where, f"line {line}: copied into '{into.get('name')}', which is not tracked")
            return
        res.add(
            "violated",
            where,
            f"line {line}: stored into {into.get('target') or 'memory'}; the pointer may outlive the borrow",
        )
        return
    if kind == "call-arg":
        callee, arg = s.get("callee"), s.get("arg")
        if callee is None:
            res.add("unknown", where, f"line {line}: passed to an indirect call")
            return
        key = f"{f.get('file')}::{f.get('function')}"
        defs = [] if program.models.is_boundary(callee) else program.resolve(key, callee)
        if defs:
            targets = params.get((callee, arg), [])
            if not targets:
                res.add(
                    "unknown",
                    where,
                    f"line {line}: passed to {callee}() argument {arg + 1}, which is not an analysed pointer parameter",
                )
            work.extend(targets)
            return
        model = program.models.lookup(callee)
        if model is None:
            res.add("unknown", where, f"line {line}: passed to {callee}(), which has no reviewed effect model")
            return
        if model.writes == "any" or arg in (model.writes or []):
            res.add("violated", where, f"line {line}: {callee}() writes through argument {arg + 1}")
        elif arg in (model.retains or []):
            res.add("violated", where, f"line {line}: {callee}() keeps argument {arg + 1} after returning")
        elif model.calls_back:
            res.add("unknown", where, f"line {line}: {callee}() may run program callbacks")
        return
    if kind == "indirect-call":
        res.add("unknown", where, f"line {line}: used as a function pointer")
        return
    res.add("unknown", where, f"line {line}: a derived pointer is used as {s.get('parent') or kind}")
