"""Per-function effect summaries from the AST.

These are the syntactic facts interprocedural reasoning starts from: which
functions a body calls (with argument spans and shapes), which named objects it
writes, which writes go through a pointer, where a function's address is taken,
and every declaration of each function with its parameter spans.  They are
compiler-established syntax facts; *which object* a pointer write reaches is
left to points-to evidence (``weaver.flow``).
"""

from __future__ import annotations

from typing import Any

from weaver.frontend.clang_ast import Loc, Node, TranslationUnit
from weaver.frontend.typestr import resolve_typedefs, safe_parse
from weaver.util import rel_or_abs

WRITE_PARENTS = ("BinaryOperator", "CompoundAssignOperator", "UnaryOperator")
SIDE_EFFECT_KINDS = {"CallExpr", "StmtExpr"}
SIDE_EFFECT_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|=", "++", "--"}


def _pos(loc: Loc, root: str) -> dict[str, Any] | None:
    loc = loc.file_loc
    if not loc.valid:
        return None
    return {"file": rel_or_abs(loc.file, root), "line": loc.line, "col": loc.col, "offset": loc.offset}


def span_of(n: Node, root: str) -> dict[str, Any] | None:
    """Plain-text span of a node (None if it touches a macro expansion)."""
    sp = n.file_span()
    if sp is None:
        return None
    return {
        "file": rel_or_abs(sp[0], root),
        "start": sp[1],
        "end": sp[2],
        "line": n.begin.line,
        "col": n.begin.col,
        "end_line": n.end.line,
        "end_col": (n.end.col or 0) + max((n.end.tok_len or 1) - 1, 0),
    }


def expansion_span_lc(n: Node) -> tuple[int, int, int, int] | None:
    b, e = n.begin.file_loc, n.end.file_loc
    if not b.valid or not e.valid:
        return None
    return (b.line or 0, b.col or 0, e.line or 0, (e.col or 0) + max((e.tok_len or 1) - 1, 0))


def strip(n: Node | None) -> Node | None:
    while n is not None and n.kind in ("ParenExpr", "ImplicitCastExpr"):
        n = n.child(0)
    return n


def designator(n: Node | None, root: str, tu: TranslationUnit) -> dict[str, Any] | None:
    """Named object reached through a constant access path (x, s.f, a[3]); None otherwise."""
    n = strip(n)
    path: list[str] = []
    while n is not None:
        if n.kind == "DeclRefExpr":
            rd = n.raw.get("referencedDecl") or {}
            if rd.get("kind") not in ("VarDecl", "ParmVarDecl"):
                return None
            decl = tu.node(rd.get("id"))
            d: dict[str, Any] = {"name": rd.get("name"), "decl_kind": rd.get("kind"), "path": list(reversed(path))}
            if decl is not None:
                p = _pos(decl.loc, root)
                d.update(
                    decl_file=p["file"] if p else None,
                    decl_line=p["line"] if p else None,
                    storage=decl.storage_class,
                    global_=_is_global(decl),
                )
            return d
        if n.kind == "MemberExpr" and not n.raw.get("isArrow"):
            path.append("." + (n.name or "?"))
            n = strip(n.child(0))
            continue
        if n.kind == "ArraySubscriptExpr":
            base = n.child(0)
            idx = strip(n.child(1))
            if not (base is not None and base.kind == "ImplicitCastExpr" and base.cast_kind == "ArrayToPointerDecay"):
                return None
            if idx is None or idx.kind != "IntegerLiteral":
                return None
            path.append(f"[{idx.raw.get('value')}]")
            n = strip(base.child(0))
            continue
        return None
    return None


def _is_global(decl: Node) -> bool:
    if decl.kind != "VarDecl":
        return False
    if decl.storage_class in ("static", "extern"):
        return True
    return decl.enclosing("FunctionDecl") is None


def has_side_effects(n: Node | None) -> bool:
    if n is None:
        return False
    for x in n.walk():
        if x.kind in SIDE_EFFECT_KINDS:
            return True
        if x.kind in ("BinaryOperator", "CompoundAssignOperator", "UnaryOperator") and x.opcode in SIDE_EFFECT_OPS:
            return True
        if x.kind in ("GCCAsmStmt", "MSAsmStmt"):
            return True
    return False


