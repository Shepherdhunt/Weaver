"""Configuration coverage: which source code no analysed configuration compiled.

Unparsed files and inactive configurations are *unexamined*, never pointer-free
(pointer-tracker plan §2).  For every project file seen by at least one unit,
the raw lexer's token-bearing lines are compared with the union of lines that
produced tokens in each unit's preprocessed output.  Lines inside a
parenthesised group that began on a covered line (multi-line macro arguments,
which ``-E`` folds onto the invocation line) count as covered unless a
directive intervenes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from weaver.frontend.lexer import LexResult, lex
from weaver.frontend.preproc import active_lines
from weaver.toolchain.options import SOURCE_EXTS
from weaver.util import is_within, rel_or_abs


def continuation_parent(lx: LexResult) -> dict[int, int]:
    """Map line -> line that opened the outermost paren group enclosing its first token."""
    out: dict[int, int] = {}
    stack: list[int] = []
    last_line = 0
    for t in lx.tokens:
        if t.directive is not None:
            continue
        if t.line != last_line:
            if stack:
                out[t.line] = stack[0]
            last_line = t.line
        if t.text in ("(", "["):
            stack.append(t.line)
        elif t.text in (")", "]") and stack:
            stack.pop()
    return out


def uncovered_lines(lx: LexResult, active: set[int]) -> list[int]:
    code = lx.code_lines()
    directive_lines: set[int] = set()
    for d in lx.directives:
        directive_lines.update(range(d.line_start, d.line_end + 1))
    parent = continuation_parent(lx)
    covered: dict[int, bool] = {}

    def is_cov(line: int, depth: int = 0) -> bool:
        if line in covered:
            return covered[line]
        ok = line in active
        if not ok and line in parent and depth < 10000:
            opener = parent[line]
            between = range(opener + 1, line)
            ok = is_cov(opener, depth + 1) and not any(b in directive_lines for b in between)
        covered[line] = ok
        return ok

    return sorted(line for line in code if not is_cov(line))


def ranges(lines: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for ln in lines:
        if out and ln == out[-1][1] + 1:
            out[-1][1] = ln
        else:
            out.append([ln, ln])
    return out


def compute_coverage(root: Path, manifests: list[dict[str, Any]], unit_dirs: dict[str, Path]) -> dict[str, Any]:
    per_file_active: dict[str, set[int]] = {}
    per_file_units: dict[str, set[str]] = {}
    missing_i: list[str] = []
    main_files: set[str] = set()
    for m in manifests:
        main_files.add(os.path.realpath(m["file"]))
        art = m["artifacts"].get("preprocess") or m["artifacts"].get("secondary.preprocess")
        if not art or art["status"] != "ok":
            missing_i.append(m["unit_id"])
            continue
        ipath = unit_dirs[m["unit_id"]] / art["files"][0]["path"]
        for f, lines in active_lines(ipath, m["directory"]).items():
            if f.startswith("<") or not is_within(f, root):
                continue
            per_file_active.setdefault(f, set()).update(lines)
            per_file_units.setdefault(f, set()).add(m["unit_id"])

    files: dict[str, Any] = {}
    total_code = total_uncov = 0
    for f in sorted(per_file_active):
        try:
            lx = lex(Path(f).read_bytes())
        except OSError:
            continue
        unc = uncovered_lines(lx, per_file_active[f])
        code = len(lx.code_lines())
        total_code += code
        total_uncov += len(unc)
        files[rel_or_abs(f, root)] = {
            "units": sorted(per_file_units[f]),
            "code_lines": code,
            "unexamined_lines": len(unc),
            "unexamined_ranges": ranges(unc),
        }

    # Project sources that no unit compiled or included.
    unparsed = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            if ext in SOURCE_EXTS or ext == ".h":
                p = os.path.realpath(os.path.join(dirpath, fn))
                if p not in per_file_active and p not in main_files:
                    unparsed.append(rel_or_abs(p, root))
    return {
        "files": files,
        "unparsed_files": sorted(unparsed),
        "units_without_preprocessed_output": missing_i,
        "totals": {"code_lines": total_code, "unexamined_lines": total_uncov},
    }


def unexamined_in_range(coverage: dict[str, Any], file_rel: str, line_lo: int, line_hi: int) -> list[list[int]]:
    """Unexamined line ranges of ``file_rel`` that intersect [line_lo, line_hi]."""
    info = coverage.get("files", {}).get(file_rel)
    if info is None:
        return [[line_lo, line_hi]]
    return [[max(a, line_lo), min(b, line_hi)] for a, b in info["unexamined_ranges"] if b >= line_lo and a <= line_hi]
