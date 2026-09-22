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
