"""Which project files mention each identifier, for textual reference scans over the whole tree.

The complete-caller checks ask "where else does this name appear?" of every
file in the project, including files no analysed configuration compiled.  Keeping
every file's token list to answer that costs memory proportional to the whole
code base (1.6 million tokens for cFS).  This index keeps only each file's set of
identifier spellings, inverted to identifier -> files; the caller re-lexes the
few files that actually mention a name.  Per-file sets are cached on disk, keyed
by path, size and modification time, so a later run lexes only changed files.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from weaver.frontend.lexer import lex

SCHEMA = "weaver.ident-index/1"


class IdentIndex:
    def __init__(self, root: Path, cache: Path | None = None):
        self.root = root
        self.cache = cache
        self._files: list[str] = []
        self._fid: dict[str, int] = {}
        self._by_name: dict[str, list[int]] = {}
        self._disk: dict[str, list] = {}
        self._dirty = False
        if cache is not None and cache.exists():
            try:
                raw = json.loads(cache.read_text())
                if raw.get("schema") == SCHEMA:
                    self._disk = raw.get("files", {})
            except (OSError, ValueError):
                self._disk = {}

    def _stat(self, rel: str) -> tuple[int, int] | None:
        try:
            st = os.stat(rel if os.path.isabs(rel) else self.root / rel)
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    def _add(self, rel: str) -> None:
        fid = self._fid[rel] = len(self._files)
        self._files.append(rel)
        st = self._stat(rel)
        if st is None:
            return
        cached = self._disk.get(rel)
        if cached is not None and cached[0] == st[0] and cached[1] == st[1]:
            names = cached[2] = [sys.intern(n) for n in cached[2]]
        else:
            path = rel if os.path.isabs(rel) else self.root / rel
            try:
                names = sorted({sys.intern(t.text) for t in lex(Path(path).read_bytes()).tokens if t.kind == "ident"})
            except OSError:
                return
            self._disk[rel] = [st[0], st[1], names]
            self._dirty = True
        for n in names:
            self._by_name.setdefault(n, []).append(fid)

    def files_with(self, name: str, files: list[str]) -> list[str]:
        """The files among ``files`` that contain the identifier ``name``, in the order given."""
        for rel in files:
            if rel not in self._fid:
                self._add(rel)
        self.save()
        wanted = set(files)
        hits = {self._files[i] for i in self._by_name.get(name, [])}
        return [f for f in files if f in hits and f in wanted]

    def save(self) -> None:
        if not (self._dirty and self.cache is not None):
            return
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"schema": SCHEMA, "files": self._disk}, separators=(",", ":")))
            os.replace(tmp, self.cache)
            self._dirty = False
        except OSError:
            pass  # a cache: failing to write it only costs time on the next run
