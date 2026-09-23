"""Recipe ``local-alias``: replace a local alias of one known object by direct access.

Pointer-tracker plan §4, first row: *Local alias of one known object -> direct
access to that object.  Target is stable; no relevant address escape or
identity observation; access and evaluation behavior preserved.*

Example (plan §5)::

    unsigned total = 3;            unsigned total = 3;
    unsigned *p = &total;   ==>    total += 2;
    *p += 2;                       return total;
    return total;

The replacement accesses the original object directly; copying the initial
value into a separate variable would not preserve the update to ``total``.

Scope of this version: automatic locals declared alone in a block, initialized
with ``&D`` where ``D`` is a named object reached by a constant access path
(``x``, ``s.f``, ``a[3]``), and used only as ``*p`` or ``p->f``.  Everything
else is rejected with a specific reason.  A successful rejection is part of
correctness.
"""

from __future__ import annotations

from typing import Any

from weaver.analysis.uses import classify_ref, describe_use
from weaver.frontend.clang_ast import Node, TranslationUnit, strip_parens_casts
from weaver.frontend.lexer import LexResult
from weaver.frontend.typestr import CType, resolve_typedefs, safe_parse
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

SHADOWING_DECLS = {"VarDecl", "ParmVarDecl", "FunctionDecl", "TypedefDecl", "EnumConstantDecl"}

PRESERVATION = (
    "Every use of `{p}` dereferences it (`*{p}` or `{p}->f`).  `{p}` is initialized exactly once with "
    "`&{d}`, is never reassigned, never has its own address taken, and its value never leaves the "
    "expression that dereferences it (no copy, call argument, return, comparison, cast or capture).  "
    "`{d}` designates one named object through a constant access path, and the name resolves to that "
    "same object at every use site.  Hence each `*{p}` denotes the same lvalue as `{d}` with the same "
    "type and access qualifiers, so replacing it preserves every read, write and their ordering, "
    "including shared mutation of `{d}`.  Removing the declaration of `{p}` removes only the "
    "initialization of an automatic object that no longer has uses."
)


def _unqual(t: CType | None) -> str:
    if t is None:
        return "?"
    c = CType(t.kind, frozenset(), t.name, t.inner, t.params, t.variadic, t.size, False, [])
    return c.spell()


def _quals(t: CType | None) -> set[str]:
    if t is None:
        return set()
    q = {("restrict" if x.startswith("__restrict") else x) for x in t.quals}
    if t.atomic:
        q.add("_Atomic")
    return q


def _designator(n: Node | None) -> tuple[Node | None, str | None]:
    """Return (base DeclRefExpr, None) for a constant access path, else (None, reason)."""
    n = strip_parens_casts(n)
    if n is None:
        return None, "missing operand"
    if n.kind == "DeclRefExpr":
        k = (n.raw.get("referencedDecl") or {}).get("kind")
        if k in ("VarDecl", "ParmVarDecl"):
            return n, None
        return None, f"operand names a {k}, not an object"
    if n.kind == "MemberExpr":
        if n.raw.get("isArrow"):
            return None, "access path goes through another pointer ('->'), whose value may change"
        return _designator(n.child(0))
    if n.kind == "ArraySubscriptExpr":
        base = n.child(0)
        if not (base is not None and base.kind == "ImplicitCastExpr" and base.cast_kind == "ArrayToPointerDecay"):
            return None, "subscript base is a pointer, whose value may change"
        idx = strip_parens_casts(n.child(1), ("IntegralCast", "NoOp"))
        if idx is None or idx.kind != "IntegerLiteral":
            return None, "subscript index is not an integer constant"
        return _designator(base.child(0))
    return None, f"operand is a {n.kind}, not a named object path"


def _token_seq_ok(lx: LexResult, start: int, end: int, name: str, suffix: tuple[str, ...]) -> bool:
    """Tokens in [start,end) are: optional '*' prefix handled by caller, '('* name ')'* then suffix."""
    toks = [t.text for t in lx.tokens_in(start, end)]
    i = 0
    opens = 0
    while i < len(toks) and toks[i] == "(":
        opens += 1
        i += 1
    if i >= len(toks) or toks[i] != name:
        return False
    i += 1
    closes = 0
    while i < len(toks) and toks[i] == ")" and closes < opens:
        closes += 1
        i += 1
    return closes == opens and tuple(toks[i:]) == suffix