def _is_null(n: Node | None) -> bool:
    while n is not None and n.kind in ("ParenExpr", "ImplicitCastExpr", "CStyleCastExpr"):
        if n.cast_kind == "NullToPointer":
            return True
        n = n.child(0)
    return n is not None and n.kind == "IntegerLiteral" and n.raw.get("value") == "0"


def _write_context(e: Node) -> str | None:
    """If lvalue ``e`` is written by its parent, return the operator."""
    n, p = e, e.parent
    while p is not None and p.kind == "ParenExpr":
        n, p = p, p.parent
    if p is None:
        return None
    if p.kind == "BinaryOperator" and p.opcode == "=" and n.index == 0:
        return "="
    if p.kind == "CompoundAssignOperator" and n.index == 0:
        return p.opcode
    if p.kind == "UnaryOperator" and p.opcode in ("++", "--"):
        return p.opcode
    return None


def _through_pointer(lv: Node) -> Node | None:
    """For an lvalue, the node that dereferences a pointer on its access path (or None)."""
    n = lv
    while n is not None:
        while n is not None and n.kind == "ParenExpr":
            n = n.child(0)
        if n is None:
            return None
        if n.kind == "UnaryOperator" and n.opcode == "*":
            return n
        if n.kind == "MemberExpr":
            if n.raw.get("isArrow"):
                return n
            n = n.child(0)
            continue
        if n.kind == "ArraySubscriptExpr":
            base = n.child(0)
            if base is not None and base.kind == "ImplicitCastExpr" and base.cast_kind == "ArrayToPointerDecay":
                n = base.child(0)
                continue
            return n
        return None
    return None


def _named_base(lv: Node) -> Node | None:
    n = lv
    while n is not None:
        if n.kind == "ParenExpr":
            n = n.child(0)
        elif n.kind == "MemberExpr" and not n.raw.get("isArrow"):
            n = n.child(0)
        elif n.kind == "ArraySubscriptExpr":
            base = n.child(0)
            if base is not None and base.kind == "ImplicitCastExpr" and base.cast_kind == "ArrayToPointerDecay":
                n = base.child(0)
            else:
                return None
        elif n.kind == "DeclRefExpr":
            return n
        else:
            return None
    return None


def callee_ref(call: Node) -> Node | None:
    c = call.child(0)
    while c is not None and c.kind in ("ImplicitCastExpr", "ParenExpr"):
        c = c.child(0)
    if c is not None and c.kind == "DeclRefExpr" and (c.raw.get("referencedDecl") or {}).get("kind") == "FunctionDecl":
        return c
    return None


