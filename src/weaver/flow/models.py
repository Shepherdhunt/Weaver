"""External-function effect models (reviewed defaults plus project models)."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from weaver.errors import ConfigError

DEFAULTS = Path(__file__).resolve().parent.parent / "data" / "external_models.yaml"
POSIX = Path(__file__).resolve().parent.parent / "data" / "models" / "posix.yaml"


@dataclass
class EffectModel:
    name: str
    writes: list[int] | str  # arg indices, or "any"
    calls_back: bool
    assumptions: list[str] = field(default_factory=list)
    source: str = "default"
    # Judge calls by this model even when the definition is analysed (an API boundary).
    boundary: bool = False
    # The call may write the framework's own state: objects declared in ``owns`` paths.
    writes_owned: bool = False
    owns: list[str] = field(default_factory=list)
    # Pointer arguments the function keeps after returning (reviewed models state this explicitly).
    retains: list[int] = field(default_factory=list)

    def owned(self, file: str | None) -> bool:
        return bool(file) and any(fnmatch.fnmatch(file, g) for g in self.owns)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "writes": self.writes,
            "calls_back": self.calls_back,
            "assumptions": self.assumptions,
            "source": self.source,
            "boundary": self.boundary,
            "writes_owned": self.writes_owned,
            "owns": self.owns,
            "retains": self.retains,
        }


def normalise(name: str) -> str:
    """Map fortified and builtin spellings to the library name (``__builtin___memcpy_chk`` -> ``memcpy``)."""
    n = name
    if n.startswith("__builtin_"):
        n = n[len("__builtin_") :]
    m = re.fullmatch(r"_*(\w+?)_chk", n)
    if m:
        n = m.group(1)
    return n.lstrip("_") if n.startswith("__") else n


class Models:
    def __init__(self, entries: dict[str, EffectModel]):
        self.entries = entries

    def lookup(self, name: str | None) -> EffectModel | None:
        if not name:
            return None
        return self.entries.get(name) or self.entries.get(normalise(name))

    def is_boundary(self, name: str | None) -> bool:
        m = self.entries.get(name) if name else None  # boundaries are exact names, never normalised
        return bool(m and m.boundary)


def _parse(path: Path, source: str) -> dict[str, EffectModel]:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read effect models {path}: {e}") from e
    owns = [str(g) for g in raw.get("owns") or []]
    return _entries(raw.get("functions") or {}, source, owns)


def _entries(funcs: dict[str, Any], source: str, owns: list[str] | None = None) -> dict[str, EffectModel]:
    out = {}
    for name, spec in funcs.items():
        spec = spec or {}
        writes = spec.get("writes", "any")
        if writes != "any" and not (isinstance(writes, list) and all(isinstance(i, int) for i in writes)):
            raise ConfigError(f"model {name}: 'writes' must be a list of argument indices or 'any'")
        wo = bool(spec.get("writes_owned", False))
        pack_owns = [str(g) for g in spec.get("owns", owns or [])]
        if wo and not pack_owns:
            raise ConfigError(f"model {name}: 'writes_owned' needs the pack's 'owns' paths")
        out[name] = EffectModel(
            name,
            writes,
            bool(spec.get("calls_back", True)),
            [str(a) for a in spec.get("assumptions", [])],
            source,
            boundary=bool(spec.get("boundary", False)),
            writes_owned=wo,
            owns=pack_owns,
            retains=[int(i) for i in spec.get("retains", [])],
        )
    return out


def load_models(project: Any) -> Models:
    entries = _parse(DEFAULTS, "weaver-default")
    entries.update(_parse(POSIX, "weaver-posix"))
    flow = getattr(project, "flow", None)
    if flow is not None:
        for p in flow.model_files:
            entries.update(_parse(p, str(p)))
        entries.update(_entries(flow.externals, "weaver.yaml"))
    return Models(entries)
