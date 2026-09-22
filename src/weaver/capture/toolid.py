"""Identify the executable that actually compiled each file.

A product name is insufficient to select an adapter (compiler plan, preamble):
the identity records the resolved path, a content hash, the ``--version``
banner and the family inferred from predefined macros.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from weaver.evidence import ToolRef
from weaver.util import run, sha256_file


@dataclass(frozen=True)
class ToolIdentity:
    requested: str
    path: str | None
    realpath: str | None
    sha256: str | None
    version_banner: str
    family: str  # clang | gcc | unknown
    version: str  # e.g. "18.1.3" (from predefined macros when available)
    target: str  # -dumpmachine output when supported

    def ref(self) -> ToolRef:
        return ToolRef(
            path=self.realpath or self.requested,
            sha256=self.sha256,
            family=self.family,
            version=self.version,
        )

    def to_json(self) -> dict:
        return dict(self.__dict__)


def resolve_executable(name: str, cwd: str | None = None) -> str | None:
    if os.sep in name:
        p = Path(name)
        if not p.is_absolute() and cwd:
            p = Path(cwd) / p
        return str(p) if p.exists() else None
    return shutil.which(name)


def _macros(path: str) -> dict[str, str]:
    r = run([path, "-E", "-dM", "-x", "c", os.devnull], timeout=60)
    out: dict[str, str] = {}
    if not r.ok:
        return out
    for line in r.stdout.decode(errors="replace").splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == "#define":
            out[parts[1]] = parts[2] if len(parts) == 3 else ""
    return out


@lru_cache(maxsize=64)
def identify(name: str, cwd: str | None = None) -> ToolIdentity:
    path = resolve_executable(name, cwd)
    if path is None:
        return ToolIdentity(name, None, None, None, "", "unknown", "", "")
    real = os.path.realpath(path)
    try:
        digest = sha256_file(real)
    except OSError:
        digest = None
    banner = run([path, "--version"], timeout=60)
    banner_text = (banner.stdout or banner.stderr).decode(errors="replace").strip()
    macros = _macros(path)
    family = "unknown"
    version = ""
    if "__clang__" in macros:
        family = "clang"
        version = ".".join(macros.get(k, "?") for k in ("__clang_major__", "__clang_minor__", "__clang_patchlevel__"))
    elif "__GNUC__" in macros:
        family = "gcc"
        version = ".".join(macros.get(k, "?") for k in ("__GNUC__", "__GNUC_MINOR__", "__GNUC_PATCHLEVEL__"))
    dm = run([path, "-dumpmachine"], timeout=60)
    target = dm.stdout.decode(errors="replace").strip() if dm.ok else ""
    return ToolIdentity(
        requested=name,
        path=path,
        realpath=real,
        sha256=digest,
        version_banner=banner_text.splitlines()[0] if banner_text else "",
        family=family,
        version=version,
        target=target,
    )