def summarize_function(fn: Node, tu: TranslationUnit, root: str) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    indirect: list[dict[str, Any]] = []
    named_writes: list[dict[str, Any]] = []
    pointer_writes: list[dict[str, Any]] = []
    function_refs: list[dict[str, Any]] = []
    asm: list[dict[str, Any]] = []

    for n in fn.walk():
        k = n.kind
        if k == "CallExpr":
            cref = callee_ref(n)
            args = []
            for a in n.real_children()[1:]:
                inner = strip(a)
                t = resolve_typedefs(safe_parse(a.canonical_type), tu.typedefs)
                addr = None
                if inner is not None and inner.kind == "UnaryOperator" and inner.opcode == "&":
                    addr = designator(inner.child(0), root, tu)
                args.append(
                    {
                        "span": span_of(a, root),
                        "lc": expansion_span_lc(a),
                        "pointer": bool(t is not None and t.kind == "pointer"),
                        "addr_of": addr,
                        "addr_of_operand_span": span_of(inner.child(0), root)
                        if inner is not None
                        and inner.kind == "UnaryOperator"
                        and inner.opcode == "&"
                        and inner.child(0)
                        else None,
                        "null": _is_null(a),
                        "side_effects": has_side_effects(a),
                        "kind": inner.kind if inner is not None else None,
                    }
                )
            site = _pos(n.begin, root)
            entry = {
                "callee": (cref.raw.get("referencedDecl") or {}).get("name") if cref else None,
                "site": site,
                "callee_ref": _pos(cref.begin, root) if cref is not None else None,
                "in_macro": n.begin.in_macro or (cref is not None and cref.begin.in_macro),
                "args": args,
                "unevaluated": n.enclosing("UnaryExprOrTypeTraitExpr") is not None,
            }
            (calls if cref is not None else indirect).append(entry)
        elif k == "DeclRefExpr":
            rd = n.raw.get("referencedDecl") or {}
            if rd.get("kind") == "FunctionDecl":
                p = n.parent
                if not (
                    p is not None
                    and p.kind == "ImplicitCastExpr"
                    and p.cast_kind == "FunctionToPointerDecay"
                    and p.parent is not None
                    and p.parent.kind == "CallExpr"
                    and p.index == 0
                ):
                    function_refs.append({"name": rd.get("name"), **(_pos(n.begin, root) or {})})
        elif k in ("GCCAsmStmt", "MSAsmStmt"):
            asm.append(_pos(n.begin, root) or {})

        # writes: find lvalues in write position
        if k in ("BinaryOperator", "CompoundAssignOperator") and (k != "BinaryOperator" or n.opcode == "="):
            lv = n.child(0)
        elif k == "UnaryOperator" and n.opcode in ("++", "--"):
            lv = n.child(0)
        else:
            lv = None
        if lv is not None:
            op = n.opcode
            deref = _through_pointer(lv)
            if deref is not None:
                pointer_writes.append(
                    {
                        "op": op,
                        "site": _pos(n.begin, root),
                        "lvalue": span_of(lv, root),
                        "lc": expansion_span_lc(lv),
                        "in_macro": lv.begin.in_macro or lv.end.in_macro,
                    }
                )
            else:
                base = _named_base(lv)
                if base is not None:
                    rd = base.raw.get("referencedDecl") or {}
                    decl = tu.node(rd.get("id"))
                    dp = _pos(decl.loc, root) if decl is not None else None
                    named_writes.append(
                        {
                            "name": rd.get("name"),
                            "op": op,
                            "decl_kind": rd.get("kind"),
                            "global": _is_global(decl) if decl is not None else True,
                            "decl_file": dp["file"] if dp else None,
                            "decl_line": dp["line"] if dp else None,
                            "site": _pos(n.begin, root),
                        }
                    )
                else:
                    pointer_writes.append(
                        {
                            "op": op,
                            "site": _pos(n.begin, root),
                            "lvalue": span_of(lv, root),
                            "lc": expansion_span_lc(lv),
                            "in_macro": True,
                            "unknown_base": True,
                        }
                    )

    loc = fn.loc.file_loc
    return {
        "name": fn.name,
        "file": rel_or_abs(loc.file, root) if loc.file else None,
        "line": loc.line,
        "end_line": fn.end.file_loc.line,
        "static": fn.storage_class == "static",
        "variadic": bool(fn.raw.get("variadic")) or "..." in (fn.canonical_type or ""),
        "calls": calls,
        "indirect_calls": indirect,
        "named_writes": named_writes,
        "pointer_writes": pointer_writes,
        "function_refs": function_refs,
        "asm": asm,
    }


def declaration_record(fn: Node, tu: TranslationUnit, root: str) -> dict[str, Any]:
    params = []
    for i, p in enumerate(c for c in fn.real_children() if c.kind == "ParmVarDecl"):
        params.append(
            {
                "index": i,
                "name": p.name,
                "type": p.qual_type,
                "canonical_type": p.canonical_type,
                "span": span_of(p, root),
                "name_offset": p.loc.file_loc.offset if p.name else None,
                "in_macro": p.begin.in_macro or p.end.in_macro or p.loc.in_macro,
            }
        )
    ftype = fn.canonical_type or ""
    loc = fn.loc.file_loc
    return {
        "name": fn.name,
        "file": rel_or_abs(loc.file, root) if loc.file else None,
        "line": loc.line,
        "offset": loc.offset,
        "static": fn.storage_class == "static",
        "definition": any(c.kind == "CompoundStmt" for c in fn.real_children()),
        "prototyped": "(" in ftype and not ftype.rstrip().endswith("()"),
        "variadic": bool(fn.raw.get("variadic")) or "..." in ftype,
        "type": fn.qual_type,
        "params": params,
        "in_macro": fn.loc.in_macro,
    }


def file_scope_function_refs(tu: TranslationUnit, root: str) -> list[dict[str, Any]]:
    """Function names used outside any function body (e.g. initializers of function-pointer tables)."""
    out = []
    for top in tu.top:
        if top.kind == "FunctionDecl":
            continue
        for n in top.walk():
            if n.kind == "DeclRefExpr" and (n.raw.get("referencedDecl") or {}).get("kind") == "FunctionDecl":
                out.append({"name": n.raw["referencedDecl"].get("name"), **(_pos(n.begin, root) or {})})
    return out