class LocalAliasRecipe(Recipe):
    id = "local-alias"
    version = "1"
    title = "Replace a local alias of one known object with direct access"
    finding_kinds = ("local",)

    def evaluate(self, ctx: RecipeContext, finding: dict[str, Any]) -> RecipeResult:
        name: str = finding["name"]
        file_rel: str = finding["file"]
        file_abs = ctx.abs(file_rel)
        decl_off: int = finding["offset"]

        P = {
            "current": Precondition("LA.evidence-current", "Evidence describes the current source revision"),
            "configs": Precondition(
                "LA.configurations",
                "Every configured profile that compiles the file has an analysed unit with adequate evidence",
            ),
            "shape": Precondition(
                "LA.decl-shape", "The pointer is an automatic local declared alone in a block, without attributes"
            ),
            "ptype": Precondition(
                "LA.pointer-type", "The pointer is an object pointer whose own accesses are not volatile or atomic"
            ),
            "target": Precondition(
                "LA.target-stable",
                "Initialized once with the address of a named object reached through a constant access path",
            ),
            "quals": Precondition(
                "LA.access-qualifiers",
                "Accesses through the pointer have the same type and volatile/atomic qualification as direct access",
            ),
            "uses": Precondition(
                "LA.dereference-only",
                "Every use dereferences the pointer; it is never reassigned, copied, passed, returned, compared, "
                "cast, indexed or has its address taken",
            ),
            "capture": Precondition("LA.no-capture", "No use is inside a block literal"),
            "flow": Precondition(
                "LA.initialization-dominates", "No jump can reach a use while bypassing the initialization"
            ),
            "names": Precondition("LA.name-resolution", "The target's name denotes the same object at every use site"),
            "source": Precondition(
                "LA.edits-in-source", "Every edited range is plain source text, not a macro expansion"
            ),
            "textual": Precondition(
                "LA.all-references-explained",
                "Every textual occurrence of the pointer's name in its scope is explained by an analysed AST",
            ),
            "consistent": Precondition(
                "LA.consistent-across-configurations",
                "All analysed configurations agree on the declaration and its target",
            ),
        }

        # -- evidence currency and configuration coverage -------------------
        cur_hash = ctx.current_hash(file_rel)
        if finding.get("file_sha256") != cur_hash:
            P["current"].fail(
                VIOLATED,
                f"{file_rel} changed since the inventory was built",
                "re-run 'weaver collect' and 'weaver inventory'",
            )
        units = ctx.units_for_file(file_rel)
        for u in units:
            if u["file_sha256"] != cur_hash:
                P["current"].fail(
                    VIOLATED,
                    f"unit {u['unit_id']} ({u['profile']}) analysed a different revision",
                    "re-run 'weaver collect' and 'weaver inventory'",
                )
        if P["current"].status == ESTABLISHED:
            P["current"].ok(f"{file_rel} sha256 {cur_hash[:12]} matches all {len(units)} analysed unit(s)")

        if not units:
            P["configs"].fail(
                UNRESOLVED,
                f"{file_rel} is not the main file of any analysed unit "
                "(declarations in headers need multi-TU coverage, not implemented)",
            )
        compiling = ctx.profiles_compiling(file_rel)
        analysed_profiles = {u["profile"] for u in units}
        for pid, compiles in compiling.items():
            if compiles and pid not in analysed_profiles:
                P["configs"].fail(
                    UNRESOLVED,
                    f"profile {pid} compiles {file_rel} but has no analysed unit",
                    f"run 'weaver collect --profile {pid}' and 'weaver inventory'",
                )
        for u in units:
            if not u["analyzed"]:
                P["configs"].fail(
                    UNRESOLVED,
                    f"unit {u['unit_id']} ({u['profile']}) has no AST evidence",
                    "check the collection diagnostics for this unit",
                )
            elif not ctx.evidence_ok(u["evidence_status"]):
                P["configs"].fail(
                    UNRESOLVED,
                    f"unit {u['unit_id']} ({u['profile']}) evidence is {u['evidence_status']}, "
                    f"policy requires {ctx.min_evidence.value}",
                    f"run 'weaver fidelity --profile {u['profile']}'",
                )
        if P["configs"].status == ESTABLISHED:
            P["configs"].ok(
                "analysed units: "
                + ", ".join(f"{u['unit_id']} ({u['profile']}, {u['evidence_status']})" for u in units)
            )

        edits: dict[tuple[int, int], Edit] = {}
        positive: dict[str, list[str]] = {}  # evidence for preconditions that hold

        def pos(key: str, msg: str) -> None:
            positive.setdefault(key, []).append(msg)

        explained: set[int] = {decl_off}
        targets: dict[str, tuple[Any, ...]] = {}
        unit_records: list[dict[str, Any]] = []
        scope_region: tuple[int, int] | None = None
        designator_text = "?"
        recheck: dict[str, Any] = {}
        lx = ctx.lexed(file_rel)
        text = lx.text
        function_name = finding.get("function")

        for u in units:
            tu = ctx.tu(u) if u["analyzed"] else None
            if tu is None:
                continue
            decl = tu.var_decl_at(file_abs, decl_off, name)
            if decl is not None and decl.kind != "VarDecl":
                decl = None
            if decl is None:
                unit_records.append({"unit": u["unit_id"], "profile": u["profile"], "declaration": "inactive"})
                continue
            rec = {"unit": u["unit_id"], "profile": u["profile"], "declaration": "present", "uses": 0}
            unit_records.append(rec)
            cfg = u["profile"]
            pos("shape", f"{cfg}: automatic local declared alone in a block at line {decl.loc.file_loc.line}")
            pos("ptype", f"{cfg}: type '{decl.qual_type}'")
            fn = decl.enclosing("FunctionDecl")
            ds = decl.parent

            # decl shape ---------------------------------------------------
            if decl.storage_class not in (None, "register"):
                P["shape"].fail(VIOLATED, f"storage class '{decl.storage_class}' (not an automatic object)")
            if ds is None or ds.kind != "DeclStmt":
                P["shape"].fail(VIOLATED, "not declared by a declaration statement")
            elif len(ds.real_children()) != 1:
                P["shape"].fail(VIOLATED, "declared together with other declarators; split the declaration first")
            elif ds.parent is None or ds.parent.kind != "CompoundStmt":
                P["shape"].fail(
                    VIOLATED,
                    f"declaration is part of a {ds.parent.kind if ds.parent else '?'} "
                    "(e.g. a for-init), not a block item",
                )
            attrs = [c.kind for c in decl.real_children() if c.kind.endswith("Attr")]
            if attrs:
                P["shape"].fail(VIOLATED, f"declaration has attributes {attrs}; removing it could change behavior")
            if decl.loc.in_macro or (ds is not None and (ds.begin.in_macro or ds.end.in_macro)):
                P["source"].fail(VIOLATED, "the declaration is produced by a macro expansion")

            # pointer type -------------------------------------------------
            ptype = resolve_typedefs(safe_parse(decl.canonical_type), tu.typedefs)
            if ptype is None or ptype.kind != "pointer":
                P["ptype"].fail(UNRESOLVED, f"type {decl.qual_type!r} not recognised as an object pointer")
            else:
                pointee = resolve_typedefs(ptype.inner, tu.typedefs)
                if pointee is not None and pointee.kind == "function":
                    P["ptype"].fail(VIOLATED, "function pointer (use the function-tag recipe)")
                pq = _quals(ptype)
                if "volatile" in pq or "_Atomic" in pq or ptype.atomic:
                    P["ptype"].fail(VIOLATED, f"the pointer object itself is {sorted(pq)}: its reads are observable")
                if "restrict" in pq:
                    rec["note"] = "restrict qualifier on the pointer is removed together with the pointer"

            # target -------------------------------------------------------
            init = next((c for c in decl.real_children() if not c.kind.endswith("Attr")), None)
            if init is None or decl.raw.get("init") != "c":
                P["target"].fail(VIOLATED, "no C initializer; the pointer is not bound once at its declaration")
                continue
            noop = False
            e = init
            while e is not None and (e.kind == "ParenExpr" or (e.kind == "ImplicitCastExpr" and e.cast_kind == "NoOp")):
                noop = noop or e.kind == "ImplicitCastExpr"
                e = e.child(0)
            if e is None or not (e.kind == "UnaryOperator" and e.opcode == "&"):
                P["target"].fail(
                    VIOLATED,
                    f"initializer is a {e.kind if e else '?'}"
                    f"{'(' + str(e.cast_kind) + ')' if e is not None and e.cast_kind else ''}, "
                    "not the address of an object",
                )
                continue
            operand = e.child(0)
            base_ref, why = _designator(operand)
            if base_ref is None:
                P["target"].fail(VIOLATED, f"target is not a stable designator: {why}")
                continue
            op_span = operand.file_span() if operand is not None else None
            if op_span is None or operand is None or operand.touches_macro():
                P["source"].fail(VIOLATED, "the target designator involves a macro expansion")
                continue
            designator_text = text[op_span[1] : op_span[2]]
            tdecl_id = base_ref.referenced_decl_id
            tdecl = tu.node(tdecl_id)
            tname = (base_ref.raw.get("referencedDecl") or {}).get("name")
            tfile = tdecl.loc.file_loc.file if tdecl is not None else None
            toff = tdecl.loc.file_loc.offset if tdecl is not None else None
            targets[u["unit_id"]] = (designator_text, tname, tfile, toff)
            recheck.update(
                target_name=tname, target_decl={"file": rel_or_abs(tfile, ctx.root) if tfile else None, "offset": toff}
            )
            where = (
                f"{rel_or_abs(tfile, ctx.root)}:{tdecl.loc.file_loc.line}"
                if tdecl is not None
                else "outside the project"
            )
            pos("target", f"{cfg}: initialized with '&{designator_text}' ('{tname}' declared at {where})")

            # qualifiers ---------------------------------------------------
            if ptype is not None and ptype.kind == "pointer":
                pointee = resolve_typedefs(ptype.inner, tu.typedefs)
                dtype = resolve_typedefs(safe_parse(operand.canonical_type), tu.typedefs)
                pq, dq = _quals(pointee), _quals(dtype)
                if _unqual(pointee) != _unqual(dtype):
                    P["quals"].fail(
                        VIOLATED, f"pointee type {_unqual(pointee)!r} differs from target type {_unqual(dtype)!r}"
                    )
                for q in ("volatile", "_Atomic"):
                    if (q in pq) != (q in dq):
                        P["quals"].fail(
                            VIOLATED,
                            f"'{q}' differs between accesses through the pointer "
                            f"({sorted(pq)}) and the target ({sorted(dq)})",
                        )
                if "const" in dq and "const" not in pq:
                    P["quals"].fail(VIOLATED, "the pointer discards 'const' from its target")
                if noop and P["quals"].status == ESTABLISHED:
                    rec["qualification_conversion"] = f"{sorted(dq)} -> {sorted(pq)}"
                pos(
                    "quals",
                    f"{cfg}: accesses through the pointer are '{pointee.spell() if pointee else '?'}', "
                    f"direct accesses are '{dtype.spell() if dtype else '?'}'",
                )

            # uses ---------------------------------------------------------
            refs = tu.refs_to(decl.id)
            rec["uses"] = len(refs)
            for r in refs:
                explained.add(r.begin.offset if not r.begin.in_macro else -1)
                if r.begin.in_macro and r.begin.spelling and r.begin.spelling.file == file_abs:
                    explained.add(r.begin.spelling.offset)  # macro argument spelled in this file
                use = classify_ref(r)
                line = r.begin.file_loc.line
                if use.kind not in ("deref", "arrow"):
                    P["uses"].fail(VIOLATED, f"line {line}: {describe_use(use)}")
                    continue
                if r.enclosing("BlockExpr") is not None:
                    P["capture"].fail(VIOLATED, f"line {line}: use inside a block literal (captured by value)")
                edit = self._use_edit(use, r, lx, file_rel, name, designator_text)
                if isinstance(edit, str):
                    P["source"].fail(VIOLATED, f"line {line}: {edit}")
                else:
                    edits[(edit.start, edit.end)] = edit

            pos(
                "uses",
                f"{cfg}: {len(refs)} use(s): "
                + (", ".join(f"line {r.begin.file_loc.line} {describe_use(classify_ref(r))}" for r in refs) or "none"),
            )

            # control flow -------------------------------------------------
            if ds is not None and ds.parent is not None and fn is not None:
                scope = ds.parent
                d_end = ds.end.offset or 0
                s_end = (scope.end.offset or 0) + 1
                scope_region = (ds.begin.offset or 0, s_end)
                self._check_flow(fn, ds, d_end, s_end, P["flow"])
                pos(
                    "flow",
                    f"{cfg}: no goto, computed goto or switch label enters the scope after line {ds.end.file_loc.line}",
                )

            # names --------------------------------------------------------
            if fn is not None and tname:
                for n in fn.walk():
                    if n.kind in SHADOWING_DECLS and n.name == tname and n.id != tdecl_id:
                        P["names"].fail(
                            VIOLATED,
                            f"line {n.loc.file_loc.line}: another declaration of "
                            f"'{tname}' in {fn.name}() may shadow the target at a use site",
                        )
                if tname in ctx.macros(u):
                    P["names"].fail(VIOLATED, f"'{tname}' is defined as a macro in this configuration")
                pos("names", f"{cfg}: no other declaration named '{tname}' in {fn.name}(); not a macro")
            for d in lx.directives:
                if d.name in ("define", "undef") and len(d.tokens) > 2 and d.tokens[2].text in (tname, name):
                    P["names"].fail(VIOLATED, f"line {d.line_start}: #{d.name} {d.tokens[2].text}")

            # declaration removal edit -------------------------------------
            if ds is not None and P["shape"].status == ESTABLISHED:
                sp = ds.file_span()
                if sp is None:
                    P["source"].fail(VIOLATED, "declaration range is not plain source text")
                else:
                    rm = self._removal_edit(text, sp[1], sp[2], file_rel, name)
                    if isinstance(rm, str):
                        P["source"].fail(VIOLATED, rm)
                    else:
                        edits[(rm.start, rm.end)] = rm
            recheck.setdefault(
                "pointer_count_in_function",
                sum(1 for n in fn.walk() if n.kind == "VarDecl" and n.name == name) if fn else None,
            )

        # -- across configurations -----------------------------------------
        present = [r for r in unit_records if r["declaration"] == "present"]
        if not present and units:
            P["consistent"].fail(UNRESOLVED, "declaration not found in any analysed unit")
        distinct = set(targets.values())
        if len(distinct) > 1:
            P["consistent"].fail(VIOLATED, f"configurations disagree on the target: {sorted(map(str, distinct))}")
        elif distinct:
            P["consistent"].ok(f"{len(present)} configuration(s) bind the pointer to '{designator_text}'")

        # -- textual accounting --------------------------------------------
        if scope_region is not None:
            unexplained = []
            toks = lx.tokens
            idx = {id(t): i for i, t in enumerate(toks)}
            for t in lx.tokens_in(*scope_region):
                if t.kind != "ident" or t.text != name or t.start in explained:
                    continue
                i = idx[id(t)]
                prev = toks[i - 1].text if i > 0 else ""
                if prev in (".", "->") and toks[i - 1].directive == t.directive:
                    continue  # a member name, not the pointer
                where = f"line {t.line}" + (f" (in #{t.directive} directive)" if t.directive else "")
                unexplained.append(where)
            if unexplained:
                P["textual"].fail(
                    VIOLATED,
                    f"occurrences of '{name}' not explained by any analysed configuration: {', '.join(unexplained)}",
                    "analyse a configuration that compiles this code, or establish that it is never compiled",
                )
            else:
                P["textual"].ok(
                    f"all occurrences of '{name}' in lines "
                    f"{lx.line_of(scope_region[0])}-{lx.line_of(scope_region[1] - 1)} are analysed uses"
                )

        if edits:
            pos("source", f"{len(edits)} edit range(s), each checked token by token against the source text")
        for key, p in P.items():
            if p.status == ESTABLISHED:
                for msg in positive.get(key, []):
                    p.ok(msg)
                if not p.evidence:
                    p.ok("checked in every analysed configuration; no counter-evidence")

        edit_list = sorted(edits.values(), key=lambda e: e.start)
        recheck.update(
            file=file_rel,
            function=function_name,
            pointer_name=name,
            scope_region=list(scope_region) if scope_region else None,
            replaced=[[e.start, e.end] for e in edit_list if e.replacement],
        )
        return RecipeResult(
            recipe=self.id,
            recipe_version=self.version,
            finding_id=finding["id"],
            preconditions=list(P.values()),
            edits=edit_list,
            file_hashes={file_rel: cur_hash},
            capabilities_required=[],  # direct access needs no CLite capability
            preservation_argument=PRESERVATION.format(p=name, d=designator_text),
            validation_plan=[
                f"compile every configuration that compiles {file_rel} with its production command",
                f"mechanical re-check: re-parse the patched file; '{name}' is gone and every replaced site "
                f"resolves to the target declaration",
                "run the configured tests and differential comparisons against the unpatched baseline",
            ],
            affected={
                "objects": [name, designator_text],
                "files": [file_rel],
                "functions": [function_name] if function_name else [],
                "interfaces": [],
            },
            units=unit_records,
            recheck=recheck,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _use_edit(use: Any, ref: Node, lx: LexResult, file_rel: str, name: str, designator: str) -> Edit | str:
        site = use.site
        if ref.begin.in_macro or site.begin.in_macro or site.end.in_macro:
            return "use is inside a macro expansion"
        if use.kind == "deref":
            sp = site.file_span()
            if sp is None:
                return "dereference range is not plain source text"
            toks = lx.tokens_in(sp[1], sp[2])
            if not toks or toks[0].text != "*" or not _token_seq_ok(lx, toks[0].end, sp[2], name, ()):
                return f"unexpected tokens in '{lx.text[sp[1] : sp[2]]}'"
            return Edit(
                file_rel,
                sp[1],
                sp[2],
                lx.text[sp[1] : sp[2]],
                designator,
                f"'{lx.text[sp[1] : sp[2]]}' accesses the object designated by '{designator}'",
            )
        # arrow: replace "<p>->" (possibly parenthesised) by "<designator>."
        start = site.begin.offset
        member = site.end.offset
        if start is None or member is None or site.end.in_macro:
            return "member access range is not plain source text"
        if not _token_seq_ok(lx, start, member, name, ("->",)):
            return f"unexpected tokens in '{lx.text[start:member]}'"
        return Edit(
            file_rel,
            start,
            member,
            lx.text[start:member],
            designator + ".",
            f"'{lx.text[start:member]}{site.name}' accesses a member of '{designator}'",
        )

    @staticmethod
    def _removal_edit(text: str, start: int, end: int, file_rel: str, name: str) -> Edit | str:
        if not text[start:end].rstrip().endswith(";"):
            return "declaration statement does not end with ';' where expected"
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end
        if not text[line_start:start].strip() and not text[end:line_end].strip():
            s, e = line_start, min(line_end + 1, len(text))
        else:
            s, e = start, end
        return Edit(file_rel, s, e, text[s:e], "", f"remove the declaration of '{name}' (no remaining uses)")

    @staticmethod
    def _check_flow(fn: Node, ds: Node, d_end: int, s_end: int, pre: Precondition) -> None:
        labels: dict[str, Node] = {}
        gotos: list[Node] = []
        for n in fn.walk():
            if n.kind == "LabelStmt":
                labels[n.raw.get("declId", "")] = n
            elif n.kind == "GotoStmt":
                gotos.append(n)
            elif n.kind == "IndirectGotoStmt":
                pre.fail(UNRESOLVED, f"line {n.begin.file_loc.line}: computed goto; jump targets unknown")
        for g in gotos:
            lab = labels.get(g.raw.get("targetLabelDeclId", ""))
            if lab is None:
                continue
            lo = lab.begin.file_loc.offset or 0
            go = g.begin.file_loc.offset or 0
            if d_end < lo < s_end and not (d_end < go < s_end):
                pre.fail(
                    VIOLATED, f"line {g.begin.file_loc.line}: goto into the pointer's scope past its initialization"
                )
        for n in fn.walk():
            if n.kind in ("CaseStmt", "DefaultStmt"):
                off = n.begin.file_loc.offset or 0
                if d_end < off < s_end:
                    sw = n.enclosing("SwitchStmt")
                    if sw is not None and any(a is sw for a in ds.ancestors()):
                        pre.fail(VIOLATED, f"line {n.begin.file_loc.line}: switch label past the initialization")

    # ------------------------------------------------------------------
    def recheck(
        self, result: dict[str, Any], tu: TranslationUnit, offset_maps: dict[str, OffsetMap], root: str
    ) -> list[str] | None:
        """Re-parse check on the patched unit (the mechanical part of validation).

        ``tu`` is the AST of the patched file, collected from a workspace whose
        root is ``root``; ``offset_map`` maps original offsets to patched ones.
        """
        rc = result["recheck"]
        problems: list[str] = []
        main = tu.main_file
        if rel_or_abs(main, root) != rc["file"] or rc["file"] not in offset_maps:
            return None  # this unit does not compile the edited function
        offset_map = offset_maps[rc["file"]]
        name = rc["pointer_name"]
        fn = next((f for f in tu.functions() if f.name == rc.get("function")), None)
        if fn is None:
            return [f"function {rc.get('function')} not found after patching"]
        before = rc.get("pointer_count_in_function")
        after = sum(1 for n in fn.walk() if n.kind == "VarDecl" and n.name == name)
        if before is not None and after != before - 1:
            problems.append(f"expected {before - 1} declaration(s) of '{name}' after patching, found {after}")
        tdecl = rc.get("target_decl") or {}
        edits_by_range = {(e.start, e.end): e for e in offset_map.edits}
        refs = [n for n in fn.walk() if n.kind == "DeclRefExpr" and n.begin.file == main and not n.begin.in_macro]
        for s, e in rc["replaced"]:
            edit = edits_by_range.get((s, e))
            if edit is None:
                problems.append(f"edit {s}-{e} missing from patch")
                continue
            lo, hi = offset_map.replaced_range(edit)
            inside = sorted((r for r in refs if lo <= (r.begin.offset or -1) < hi), key=lambda r: r.begin.offset or 0)
            if not inside:
                problems.append(f"no reference found in replaced text at {lo}-{hi}")
                continue
            base = inside[0]
            rd = base.raw.get("referencedDecl") or {}
            if rd.get("name") != rc.get("target_name"):
                problems.append(
                    f"replaced text at {lo} refers to '{rd.get('name')}', expected '{rc.get('target_name')}'"
                )
                continue
            node = tu.node(base.referenced_decl_id)
            if node is not None and tdecl.get("offset") is not None:
                loc = node.loc.file_loc
                want = tdecl["offset"]
                if tdecl.get("file") == rc["file"]:
                    want = offset_map.map(want)  # declarations in the edited file move with the edits
                got_file = rel_or_abs(loc.file, root) if loc.file else None
                if got_file != tdecl.get("file") or loc.offset != want:
                    problems.append(
                        f"replaced text at {lo} binds to a different '{rd.get('name')}' "
                        f"({got_file}@{loc.offset}, expected {tdecl.get('file')}@{want})"
                    )
        region = rc.get("scope_region")
        if region:
            lo = offset_map.map(region[0])
            hi = offset_map.map(region[1] - 1)
            lo = lo if lo is not None else 0
            hi = hi if hi is not None else 1 << 62
            for r in refs:
                rd = r.raw.get("referencedDecl") or {}
                if rd.get("name") == name and lo <= (r.begin.offset or -1) <= hi:
                    problems.append(f"a reference to '{name}' remains at offset {r.begin.offset}")
        return problems
