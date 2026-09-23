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

Why the change preserves behaviour:

* every call passes ``&x`` where ``x`` is a whole automatic variable of the
  caller whose address is taken nowhere else, so no code other than the callee,
  through this parameter, can read or write ``x`` during the call (the same
  pointer-provenance argument as the task model's), including other threads;
* inside the callee the parameter is only written, each write a whole
  statement, never read, passed, compared (except with a null pointer
  constant) or reassigned, so it is equivalent to a local variable whose final
  value the caller receives;
* the value is written on every path before every return (a definitely-assigned
  analysis over the AST), so the caller receives the same value it would have
  found in ``x``;
* a null test of the parameter can never succeed, since ``&x`` is never null;
  it is folded to its constant result;
* every caller is known (the complete-caller checks of ``scalar-input``), and
  every call sits where the result can be assigned: a statement of its own, or
  (for a status) an assignment, a declaration or a return.
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

PRESERVATION = (
    "Every call passes the address of a local variable of the caller whose address is taken nowhere else, so "
    "during the call nothing but `{f}()`, through `{p}`, can read or write it. `{f}()` only writes `*{p}`, each "
    "write a statement of its own, and writes it on every path before every return ({why}), so a local variable "
    "holding the same values ends with the value the caller would have found. {form} Null tests of `{p}` can "
    "never succeed (the argument is always `&variable`) and are folded to their constant result. The "
    "function's address is never taken and every reference to its name is an analysed call or declaration, so "
    "every caller changes together with its definition and prototypes."
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


class _Written:
    """Definitely-written analysis: is ``*p`` written on every path before every return?"""

    def __init__(self, decl_id: str):
        self.decl_id = decl_id
        self.problems: list[str] = []
        self.returns: list[Node] = []

    def stmt(self, n: Node | None, w: bool) -> tuple[bool, bool]:
        """(written afterwards, control cannot fall through)."""
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
            self.returns.append(n)
            if not w:
                self.problems.append(f"line {n.begin.file_loc.line}: returns before the output is written")
            return w, True
        if _is_write(n, self.decl_id):
            return True, False
        if k == "IfStmt":
            cond, then, other = _if_parts(n)
            f = _fold(cond, self.decl_id)
            wt, tt = self.stmt(then, w) if f is not False else (w, False)
            we, te = self.stmt(other, w) if f is not True else (w, False)
            if f is True:
                return wt, tt
            if f is False:
                return we, te
            if tt and te:
                return w, True
            return (we if tt else wt if te else wt and we), False
        if k in ("WhileStmt", "ForStmt", "DoStmt", "SwitchStmt"):
            for c in n.real_children():  # returns inside are checked with the state on entry
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


def _stmt_end(ctx: RecipeContext, file: str, end: int) -> int | None:
    """Offset just past the ';' that ends a statement whose expression ends at ``end``."""
    lx = ctx.lexed(file)
    nxt = next((t for t in lx.tokens if t.start >= end), None)
    return nxt.end if nxt is not None and nxt.text == ";" else None


class OutputParamRecipe(Recipe):
    id = "output-param"
    version = "1"
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
                "OP.written-on-every-path", "The target is written on every path before every return"
            ),
            "private": Precondition(
                "OP.private-target",
                "Every call passes the address of a whole local variable of the caller whose address is taken "
                "nowhere else, so nothing else can access it during the call",
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
                "The function returns nothing, or a scalar status that can travel in a result record declared "
                "next to its only prototype",
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
                        if st.parent is None or st.parent.kind not in CONTAINERS | {
                            "IfStmt",
                            "WhileStmt",
                            "ForStmt",
                            "DoStmt",
                        }:
                            P["uses"].fail(
                                VIOLATED, f"line {st.begin.file_loc.line}: the write is part of a larger expression"
                            )
            wa = _Written(decl_id)
            end_w, end_t = wa.stmt(body, False)
            for pr in wa.problems:
                P["written"].fail(VIOLATED if "returns before" in pr else UNRESOLVED, pr)
            if not end_t and not end_w:
                P["written"].fail(VIOLATED, "control can reach the end of the function before the output is written")
            if P["written"].status == ESTABLISHED:
                pos(
                    "written", f"written before each of {len(wa.returns)} return(s)" + ("" if end_t else " and the end")
                )
        if P["uses"].status == ESTABLISHED:
            n_w = sum(1 for u in uses if u["kind"] == "deref")
            n_t = len(uses) - n_w
            pos("uses", f"{n_w} write(s)" + (f", {n_t} null test(s) that fold to a constant" if n_t else ""))

        # -- return type: void or a scalar status -----------------------------------------------------
        ret = (fsum and self._return_spelling(decls)) or ""
        form = "void" if ret == "void" else "status"
        result_name = f"{fname}_result_t"
        if form == "status":
            rt = resolve_typedefs(safe_parse(ret), {})
            if not ret or "*" in ret or rt is None or rt.kind != "base" or ret.startswith(("struct", "union")):
                P["result"].fail(VIOLATED, f"returns '{ret or '?'}', not a scalar status")
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
                pos("result", f"returns '{ret}'; status and value travel in {result_name} (needs value records)")
        else:
            pos("result", "returns nothing: the value becomes the return value")

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
            if form == "void":
                put(Edit(d["file"], s0, e0, text, tspell, f"{fname}() returns the value"))
            else:
                ret_starts.append((d, s0, e0, text))
        if not tspell and P["type"].status == ESTABLISHED:
            P["type"].fail(VIOLATED, "the parameter's type could not be read from its declaration")
        # the result record goes before the first declaration in the header (or, without one, in the source)
        home = sorted(ret_starts, key=lambda x: (x[0]["file"] == def_file, x[1]))
        typedef = (
            f"/* {fname}(): its status and the value it used to write through '{pname}' */\n"
            f"typedef struct\n{{\n    {ret} status;\n    {tspell} value;\n}} {result_name};\n\n"
        )
        for i, (d, s0, e0, text) in enumerate(home):
            why = f"{fname}() returns its status and the value"
            if i == 0:
                # the record goes on the lines before the first declaration in the header (else the source),
                # in the same edit as that declaration's return type
                src = ctx.lexed(d["file"]).text
                ls = _above_comment(src, src.rfind("\n", 0, s0) + 1)
                put(Edit(d["file"], ls, e0, src[ls:e0], typedef + src[ls:s0] + result_name, why + "; result record"))
            else:
                put(Edit(d["file"], s0, e0, text, result_name, why))
        if fn is not None and body is not None and tspell:
            self._body_edits(ctx, finding, fn, body, params[idx].id if idx < len(params) else "", tspell, form,
                             result_name, wa, put)  # fmt: skip
        tmp_used: set[str] = set()
        for ck, c in callers:
            self._call_edits(ctx, ck, c, idx, fname, form, result_name, P, put, tmp_used)
        if P["sites"].status == ESTABLISHED and P["private"].status == ESTABLISHED:
            pos("sites", f"{len(callers)} call site(s) rewritten")
            pos("private", f"{len(callers)} call site(s) pass the address of a private local variable")
        if P["source"].status == ESTABLISHED and edits:
            pos("source", f"{len(edits)} edit(s) in {len({e.file for e in edits.values()})} file(s)")
        return self._result(
            finding, P, positive, sorted(edits.values(), key=lambda e: (e.file, e.start)), hashes, ctx, callers,
            {"form": form, "ret": ret, "value_type": tspell, "result": result_name, "params": len(params)},
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

    def _body_edits(
        self,
        ctx: RecipeContext,
        finding: dict[str, Any],
        fn: Node,
        body: Node,
        decl_id: str,
        tspell: str,
        form: str,
        result_name: str,
        wa: _Written | None,
        put: Any,
    ) -> None:
        pname = finding["name"]
        file = finding["file"]
        lx = ctx.lexed(file)
        bs = body.file_span()
        if bs is None:
            put("the function body is not plain source text")
            return
        first = next(iter(body.real_children()), None)
        indent = "    "
        if first is not None and first.begin.file_loc.col:
            indent = " " * max(first.begin.file_loc.col - 1, 1)
        put(Edit(file, bs[1] + 1, bs[1] + 1, "", f"\n{indent}{tspell} {pname};", f"'{pname}' becomes a local variable"))
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
        # every return is rewritten; one in a branch that a folded null test makes dead never runs and returns
        # no value (the record's value is then zero-initialised, and no uninitialised variable is read)
        live = {id(r) for r in (wa.returns if wa else [])}
        for r in (n for n in body.walk() if n.kind == "ReturnStmt"):
            val = r.child(0)
            sp = _returned_text(lx, r, val)
            if isinstance(sp, str):
                put(f"line {r.begin.file_loc.line}: {sp}")
                continue
            dead = id(r) not in live
            if form == "void":
                if val is not None:
                    put(f"line {r.begin.file_loc.line}: a void function returns a value")
                    continue
                new = "return 0" if dead else f"return {pname}"
                put(Edit(file, sp[0], sp[0] + len("return"), "return", new, "return the value"))
            else:
                if val is None:
                    put(f"line {r.begin.file_loc.line}: returns no status")
                    continue
                txt = lx.text[sp[1] : sp[2]]
                value = "" if dead else f", .value = {pname}"
                why = "unreachable: return the status only" if dead else "return the status and the value"
                put(Edit(file, sp[1], sp[2], txt, f"({result_name}){{.status = ({txt}){value}}}", why))
        if form == "void" and wa is not None:
            # control reaching the closing brace: return the value there too
            _, t = _Written(decl_id).stmt(body, True)
            if not t:
                close = bs[2] - 1
                line_start = lx.text.rfind("\n", 0, close) + 1
                if lx.text[line_start:close].strip() == "":
                    put(
                        Edit(
                            file,
                            line_start,
                            line_start,
                            "",
                            f"{indent}return {pname};\n",
                            "return the value at the end",
                        )
                    )
                else:
                    put(Edit(file, close, close, "", f" return {pname}; ", "return the value at the end"))

    def _call_edits(
        self,
        ctx: RecipeContext,
        ck: str,
        c: dict[str, Any],
        idx: int,
        fname: str,
        form: str,
        result_name: str,
        P: dict[str, Precondition],
        put: Any,
        tmp_used: set[str],
    ) -> None:
        site = c.get("site") or {}
        where = f"{site.get('file')}:{site.get('line')}: "
        if c.get("in_macro"):
            P["source"].fail(VIOLATED, f"{where}the call is inside a macro expansion")
            return
        if idx >= len(c["args"]):
            P["sites"].fail(VIOLATED, f"{where}the call has too few arguments")
            return
        a = c["args"][idx]
        des = a.get("addr_of")
        if not des or des.get("path") or des.get("global_") or des.get("storage") in ("static", "extern"):
            what = f"&{des['name']}{''.join(des.get('path') or [])}" if des else "a pointer value"
            P["private"].fail(
                VIOLATED if des else UNRESOLVED,
                f"{where}passes {what}, not the address of a whole local variable of the caller",
            )
            return
        caller = ctx.program.funcs.get(ck) or {}
        unit = next((u for u in ctx.inventory["units"] if u["unit_id"] in set(caller.get("units", []))), None)
        tu = ctx.tu(unit) if unit else None
        call = None
        if tu is not None:
            for n in tu.all_nodes():
                if n.kind == "CallExpr" and _callee(n) == fname:
                    fl = n.begin.file_loc
                    if fl.offset == site.get("offset") and rel_or_abs(fl.file or "", ctx.root) == site.get("file"):
                        call = n
                        break
        if call is None:
            P["sites"].fail(UNRESOLVED, f"{where}the call was not found in the caller's AST")
            return
        arg = _strip(call.real_children()[1 + idx] if len(call.real_children()) > 1 + idx else None)
        ref = _strip(arg.child(0)) if arg is not None and arg.kind == "UnaryOperator" and arg.opcode == "&" else None
        vid = (ref.raw.get("referencedDecl") or {}).get("id") if ref is not None and ref.kind == "DeclRefExpr" else None
        if vid is None:
            P["private"].fail(UNRESOLVED, f"{where}the argument's variable was not found in the AST")
            return
        others, read = 0, False
        for r in tu.refs_to(vid):
            if r is ref:
                continue
            others += 1
            q = r.parent
            while q is not None and q.kind == "ParenExpr":
                q = q.parent
            read = read or (q is not None and q.kind == "ImplicitCastExpr" and q.cast_kind == "LValueToRValue")
            p = r.parent
            while p is not None and p.kind in ("ParenExpr", "ImplicitCastExpr"):
                p = p.parent
            if p is not None and p.kind == "UnaryOperator" and p.opcode == "&" and p is not arg:
                P["private"].fail(
                    VIOLATED, f"{where}the address of {des['name']} is also taken at line {p.begin.file_loc.line}"
                )
                return
        # A caller that never reads the variable discards the output.  Assigning it would leave the variable
        # set but never used (an error under -Werror), so the value is dropped and the declaration with it.
        drop: Edit | None = None
        if not read:
            from weaver.analysis.functions import has_side_effects

            vd = tu.node(vid)
            ds = vd.parent if vd is not None else None
            dsp = ds.file_span() if ds is not None and ds.kind == "DeclStmt" else None
            init = vd.real_children() if vd is not None else []
            effects = any(has_side_effects(i) or any("volatile" in (x.qual_type or "") for x in i.walk()) for i in init)
            if others or ds is None or len(ds.real_children()) != 1 or not dsp or effects:
                P["sites"].fail(
                    VIOLATED,
                    f"{where}the caller never reads {des['name']}; only a variable declared on its own, with no "
                    "side effects in its initialiser and used nowhere else, can be dropped with the output",
                )
                return
            dtext = ctx.lexed(site["file"]).text
            s, e = dsp[1], dsp[2]
            ls, le = dtext.rfind("\n", 0, s) + 1, dtext.find("\n", e)
            if dtext[ls:s].strip() == "" and le >= 0 and dtext[e:le].strip() == "":
                s, e = ls, le + 1  # the whole line
            why = f"{where}{des['name']} is never read: the output is discarded"
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
        arg_sp = a.get("span")
        spans = [x.get("span") for x in c["args"]]
        if cs is None or arg_sp is None or any(s is None for s in spans):
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
        op = a.get("addr_of_operand_span") or {}
        var = text[op["start"] : op["end"]] if op else des["name"]
        line = site.get("line")
        tmp = f"{fname}_r{line}"
        if tmp in tmp_used or ctx.ident_occurrences(tmp, [site["file"]]):
            P["sites"].fail(VIOLATED, f"{where}two calls on one line, or the name {tmp} is taken")
            return
        tmp_used.add(tmp)
        f = site["file"]
        if drop is not None:
            put(drop)
            new = f"(void){new_call}" if kind == "stmt" else f"{new_call}.status"
            why = "discard the value" + ("" if kind == "stmt" else ", keep the status")
            put(Edit(f, cs[1], cs[2], text[cs[1] : cs[2]], new, f"{where}{why}"))
            return
        if kind == "stmt":
            new = f"{var} = {new_call}" + (".value" if form == "status" else "")
            put(Edit(f, cs[1], cs[2], text[cs[1] : cs[2]], new, f"{where}receive the value"))
            return
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
        if kind == "assign":
            lhs = par.child(0)
            ls = lhs.file_span() if lhs is not None else None
            if ls is None or _refers(lhs, vid):
                P["sites"].fail(VIOLATED, f"{where}the assigned expression is not plain or is the output itself")
                return
            from weaver.analysis.functions import has_side_effects

            if has_side_effects(lhs):
                P["sites"].fail(VIOLATED, f"{where}the assigned expression has side effects")
                return
            lhs_t = text[ls[1] : ls[2]]
            new = (
                f"{{{inner}{result_name} {tmp} = {new_call};{inner}{lhs_t} = {tmp}.status;{inner}"
                f"{var} = {tmp}.value;{nl}}}"
            )
        elif kind == "decl":
            init_start = cs[1]
            new = (
                f"{result_name} {tmp} = {new_call};{nl}{text[ss[1] : init_start]}{tmp}.status"
                f"{text[cs[2] : end - 1]};{nl}{var} = {tmp}.value;"
            )
        else:
            new = (
                f"{{{inner}{result_name} {tmp} = {new_call};{inner}{var} = {tmp}.value;{inner}"
                f"return {tmp}.status;{nl}}}"
            )
        put(Edit(f, ss[1], end, text[ss[1] : end], new, f"{where}receive the status and the value"))

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
        status = info.get("form") == "status"
        form = (
            f"The function now returns {info.get('result')}: the status it returned and the value; each call "
            "assigns both, the status where it went before and the value to the variable."
            if status
            else "The function now returns the value, and each call assigns it to the variable."
        )
        why = next((e for e in P["written"].evidence), "definitely-assigned analysis")
        return RecipeResult(
            recipe=self.id,
            recipe_version=self.version,
            finding_id=finding["id"],
            preconditions=list(P.values()),
            edits=edits,
            file_hashes={f: h for f, h in hashes.items() if any(e.file == f for e in edits)} or hashes,
            capabilities_required=["value_records"] if status else [],
            preservation_argument=PRESERVATION.format(f=fname, p=pname, why=why, form=form),
            validation_plan=[
                "compile every unit that includes an edited file, in every profile, with its production command",
                f"mechanical re-check: {fname}() has one parameter fewer in every declaration and call, and returns "
                + (f"{info.get('result')}" if status else "the value"),
                "run the configured tests and differential comparisons against the unpatched baseline",
            ],
            affected={
                "objects": [pname],
                "files": sorted({e.file for e in edits} | set(hashes)),
                "functions": sorted({fname} | {ck.split("::")[1] for ck, _ in callers}),
                "interfaces": [fname],
            },
            units=[],
            notes=[f"form: {info.get('form')}"] if info else [],
            recheck={
                "function": fname,
                "param_name": pname,
                "params": info.get("params"),
                "form": info.get("form"),
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
                if rc.get("form") == "void" and rt == "void":
                    problems.append(f"{fname}() still returns void")
                if rc.get("form") == "status" and rt != rc.get("result"):
                    problems.append(f"{fname}() returns '{rt}', not {rc.get('result')}")
        for node in tu.all_nodes():
            if node.kind == "CallExpr" and _callee(node) == fname:
                relevant = True
                if n is not None and len(node.real_children()) - 1 != n - 1:
                    problems.append(
                        f"line {node.begin.file_loc.line}: call passes {len(node.real_children()) - 1} argument(s)"
                    )
        return problems if relevant else None
