"""Read preprocessed output (``-E``) line markers.

Both GCC and Clang emit ``# <line> "<file>" <flags>`` markers.  Replaying them
gives, for each source file, the set of physical lines that produced at least
one token in this configuration.  Comparing that with the raw lexer's
token-bearing lines exposes code that no analysed configuration compiled.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_MARKER = re.compile(r'^#(?:line)?\s+(\d+)\s+"((?:[^"\\]|\\.)*)"')


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def active_lines(i_path: str | os.PathLike[str], directory: str) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    cur_file: str | None = None
    cur_line = 0
    cache: dict[str, str] = {}
    with open(Path(i_path), "rb") as f:
        for raw in f:
            line = raw.decode("latin-1").rstrip("\r\n")
            if line.startswith("#"):
                m = _MARKER.match(line)
                if m:
                    cur_line = int(m.group(1))
                    name = _unescape(m.group(2))
                    if name not in cache:
                        cache[name] = (
                            name
                            if name.startswith("<")
                            else os.path.realpath(name if os.path.isabs(name) else os.path.join(directory, name))
                        )
                    cur_file = cache[name]
                    continue
                # #pragma / #ident lines pass through: they are directives, not code.
                cur_line += 1
                continue
            if cur_file is not None and line.strip():
                out.setdefault(cur_file, set()).add(cur_line)
            cur_line += 1
    return out


def conditional_segments(path: str | os.PathLike[str]) -> list[tuple[int, int]]:
    """Maximal line ranges [first, last] of a file whose activity cannot differ internally.

    Whether a line is compiled can only change at a conditional directive, so
    the lines between consecutive conditional directives are all active or all
    inactive.  Comparing frontends per segment (rather than per line) ignores
    how each one lays out macro expansions in its preprocessed output.
    """
    from weaver.frontend.lexer import lex

    lx = lex(Path(path).read_bytes())
    last = len(lx.line_starts) - (1 if lx.text.endswith("\n") else 0)
    out: list[tuple[int, int]] = []
    start = 1
    for d in lx.directives:
        if d.is_conditional:
            if d.line_start > start:
                out.append((start, d.line_start - 1))
            start = d.line_end + 1
    if start <= last:
        out.append((start, last))
    return out
