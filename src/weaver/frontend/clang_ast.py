"""Load a Clang ``-ast-dump=json`` artifact into a navigable tree.

Clang's JSON dumper de-duplicates location fields: ``file`` is written only
when it changes from the previously printed location and ``line`` only when it
changes, across the *whole* dump in emission order.  Locations inside macro
expansions are written as ``spellingLoc``/``expansionLoc`` pairs.  This module
replays that state in document order (Python's ``json`` preserves key order) so
every location carries an absolute file, line, column and byte offset.

AST-internal ``id`` values are not persistent identities; Weaver only uses them
within one loaded dump.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from weaver.util import read_json


@dataclass(frozen=True)
class Loc:
    file: str | None
    line: int | None
    col: int | None
    offset: int | None
    tok_len: int | None
    spelling: "Loc | None" = None
    expansion: "Loc | None" = None
    macro_arg: bool = False

    @property
    def in_macro(self) -> bool:
        return self.expansion is not None

    @property
    def file_loc(self) -> "Loc":
        """Where the construct appears in a file (the expansion site for macros)."""
        return self.expansion if self.expansion is not None else self

    @property
    def valid(self) -> bool:
        return self.offset is not None and self.file is not None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"file": self.file, "line": self.line, "col": self.col, "offset": self.offset}
        if self.in_macro:
            d["macro"] = True
            d["spelling"] = self.spelling.to_json() if self.spelling else None
            d["expansion"] = self.expansion.to_json() if self.expansion else None
        return d


NO_LOC = Loc(None, None, None, None, None)


class _LocResolver:
    def __init__(self, directory: str, unwrap: Any = None):
        self.directory = directory
        self.unwrap = unwrap  # weaver.frontend.wrappers.ArgUnwrapper or None
        self.unwrapped = 0
        self.last_file: str | None = None
        self.last_line: int | None = None
        self._cache: dict[str, str] = {}

    def _abs(self, f: str) -> str:
        r = self._cache.get(f)
        if r is None:
            if f.startswith("<"):
                r = f  # <built-in>, <scratch space>, <command line>
            else:
                r = os.path.realpath(f if os.path.isabs(f) else os.path.join(self.directory, f))
            self._cache[f] = r
        return r

    def bare(self, d: dict[str, Any]) -> Loc:
        if "offset" not in d:
            return NO_LOC
        if "file" in d:
            self.last_file = self._abs(d["file"])
        if "line" in d:
            self.last_line = d["line"]
        return Loc(
            self.last_file,
            self.last_line,
            d.get("col"),
            d["offset"],
            d.get("tokLen"),
            macro_arg=bool(d.get("isMacroArgExpansion")),
        )

    def loc(self, d: dict[str, Any] | None) -> Loc:
        if not d:
            return NO_LOC
        if "spellingLoc" in d or "expansionLoc" in d:
            sp = self.bare(d.get("spellingLoc") or {})
            ex = self.bare(d.get("expansionLoc") or {})
            if (
                self.unwrap is not None
                and ex.macro_arg
                and sp.valid
                and sp.file == ex.file
                and self.unwrap.plain(ex.file, ex.offset, sp.offset)
            ):
                # an argument of a transparent secondary-only wrapper (see frontend.wrappers)
                self.unwrapped += 1
                return Loc(sp.file, sp.line, sp.col, sp.offset, sp.tok_len)
            return Loc(
                ex.file, ex.line, ex.col, ex.offset, ex.tok_len, spelling=sp, expansion=ex, macro_arg=ex.macro_arg
            )
        return self.bare(d)


class Node:
    __slots__ = ("raw", "parent", "children", "loc", "begin", "end", "index")

    def __init__(self, raw: dict[str, Any], parent: "Node | None", index: int):
        self.raw = raw
        self.parent = parent
        self.children: list[Node | None] = []
        self.loc: Loc = NO_LOC
        self.begin: Loc = NO_LOC
        self.end: Loc = NO_LOC
        self.index = index  # position within parent's inner list

    # -- raw accessors ---------------------------------------------------
    @property
    def kind(self) -> str:
        return self.raw.get("kind", "")

    @property
    def id(self) -> str:
        return self.raw.get("id", "")

    @property
    def name(self) -> str | None:
        return self.raw.get("name")

    @property
    def qual_type(self) -> str | None:
        t = self.raw.get("type")
        return t.get("qualType") if isinstance(t, dict) else None

    @property
    def canonical_type(self) -> str | None:
        t = self.raw.get("type")
        if not isinstance(t, dict):
            return None
        return t.get("desugaredQualType") or t.get("qualType")

    @property
    def opcode(self) -> str | None:
        return self.raw.get("opcode")

    @property
    def cast_kind(self) -> str | None:
        return self.raw.get("castKind")

    @property
    def referenced_decl_id(self) -> str | None:
        r = self.raw.get("referencedDecl")
        return r.get("id") if isinstance(r, dict) else None

    @property
    def storage_class(self) -> str | None:
        return self.raw.get("storageClass")

    def child(self, i: int) -> "Node | None":
        return self.children[i] if i < len(self.children) else None

    def real_children(self) -> list["Node"]:
        return [c for c in self.children if c is not None]

    # -- navigation --------------------------------------------------------
    def walk(self) -> Iterator["Node"]:
        stack: list[Node] = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.real_children()))

    def ancestors(self) -> Iterator["Node"]:
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def enclosing(self, *kinds: str) -> "Node | None":
        for a in self.ancestors():
            if a.kind in kinds:
                return a
        return None

    # -- spans -------------------------------------------------------------
    def file_span(self) -> tuple[str, int, int] | None:
        """(file, start, end) byte span in a file, or None if not a plain file range.

        Returns None when either end lies inside a macro expansion or the ends
        are in different files: such a range cannot be edited safely as text.
        """
        b, e = self.begin, self.end
        if b.in_macro or e.in_macro or not b.valid or not e.valid or b.file != e.file:
            return None
        if e.tok_len is None:
            return None
        return b.file, b.offset, e.offset + e.tok_len  # type: ignore[operator]

    def expansion_span(self) -> tuple[str, int, int] | None:
        """Like file_span but using expansion locations (for reporting only)."""
        b, e = self.begin.file_loc, self.end.file_loc
        if not b.valid or not e.valid or b.file != e.file or e.tok_len is None:
            return None
        return b.file, b.offset, e.offset + e.tok_len  # type: ignore[operator]

    def touches_macro(self) -> bool:
        return any(n.begin.in_macro or n.end.in_macro or n.loc.in_macro for n in self.walk())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind} {self.name or ''} @{self.loc.file_loc.line}:{self.loc.file_loc.col}>"


class TranslationUnit:
    """A loaded AST with resolved locations.

    Only top-level declarations whose location lies in ``keep_files`` (or under
    ``keep_root``) are materialised as :class:`Node` trees; typedefs and record
    names from every header are indexed so types can be desugared.
    """

    def __init__(
        self,
        ast_path: str | os.PathLike[str],
        directory: str,
        main_file: str,
        keep_root: str | os.PathLike[str] | None = None,
        unwrap: Any = None,
    ):
        raw = read_json(ast_path)
        if raw.get("kind") != "TranslationUnitDecl":
            raise ValueError(f"{ast_path}: not a TranslationUnitDecl dump")
        self.main_file = os.path.realpath(main_file)
        self.keep_root = os.path.realpath(keep_root) if keep_root else None
        self.nodes: dict[str, Node] = {}
        self.top: list[Node] = []
        self.typedefs: dict[str, str] = {}  # name -> canonical (desugared) type string
        self.typedef_decls: dict[str, dict[str, Any]] = {}
        self.decl_files: dict[str, str | None] = {}  # every decl id -> file (for referencedDecl lookups)
        res = _LocResolver(directory, unwrap)
        for i, top in enumerate(raw.get("inner", [])):
            if not isinstance(top, dict):
                continue
            node = self._build(top, None, i, res)
            f = node.loc.file_loc.file
            if top.get("kind") == "TypedefDecl" and top.get("name"):
                t = top.get("type", {})
                self.typedefs[top["name"]] = t.get("desugaredQualType") or t.get("qualType", "")
                self.typedef_decls[top["name"]] = {"file": f, "line": node.loc.file_loc.line}
            if self._keep(f):
                self.top.append(node)
            else:
                # Drop the subtree from the id index to save memory.
                for n in node.walk():
                    if n is not node:
                        self.nodes.pop(n.id, None)
        self.transparent_macros = sorted(unwrap.wrappers) if unwrap is not None else []
        self.unwrapped_locations = res.unwrapped

    def _keep(self, f: str | None) -> bool:
        if f is None:
            return False
        if f == self.main_file:
            return True
        return bool(self.keep_root) and f.startswith(self.keep_root + os.sep)

    def _build(self, raw: dict[str, Any], parent: Node | None, index: int, res: _LocResolver) -> Node:
        node = Node(raw, parent, index)
        # Emission order within a node: loc, range(begin, end), then the rest.
        for key, val in raw.items():
            if key == "loc":
                node.loc = res.loc(val)
            elif key == "range" and isinstance(val, dict):
                node.begin = res.loc(val.get("begin"))
                node.end = res.loc(val.get("end"))
            elif key == "inner" and isinstance(val, list):
                for j, c in enumerate(val):
                    if isinstance(c, dict) and c:
                        node.children.append(self._build(c, node, j, res))
                    else:
                        node.children.append(None)
            elif isinstance(val, dict):
                _consume_locs(val, res)
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, dict):
                        _consume_locs(v, res)
        if node.id:
            self.nodes[node.id] = node
            if raw.get("kind", "").endswith("Decl"):
                self.decl_files[node.id] = node.loc.file_loc.file
        return node

    def node(self, nid: str | None) -> Node | None:
        return self.nodes.get(nid) if nid else None

    def functions(self) -> Iterator[Node]:
        for n in self.top:
            if n.kind == "FunctionDecl" and any(c.kind == "CompoundStmt" for c in n.real_children()):
                yield n

    def all_nodes(self) -> Iterator[Node]:
        for t in self.top:
            yield from t.walk()

    def _build_index(self) -> None:
        refs: dict[str, list[Node]] = {}
        decls: dict[tuple[str | None, int | None, str | None], Node] = {}
        for n in self.all_nodes():
            if n.kind == "DeclRefExpr":
                did = n.referenced_decl_id
                if did:
                    refs.setdefault(did, []).append(n)
            elif n.kind in ("VarDecl", "ParmVarDecl"):
                loc = n.loc.file_loc
                decls.setdefault((loc.file, loc.offset, n.name), n)
        self._refs, self._decls = refs, decls

    def refs_to(self, decl_id: str) -> list[Node]:
        """DeclRefExprs naming ``decl_id`` (indexed once per unit)."""
        if not hasattr(self, "_refs"):
            self._build_index()
        return self._refs.get(decl_id, [])

    def var_decl_at(self, file: str, offset: int, name: str) -> Node | None:
        """The variable or parameter declared at a file offset (its name token)."""
        if not hasattr(self, "_decls"):
            self._build_index()
        return self._decls.get((file, offset, name))


def _consume_locs(d: dict[str, Any], res: _LocResolver) -> None:
    """Advance resolver state through location objects nested in attributes."""
    if "offset" in d:
        res.bare(d)
        return
    for k, v in d.items():
        if k == "includedFrom":
            continue
        if isinstance(v, dict):
            _consume_locs(v, res)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    _consume_locs(x, res)


def strip_parens_casts(n: Node | None, casts: tuple[str, ...] = ()) -> Node | None:
    """Skip ParenExpr and the named implicit cast kinds."""
    while n is not None:
        if n.kind == "ParenExpr":
            n = n.child(0)
        elif n.kind == "ImplicitCastExpr" and n.cast_kind in casts:
            n = n.child(0)
        else:
            return n
    return None


def load_unit_ast(manifest: dict[str, Any], unit_dir: Path, keep_root: str | None) -> TranslationUnit | None:
    key = manifest.get("ast_artifact")
    if not key:
        return None
    art = manifest["artifacts"][key]
    path = unit_dir / art["files"][0]["path"]
    from weaver.frontend.wrappers import unwrapper_for

    return TranslationUnit(path, manifest["directory"], manifest["file"], keep_root, unwrapper_for(manifest, unit_dir))
