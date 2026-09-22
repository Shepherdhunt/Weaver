"""Deterministic byte-range source edits.

Edits are computed against a specific file revision (its SHA-256) and carry the
exact bytes they expect to replace.  Applying them refuses stale input, rejects
overlaps, and produces a unified diff for review.  The LLM never writes
patches: all edits come from recipes through this module.
"""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from weaver.errors import StaleEvidenceError, WeaverError
from weaver.util import sha256_bytes


@dataclass
class Edit:
    file: str  # path relative to the project root
    start: int
    end: int
    expected: str  # current bytes (latin-1 decoded)
    replacement: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Edit":
        return cls(**d)


def check_overlaps(edits: list[Edit]) -> None:
    by_file: dict[str, list[Edit]] = {}
    for e in edits:
        if e.start > e.end:
            raise WeaverError(f"invalid edit range {e.start}>{e.end} in {e.file}")
        by_file.setdefault(e.file, []).append(e)
    for f, es in by_file.items():
        es.sort(key=lambda e: (e.start, e.end))
        for a, b in zip(es, es[1:]):
            if b.start < a.end or (b.start == a.end and a.start == a.end == b.start):
                raise WeaverError(f"overlapping edits in {f} at {a.start}-{a.end} and {b.start}-{b.end}")


def apply_to_bytes(data: bytes, edits: list[Edit]) -> bytes:
    text = data.decode("latin-1")
    out = []
    pos = 0
    for e in sorted(edits, key=lambda e: e.start):
        cur = text[e.start : e.end]
        if cur != e.expected:
            raise StaleEvidenceError(
                f"{e.file}:{e.start}-{e.end}: expected {e.expected!r}, found {cur!r} (source changed since analysis)"
            )
        out.append(text[pos : e.start])
        out.append(e.replacement)
        pos = e.end
    out.append(text[pos:])
    return "".join(out).encode("latin-1")


def apply_edits(root: Path, edits: list[Edit], expected_hashes: dict[str, str]) -> dict[str, tuple[bytes, bytes]]:
    """Compute new contents for each file; returns {file: (old_bytes, new_bytes)}.

    Nothing is written.  ``expected_hashes`` binds the edits to the analysed
    revision of each file.
    """
    check_overlaps(edits)
    out: dict[str, tuple[bytes, bytes]] = {}
    by_file: dict[str, list[Edit]] = {}
    for e in edits:
        by_file.setdefault(e.file, []).append(e)
    for f, es in by_file.items():
        data = (root / f).read_bytes()
        want = expected_hashes.get(f)
        if want is None:
            raise WeaverError(f"no analysed revision recorded for {f}")
        if sha256_bytes(data) != want:
            raise StaleEvidenceError(f"{f} changed since it was analysed; re-run collect/inventory")
        out[f] = (data, apply_to_bytes(data, es))
    return out


def unified_diff(changes: dict[str, tuple[bytes, bytes]], context: int = 3) -> str:
    parts = []
    for f in sorted(changes):
        old, new = changes[f]
        parts.extend(
            difflib.unified_diff(
                old.decode("latin-1").splitlines(keepends=True),
                new.decode("latin-1").splitlines(keepends=True),
                fromfile=f"a/{f}",
                tofile=f"b/{f}",
                n=context,
            )
        )
    return "".join(parts)


class OffsetMap:
    """Map byte offsets of the original file to the edited file."""

    def __init__(self, edits: list[Edit]):
        self.edits = sorted(edits, key=lambda e: e.start)

    def map(self, offset: int) -> int | None:
        """New offset of an original position; None if it lies inside a replaced range."""
        delta = 0
        for e in self.edits:
            if offset < e.start:
                break
            if offset < e.end:
                return None
            delta += len(e.replacement) - (e.end - e.start)
        return offset + delta

    def replaced_range(self, e: Edit) -> tuple[int, int]:
        """Range the replacement text of ``e`` occupies in the edited file."""
        delta = 0
        for x in self.edits:
            if x is e or (x.start == e.start and x.end == e.end):
                break
            delta += len(x.replacement) - (x.end - x.start)
        start = e.start + delta
        return start, start + len(e.replacement)
