"""Classify how an expression uses a pointer.

Classification is purely syntactic over the compiler-resolved AST: it states
what each occurrence *does* (dereference and write, copy into another object,
pass to a callee, compare...).  It does not establish aliasing, lifetime or
whole-program behavior; those need the flow analyses layered on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from weaver.frontend.clang_ast import Node

COMPARE_OPS = {"==", "!=", "<", ">", "<=", ">="}
ARITH_OPS = {"+", "-"}
LOGICAL_OPS = {"&&", "||"}

# Use kinds that keep the pointer value inside the expression that uses it.
DEREF_KINDS = {"deref", "arrow", "subscript"}
# Use kinds through which the pointer value (an address) leaves this variable.
ESCAPE_KINDS = {"copy", "call-arg", "return", "cast", "asm", "address-of-pointer", "other"}


@dataclass
class Use:
    kind: str  # deref | arrow | subscript | reassign | arith-update | copy | arith | compare | null-test
    #            call-arg | indirect-call | return | address-of-pointer | cast | unevaluated | asm | other
    access: (
        str | None
    )  # for deref/arrow/subscript: read | write | readwrite | address | member | decay | unevaluated | other
    ref: Node  # the DeclRefExpr
    site: Node  # the outermost node describing the use (e.g. the UnaryOperator '*')
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        span = self.site.expansion_span()
        loc = self.ref.begin.file_loc
        return {
            "kind": self.kind,
            "access": self.access,
            "line": loc.line,
            "col": loc.col,
            "offset": loc.offset,
            "site_span": [span[1], span[2]] if span else None,
            "in_macro": self.ref.begin.in_macro or self.site.begin.in_macro or self.site.end.in_macro,
            **({"detail": self.detail} if self.detail else {}),
        }


def _up(n: Node) -> tuple[Node, Node | None]:
    """Skip enclosing ParenExprs; return (outermost paren-wrapped node, its parent)."""
    p = n.parent
    while p is not None and p.kind == "ParenExpr":
        n, p = p, p.parent
    return n, p


def lvalue_access(e: Node) -> str:
    """How an lvalue expression (``*p``, ``p->f``, ``p[i]``) is accessed."""
    n, p = _up(e)
    if p is None:
        return "other"
    k = p.kind
    if k == "ImplicitCastExpr":
        ck = p.cast_kind
        if ck == "LValueToRValue":
            return "read"
        if ck in ("ArrayToPointerDecay", "FunctionToPointerDecay"):
            return "decay"
        return "other"
    if k == "BinaryOperator" and p.opcode == "=" and n.index == 0:
        return "write"
    if k == "CompoundAssignOperator" and n.index == 0:
        return "readwrite"
    if k == "UnaryOperator" and p.opcode in ("++", "--"):
        return "readwrite"
    if k == "UnaryOperator" and p.opcode == "&":
        return "address"
    if k == "MemberExpr" and not p.raw.get("isArrow"):
        return "member-" + lvalue_access(p)
    if k == "UnaryExprOrTypeTraitExpr":
        return "unevaluated"
    if k in ("GCCAsmStmt", "MSAsmStmt"):
        return "asm"
    return "other"


def _callee_name(call: Node) -> str | None:
    c = call.child(0)
    while c is not None and c.kind in ("ImplicitCastExpr", "ParenExpr"):
        c = c.child(0)
    if c is not None and c.kind == "DeclRefExpr":
        r = c.raw.get("referencedDecl", {})
        if r.get("kind") == "FunctionDecl":
            return r.get("name")
    return None


def _describe_lhs(n: Node | None) -> dict[str, Any]:
    while n is not None and n.kind in ("ParenExpr", "ImplicitCastExpr"):
        n = n.child(0)
    if n is None:
        return {"target": "unknown"}
    if n.kind == "DeclRefExpr":
        r = n.raw.get("referencedDecl", {})
        return {"target": "variable", "name": r.get("name"), "decl": r.get("id")}
    if n.kind == "MemberExpr":
        return {"target": "field", "name": n.name, "arrow": bool(n.raw.get("isArrow"))}
    if n.kind == "ArraySubscriptExpr":
        return {"target": "element"}
    if n.kind == "UnaryOperator" and n.opcode == "*":
        return {"target": "dereference"}
    return {"target": n.kind}


def classify_ref(ref: Node) -> Use:
    """Classify one DeclRefExpr that names a pointer-typed object."""
    n, p = _up(ref)
    if p is None:
        return Use("other", None, ref, ref)
    k = p.kind
    if k == "ImplicitCastExpr" and p.cast_kind == "LValueToRValue":
        return _classify_value(ref, p)
    if k == "UnaryOperator" and p.opcode == "&":
        return Use("address-of-pointer", None, ref, p)
    if k == "BinaryOperator" and p.opcode == "=" and n.index == 0:
        return Use("reassign", None, ref, p, {"from": _describe_rhs(p.child(1))})
    if k == "CompoundAssignOperator" and n.index == 0:
        return Use("arith-update", None, ref, p, {"op": p.opcode})
    if k == "UnaryOperator" and p.opcode in ("++", "--"):
        return Use("arith-update", None, ref, p, {"op": p.opcode})
    if k == "UnaryExprOrTypeTraitExpr":
        return Use("unevaluated", None, ref, p)
    if k in ("GCCAsmStmt", "MSAsmStmt"):
        return Use("asm", None, ref, p)
    if k == "ImplicitCastExpr" and p.cast_kind in ("ArrayToPointerDecay", "FunctionToPointerDecay"):
        return Use("decay", None, ref, p)
    return Use("other", None, ref, p, {"parent": k})


def _describe_rhs(n: Node | None) -> dict[str, Any]:
    while n is not None and (
        n.kind == "ParenExpr" or (n.kind == "ImplicitCastExpr" and n.cast_kind in ("NoOp", "BitCast", "LValueToRValue"))
    ):
        n = n.child(0)
    if n is None:
        return {"source": "unknown"}
    if n.cast_kind == "NullToPointer":
        return {"source": "null"}
    if n.kind == "UnaryOperator" and n.opcode == "&":
        return {"source": "address-of", "object": _object_designator(n.child(0))}
    if n.kind == "DeclRefExpr":
        r = n.raw.get("referencedDecl", {})
        return {"source": "variable", "name": r.get("name"), "decl": r.get("id")}
    if n.kind == "CallExpr":
        return {"source": "call", "callee": _callee_name(n)}
    if n.kind == "IntegerLiteral":
        return {"source": "null"}
    if n.kind == "ImplicitCastExpr" and n.cast_kind == "ArrayToPointerDecay":
        return {"source": "array-decay", "object": _object_designator(n.child(0))}
    if n.kind == "ImplicitCastExpr" and n.cast_kind == "FunctionToPointerDecay":
        return {"source": "function", "object": _object_designator(n.child(0))}
    if n.kind == "CStyleCastExpr":
        return {"source": "cast", "cast_kind": n.cast_kind}
    if n.kind == "BinaryOperator" and n.opcode in ARITH_OPS:
        return {"source": "pointer-arithmetic"}
    return {"source": n.kind}


def _object_designator(n: Node | None) -> dict[str, Any] | None:
    while n is not None and n.kind == "ParenExpr":
        n = n.child(0)
    if n is None:
        return None
    if n.kind == "DeclRefExpr":
        r = n.raw.get("referencedDecl", {})
        return {"kind": "variable", "name": r.get("name"), "decl": r.get("id"), "decl_kind": r.get("kind")}
    if n.kind == "MemberExpr":
        return {
            "kind": "field",
            "name": n.name,
            "arrow": bool(n.raw.get("isArrow")),
            "base": _object_designator(n.child(0)),
        }
    if n.kind == "ArraySubscriptExpr":
        return {"kind": "element"}
    if n.kind == "UnaryOperator" and n.opcode == "*":
        return {"kind": "dereference"}
    if n.kind == "ImplicitCastExpr":
        return _object_designator(n.child(0))
    return {"kind": n.kind}


def _classify_value(ref: Node, rv: Node) -> Use:
    """The pointer's *value* is read (LValueToRValue); what happens to it?"""
    n, p = _up(rv)
    if p is None:
        return Use("other", None, ref, rv)
    k, op = p.kind, p.opcode
    if k == "UnaryOperator" and op == "*":
        return Use("deref", lvalue_access(p), ref, p)
    if k == "MemberExpr" and p.raw.get("isArrow"):
        return Use("arrow", lvalue_access(p), ref, p, {"field": p.name})
    if k == "ArraySubscriptExpr":
        return Use("subscript", lvalue_access(p), ref, p)
    if k == "UnaryOperator" and op == "!":
        return Use("null-test", None, ref, p)
    if k == "BinaryOperator" and op in LOGICAL_OPS:
        return Use("null-test", None, ref, p)
    if k == "BinaryOperator" and op in COMPARE_OPS:
        return Use("compare", None, ref, p, {"op": op})
    if k == "BinaryOperator" and op in ARITH_OPS:
        return Use("arith", None, ref, p, {"op": op})
    if k == "BinaryOperator" and op == "=" and n.index == 1:
        return Use("copy", None, ref, p, {"into": _describe_lhs(p.child(0))})
    if k == "BinaryOperator" and op == ",":
        return Use("other", None, ref, p, {"parent": "comma"})
    if (
        (k in ("IfStmt", "WhileStmt") and n.index == 0)
        or (k == "DoStmt" and n.index == 1)
        or (k == "ForStmt" and n.index == 2)
    ):
        return Use("null-test", None, ref, p)
    if k == "ConditionalOperator":
        if n.index == 0:
            return Use("null-test", None, ref, p)
        return Use("copy", None, ref, p, {"into": {"target": "conditional-result"}})
    if k == "CallExpr":
        if n.index == 0:
            return Use("indirect-call", None, ref, p)
        return Use("call-arg", None, ref, p, {"callee": _callee_name(p), "arg": n.index - 1})
    if k == "ReturnStmt":
        return Use("return", None, ref, p)
    if k == "VarDecl":
        return Use("copy", None, ref, p, {"into": {"target": "variable", "name": p.name, "decl": p.id}})
    if k == "InitListExpr":
        return Use("copy", None, ref, p, {"into": {"target": "aggregate-initializer"}})
    if k in ("CStyleCastExpr",) or (k == "ImplicitCastExpr" and p.cast_kind not in ("LValueToRValue",)):
        return Use(
            "cast", None, ref, p, {"cast_kind": p.cast_kind, "explicit": k == "CStyleCastExpr", "to": p.qual_type}
        )
    if k == "UnaryExprOrTypeTraitExpr":
        return Use("unevaluated", None, ref, p)
    if k in ("GCCAsmStmt", "MSAsmStmt"):
        return Use("asm", None, ref, p)
    return Use("other", None, ref, p, {"parent": k})


