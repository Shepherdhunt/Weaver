"""Forwarding wrapper macros that only a secondary frontend sees.

With ``_FORTIFY_SOURCE``, glibc gives compilers without
``__builtin_va_arg_pack`` (Clang) function-like macros such as::

    #define printf(...) __printf_chk (__USE_FORTIFY_LEVEL - 1, __VA_ARGS__)

while GCC sees ``printf`` as an always-inline function making the same call.
Left alone, every unit that calls ``printf`` would differ between the
frontends, and every expression spelled inside ``printf(...)`` would look like
macro-expanded text that cannot be edited.

A secondary-only macro is *transparent* for pointer facts when

* the production compiler does not define the name at all, so for production
  the invocation is an ordinary call spelled in the file;
* the secondary definition is function-like and its whole replacement list is a
  single call ``T(a1, ..., an)`` where ``T`` is the fortified or builtin
  spelling of the same library function (it normalises to the macro's name,
  so it shares the reviewed effect model);
* every macro parameter (or ``__VA_ARGS__``) appears exactly once, as a
  complete top-level argument of that call, never stringified or pasted, and
  the remaining arguments mention no parameter.

Then each argument expression is evaluated once, unchanged, as an argument of
the same library call under both compilers.  The AST loader treats tokens in
such an invocation's arguments as plain file text at their spelling location,
unless the argument text names a function-like macro (tokens of a nested
invocation's arguments carry the same outermost expansion location, so they
stay marked as macro text).
"""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

from weaver.frontend.lexer import lex


def _normalise(name: str) -> str:
    from weaver.flow.models import normalise

    return normalise(name)


def _params(sig: str) -> list[str] | None:
    inner = sig.strip()
    if not (inner.startswith("(") and inner.endswith(")")):
        return None
    out = []
    for p in (x.strip() for x in inner[1:-1].split(",")):
        if not p:
            continue
        if p == "...":
            out.append("__VA_ARGS__")
        elif p.endswith("..."):  # GNU named variadic parameter
            out.append(p[:-3].strip())
        else:
            out.append(p)
    return out


def forwarding_target(name: str, definition: str) -> str | None:
    """The forwarded-to function if ``definition`` (as read by ``read_macros``) is a transparent wrapper."""
    if not definition.startswith("(fn)("):
        return None
    close = definition.find(")", 4)
    params = _params(definition[4 : close + 1])
    body = definition[close + 1 :].strip()
    if params is None or not body:
        return None
    toks = lex(body).tokens
    if len(toks) < 3 or toks[0].kind != "ident" or toks[1].text != "(" or toks[-1].text != ")":
        return None
    if any(t.text in ("#", "##", "%:", "%:%:") for t in toks):
        return None
    # the '(' after the target must close at the very end
    depth, args, cur = 0, [], []
    for i, t in enumerate(toks[1:], start=1):
        if t.text in ("(", "[", "{"):
            depth += 1
            if depth == 1:
                continue
        elif t.text in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                if i != len(toks) - 1:
                    return None
                args.append(cur)
                break
        if depth == 1 and t.text == ",":
            args.append(cur)
            cur = []
        else:
            cur.append(t.text)
    target = toks[0].text
    if target == name or _normalise(target) != _normalise(name):
        return None
    pset = set(params)
    for p in params:
        if sum(a == [p] for a in args) != 1:
            return None
    for a in args:
        if a != [] and len(a) == 1 and a[0] in pset:
            continue
        if pset & set(a):
            return None
    return target


def transparent_wrappers(production: dict[str, str], secondary: dict[str, str]) -> dict[str, str]:
    """name -> forwarded-to function, for secondary-only transparent wrapper macros."""
    out = {}
    for name, d in secondary.items():
        if name in production:
            continue
        t = forwarding_target(name, d)
        if t:
            out[name] = t
    return out


class ArgUnwrapper:
    """Decides whether a macro-argument location may be read as plain file text."""

    def __init__(self, wrappers: dict[str, str], macro_names: set[str]):
        self.wrappers = wrappers
        self.macro_names = macro_names
        self._lexed: dict[str, Any] = {}
        self._regions: dict[tuple[str, int], tuple[int, int] | None] = {}
        self._token_starts: dict[str, list[int]] = {}

    def _lex(self, path: str) -> Any:
        if path not in self._lexed:
            try:
                self._lexed[path] = lex(Path(path).read_bytes())
            except OSError:
                self._lexed[path] = None
        return self._lexed[path]

    def _starts(self, path: str, lx: Any) -> list[int]:
        if path not in self._token_starts:
            self._token_starts[path] = [t.start for t in lx.tokens]
        return self._token_starts[path]

    def region(self, path: str, invocation: int) -> tuple[int, int] | None:
        """Argument text [start, end) of a transparent wrapper invoked at ``invocation``, or None."""
        key = (path, invocation)
        if key in self._regions:
            return self._regions[key]
        res = None
        lx = self._lex(path)
        if lx is not None:
            toks = lx.tokens
            i = bisect.bisect_left(self._starts(path, lx), invocation)
            if (
                i + 2 < len(toks)
                and toks[i].start == invocation
                and toks[i].text in self.wrappers
                and toks[i + 1].text == "("
            ):
                depth = 0
                for j in range(i + 1, len(toks)):
                    t = toks[j]
                    if t.text == "(":
                        depth += 1
                    elif t.text == ")":
                        depth -= 1
                        if depth == 0:
                            inner = toks[i + 2 : j]
                            if not any(x.kind == "ident" and x.text in self.macro_names for x in inner) and not any(
                                x.in_directive for x in inner
                            ):
                                res = (toks[i + 1].end, t.start)
                            break
        self._regions[key] = res
        return res

    def plain(self, path: str, invocation: int, spelling: int) -> bool:
        r = self.region(path, invocation)
        return r is not None and r[0] <= spelling < r[1]


def argument_capturing(macros: dict[str, str]) -> set[str]:
    """Macros whose invocation can take arguments: function-like ones, and object-like ones naming one.

    Tokens of a nested invocation's arguments carry the same outermost expansion
    location as the wrapper's own arguments, so an argument region mentioning
    any of these cannot be read as plain text.
    """
    fn = {n for n, d in macros.items() if d.startswith("(fn)(")}
    out = set(fn)
    for n, d in macros.items():
        if n not in fn and any(t.kind == "ident" and t.text in fn for t in lex(d).tokens):
            out.add(n)
    return out


def read_unit_wrappers(unit_dir: Path) -> tuple[dict[str, str], set[str]]:
    from weaver.toolchain.collect import read_macros

    prod = read_macros(unit_dir / "unit.macros.txt")
    sec = read_macros(unit_dir / "secondary.macros.txt")
    if not prod or not sec:
        return {}, set()
    wrappers = transparent_wrappers(prod, sec)
    return wrappers, argument_capturing(sec) if wrappers else set()


def unwrapper_for(manifest: dict[str, Any], unit_dir: Path) -> ArgUnwrapper | None:
    """An unwrapper for a unit whose AST evidence came from the secondary frontend."""
    if not (manifest.get("ast_artifact") or "").startswith("secondary."):
        return None
    wrappers, names = read_unit_wrappers(unit_dir)
    return ArgUnwrapper(wrappers, names) if wrappers else None
