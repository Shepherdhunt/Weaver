"""Recipe ``output-param``: return an output value instead of writing it through a pointer parameter.

Pointer-tracker roadmap: *Output parameter -> return value.*  Two forms::

    void get(int a, int *out)             int get(int a)
    { *out = a * 2; }            ==>      { int out; out = a * 2; return out; }
    ... get(1, &v); ...                   ... v = get(1); ...

    int32 read(uint32 *out)               read_result_t read(void)
    { if (out == NULL) return -1;  ==>    { uint32 out; if (0) return (read_result_t){.status = (-1), .value = out};
      *out = 7; return 0; }                 out = 7; return (read_result_t){.status = (0), .value = out}; }
    ... Status = read(&v); ...            ... { read_result_t read_r12 = read(); Status = read_r12.status;
                                                v = read_r12.value; } ...

A function that returns nothing returns the value.  A function that returns a
status returns a small result record (``<name>_result_t``: the status and the
value), declared once next to its prototype; this needs value records, a
provisional CLite capability.

When the value is written before some returns and not others (an error path
that returns early), the record also carries ``has_value``, and each caller
assigns the value only when it was written::

    int32 get(uint32 *out)                get_result_t get(void)
    { if (!ready) return -1;       ==>    { uint32 out; if (!ready) return (get_result_t){.status = (-1)};
      *out = 7; return 0; }                 out = 7; return (get_result_t){.status = (0), .value = out,
                                                                          .has_value = 1}; }
    ... s = get(&v); ...                  ... { get_result_t get_r9 = get(); s = get_r9.status;
                                                if (get_r9.has_value) v = get_r9.value; } ...

A return reached with the value written on some paths only reads a flag set
next to each write.

Why the change preserves behaviour:

* every call passes ``&x`` where ``x`` is a local variable of the caller (or a
  field of one, reached with '.') whose address is taken nowhere else, so no
  code other than the callee, through this parameter, can read or write ``x``
  during the call (the same pointer-provenance argument as the task model's),
  including other threads.  A caller may instead pass on its own pointer
  parameter, if it only dereferences, null-tests and forwards it, and every
  one of its callers passes such an address in turn (leaf-first conversion:
  once the callee returns the value, the caller writes it through its own
  parameter, which can then be converted the same way);
* inside the callee the parameter is only written, each write a whole
  statement, never read, passed, compared (except with a null pointer
  constant) or reassigned, so it is equivalent to a local variable whose final
  value the caller receives;
* at every return the analysis knows whether the value was written: always
  (the caller receives it), never, or on some paths (a flag says which); the
  caller assigns the value exactly when the original would have written it;
* a null test of the parameter can never succeed, since the argument always
  points to a variable; it is folded to its constant result;
* every caller is known (the complete-caller checks of ``scalar-input``), and
  every call sits where the result can be assigned: a statement of its own, or
  (for a status) an assignment, a declaration or a return.  A caller that
  never reads its variable discards the value and the variable.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from weaver.analysis.uses import describe_use_parts
from weaver.frontend.clang_ast import Node, TranslationUnit
from weaver.frontend.typestr import resolve_typedefs, safe_parse
from weaver.recipes.base import (
    ESTABLISHED,
    UNRESOLVED,
    VIOLATED,
    Precondition,
    Recipe,
    RecipeContext,
    RecipeResult,
)
from weaver.recipes.scalar_input import NORETURN, ScalarInputRecipe, _callee, _is_scalar, param_types
from weaver.rewrite import Edit, OffsetMap
from weaver.util import rel_or_abs

SPECIFIERS = {"static", "extern", "inline", "__inline", "__inline__", "_Noreturn", "register"}
NULL_TOKENS = {"NULL", "0"}
CONTAINERS = {"CompoundStmt", "CaseStmt", "DefaultStmt", "LabelStmt"}
LOOPS = {"WhileStmt", "ForStmt", "DoStmt"}
MAX_CHAIN = 6  # forwarding calls followed from a call site back to the private variable

PRESERVATION = (
    "Every call passes the address of a local variable of the caller (or of a field of one) whose address is "
    "taken nowhere else{reach}, so during the call nothing but `{f}()`, through `{p}`, can read or write it. "
    "`{f}()` only writes `*{p}`, each write a statement of its own, so a local variable holding the same values "
    "ends with the value the caller would have found. {written} {form} Null tests of `{p}` can never succeed (the "
    "argument always points to a variable) and are folded to their constant result. The function's address is "
    "never taken and every reference to its name is an analysed call or declaration, so every caller changes "
    "together with its definition and prototypes."
)


def _strip(n: Node | None) -> Node | None:
    while n is not None and n.kind in ("ParenExpr", "ImplicitCastExpr"):
        n = n.child(0)
    return n


def _refers(n: Node | None, decl_id: str) -> bool:
    n = _strip(n)
    return n is not None and n.kind == "DeclRefExpr" and (n.raw.get("referencedDecl") or {}).get("id") == decl_id


def _null(n: Node | None) -> bool:
    while n is not None and n.kind in ("ParenExpr", "ImplicitCastExpr", "CStyleCastExpr"):
        if n.cast_kind == "NullToPointer":
            return True
        n = n.child(0)
    return n is not None and n.kind == "IntegerLiteral" and n.raw.get("value") == "0"


def _is_write(n: Node, decl_id: str) -> bool:
    """``*p = e`` (or ``(*p) = e``) with ``p`` the parameter."""
    if n.kind != "BinaryOperator" or n.opcode != "=":
        return False
    lhs = n.child(0)
    while lhs is not None and lhs.kind == "ParenExpr":
        lhs = lhs.child(0)
    return lhs is not None and lhs.kind == "UnaryOperator" and lhs.opcode == "*" and _refers(lhs.child(0), decl_id)


def _fold(cond: Node | None, decl_id: str) -> bool | None:
    """The constant value of a condition that only tests the parameter against null (it is never null)."""
    c = _strip(cond)
    if c is None:
        return None
    if _refers(c, decl_id):
        return True
    if c.kind == "UnaryOperator" and c.opcode == "!" and _refers(c.child(0), decl_id):
        return False
    if c.kind == "BinaryOperator" and c.opcode in ("==", "!="):
        a, b = c.child(0), c.child(1)
        if (_refers(a, decl_id) and _null(b)) or (_refers(b, decl_id) and _null(a)):
            return c.opcode == "!="
    return None


def _if_parts(n: Node) -> tuple[Node | None, Node | None, Node | None]:
    kids = n.real_children()
    i = int(bool(n.raw.get("hasInit"))) + int(bool(n.raw.get("hasVar")))
    cond = kids[i] if i < len(kids) else None
    then = kids[i + 1] if i + 1 < len(kids) else None
    other = kids[i + 2] if n.raw.get("hasElse") and i + 2 < len(kids) else None
    return cond, then, other


NO, MAYBE, YES = 0, 1, 2


def _join(a: int, b: int) -> int:
    return a if a == b else MAYBE


class _Written:
    """Where is ``*p`` written?  At each return: on no path before it (NO), on every path (YES), or on some
    (MAYBE).  Loops and switches are not unrolled: one that writes anywhere inside makes the state inside and
    after it MAYBE (or keeps YES), which is sound for both definite answers."""

    def __init__(self, decl_id: str):
        self.decl_id = decl_id
        self.problems: list[str] = []
        self.returns: list[tuple[Node, int]] = []

    def stmt(self, n: Node | None, w: int) -> tuple[int, bool]:
        """(state afterwards, control cannot fall through)."""
        if n is None:
            return w, False
        k = n.kind
        if k == "CompoundStmt":
            for c in n.real_children():
                w, t = self.stmt(c, w)
                if t:
                    return w, True
            return w, False
        if k == "ReturnStmt":
            self.returns.append((n, w))
            return w, True
        if _is_write(n, self.decl_id):
            return YES, False
        if k == "IfStmt":
            cond, then, other = _if_parts(n)
            f = _fold(cond, self.decl_id)
            if f is True:
                return self.stmt(then, w)
            if f is False:
                return self.stmt(other, w)
            wt, tt = self.stmt(then, w)
            we, te = self.stmt(other, w)
            if tt and te:
                return w, True
            return (we if tt else wt if te else _join(wt, we)), False
        if k in ("WhileStmt", "ForStmt", "DoStmt", "SwitchStmt"):
            if any(_is_write(x, self.decl_id) for x in n.walk()):
                w = _join(w, YES)
            for c in n.real_children():
                self.stmt(c, w)
            return w, False
        if k in ("CaseStmt", "DefaultStmt"):
            return self.stmt(n.real_children()[-1] if n.real_children() else None, w)
        if k in ("BreakStmt", "ContinueStmt"):
            return w, True
        if k in ("GotoStmt", "IndirectGotoStmt", "LabelStmt"):
            self.problems.append(f"line {n.begin.file_loc.line}: goto and labels are not analysed")
            return w, True
        if k == "CallExpr" and _callee(n) in NORETURN:
            return w, True
        return w, False


def _describe_states(returns: list[tuple[Node, int]], end: int | None) -> str:
    """What the analysis found at the returns (and at the end, when control can reach it)."""
    n = len(returns)
    yes = sum(1 for _, s in returns if s == YES)
    maybe = sum(1 for _, s in returns if s == MAYBE)
    msg = f"written before {yes} of {n} return(s)"
    if maybe:
        msg += f", on some paths before {maybe}"
    if n - yes - maybe:
        msg += f", never before {n - yes - maybe}"
    if end is not None:
        msg += {YES: "; written at the end", MAYBE: "; written on some paths to the end", NO: "; not at the end"}[end]
    if maybe or n - yes - maybe or end not in (None, YES):
        msg += (
            ". The result says whether a value was written (has_value), and each caller assigns the value only "
            "then, so its variable keeps its old value exactly when the original left it unchanged"
        )
    return msg


def _merge_insertions(edits: dict[tuple[str, int, int], Edit]) -> None:
    """Fold an insertion into the edit that starts where it is inserted (the rewriter rejects the pair)."""
    for key, ins in list(edits.items()):
        if ins.start != ins.end:
            continue
        other = next((e for k, e in edits.items() if k != key and e.file == ins.file and e.start == ins.start), None)
        if other is not None:
            del edits[key]
            del edits[(other.file, other.start, other.end)]
            merged = replace(
                other, replacement=ins.replacement + other.replacement, reason=f"{ins.reason}; {other.reason}"
            )
            edits[(merged.file, merged.start, merged.end)] = merged


def _base_ref(n: Node | None) -> Node | None:
    """The variable an lvalue such as ``x`` or ``s.a.b`` names (fields reached with '.', not '->')."""
    while n is not None:
        if n.kind in ("ParenExpr", "ImplicitCastExpr"):
            n = n.child(0)
        elif n.kind == "MemberExpr" and not n.raw.get("isArrow"):
            n = n.child(0)
        else:
            break
    return n if n is not None and n.kind == "DeclRefExpr" else None


def _climb(r: Node) -> tuple[Node, Node | None]:
    """From a reference to a variable up through parentheses and '.' field accesses: (the lvalue, its parent)."""
    n, p = r, r.parent
    while p is not None and (
        p.kind == "ParenExpr"
        or (p.kind == "MemberExpr" and not p.raw.get("isArrow") and p.child(0) is n)
        or (p.kind == "ImplicitCastExpr" and p.cast_kind == "NoOp")
    ):
        n, p = p, p.parent
    return n, p


def _address_taken(r: Node) -> Node | None:
    """Where a reference (or a field of it) has its address taken or decays to a pointer, if it does."""
    _, p = _climb(r)
    if p is not None and (
        (p.kind == "UnaryOperator" and p.opcode == "&")
        or (p.kind == "ImplicitCastExpr" and p.cast_kind == "ArrayToPointerDecay")
    ):
        return p
    return None


def _is_read(r: Node) -> bool:
    _, p = _climb(r)
    return p is not None and p.kind == "ImplicitCastExpr" and p.cast_kind == "LValueToRValue"


def _above_comment(text: str, line_start: int) -> int:
    """Move a line-start offset up over the comment lines directly above it (a declaration's documentation)."""
    pos = line_start
    while pos > 0:
        prev = text.rfind("\n", 0, pos - 1) + 1
        line = text[prev : pos - 1].strip()
        if not line or not (line.startswith(("/*", "*", "//")) or line.endswith("*/")):
            break
        pos = prev
    return pos


def _indent_of(text: str, start: int) -> str | None:
    """The whitespace before ``start`` on its line, or None when code precedes it."""
    ls = text.rfind("\n", 0, start) + 1
    lead = text[ls:start]
    return lead if lead.strip() == "" else None


def _returned_text(lx: Any, r: Node, val: Node | None) -> tuple[int, int, int] | str:
    """(offset of ``return``, start and end of the returned expression's text) of a return statement.

    The expression may use macros (``return CFE_SUCCESS;``): only the ``return`` keyword and the
    ``;`` must be plain source text, and the AST's expression must start at the first token after the
    keyword and end before the ``;``.
    """
    b = r.begin
    i = lx.index_at(b.offset) if b.valid and not b.in_macro else None
    if i is None or lx.tokens[i].text != "return":
        return "return inside a macro expansion"
    depth, j = 0, i + 1
    while j < len(lx.tokens):
        t = lx.tokens[j]
        if t.in_directive:
            return "a preprocessor directive inside the return statement"
        if t.text in ("(", "[", "{"):
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
            if depth < 0:
                break
        elif t.text == ";" and depth == 0:
            break
        j += 1
    if j >= len(lx.tokens) or lx.tokens[j].text != ";":
        return "the return statement does not end with a plain ';'"
    if val is None:
        return b.offset, lx.tokens[j].start, lx.tokens[j].start
    s, e = lx.tokens[i + 1].start, lx.tokens[j - 1].end
    vb, ve = val.begin.file_loc, val.end.file_loc
    if j == i + 1 or vb.offset != s or ve.offset is None or not s <= ve.offset < lx.tokens[j].start:
        return "the returned expression does not match its source text"
    return b.offset, s, e


def _semicolon(lx: Any, start: int) -> int | None:
    """Offset of the ';' that ends the statement whose first token starts at ``start`` (plain source only)."""
    i = lx.index_at(start)
    if i is None:
        return None
    depth = 0
    for t in lx.tokens[i:]:
        if t.in_directive:
            return None
        if t.text in ("(", "[", "{"):
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
            if depth < 0:
                return None
        elif t.text == ";" and depth == 0:
            return t.start
    return None


def _stmt_end(ctx: RecipeContext, file: str, end: int) -> int | None:
    """Offset just past the ';' that ends a statement whose expression ends at ``end``."""
    lx = ctx.lexed(file)
    nxt = next((t for t in lx.tokens if t.start >= end), None)
    return nxt.end if nxt is not None and nxt.text == ";" else None


class OutputParamRecipe(Recipe):
    id = "output-param"
    version = "2"
    title = "Return an output value instead of writing it through a pointer parameter"
    finding_kinds = ("parameter",)

    def applicable(self, finding: dict[str, Any]) -> bool:
        return (
            finding.get("kind") == "parameter"
            and not finding.get("function_pointer")
            and any(u["kind"] == "deref" and u.get("access") == "write" for u in finding.get("uses", []))
        )

    # ------------------------------------------------------------------
    def evaluate(self, ctx: RecipeContext, finding: dict[str, Any]) -> RecipeResult:
        inv = ctx.inventory
        prog = ctx.program
        fname: str = finding["function"]
        pname: str = finding.get("name") or "?"
        idx: int = finding["param_index"]
        def_file: str = finding["file"]
        fkey = f"{def_file}::{fname}"
        fsum = prog.funcs.get(fkey)
        P = {
            "current": Precondition("OP.evidence-current", "Evidence describes the current source revision"),
            "program": Precondition(
                "OP.whole-program", "Every unit of every configured profile was analysed with adequate evidence"
            ),
            "type": Precondition(
                "OP.parameter-type",
                "The parameter points to a non-const, non-volatile, non-atomic scalar and is a plain pointer "
                "declarator in every declaration",
            ),
            "uses": Precondition(
                "OP.write-only",
                "Inside the function the target is only written, each write a statement of its own; the pointer "
                "is otherwise only compared with a null pointer constant",
            ),
            "written": Precondition(
                "OP.written-before-return",
                "At every return the analysis knows whether the target was written (always, never, or on some "
                "paths, then tracked by a flag), and a function that returns a status never falls off its end",
            ),
            "private": Precondition(
                "OP.private-target",
                "Every call passes the address of a local variable of the caller (or of a field of one) whose "
                "address is taken nowhere else, or forwards its own pointer parameter from callers that do, so "
                "nothing else can access the target during the call",
            ),
            "callers": Precondition(
                "OP.complete-callers",
                "Every caller is known: the address is never taken, every reference is an analysed call or "
                "declaration, every linked object was analysed, and the interface is not frozen",
            ),
            "sites": Precondition(
                "OP.call-sites",
                "Every call is a statement of its own, or (for a status) the value of an assignment, a declaration "
                "or a return",
            ),
            "result": Precondition(
                "OP.return-type",
                "The function returns nothing, or a scalar status; a result record, when one is needed, can be "
                "declared next to its only prototype",
            ),
            "source": Precondition("OP.edits-in-source", "Every edited range is plain source text"),
        }
        positive: dict[str, list[str]] = {}

        def pos(k: str, msg: str) -> None:
            positive.setdefault(k, []).append(msg)

        if fsum is None:
            P["program"].fail(UNRESOLVED, f"no analysed definition of {fname}() in {def_file}")
            return self._result(finding, P, positive, [], {}, ctx, [], None)

        decls = [
            d
            for d in inv.get("function_decls", [])
            if d["name"] == fname and (not fsum["static"] or d["file"] == def_file)
        ]
        callers = prog.callers_of(fkey)
        files = {def_file} | {d["file"] for d in decls} | {c["site"]["file"] for _, c in callers if c.get("site")}
        hashes: dict[str, str] = {}
        for f in sorted(files):
            cur = ctx.current_hash(f)
            hashes[f] = cur
            if inv["files"].get(f) not in (None, cur):
                P["current"].fail(VIOLATED, f"{f} changed since the inventory was built", "re-run 'weaver refresh'")
        if P["current"].status == ESTABLISHED:
            pos("current", f"{len(files)} affected file(s) match the analysed revision")
        for status, msg in ctx.whole_program(list(fsum.get("units", []))):
            P["program"].fail(status, msg, "collect every unit and run 'weaver inventory'")

        # -- the parameter's type -------------------------------------------------------------
        units = [u for u in inv["units"] if u["unit_id"] in set(fsum.get("units", []))]
        pointee = None
        for pt, pointee in param_types(ctx, finding, units):
            if pointee is None:
                P["type"].fail(UNRESOLVED, f"type {finding.get('type')!r} not recognised as a pointer")
                continue
            if finding.get("typedef_hidden"):
                P["type"].fail(VIOLATED, "the pointer is hidden behind a typedef")
            if not _is_scalar(pointee):
                P["type"].fail(VIOLATED, f"pointee '{pt.inner.spell()}' is not a scalar type")
            if "const" in pointee.quals:
                P["type"].fail(VIOLATED, "the pointee is const; the function cannot write through it")
            if "volatile" in pointee.quals or pointee.atomic or "volatile" in pt.quals:
                P["type"].fail(VIOLATED, "accesses are volatile or atomic")
        if P["type"].status == ESTABLISHED and pointee is not None:
            pos("type", f"'{finding.get('type')}': pointer to scalar '{pointee.spell()}'")

        # -- uses: write-only, plus null tests -----------------------------------------------------
        uses = finding.get("uses", [])
        for u in uses:
            d = u.get("detail") or {}
            if u["kind"] == "deref" and u.get("access") == "write":
                continue
            if u["kind"] == "compare" and d.get("null") or u["kind"] == "null-test":
                continue
            P["uses"].fail(VIOLATED, f"line {u['line']}: {describe_use_parts(u['kind'], u.get('access'), d)}")

        # -- the definition: whole-statement writes, definitely written, returns ------------------------
        unit = next((u for u in inv["units"] if u["unit_id"] in set(fsum.get("units", []))), None)
        tu = ctx.tu(unit) if unit else None
        fn = (
            next(
                (
                    f
                    for f in tu.functions()
                    if f.name == fname and rel_or_abs(f.loc.file_loc.file or "", ctx.root) == def_file
                ),
                None,
            )
            if tu
            else None
        )
        params = [c for c in fn.real_children() if c.kind == "ParmVarDecl"] if fn else []
        body = next((c for c in fn.real_children() if c.kind == "CompoundStmt"), None) if fn else None
        wa = None
        optional = False
        writes: list[Node] = []
        if fn is None or body is None or idx >= len(params):
            P["written"].fail(UNRESOLVED, "the definition was not found in the AST")
        else:
            decl_id = params[idx].id
            for r in tu.refs_to(decl_id):
                p = r.parent
                while p is not None and p.kind in ("ImplicitCastExpr", "ParenExpr"):
                    p = p.parent
                if p is not None and p.kind == "UnaryOperator" and p.opcode == "*":
                    st = p.parent
                    while st is not None and st.kind == "ParenExpr":
                        st = st.parent
                    if st is not None and _is_write(st, decl_id):
                        writes.append(st)
                        if st.parent is None or st.parent.kind not in CONTAINERS | LOOPS | {"IfStmt"}:
                            P["uses"].fail(
                                VIOLATED, f"line {st.begin.file_loc.line}: the write is part of a larger expression"
                            )
            wa = _Written(decl_id)
            end_w, end_t = wa.stmt(body, NO)
            for pr in wa.problems:
                P["written"].fail(UNRESOLVED, pr)
            states = [s for _, s in wa.returns] + ([] if end_t else [end_w])
            optional = any(s != YES for s in states)
            if not end_t and self._return_spelling(decls) not in ("", "void"):
                P["written"].fail(
                    VIOLATED,
                    "control can reach the end of a function that returns a status (a missing return, or a switch "
                    "or loop the analysis does not see through), so the result would be unset",
                )
            if P["written"].status == ESTABLISHED:
                pos("written", _describe_states(wa.returns, None if end_t else end_w))
        if P["uses"].status == ESTABLISHED:
            n_w = sum(1 for u in uses if u["kind"] == "deref")
            n_t = len(uses) - n_w
            pos("uses", f"{n_w} write(s)" + (f", {n_t} null test(s) that fold to a constant" if n_t else ""))

        # -- return type: void or a scalar status -----------------------------------------------------
        ret = (fsum and self._return_spelling(decls)) or ""
        form = "void" if ret == "void" else "status"
        record = form == "status" or optional  # a status, or whether the value was written, travels with it
        result_name = f"{fname}_result_t"
        if form == "status":
            rt = resolve_typedefs(safe_parse(ret), {})
            if not ret or "*" in ret or rt is None or rt.kind != "base" or ret.startswith(("struct", "union")):
                P["result"].fail(VIOLATED, f"returns '{ret or '?'}', not a scalar status")
        if record:
            homes = sorted({d["file"] for d in decls if d["file"] != def_file})
            if len(homes) > 1:
                P["result"].fail(
                    VIOLATED, f"declared in several files ({', '.join(homes)}); the result record would be duplicated"
                )
            elif homes and homes[0].endswith(".c"):
                P["result"].fail(VIOLATED, f"declared in {homes[0]}, a source file other than the definition's")
            if ctx.ident_occurrences(result_name, sorted(inv["files"])):
                P["result"].fail(VIOLATED, f"the name {result_name} is already used")
            if P["result"].status == ESTABLISHED:
                what = (
                    ("its status, " if form == "status" else "")
                    + "the value"
                    + (" and whether it was written" if optional else "")
                )
                pos("result", f"returns '{ret}'; {what} travel in {result_name} (needs value records)")
        else:
            pos("result", "returns nothing: the value becomes the return value")
        flag_t, (no, yes) = "_Bool", ("0", "1")
        if tu is not None and unit is not None:
            macros = ctx.macros(unit)
            if "bool" in tu.typedefs or "bool" in macros:
                flag_t = "bool"
                if "true" in macros and "false" in macros:
                    no, yes = "false", "true"

        # -- complete callers (as scalar-input) --------------------------------------------------------
        ScalarInputRecipe()._check_callers(ctx, fname, fsum, decls, callers, P["callers"], pos)

        # -- edits -----------------------------------------------------------------------------
        edits: dict[tuple[str, int, int], Edit] = {}

        def put(e: Edit | str | None, pre: str = "source", where: str = "") -> None:
            if isinstance(e, str):
                P[pre].fail(VIOLATED, f"{where}{e}")
            elif e is not None:
                edits[(e.file, e.start, e.end)] = e

        tspell = ""
        ret_starts: list[tuple[dict[str, Any], int, int, str]] = []
        for d in decls:
            if d.get("in_macro"):
                P["source"].fail(VIOLATED, f"{d['file']}:{d['line']}: declaration produced by a macro")
                continue
            if idx >= len(d["params"]):
                P["type"].fail(VIOLATED, f"{d['file']}:{d['line']}: declaration has {len(d['params'])} parameter(s)")
                continue
            sp = d["params"][idx].get("span")
            if sp and not tspell:
                txt = ctx.lexed(sp["file"]).text[sp["start"] : sp["end"]]
                tspell = txt[: txt.find("*")].strip() if "*" in txt else ""
            put(self._drop_param(ctx, d, idx), "source", f"{d['file']}:{d['line']}: ")
            rs = self._return_span(ctx, d)
            if isinstance(rs, str):
                P["result"].fail(VIOLATED, f"{d['file']}:{d['line']}: {rs}")
                continue
            s0, e0, text = rs
            if not record:
                put(Edit(d["file"], s0, e0, text, tspell, f"{fname}() returns the value"))
            else:
                ret_starts.append((d, s0, e0, text))
        if not tspell and P["type"].status == ESTABLISHED:
            P["type"].fail(VIOLATED, "the parameter's type could not be read from its declaration")
        # the result record goes before the first declaration in the header (or, without one, in the source)
        home = sorted(ret_starts, key=lambda x: (x[0]["file"] == def_file, x[1]))
        carried = (["its status"] if form == "status" else []) + [f"the value it used to write through '{pname}'"]
        carried += ["whether it wrote one"] if optional else []
        fields = ([f"{ret} status;"] if form == "status" else []) + [f"{tspell} value;"]
        fields += [f"{flag_t} has_value;"] if optional else []
        typedef = (
            f"/* {fname}(): {', '.join(carried[:-1]) + ' and ' if len(carried) > 1 else ''}{carried[-1]} */\n"
            "typedef struct\n{\n" + "".join(f"    {x}\n" for x in fields) + f"}} {result_name};\n\n"
        )
        for i, (d, s0, e0, text) in enumerate(home):
            why = f"{fname}() returns {result_name}"
            if i == 0:
                # the record goes on the lines before the first declaration in the header (else the source),
                # in the same edit as that declaration's return type
                src = ctx.lexed(d["file"]).text
                ls = _above_comment(src, src.rfind("\n", 0, s0) + 1)
                gap = "\n" if ls > 1 and src[src.rfind("\n", 0, ls - 1) + 1 : ls - 1].strip() else ""
                record_text = gap + typedef + src[ls:s0] + result_name
                put(Edit(d["file"], ls, e0, src[ls:e0], record_text, why + "; result record"))
            else:
                put(Edit(d["file"], s0, e0, text, result_name, why))
        shape = {"form": form, "record": record, "optional": optional, "result": result_name, "flag_t": flag_t,
                 "no": no, "yes": yes}  # fmt: skip
        if fn is not None and body is not None and tspell:
            self._body_edits(ctx, finding, body, params[idx].id if idx < len(params) else "", tspell, shape,
                             wa, writes, put)  # fmt: skip
        tmp_used: set[str] = set()
        tally: dict[str, int] = {}
        then: list[str] = []
        for ck, c in callers:
            self._call_edits(ctx, ck, c, idx, fname, shape, P, put, tmp_used, tally, then)
        _merge_insertions(edits)
        if P["sites"].status == ESTABLISHED and P["private"].status == ESTABLISHED:
            pos("sites", f"{len(callers)} call site(s) rewritten")
            if tally.get("local"):
                pos("private", f"{tally['local']} call site(s) pass the address of a private local variable")
            if tally.get("member"):
                pos("private", f"{tally['member']} pass the address of a field of one")
            if tally.get("drop"):
                pos("sites", f"{tally['drop']} discard the value: the caller never reads its variable")
            if tally.get("forward"):
                pos(
                    "private",
                    f"{tally['forward']} forward the caller's own pointer parameter, and every caller up the chain "
                    "passes the address of a private local variable",
                )
        if P["source"].status == ESTABLISHED and edits:
            pos("source", f"{len(edits)} edit(s) in {len({e.file for e in edits.values()})} file(s)")
        return self._result(
            finding, P, positive, sorted(edits.values(), key=lambda e: (e.file, e.start)), hashes, ctx, callers,
            {**shape, "ret": ret, "value_type": tspell, "params": len(params), "then": then,
             "forward": bool(tally.get("forward"))},
        )  # fmt: skip

    # ------------------------------------------------------------------
    @staticmethod
    def _return_spelling(decls: list[dict[str, Any]]) -> str:
        for d in decls:
            t = d.get("type") or ""
            depth = 0
            for i, ch in enumerate(t):
                if ch == "(":
                    if depth == 0:
                        return re.sub(r"\s+", " ", t[:i]).strip()
                    depth += 1
                elif ch == ")":
                    depth -= 1
        return ""

    @staticmethod
    def _return_span(ctx: RecipeContext, d: dict[str, Any]) -> tuple[int, int, str] | str:
        """The return type's tokens before the function name, without storage-class specifiers."""
        lx = ctx.lexed(d["file"])
        toks = lx.tokens
        i = next((j for j, t in enumerate(toks) if t.start == d["offset"]), None)
        if i is None:
            return "the function name is not plain source text"
        j = i - 1
        while j >= 0 and toks[j].text not in (";", "}", "{", ")") and not toks[j].in_directive:
            j -= 1
        span = [t for t in toks[j + 1 : i] if t.text not in SPECIFIERS]
        if not span or any(t.kind not in ("ident", "punct") or t.text in ("(", "[") for t in span):
            return "the return type is not a plain type name"
        if any(t.text.startswith("__attribute") for t in span):
            return "attributes before the return type"
        return span[0].start, span[-1].end, lx.text[span[0].start : span[-1].end]

    @staticmethod
    def _drop_param(ctx: RecipeContext, d: dict[str, Any], idx: int) -> Edit | str:
        ps = d["params"]
        sp = ps[idx].get("span")
        if sp is None or ps[idx].get("in_macro"):
            return "parameter declaration is not plain source text"
        lx = ctx.lexed(sp["file"])
        if len(ps) == 1:
            s, e, new = sp["start"], sp["end"], "void"
        elif idx == len(ps) - 1:
            prev = ps[idx - 1].get("span")
            if prev is None:
                return "previous parameter is not plain source text"
            s, e, new = prev["end"], sp["end"], ""
        else:
            nxt = ps[idx + 1].get("span")
            if nxt is None:
                return "next parameter is not plain source text"
            s, e, new = sp["start"], nxt["start"], ""
        return Edit(sp["file"], s, e, lx.text[s:e], new, f"{d['name']}(): parameter {idx + 1} removed")

    @staticmethod
    def _literal(shape: dict[str, Any], status: str | None, state: int | None, pname: str, flag: str) -> str:
        """The record a return gives back.  ``state`` None: a return a folded null test makes unreachable."""
        parts = [f".status = ({status})"] if status is not None else []
        if state == YES or (state is not None and not shape["optional"]):
            parts.append(f".value = {pname}")
            if shape["optional"]:
                parts.append(f".has_value = {shape['yes']}")
        elif state == MAYBE:
            parts += [f".value = {pname}", f".has_value = {flag}"]
        elif not parts:
            parts.append(f".has_value = {shape['no']}")
        return f"({shape['result']}){{{', '.join(parts)}}}"

    def _body_edits(
        self,
        ctx: RecipeContext,
        finding: dict[str, Any],
        body: Node,
        decl_id: str,
        tspell: str,
        shape: dict[str, Any],
        wa: _Written | None,
        writes: list[Node],
        put: Any,
    ) -> None:
        pname = finding["name"]
        file = finding["file"]
        form, record = shape["form"], shape["record"]
        lx = ctx.lexed(file)
        bs = body.file_span()
        if bs is None:
            put("the function body is not plain source text")
            return
        first = next(iter(body.real_children()), None)
        indent = "    "
        if first is not None and first.begin.file_loc.col:
            indent = " " * max(first.begin.file_loc.col - 1, 1)
        states = {id(r): s for r, s in (wa.returns if wa else [])}
        end_w, end_t = _Written(decl_id).stmt(body, NO)
        # a return reached with the value written on some paths only needs a flag, and reads the variable
        # either way, so it starts at zero
        maybe = shape["optional"] and (MAYBE in states.values() or (not end_t and end_w == MAYBE))
        flag = f"{pname}_written"
        local = f"\n{indent}{tspell} {pname};"
        why = f"'{pname}' becomes a local variable"
        if maybe:
            if ctx.ident_occurrences(flag, [file]):
                put(f"the name {flag} is already used in {file}")
            local = f"\n{indent}{tspell} {pname} = 0;\n{indent}{shape['flag_t']} {flag} = {shape['no']};"
            why += f"; {flag} records whether it was written"
        put(Edit(file, bs[1] + 1, bs[1] + 1, "", local, why))
        for u in finding.get("uses", []):
            d = u.get("detail") or {}
            if u["kind"] == "deref":
                e = ScalarInputRecipe._deref_edit(ctx, file, u, pname)
                if isinstance(e, Edit):
                    e = replace(e, reason=f"'*{pname}' writes the local variable '{pname}'")
                put(e, "source", f"line {u['line']}: ")
            elif u["kind"] == "compare" and d.get("null"):
                s, e = u["site_span"]
                toks = [t.text for t in lx.tokens_in(s, e)]
                ok = (
                    len(toks) == 3
                    and toks[1] in ("==", "!=")
                    and ((toks[0] == pname and toks[2] in NULL_TOKENS) or (toks[2] == pname and toks[0] in NULL_TOKENS))
                )
                if not ok:
                    put(f"line {u['line']}: null comparison '{lx.text[s:e]}' is not plain 'p == NULL'")
                else:
                    truth = toks[1] == "!="
                    why = f"'{lx.text[s:e]}' is always {'true' if truth else 'false'}: the argument is &variable"
                    put(Edit(file, s, e, lx.text[s:e], "1" if truth else "0", why))
            elif u["kind"] == "null-test":
                s = u["offset"]
                e = s + len(pname)
                if lx.text[s:e] != pname:
                    put(f"line {u['line']}: the tested name is not plain source text")
                else:
                    put(Edit(file, s, e, pname, "1", f"'{pname}' is never null: the argument is &variable"))
        if maybe:
            for st in writes:
                b = st.begin
                semi = _semicolon(lx, b.offset) if b.valid and not b.in_macro else None
                line = b.file_loc.line
                if semi is None:
                    put(f"line {line}: the write is not a plain statement")
                    continue
                why = f"line {line}: record that '{pname}' was written"
                if st.parent is not None and st.parent.kind in CONTAINERS:
                    put(Edit(file, semi + 1, semi + 1, "", f" {flag} = {shape['yes']};", why))
                else:  # the only statement of an if or a loop: braces keep both under it
                    put(Edit(file, b.offset, b.offset, "", "{ ", why))
                    put(Edit(file, semi + 1, semi + 1, "", f" {flag} = {shape['yes']}; }}", why))
        # every return is rewritten; one in a branch that a folded null test makes dead never runs and returns
        # no value (the record's value is then zero-initialised, and no uninitialised variable is read)
        whys = {
            YES: "return the value",
            MAYBE: "return the value and whether it was written",
            NO: "nothing was written: return without a value",
            None: "unreachable: return without a value",
        }
        for r in (n for n in body.walk() if n.kind == "ReturnStmt"):
            val = r.child(0)
            sp = _returned_text(lx, r, val)
            if isinstance(sp, str):
                put(f"line {r.begin.file_loc.line}: {sp}")
                continue
            state = states.get(id(r))
            if form == "void":
                if val is not None:
                    put(f"line {r.begin.file_loc.line}: a void function returns a value")
                    continue
                if not record:
                    new = "return 0" if state is None else f"return {pname}"
                else:
                    new = "return " + self._literal(shape, None, state, pname, flag)
                put(Edit(file, sp[0], sp[0] + len("return"), "return", new, whys[state]))
            else:
                if val is None:
                    put(f"line {r.begin.file_loc.line}: returns no status")
                    continue
                txt = lx.text[sp[1] : sp[2]]
                put(Edit(file, sp[1], sp[2], txt, self._literal(shape, txt, state, pname, flag), whys[state]))
        if form == "void" and wa is not None and not end_t:
            # control reaching the closing brace: return the value there too
            value = pname if not record else self._literal(shape, None, end_w, pname, flag)
            close = bs[2] - 1
            line_start = lx.text.rfind("\n", 0, close) + 1
            why = "return at the end: " + whys[end_w]
            if lx.text[line_start:close].strip() == "":
                put(Edit(file, line_start, line_start, "", f"{indent}return {value};\n", why))
            else:
                put(Edit(file, close, close, "", f" return {value}; ", why))

    @staticmethod
    def _locate(ctx: RecipeContext, ck: str, c: dict[str, Any], callee: str, idx: int) -> tuple[Any, Node, Node] | str:
        """The call in its caller's AST: (translation unit, call, argument ``idx``)."""
        caller = ctx.program.funcs.get(ck) or {}
        unit = next((u for u in ctx.inventory["units"] if u["unit_id"] in set(caller.get("units", []))), None)
        tu = ctx.tu(unit) if unit else None
        site = c.get("site") or {}
        if tu is not None:
            for n in tu.all_nodes():
                if n.kind == "CallExpr" and _callee(n) == callee:
                    fl = n.begin.file_loc
                    if fl.offset == site.get("offset") and rel_or_abs(fl.file or "", ctx.root) == site.get("file"):
                        kids = n.real_children()
                        if len(kids) > 1 + idx:
                            return tu, n, kids[1 + idx]
        return "the call was not found in the caller's AST"

    def _target(
        self,
        ctx: RecipeContext,
        ck: str,
        c: dict[str, Any],
        idx: int,
        callee: str,
        depth: int = 0,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> dict[str, Any] | tuple[str, str]:
        """What a call passes for the output: the address of a whole local variable of the caller or of a field
        of one ('.' only), taken nowhere else; or the caller's own pointer parameter, which the caller only
        dereferences, null-tests or forwards here, and for which every caller of the caller passes such a target
        in turn (followed up to MAX_CHAIN calls).  Anything else is a (status, reason) failure."""
        a = c["args"][idx]
        des = a.get("addr_of")
        found = self._locate(ctx, ck, c, callee, idx)
        if isinstance(found, str):
            return UNRESOLVED, found
        tu, call, arg = found
        if des:
            path = des.get("path") or []
            what = f"&{des['name']}{''.join(path)}"
            if (
                des.get("global_")
                or des.get("storage") in ("static", "extern")
                or any(not x.startswith(".") for x in path)
            ):
                return VIOLATED, f"passes {what}, not the address of a local variable of the caller or a field of one"
            inner = _strip(arg)
            base = _base_ref(inner.child(0)) if inner is not None and inner.kind == "UnaryOperator" else None
            vid = (base.raw.get("referencedDecl") or {}).get("id") if base is not None else None
            if vid is None:
                return UNRESOLVED, "the argument's variable was not found in the AST"
            others, read = 0, False
            for r in tu.refs_to(vid):
                if r is base:
                    continue
                others += 1
                esc = _address_taken(r)
                if esc is not None:
                    return VIOLATED, f"the address of {des['name']} is also taken at line {esc.begin.file_loc.line}"
                read = read or _is_read(r)
            span = a.get("addr_of_operand_span")
            text = ctx.lexed(span["file"]).text[span["start"] : span["end"]] if span else des["name"]
            kind = "member" if path else "local"
            return {"kind": kind, "var": text, "name": des["name"], "vid": vid, "tu": tu, "call": call, "arg": arg,
                    "others": others, "read": read}  # fmt: skip
        # a pointer value: the caller's own pointer parameter, passed on unchanged
        n = arg
        while n is not None and (
            n.kind == "ParenExpr" or (n.kind == "ImplicitCastExpr" and n.cast_kind in ("LValueToRValue", "NoOp"))
        ):
            n = n.child(0)
        rd = (n.raw.get("referencedDecl") or {}) if n is not None and n.kind == "DeclRefExpr" else {}
        if rd.get("kind") != "ParmVarDecl":
            return UNRESOLVED, "passes a pointer value, not the address of a local variable of the caller"
        cfile, cname = ck.split("::", 1)
        qname = rd.get("name") or "?"
        via = f"passes {cname}()'s parameter '{qname}'"
        if (ck, qname) in seen or depth >= MAX_CHAIN:
            return UNRESOLVED, f"{via}, forwarded recursively or through more than {MAX_CHAIN} calls"
        qf = next(
            (
                f
                for f in ctx.inventory["findings"]
                if f.get("kind") == "parameter" and f.get("function") == cname and f.get("file") == cfile
                and f.get("name") == qname
            ),
            None,
        )  # fmt: skip
        if qf is None:
            return UNRESOLVED, f"{via}, which has no pointer finding"
        for u in qf.get("uses", []):
            d = u.get("detail") or {}
            if u["kind"] == "deref" and u.get("access") in ("read", "write"):
                continue
            if u["kind"] == "null-test" or (u["kind"] == "compare" and d.get("null")):
                continue
            if u["kind"] == "call-arg" and d.get("callee") == callee and d.get("arg") == idx:
                continue
            use = describe_use_parts(u["kind"], u.get("access"), d)
            return VIOLATED, f"{via}, which {cname}() also uses otherwise (line {u['line']}: {use})"
        csum = ctx.program.funcs.get(ck)
        if csum is None:
            return UNRESOLVED, f"{via}; {cname}() has no analysed definition"
        cdecls = [
            d
            for d in ctx.inventory.get("function_decls", [])
            if d["name"] == cname and (not csum["static"] or d["file"] == cfile)
        ]
        up = ctx.program.callers_of(ck)
        known = Precondition("chain", "")
        ScalarInputRecipe()._check_callers(ctx, cname, csum, cdecls, up, known, lambda *_: None)
        if known.status != ESTABLISHED:
            return known.status, f"{via}, but not every caller of {cname}() is known: {known.evidence[0]}"
        if not up:
            return UNRESOLVED, f"{via}; {cname}() has no analysed caller"
        qidx = qf["param_index"]
        for ck2, c2 in up:
            where = f"{(c2.get('site') or {}).get('file')}:{(c2.get('site') or {}).get('line')}"
            if qidx >= len(c2["args"]):
                return VIOLATED, f"{via}; the call at {where} has too few arguments"
            t = self._target(ctx, ck2, c2, qidx, cname, depth + 1, seen | {(ck, qname)})
            if isinstance(t, tuple):
                return t[0], f"{via}; at {where}, {cname}()'s caller {t[1]}"
        writes_only = not any(u["kind"] == "deref" and u.get("access") == "read" for u in qf.get("uses", []))
        return {"kind": "forward", "var": f"*{qname}", "name": qname, "caller": cname, "then": writes_only,
                "vid": rd.get("id"), "tu": tu, "call": call, "arg": arg, "chain": len(up)}  # fmt: skip

    def _call_edits(
        self,
        ctx: RecipeContext,
        ck: str,
        c: dict[str, Any],
        idx: int,
        fname: str,
        shape: dict[str, Any],
        P: dict[str, Precondition],
        put: Any,
        tmp_used: set[str],
        tally: dict[str, int],
        then: list[str],
    ) -> None:
        form, record, optional, result_name = shape["form"], shape["record"], shape["optional"], shape["result"]
        site = c.get("site") or {}
        where = f"{site.get('file')}:{site.get('line')}: "
        if c.get("in_macro"):
            P["source"].fail(VIOLATED, f"{where}the call is inside a macro expansion")
            return
        if idx >= len(c["args"]):
            P["sites"].fail(VIOLATED, f"{where}the call has too few arguments")
            return
        tgt = self._target(ctx, ck, c, idx, fname)
        if isinstance(tgt, tuple):
            P["private"].fail(tgt[0], where + tgt[1])
            return
        tally[tgt["kind"]] = tally.get(tgt["kind"], 0) + 1
        if tgt["kind"] == "forward" and tgt["then"]:
            then.append(
                f"{tgt['caller']}()'s parameter '{tgt['name']}' is then only written: convert it next (leaf-first)"
            )
        call = tgt["call"]
        # A caller that never reads the variable discards the output.  Assigning it would leave the variable
        # set but never used (an error under -Werror), so the value is dropped and the declaration with it.
        drop: Edit | None = None
        if tgt["kind"] != "forward" and not tgt["read"]:
            from weaver.analysis.functions import has_side_effects

            vd = tgt["tu"].node(tgt["vid"])
            ds = vd.parent if vd is not None else None
            dsp = ds.file_span() if ds is not None and ds.kind == "DeclStmt" else None
            init = vd.real_children() if vd is not None else []
            effects = any(has_side_effects(i) or any("volatile" in (x.qual_type or "") for x in i.walk()) for i in init)
            if tgt["others"] or ds is None or len(ds.real_children()) != 1 or not dsp or effects:
                P["sites"].fail(
                    VIOLATED,
                    f"{where}the caller never reads {tgt['name']}; only a variable declared on its own, with no "
                    "side effects in its initialiser and used nowhere else, can be dropped with the output",
                )
                return
            dtext = ctx.lexed(site["file"]).text
            s, e = dsp[1], dsp[2]
            ls, le = dtext.rfind("\n", 0, s) + 1, dtext.find("\n", e)
            if dtext[ls:s].strip() == "" and le >= 0 and dtext[e:le].strip() == "":
                s, e = ls, le + 1  # the whole line
            why = f"{where}{tgt['name']} is never read: the output is discarded"
            drop = Edit(site["file"], s, e, dtext[s:e], "", why)
        # where the call sits
        top = call
        while top.parent is not None and top.parent.kind == "ParenExpr":
            top = top.parent
        par = top.parent
        kind = "other"
        if par is not None and par.kind in CONTAINERS:
            kind = "stmt"
        elif par is not None and par.kind == "BinaryOperator" and par.opcode == "=" and top.index == 1:
            kind = "assign" if par.parent is not None and par.parent.kind in CONTAINERS else "other"
        elif par is not None and par.kind == "VarDecl" and par.parent is not None and par.parent.kind == "DeclStmt":
            kind = (
                "decl"
                if len(par.parent.real_children()) == 1
                and par.parent.parent is not None
                and par.parent.parent.kind == "CompoundStmt"
                else "other"
            )
        elif par is not None and par.kind == "ReturnStmt":
            kind = "return"
        if form == "void" and kind != "stmt" or kind == "other":
            P["sites"].fail(VIOLATED, f"{where}the call's value is used inside a larger expression")
            return
        # the call without the argument
        cs = top.file_span()
        spans = [x.get("span") for x in c["args"]]
        if cs is None or any(s is None for s in spans):
            P["source"].fail(VIOLATED, f"{where}the call is not plain source text")
            return
        lx = ctx.lexed(site["file"])
        text = lx.text
        keep = [text[s["start"] : s["end"]] for j, s in enumerate(spans) if j != idx]
        head = text[cs[1] : spans[0]["start"]]
        new_call = head + ", ".join(keep) + ")"
        if not head.rstrip().endswith("("):
            P["source"].fail(VIOLATED, f"{where}unexpected call syntax")
            return
        var = tgt["var"]
        line = site.get("line")
        tmp = f"{fname}_r{line}"
        if tmp in tmp_used or ctx.ident_occurrences(tmp, [site["file"]]):
            P["sites"].fail(VIOLATED, f"{where}two calls on one line, or the name {tmp} is taken")
            return
        tmp_used.add(tmp)
        f = site["file"]
        if drop is not None:
            tally["drop"] = tally.get("drop", 0) + 1
            put(drop)
            new = f"(void){new_call}" if kind == "stmt" else f"{new_call}.status"
            why = "discard the value" + ("" if kind == "stmt" else ", keep the status")
            put(Edit(f, cs[1], cs[2], text[cs[1] : cs[2]], new, f"{where}{why}"))
            return
        take = (lambda t: f"if ({t}.has_value) {var} = {t}.value;") if optional else (lambda t: f"{var} = {t}.value;")
        what = "receive the value only when it was written" if optional else "receive the value"
        if kind == "stmt" and not optional:
            new = f"{var} = {new_call}" + (".value" if record else "")
            put(Edit(f, cs[1], cs[2], text[cs[1] : cs[2]], new, f"{where}{what}"))
            return
        if kind == "stmt":
            ss, end = cs, _stmt_end(ctx, f, cs[2])
        else:
            stmt = par if kind == "assign" else par.parent if kind == "decl" else par
            ss = stmt.file_span()
            end = None
            if ss is not None:  # a declaration's range ends at its ';', an expression's before it
                end = ss[2] if text[ss[2] - 1] == ";" else _stmt_end(ctx, f, ss[2])
        if ss is None or end is None:
            P["source"].fail(VIOLATED, f"{where}the statement is not plain source text")
            return
        ind = _indent_of(text, ss[1])
        nl = f"\n{ind}" if ind is not None else " "
        inner = f"\n{ind}    " if ind is not None else " "
        got = f"{result_name} {tmp} = {new_call};"
        if kind == "stmt":
            new = f"{{{inner}{got}{inner}{take(tmp)}{nl}}}"
        elif kind == "assign":
            lhs = par.child(0)
            ls = lhs.file_span() if lhs is not None else None
            base, lhs_s = _base_ref(lhs), _strip(lhs)
            # the status must not land in the output itself: the value would then be stored after it
            itself = (base is not None and (base.raw.get("referencedDecl") or {}).get("id") == tgt["vid"]) or (
                lhs_s is not None and lhs_s.kind == "UnaryOperator" and lhs_s.opcode == "*"
                and _refers(lhs_s.child(0), tgt["vid"])
            )  # fmt: skip
            if ls is None or itself:
                P["sites"].fail(VIOLATED, f"{where}the assigned expression is not plain or is the output itself")
                return
            from weaver.analysis.functions import has_side_effects

            if has_side_effects(lhs):
                P["sites"].fail(VIOLATED, f"{where}the assigned expression has side effects")
                return
            new = f"{{{inner}{got}{inner}{text[ls[1] : ls[2]]} = {tmp}.status;{inner}{take(tmp)}{nl}}}"
        elif kind == "decl":
            new = f"{got}{nl}{text[ss[1] : cs[1]]}{tmp}.status{text[cs[2] : end - 1]};{nl}{take(tmp)}"
        else:
            new = f"{{{inner}{got}{inner}{take(tmp)}{inner}return {tmp}.status;{nl}}}"
        what += "" if kind == "stmt" else ", and the status"
        put(Edit(f, ss[1], end, text[ss[1] : end], new, f"{where}{what}"))

    def _result(
        self,
        finding: dict[str, Any],
        P: dict[str, Precondition],
        positive: dict[str, list[str]],
        edits: list[Edit],
        hashes: dict[str, str],
        ctx: RecipeContext,
        callers: list[Any],
        info: dict[str, Any] | None,
    ) -> RecipeResult:
        for k, p in P.items():
            if p.status == ESTABLISHED:
                for m in positive.get(k, []):
                    p.ok(m)
                if not p.evidence:
                    p.ok("checked; no counter-evidence")
        fname, pname = finding["function"], finding.get("name") or "?"
        info = info or {}
        record, optional = bool(info.get("record")), bool(info.get("optional"))
        if not record:
            form = "The function now returns the value, and each call assigns it to the variable."
        else:
            form = f"The function now returns {info.get('result')}" + (
                ": the status it returned and the value; each call assigns the status where it went before and "
                if info.get("form") == "status"
                else ": the value; each call assigns "
            ) + ("the value to the variable only when has_value says it was written." if optional
                 else "the value to the variable.")  # fmt: skip
        why = next((e for e in P["written"].evidence), "definitely-assigned analysis")
        written = (
            f"It writes it before some returns and not others ({why})."
            if optional
            else f"It writes it on every path before every return ({why})."
        )
        reach = (
            " (directly, or through callers that only pass on their own pointer parameter, each of whose "
            "callers does the same)"
            if info.get("forward")
            else ""
        )
        notes = [f"form: {info.get('form')}"] if info else []
        notes += ["output: optional (has_value)"] if optional else []
        notes += [f"then: {x}" for x in info.get("then", [])]
        return RecipeResult(
            recipe=self.id,
            recipe_version=self.version,
            finding_id=finding["id"],
            preconditions=list(P.values()),
            edits=edits,
            file_hashes={f: h for f, h in hashes.items() if any(e.file == f for e in edits)} or hashes,
            capabilities_required=["value_records"] if record else [],
            preservation_argument=PRESERVATION.format(f=fname, p=pname, written=written, form=form, reach=reach),
            validation_plan=[
                "compile every unit that includes an edited file, in every profile, with its production command",
                f"mechanical re-check: {fname}() has one parameter fewer in every declaration and call, and returns "
                + (f"{info.get('result')}" if record else "the value"),
                "run the configured tests and differential comparisons against the unpatched baseline",
            ],
            affected={
                "objects": [pname],
                "files": sorted({e.file for e in edits} | set(hashes)),
                "functions": sorted({fname} | {ck.split("::")[1] for ck, _ in callers}),
                "interfaces": [fname],
            },
            units=[],
            notes=notes,
            recheck={
                "function": fname,
                "param_name": pname,
                "params": info.get("params"),
                "form": info.get("form"),
                "record": record,
                "result": info.get("result"),
            },
        )

    # ------------------------------------------------------------------
    def recheck(
        self, result: dict[str, Any], tu: TranslationUnit, offset_maps: dict[str, OffsetMap], root: str
    ) -> list[str] | None:
        rc = result["recheck"]
        fname, n = rc["function"], rc.get("params")
        problems: list[str] = []
        relevant = False
        for top in tu.top:
            if top.kind == "FunctionDecl" and top.name == fname:
                relevant = True
                params = [c for c in top.real_children() if c.kind == "ParmVarDecl"]
                if n is not None and len(params) != n - 1:
                    problems.append(f"{fname}() declaration has {len(params)} parameter(s), expected {n - 1}")
                if any(p.name == rc["param_name"] for p in params):
                    problems.append(f"{fname}() still has parameter '{rc['param_name']}'")
                rt = (top.qual_type or "").split("(")[0].strip()
                record = rc.get("record", rc.get("form") == "status")
                if not record and rt == "void":
                    problems.append(f"{fname}() still returns void")
                if record and rt != rc.get("result"):
                    problems.append(f"{fname}() returns '{rt}', not {rc.get('result')}")
        for node in tu.all_nodes():
            if node.kind == "CallExpr" and _callee(node) == fname:
                relevant = True
                if n is not None and len(node.real_children()) - 1 != n - 1:
                    problems.append(
                        f"line {node.begin.file_loc.line}: call passes {len(node.real_children()) - 1} argument(s)"
                    )
        return problems if relevant else None