def summarize(uses: list[Use]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in uses:
        key = u.kind + (f":{u.access}" if u.access else "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _designator_text(d: dict[str, Any] | None) -> str:
    if not d:
        return "?"
    if d.get("kind") == "variable":
        return str(d.get("name"))
    if d.get("kind") == "field":
        return f"{_designator_text(d.get('base'))}{'->' if d.get('arrow') else '.'}{d.get('name')}"
    return f"<{d.get('kind')}>"


def describe_use(use: Use) -> str:
    """Human-readable description of a use (no AST-internal identifiers)."""
    d = use.detail or {}
    k = use.kind
    if k in ("deref", "arrow", "subscript"):
        return f"{k} ({use.access})"
    if k == "call-arg":
        callee = d.get("callee")
        return f"passed as argument {d.get('arg', 0) + 1} to " + (f"{callee}()" if callee else "an indirect call")
    if k == "reassign":
        src = d.get("from") or {}
        what = {
            "address-of": f"&{_designator_text(src.get('object'))}",
            "variable": f"the value of '{src.get('name')}'",
            "call": f"the result of {src.get('callee') or 'an indirect call'}()",
            "null": "a null pointer",
        }.get(src.get("source"), str(src.get("source")))
        return f"reassigned from {what}"
    if k == "copy":
        into = d.get("into") or {}
        if into.get("target") == "variable":
            return f"copied into '{into.get('name')}'"
        return f"copied into {into.get('target', 'another object')}"
    if k == "compare":
        return f"identity compared with '{d.get('op')}'"
    if k == "cast":
        return f"{'explicitly ' if d.get('explicit') else ''}converted ({d.get('cast_kind')}) to {d.get('to')}"
    if k == "arith-update":
        return f"updated with '{d.get('op')}'"
    if k == "arith":
        return f"used in pointer arithmetic ('{d.get('op')}')"
    if k == "other":
        return f"used in an unclassified context ({d.get('parent', '?')})"
    return k.replace("-", " ")
