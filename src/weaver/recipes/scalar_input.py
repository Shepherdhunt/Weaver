"""Recipe ``scalar-input``: a read-only scalar input parameter becomes a value parameter.

Pointer-tracker plan §4, second row: *Read-only scalar input -> value
parameter.  A snapshot has the same behavior as all original reads; const
alone is insufficient.*

    int scale(const int *factor, int x)       int scale(const int factor, int x)
    { return x * *factor; }            ==>    { return x * factor; }
    ... scale(&k, 7) ...                      ... scale(k, 7) ...

Why a snapshot can replace every read: the callee reads the target on every
path (so evaluating it at the call site reads the same object, which is valid),
nothing the call can execute writes any object the parameter may point to
(interprocedural may-modify over AST summaries, SVF points-to evidence and
reviewed external models), nothing else runs concurrently with the call
(declared contract), and no other argument at any call site has side effects
(so reading the target during argument evaluation cannot be reordered against
them).  The function's complete caller set must be known: its address is never
taken, every textual reference is an analysed call or declaration, and every
linked object was analysed.  All declarations, the body and every call site
change together in one transaction.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from weaver.analysis.uses import describe_use_parts
from weaver.flow.program import may_modify
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
from weaver.rewrite import Edit, OffsetMap
from weaver.util import rel_or_abs

SCALAR_WORDS = {
    "char",
    "short",
    "int",
    "long",
    "signed",
    "unsigned",
    "float",
    "double",
    "_Bool",
    "bool",
    "_Complex",
    "__int128",
    "_Float16",
    "__fp16",
}
EXITS = {"ReturnStmt", "GotoStmt", "IndirectGotoStmt", "BreakStmt", "ContinueStmt"}
NORETURN = {"exit", "_Exit", "abort", "longjmp", "siglongjmp", "__builtin_unreachable", "__builtin_trap"}
CONCURRENCY_OK = {"single-threaded", "none", "no-shared-writes"}

PRESERVATION = (
    "`{f}()` reads `*{p}` on every path and never writes through it, compares, stores, passes or returns "
    "it, so the function depends only on the value of the object `{p}` designates.  No code a call to "
    "`{f}()` can execute writes any object `{p}` may point to ({mod}), and the preservation contract "
    "declares no concurrent writers.  Therefore the object's value at the call equals its value at every "
    "read inside `{f}()`, and passing that value is equivalent.  Every call site evaluates its argument "
    "exactly as before, then reads the object; no other argument has side effects, so the read cannot be "
    "reordered against them.  The function's address is never taken and every reference to its name is an "
    "analysed call or declaration, so all callers change together with its definition and prototypes."
)


def _is_scalar(t: Any) -> bool:
    if t is None or t.kind != "base":
        return False
    words = t.name.replace("*", " ").split()
    if t.name.startswith("enum "):
        return True
    return bool(words) and all(w in SCALAR_WORDS for w in words)


def _unconditional_read(fn: Node, param: Node, tu: TranslationUnit) -> tuple[bool, str]:
    """Is ``*param`` read on every path from entry (before any possible exit)?"""
    from weaver.analysis.uses import classify_ref

    body = next((c for c in fn.real_children() if c.kind == "CompoundStmt"), None)
    if body is None:
        return False, "no body"
    for r in tu.refs_to(param.id):
        use = classify_ref(r)
        if use.kind != "deref" or use.access != "read":
            continue
        ok = True
        child: Node = use.site
        anc = child.parent
        while anc is not None and anc is not fn:
            k = anc.kind
            idx = child.index
            if k == "CompoundStmt":
                for sib in anc.real_children():
                    if sib is child:
                        break
                    if any(x.kind in EXITS for x in sib.walk()) or any(
                        x.kind == "CallExpr" and _callee(x) in NORETURN for x in sib.walk()
                    ):
                        ok = False
                        break
            elif k in ("IfStmt", "SwitchStmt", "WhileStmt", "ConditionalOperator", "BinaryConditionalOperator"):
                ok = ok and idx == 0
            elif k == "ForStmt":
                ok = ok and idx in (0, 2)
            elif k == "BinaryOperator" and anc.opcode in ("&&", "||"):
                ok = ok and idx == 0
            elif k in ("CaseStmt", "DefaultStmt", "UnaryExprOrTypeTraitExpr"):
                ok = False
            if not ok:
                break
            child, anc = anc, anc.parent
        if ok:
            return True, f"line {r.begin.file_loc.line} reads *{param.name} on every path from entry"
    return False, "no read of the target is executed on every path from entry"


def _callee(call: Node) -> str | None:
    c = call.child(0)
    while c is not None and c.kind in ("ImplicitCastExpr", "ParenExpr"):
        c = c.child(0)
    if c is not None and c.kind == "DeclRefExpr":
        return (c.raw.get("referencedDecl") or {}).get("name")
    return None


class ScalarInputRecipe(Recipe):
    id = "scalar-input"
    version = "1"
    title = "Pass a read-only scalar input by value instead of by pointer"
    finding_kinds = ("parameter",)

    def applicable(self, finding: dict[str, Any]) -> bool:
        return finding.get("kind") == "parameter" and not finding.get("function_pointer")

    # ------------------------------------------------------------------
    def evaluate(self, ctx: RecipeContext, finding: dict[str, Any]) -> RecipeResult:
        inv = ctx.inventory
        prog = ctx.program
        fname: str = finding["function"]
        idx: int = finding["param_index"]
        def_file: str = finding["file"]
        fkey = f"{def_file}::{fname}"
        fsum = prog.funcs.get(fkey)

        P = {
            "current": Precondition("SI.evidence-current", "Evidence describes the current source revision"),
            "program": Precondition(
                "SI.whole-program", "Every unit of every configured profile was analysed with adequate evidence"
            ),
            "type": Precondition(
                "SI.parameter-type",
                "The parameter points to a non-volatile, non-atomic scalar and is spelled as a plain pointer "
                "declarator in every declaration",
            ),
            "uses": Precondition(
                "SI.read-only-uses",
                "Every use reads the target through the pointer; the pointer is never written through, "
                "null-tested, compared, reassigned, copied, passed, returned or cast",
            ),
            "always": Precondition("SI.unconditional-read", "The target is read on every path through the function"),
            "mod": Precondition(
                "SI.no-modification-during-call",
                "Nothing a call can execute writes any object the parameter may point to",
            ),
            "concurrency": Precondition(
                "SI.no-concurrent-writers", "The preservation contract declares no concurrent writers"
            ),
            "callers": Precondition(
                "SI.complete-callers",
                "Every caller is known: the address is never taken, every reference is an analysed call or "
                "declaration, every linked object was analysed, and the interface is not frozen",
            ),
            "sites": Precondition(
                "SI.call-sites",
                "Every call site passes a non-null pointer expression and has no other argument with side effects",
            ),
            "source": Precondition("SI.edits-in-source", "Every edited range is plain source text"),
        }
        positive: dict[str, list[str]] = {}

        def pos(k: str, msg: str) -> None:
            positive.setdefault(k, []).append(msg)

        if fsum is None:
            P["program"].fail(UNRESOLVED, f"no analysed definition of {fname}() in {def_file}")
            return self._result(finding, P, positive, [], {}, ctx, {}, [])

        # -- decls, callers, affected files ------------------------------------
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
                P["current"].fail(
                    VIOLATED,
                    f"{f} changed since the inventory was built",
                    "re-run 'weaver refresh' (collect + inventory) and 'weaver flow'",
                )
        if P["current"].status == ESTABLISHED:
            pos("current", f"{len(files)} affected file(s) match the analysed revision")

        progs = ctx.programs_for_units(list(fsum.get("units", [])))
        for status, msg in ctx.whole_program(list(fsum.get("units", []))):
            P["program"].fail(status, msg, "collect every unit and run 'weaver inventory' (and fidelity checks)")
        if P["program"].status == ESTABLISHED:
            if progs:
                n = len({u for us in progs.values() for u in us})
                pos("program", f"all {n} unit(s) of program(s) {', '.join(sorted(progs))} analysed")
            else:
                pos("program", f"all {len(inv['units'])} unit(s) of {len(ctx.project.profiles)} profile(s) analysed")

        # -- parameter type -------------------------------------------------------
        pt = resolve_typedefs(safe_parse(finding.get("canonical_type") or finding.get("type")), {})
        if pt is None or pt.kind != "pointer":
            P["type"].fail(UNRESOLVED, f"type {finding.get('type')!r} not recognised as a pointer")
        else:
            pointee = pt.inner
            if finding.get("typedef_hidden"):
                P["type"].fail(VIOLATED, "the pointer is hidden behind a typedef; its declarator cannot be edited")
            if pointee is not None and pointee.kind == "base" and not _is_scalar(pointee):
                P["type"].fail(VIOLATED, f"pointee '{pointee.spell()}' is not a scalar type")
            elif pointee is not None and pointee.kind != "base":
                P["type"].fail(VIOLATED, f"pointee '{pointee.spell()}' is not a scalar type")
            if pointee is not None and ("volatile" in pointee.quals or pointee.atomic):
                P["type"].fail(VIOLATED, "accesses through the pointer are volatile or atomic")
            if "volatile" in pt.quals:
                P["type"].fail(VIOLATED, "the pointer object itself is volatile")
            if P["type"].status == ESTABLISHED:
                pos("type", f"'{finding.get('type')}': pointer to scalar '{pointee.spell() if pointee else '?'}'")

        # -- uses inside the function ----------------------------------------------
        uses = finding.get("uses", [])
        for u in uses:
            ok = u["kind"] == "deref" and u.get("access") in ("read", "unevaluated")
            if not ok:
                P["uses"].fail(VIOLATED, f"line {u['line']}: {self._describe(u)}")
            elif u.get("in_macro"):
                P["source"].fail(VIOLATED, f"line {u['line']}: use inside a macro expansion")
        if not any(u["kind"] == "deref" and u.get("access") == "read" for u in uses):
            P["uses"].fail(VIOLATED, "the target is never read (an unused parameter needs a different recipe)")
        if P["uses"].status == ESTABLISHED:
            pos("uses", f"{len(uses)} use(s), all reads: " + ", ".join(f"line {u['line']}" for u in uses))

        # -- unconditional read (structural, per configuration) ------------------------
        defining_units = [u for u in inv["units"] if u["unit_id"] in set(fsum.get("units", []))]
        for u in defining_units:
            tu = ctx.tu(u)
            if tu is None:
                P["always"].fail(UNRESOLVED, f"{u['profile']}: no AST for {u['file']}")
                continue
            fn = next(
                (
                    f
                    for f in tu.functions()
                    if f.name == fname and rel_or_abs(f.loc.file_loc.file or "", ctx.root) == def_file
                ),
                None,
            )
            params = [c for c in fn.real_children() if c.kind == "ParmVarDecl"] if fn else []
            if fn is None or idx >= len(params):
                P["always"].fail(UNRESOLVED, f"{u['profile']}: definition not found in the AST")
                continue
            ok, why = _unconditional_read(fn, params[idx], tu)
            if ok:
                pos("always", f"{u['profile']}: {why}")
            else:
                P["always"].fail(
                    VIOLATED,
                    f"{u['profile']}: {why}; reading at the call site would add a read "
                    "the original program does not perform",
                )

        # -- may-modify (flow) --------------------------------------------------
        flows = ctx.flows_for_units(list(fsum.get("units", [])))
        if not flows:  # linked into no known program: fall back to each profile's only program
            from weaver.flow.evidence import load_flow

            flows = {p: load_flow(ctx.project, p, inv) for p in sorted({u["profile"] for u in defining_units})}
        if not ctx.project.flow.uses("svf"):
            flows = {k: None for k in flows}
        arg_designators: list[dict[str, Any]] | None = []
        for _, c in callers:
            a = c["args"][idx] if idx < len(c["args"]) else None
            if a is None or not a.get("addr_of"):
                arg_designators = None
                break
            arg_designators.append(a["addr_of"])
        mod = may_modify(prog, fkey, idx, flows, arg_designators)
        mod_desc = "no write found"
        svf_present = any(fe is not None for fe in flows.values())
        gcc = _gcc_verdicts(ctx, fname, idx, list(fsum.get("units", [])))
        gcc_yes = [g for g in gcc if g["status"] == "yes"]
        gcc_status = (
            None if not gcc else "yes" if gcc_yes else "unknown" if any(g["status"] == "unknown" for g in gcc) else "no"
        )
        # Backends that answered: SVF (through may_modify with flow evidence) and GCC.  Without SVF
        # evidence, may_modify's own answer (models and call-site designators) stands in for it.
        verdicts = {"svf" if svf_present else "models": mod.status}
        if gcc_status is not None:
            verdicts["gcc"] = gcc_status
            if not svf_present and mod.status == "unknown":
                del verdicts["models"]  # GCC's whole-image solution replaces designator-only reasoning
        agree_any = ctx.project.flow.agreement == "any"
        combined = (
            "yes"
            if "yes" in verdicts.values()
            else "no"
            if all(v == "no" for v in verdicts.values()) or (agree_any and "no" in verdicts.values())
            else "unknown"
        )
        if mod.status == "yes":
            for r in mod.reasons:
                if r["status"] == "yes":
                    P["mod"].fail(VIOLATED, f"{r['function']}(){self._at(r)}: {r['detail']}")
        for g in gcc_yes:
            P["mod"].fail(VIOLATED, f"{g['text']} [{g['program']}]")
        if combined == "unknown":
            if mod.status == "unknown" and ("models" in verdicts or "svf" in verdicts):
                for r in mod.reasons:
                    P["mod"].fail(
                        UNRESOLVED,
                        f"{r['function']}(){self._at(r)}: {r['detail']}",
                        "run 'weaver flow' (SVF or GCC points-to), or add a reviewed effect model in weaver.yaml",
                    )
            for g in gcc:
                if g["status"] == "unknown":
                    P["mod"].fail(UNRESOLVED, f"{g['text']} [{g['program']}]")
            if len(verdicts) > 1 and "no" in verdicts.values():
                P["mod"].fail(
                    UNRESOLVED,
                    "points-to backends disagree: "
                    + ", ".join(f"{k} says {'no write' if v == 'no' else v}" for k, v in verdicts.items())
                    + "; flow.agreement is 'all'",
                    "review the unknown answer above; 'flow: {agreement: any}' accepts one backend's 'no write'",
                )
        elif combined == "no":
            src = "SVF points-to evidence" if any(flows.values()) else "call-site designators"
            tnames = sorted({t.get("name") or "?" for t in mod.targets}) or sorted(
                {d.get("name") for d in (arg_designators or [])}
            )
            if mod.status == "no":
                mod_desc = f"checked {sum(mod.checked.values())} write(s) in {len(mod.closure)} function(s) using {src}"
                pos("mod", f"{mod_desc}; possible targets: {', '.join(str(t) for t in tnames) or 'none'}")
                for a in mod.assumptions:
                    pos("mod", f"assumption: {a}")
            for g in gcc:
                if g["status"] == "no":
                    mod_desc = "GCC's whole-image points-to solution shows no write" if mod.status != "no" else mod_desc
                    pos("mod", f"{g['text']} [{g['program']}]")
            if len(set(verdicts.values())) > 1:
                pos(
                    "mod",
                    "backends disagree ("
                    + ", ".join(f"{k}: {v}" for k, v in verdicts.items())
                    + "); established because flow.agreement is 'any'",
                )
        for p, fe in flows.items():
            if fe is not None:
                pos(
                    "mod",
                    f"flow evidence {p}: {fe.run.get('status')} ({fe.provenance().get('svf')}, {fe.evidence_status})",
                )

        # -- concurrency contract ---------------------------------------------------
        conc = str(ctx.project.preservation.get("concurrency", "")).strip()
        if conc in CONCURRENCY_OK:
            pos("concurrency", f"preservation contract: concurrency = {conc}")
        else:
            P["concurrency"].fail(
                UNRESOLVED,
                "no concurrency model is declared; another thread, task or interrupt "
                "could write the target during the call",
                "declare 'preservation: {concurrency: single-threaded}' in weaver.yaml if true",
            )

        # -- complete callers ----------------------------------------------------------
        self._check_callers(ctx, fname, fsum, decls, callers, P["callers"], pos)

        # -- edits -------------------------------------------------------------------
        edits: dict[tuple[str, int, int], Edit] = {}
        for d in decls:
            if d.get("in_macro"):
                P["source"].fail(VIOLATED, f"{d['file']}:{d['line']}: declaration produced by a macro")
                continue
            if idx >= len(d["params"]):
                P["type"].fail(VIOLATED, f"{d['file']}:{d['line']}: declaration has {len(d['params'])} parameter(s)")
                continue
            e = self._param_edit(ctx, d, d["params"][idx])
            if isinstance(e, str):
                P["type"].fail(VIOLATED, f"{d['file']}:{d['line']}: {e}")
            else:
                edits[(e.file, e.start, e.end)] = e
        for u in uses:
            if u.get("access") not in ("read", "unevaluated") or not u.get("site_span"):
                continue
            e = self._deref_edit(ctx, def_file, u, finding["name"])
            if isinstance(e, str):
                P["source"].fail(VIOLATED, f"line {u['line']}: {e}")
            else:
                edits[(e.file, e.start, e.end)] = e
        for ck, c in callers:
            e = self._call_edit(ctx, ck, c, idx, P["sites"], P["source"])
            if e is not None:
                edits[(e.file, e.start, e.end)] = e
        if P["sites"].status == ESTABLISHED:
            pos(
                "sites",
                f"{len(callers)} call site(s): "
                + ", ".join(f"{c['site']['file']}:{c['site']['line']}" for _, c in callers),
            )
        if P["source"].status == ESTABLISHED and edits:
            pos("source", f"{len(edits)} edit(s) in {len({e.file for e in edits.values()})} file(s), token-checked")

        return self._result(
            finding,
            P,
            positive,
            sorted(edits.values(), key=lambda e: (e.file, e.start)),
            hashes,
            ctx,
            mod.to_json(),
            callers,
            mod_desc,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _at(r: dict[str, Any]) -> str:
        s = r.get("site") or {}
        return f" at {s.get('file')}:{s.get('line')}" if s.get("line") else ""

    @staticmethod
    def _describe(u: dict[str, Any]) -> str:
        return describe_use_parts(u["kind"], u.get("access"), u.get("detail") or {})

    def _check_callers(
        self,
        ctx: RecipeContext,
        fname: str,
        fsum: dict[str, Any],
        decls: list[dict[str, Any]],
        callers: list[Any],
        pre: Precondition,
        pos: Any,
    ) -> None:
        inv = ctx.inventory
        if fname == "main":
            pre.fail(VIOLATED, "main() is called by the runtime")
        if fsum.get("variadic") or any(d.get("variadic") for d in decls):
            pre.fail(VIOLATED, "variadic function")
        for d in decls:
            if not d.get("prototyped", True):
                pre.fail(VIOLATED, f"{d['file']}:{d['line']}: declaration without a prototype")
        for ref in inv.get("function_refs", []):
            if ref.get("name") == fname:
                pre.fail(
                    VIOLATED,
                    f"{ref.get('file')}:{ref.get('line')}: the address of {fname}() is taken; "
                    "calls through the pointer cannot all be updated",
                )
        frozen = [str(x) for x in ctx.project.preservation.get("interfaces", []) or []]
        hit = [x for x in frozen if x == fname or x in {d["file"] for d in decls}]
        if hit:
            pre.fail(VIOLATED, f"the interface is frozen by the preservation contract: {hit}")

        # Every textual reference to the name must be an analysed call or declaration.
        explained: dict[str, set[int]] = {}
        for d in decls:
            explained.setdefault(d["file"], set()).add(d["offset"])
        for _, c in callers:
            cr = c.get("callee_ref") or {}
            if cr.get("file") is not None:
                explained.setdefault(cr["file"], set()).add(cr["offset"])
        scan = sorted(set(inv["files"]) | set(inv["coverage"].get("unparsed_files", [])))
        if fsum["static"]:
            scan = [fsum["file"]]
        unexplained = []
        for rel, i in ctx.ident_occurrences(fname, scan):
            toks = ctx.lexed(rel).tokens
            t = toks[i]
            if t.start in explained.get(rel, set()):
                continue
            if i > 0 and toks[i - 1].text in (".", "->"):
                continue
            unexplained.append(f"{rel}:{t.line}" + (f" (#{t.directive})" if t.directive else ""))
        if unexplained:
            pre.fail(
                VIOLATED,
                "references to the name not explained by analysed calls or declarations: "
                + ", ".join(unexplained[:12]),
                "analyse the configuration that compiles them, or remove the stale reference",
            )
        for rel in self._asm_files(ctx):
            if fname in Path(ctx.abs(rel)).read_text(errors="replace"):
                pre.fail(UNRESOLVED, f"assembly file {rel} mentions {fname}")
        for status, msg in self._link_problems(ctx, fname, list(fsum.get("units", [])), bool(fsum["static"])):
            pre.fail(status, msg, "link only analysed objects, or declare the program and its entry points")
        if pre.status == ESTABLISHED:
            pos(
                "callers",
                f"{len(callers)} direct call(s); address never taken; "
                f"{'static' if fsum['static'] else 'external'} linkage; all references explained",
            )

    @staticmethod
    def _asm_files(ctx: RecipeContext) -> list[str]:
        cached = getattr(ctx, "_asm_cache", None)
        if cached is not None:
            return cached
        out = []
        for dirpath, dirnames, filenames in os.walk(ctx.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if os.path.splitext(fn)[1] in (".s", ".S", ".asm"):
                    out.append(rel_or_abs(os.path.join(dirpath, fn), ctx.root))
        ctx._asm_cache = out  # type: ignore[attr-defined]
        return out

    @staticmethod
    def _link_problems(ctx: RecipeContext, fname: str, units: list[str], static: bool) -> list[tuple[str, str]]:
        """Callers outside the analysed code, from each profile's link model (see ``weaver.link``)."""
        out: list[tuple[str, str]] = []
        by_profile: dict[str, list[str]] = {}
        for u in ctx.inventory["units"]:
            if u["unit_id"] in units:
                by_profile.setdefault(u["profile"], []).append(u["unit_id"])
        for pid, us in by_profile.items():
            for status, msg in ctx.link(pid).external_callers(fname, us, static):
                out.append((VIOLATED if status == "violated" else UNRESOLVED, f"profile {pid}: {msg}"))
        return out

    @staticmethod
    def _param_edit(ctx: RecipeContext, d: dict[str, Any], prm: dict[str, Any]) -> Edit | str:
        sp = prm.get("span")
        if sp is None or prm.get("in_macro"):
            return "parameter declaration is not plain source text"
        lx = ctx.lexed(sp["file"])
        toks = lx.tokens_in(sp["start"], sp["end"])
        stars = [t for t in toks if t.text == "*"]
        if len(stars) != 1 or any(t.text in ("(", ")", "[", "]") for t in toks):
            return f"parameter declarator '{lx.text[sp['start'] : sp['end']]}' is not a plain pointer"
        star = stars[0]
        after = [t for t in toks if t.start > star.start]
        quals = {"const", "volatile", "restrict", "__restrict", "__restrict__"}
        name_tok = after[-1] if after and after[-1].kind == "ident" and after[-1].text not in quals else None
        if any(t.text not in quals for t in after if t is not name_tok):
            return f"unexpected tokens after '*' in '{lx.text[sp['start'] : sp['end']]}'"
        spec = lx.text[sp["start"] : star.start].rstrip()
        new = f"{spec} {name_tok.text}" if name_tok else spec
        return Edit(
            sp["file"],
            sp["start"],
            sp["end"],
            lx.text[sp["start"] : sp["end"]],
            new,
            f"{d['name']}(): parameter {prm['index'] + 1} becomes a value parameter",
        )

    @staticmethod
    def _deref_edit(ctx: RecipeContext, file: str, u: dict[str, Any], name: str) -> Edit | str:
        s, e = u["site_span"]
        lx = ctx.lexed(file)
        toks = [t.text for t in lx.tokens_in(s, e)]
        inner = toks[1:]
        while len(inner) >= 3 and inner[0] == "(" and inner[-1] == ")":
            inner = inner[1:-1]
        if not toks or toks[0] != "*" or inner != [name]:
            return f"unexpected tokens in '{lx.text[s:e]}'"
        return Edit(file, s, e, lx.text[s:e], name, f"'{lx.text[s:e]}' reads the value now passed directly")

    @staticmethod
    def _call_edit(
        ctx: RecipeContext, caller: str, c: dict[str, Any], idx: int, sites: Precondition, source: Precondition
    ) -> Edit | None:
        where = f"{(c.get('site') or {}).get('file')}:{(c.get('site') or {}).get('line')}"
        if c.get("in_macro"):
            source.fail(VIOLATED, f"{where}: the call is inside a macro expansion")
            return None
        if idx >= len(c["args"]):
            sites.fail(VIOLATED, f"{where}: call has too few arguments")
            return None
        a = c["args"][idx]
        if a.get("null"):
            sites.fail(VIOLATED, f"{where}: passes a null pointer constant")
            return None
        for j, other in enumerate(c["args"]):
            if j != idx and other.get("side_effects"):
                sites.fail(
                    VIOLATED,
                    f"{where}: argument {j + 1} has side effects; reading the target during "
                    "argument evaluation would be unsequenced relative to them",
                )
        sp = a.get("span")
        if sp is None:
            source.fail(VIOLATED, f"{where}: argument is not plain source text")
            return None
        lx = ctx.lexed(sp["file"])
        text = lx.text[sp["start"] : sp["end"]]
        if a.get("addr_of") and a.get("addr_of_operand_span"):
            op = a["addr_of_operand_span"]
            new = lx.text[op["start"] : op["end"]]
        elif a.get("kind") in ("DeclRefExpr", "MemberExpr", "ArraySubscriptExpr", "CallExpr", "ParenExpr"):
            new = "*" + text
        else:
            new = f"*({text})"
        return Edit(sp["file"], sp["start"], sp["end"], text, new, f"{where}: pass the value instead of a pointer")

    def _result(
        self,
        finding: dict[str, Any],
        P: dict[str, Precondition],
        positive: dict[str, list[str]],
        edits: list[Edit],
        hashes: dict[str, str],
        ctx: RecipeContext,
        mod: dict[str, Any],
        callers: list[Any],
        mod_desc: str = "",
    ) -> RecipeResult:
        for k, p in P.items():
            if p.status == ESTABLISHED:
                for m in positive.get(k, []):
                    p.ok(m)
                if not p.evidence:
                    p.ok("checked; no counter-evidence")
        fname, pname = finding["function"], finding["name"] or "?"
        files = sorted({e.file for e in edits} | set(hashes))
        return RecipeResult(
            recipe=self.id,
            recipe_version=self.version,
            finding_id=finding["id"],
            preconditions=list(P.values()),
            edits=edits,
            file_hashes={f: h for f, h in hashes.items() if any(e.file == f for e in edits)} or hashes,
            capabilities_required=[],  # a value parameter needs no CLite capability
            preservation_argument=PRESERVATION.format(f=fname, p=pname, mod=mod_desc or "see may-modify evidence"),
            validation_plan=[
                "compile every unit that includes an edited file, in every profile, with its production command",
                f"mechanical re-check: {fname}() takes parameter {finding.get('param_index', 0) + 1} by value in every "
                "declaration, the body no longer dereferences it, and every call passes a non-pointer value",
                "run the configured tests and differential comparisons against the unpatched baseline",
            ],
            affected={
                "objects": [pname],
                "files": files,
                "functions": sorted({fname} | {ck.split("::")[1] for ck, _ in callers}),
                "interfaces": [fname],
            },
            units=[
                {"unit": u["unit_id"], "profile": u["profile"], "declaration": "present"}
                for u in ctx.inventory["units"]
                if u["unit_id"] in set((ctx.program.funcs.get(f"{finding['file']}::{fname}") or {}).get("units", []))
            ],
            notes=[f"may-modify: {mod.get('status')}"] if mod else [],
            recheck={"function": fname, "param_index": finding.get("param_index"), "param_name": pname, "mod": mod},
        )

    # ------------------------------------------------------------------
    def recheck(
        self, result: dict[str, Any], tu: TranslationUnit, offset_maps: dict[str, OffsetMap], root: str
    ) -> list[str] | None:
        rc = result["recheck"]
        fname, idx = rc["function"], rc["param_index"]
        problems: list[str] = []
        relevant = False
        for top in tu.top:
            if top.kind == "FunctionDecl" and top.name == fname:
                relevant = True
                params = [c for c in top.real_children() if c.kind == "ParmVarDecl"]
                if idx >= len(params):
                    problems.append(f"{fname}() declaration lost parameter {idx + 1}")
                    continue
                t = resolve_typedefs(safe_parse(params[idx].canonical_type), tu.typedefs)
                if t is None or t.kind == "pointer":
                    problems.append(
                        f"{fname}() parameter {idx + 1} is still '{params[idx].qual_type}' at line "
                        f"{top.loc.file_loc.line}"
                    )
                for r in tu.refs_to(params[idx].id):
                    p = r.parent
                    while p is not None and p.kind in ("ImplicitCastExpr", "ParenExpr"):
                        p = p.parent
                    if p is not None and p.kind == "UnaryOperator" and p.opcode == "*":
                        problems.append(f"line {r.begin.file_loc.line}: parameter still dereferenced")
        for n in tu.all_nodes():
            if n.kind == "CallExpr" and _callee(n) == fname:
                relevant = True
                args = n.real_children()[1:]
                if idx < len(args):
                    t = resolve_typedefs(safe_parse(args[idx].canonical_type), tu.typedefs)
                    if t is None or t.kind == "pointer":
                        problems.append(f"line {n.begin.file_loc.line}: call still passes a pointer")
        return problems if relevant else None


def _gcc_verdicts(ctx: RecipeContext, fname: str, idx: int, units: list[str]) -> list[dict[str, Any]]:
    """GCC's may-modify answer for each current image solution that contains the function's units."""
    out: list[dict[str, Any]] = []
    if not ctx.project.flow.uses("gcc"):
        return out
    for key in ctx.programs_for_units(units):
        profile, _, prog = key.partition("/")
        images = {i for u in units for i in ctx.link(profile).unit_images.get(u, [])}
        for g in ctx.gcc(profile, prog).values():
            if g.image not in images:
                continue
            status, text = g.may_modify(fname, idx)
            out.append({"program": key, "image": g.image, "status": status, "text": text})
    return out
