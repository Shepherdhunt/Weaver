"""Parse the C type strings printed by Clang (``qualType``/``desugaredQualType``).

The JSON AST gives types as C spellings such as ``int *const``, ``void (*)(int)``
or ``struct S *[4]``.  This module parses them into a small type tree so the
inventory can distinguish pointers, arrays of pointers, function pointers and
typedef-hidden pointers without string heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

QUALIFIERS = {
    "const",
    "volatile",
    "restrict",
    "__restrict",
    "__restrict__",
    "_Nonnull",
    "_Nullable",
    "_Null_unspecified",
    "__ptr32",
    "__ptr64",
    "__unaligned",
    "__sptr",
    "__uptr",
}
TAG_KEYWORDS = {"struct", "union", "enum"}

_TOKEN = re.compile(r"\s*(\.\.\.|[A-Za-z_$][A-Za-z_0-9$]*|\d[\w.]*|[*()\[\],^&:<>]|\S)")


class TypeParseError(ValueError):
    pass


@dataclass
class CType:
    kind: str  # base | pointer | array | function | block
    quals: frozenset[str] = frozenset()
    name: str = ""  # base spelling
    inner: "CType | None" = None
    params: list["CType"] | None = None
    variadic: bool = False
    size: str | None = None
    atomic: bool = False
    extra: list[str] = field(default_factory=list)  # attributes etc.

    @property
    def is_pointer(self) -> bool:
        return self.kind == "pointer"

    @property
    def is_function_pointer(self) -> bool:
        return self.kind == "pointer" and self.inner is not None and self.inner.kind == "function"

    def spell(self) -> str:
        """Approximate C spelling (for display only)."""
        q = " ".join(sorted(self.quals))
        if self.kind == "base":
            return (q + " " if q else "") + self.name
        if self.kind == "pointer":
            return f"{self.inner.spell() if self.inner else '?'} *{(' ' + q) if q else ''}"
        if self.kind == "array":
            return f"{self.inner.spell() if self.inner else '?'} [{self.size or ''}]"
        if self.kind == "function":
            ps = ", ".join(p.spell() for p in self.params or [])
            return f"{self.inner.spell() if self.inner else '?'} ({ps}{', ...' if self.variadic else ''})"
        return self.kind


def _tokenize(s: str) -> list[str]:
    toks: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        # "(unnamed struct at file:line:col)" / "(anonymous ...)": one opaque token.
        m = re.match(r"\((unnamed|anonymous)\b", s[i:])
        if m:
            depth = 0
            j = i
            while j < n:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            toks.append(s[i : j + 1])
            i = j + 1
            continue
        m = _TOKEN.match(s, i)
        if not m:
            break
        toks.append(m.group(1))
        i = m.end()
    return toks


class _Parser:
    def __init__(self, s: str):
        self.s = s
        self.toks = _tokenize(s)
        self.i = 0

    def peek(self, k: int = 0) -> str | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def next(self) -> str:
        t = self.peek()
        if t is None:
            raise TypeParseError(f"unexpected end of type {self.s!r}")
        self.i += 1
        return t

    def expect(self, t: str) -> None:
        got = self.next()
        if got != t:
            raise TypeParseError(f"expected {t!r}, got {got!r} in {self.s!r}")

    def balanced(self) -> str:
        """Consume a balanced '(' ... ')' group and return its inner text."""
        self.expect("(")
        depth = 1
        parts = []
        while True:
            t = self.next()
            if t == "(":
                depth += 1
            elif t == ")":
                depth -= 1
                if depth == 0:
                    return " ".join(parts)
            parts.append(t)

    def parse_type(self) -> CType:
        base = self.specifiers()
        t = self.abstract(base)
        return t

    def specifiers(self) -> CType:
        quals: set[str] = set()
        names: list[str] = []
        atomic = False
        extra: list[str] = []
        inner_atomic: CType | None = None
        while True:
            t = self.peek()
            if t is None or t in ("*", "(", ")", "[", "]", ",", "^", "&"):
                if t == "(" and not names and inner_atomic is None:
                    raise TypeParseError(f"missing type specifier in {self.s!r}")
                break
            if t in QUALIFIERS:
                quals.add(self.next())
                continue
            if t == "_Atomic":
                self.next()
                if self.peek() == "(":
                    self.expect("(")
                    inner_atomic = self.parse_type()
                    self.expect(")")
                else:
                    atomic = True
                continue
            if t in ("__attribute__", "__attribute"):
                self.next()
                extra.append("__attribute__((" + self.balanced() + "))")
                continue
            if t in ("typeof", "__typeof__", "__typeof", "typeof_unqual"):
                self.next()
                names.append(t + "(" + self.balanced() + ")")
                continue
            if t in TAG_KEYWORDS:
                kw = self.next()
                nm = self.next()
                names.append(f"{kw} {nm}")
                continue
            if t == "...":
                break
            names.append(self.next())
        if inner_atomic is not None:
            inner_atomic.atomic = True
            inner_atomic.quals = inner_atomic.quals | frozenset(quals)
            return inner_atomic
        if not names:
            raise TypeParseError(f"no type specifier in {self.s!r}")
        return CType("base", frozenset(quals), " ".join(names), atomic=atomic, extra=extra)

    def abstract(self, base: CType) -> CType:
        # pointers bind looser than suffixes: T * D  ==> D(pointer(T))
        t = base
        while self.peek() in ("*", "^", "&"):
            op = self.next()
            quals = set()
            while self.peek() in QUALIFIERS or self.peek() in ("__attribute__",):
                if self.peek() == "__attribute__":
                    self.next()
                    self.balanced()
                    continue
                quals.add(self.next())
            t = CType("pointer" if op != "^" else "block", frozenset(quals), inner=t)
        return self.direct(t)

    def direct(self, t: CType) -> CType:
        grouped_start = None
        if self.peek() == "(" and self.peek(1) in ("*", "^", "(", "&"):
            # Parenthesised declarator: remember its tokens, apply after suffixes.
            self.next()
            grouped_start = self.i
            depth = 1
            while depth:
                tok = self.next()
                if tok == "(":
                    depth += 1
                elif tok == ")":
                    depth -= 1
            grouped_end = self.i - 1
        suffixes = []
        while self.peek() in ("[", "("):
            if self.peek() == "[":
                self.next()
                size = []
                while self.peek() != "]":
                    size.append(self.next())
                self.next()
                suffixes.append(("array", " ".join(size)))
            else:
                self.next()
                params, variadic = self.params()
                suffixes.append(("function", (params, variadic)))
        for kind, val in reversed(suffixes):
            if kind == "array":
                t = CType("array", inner=t, size=val or None)
            else:
                t = CType("function", inner=t, params=val[0], variadic=val[1])
        if grouped_start is not None:
            sub = _Parser.__new__(_Parser)
            sub.s = self.s
            sub.toks = self.toks[grouped_start:grouped_end]
            sub.i = 0
            t = sub.abstract(t)
            if sub.peek() is not None:
                raise TypeParseError(f"trailing tokens in declarator of {self.s!r}")
        return t

    def params(self) -> tuple[list[CType], bool]:
        params: list[CType] = []
        variadic = False
        if self.peek() == ")":
            self.next()
            return params, variadic
        while True:
            if self.peek() == "...":
                self.next()
                variadic = True
            else:
                params.append(self.parse_type())
            t = self.next()
            if t == ")":
                break
            if t != ",":
                raise TypeParseError(f"bad parameter list in {self.s!r}")
        if len(params) == 1 and params[0].kind == "base" and params[0].name == "void" and not params[0].quals:
            params = []
        return params, variadic


def parse_type(s: str) -> CType:
    p = _Parser(s)
    t = p.parse_type()
    if p.peek() is not None:
        raise TypeParseError(f"trailing tokens {p.toks[p.i :]} in {s!r}")
    return t


def safe_parse(s: str | None) -> CType | None:
    if not s:
        return None
    try:
        return parse_type(s)
    except (TypeParseError, RecursionError):
        return None


def contains_pointer(t: CType | None, typedefs: dict[str, str] | None = None, _depth: int = 0) -> bool:
    """True if the type is or contains (through arrays/typedefs) a pointer.

    Record types are *not* expanded here: pointer-bearing records are reported
    separately from their field declarations.
    """
    if t is None or _depth > 32:
        return False
    if t.kind in ("pointer", "block"):
        return True
    if t.kind == "array":
        return contains_pointer(t.inner, typedefs, _depth + 1)
    if t.kind == "function":
        return False
    if t.kind == "base" and typedefs and t.name in typedefs:
        return contains_pointer(safe_parse(typedefs[t.name]), typedefs, _depth + 1)
    return False


def resolve_typedefs(t: CType | None, typedefs: dict[str, str], _depth: int = 0) -> CType | None:
    """Replace a top-level typedef base by its definition (repeatedly)."""
    while t is not None and t.kind == "base" and t.name in typedefs and _depth < 32:
        inner = safe_parse(typedefs[t.name])
        if inner is None:
            return t
        inner.quals = inner.quals | t.quals
        t = inner
        _depth += 1
    return t
