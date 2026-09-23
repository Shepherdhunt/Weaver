"""External-function effect models (reviewed defaults plus project models)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from weaver.errors import ConfigError

DEFAULTS = Path(__file__).resolve().parent.parent / "data" / "external_models.yaml"


@dataclass
class EffectModel:
    name: str
    writes: list[int] | str  # arg indices, or "any"
    calls_back: bool
    assumptions: list[str] = field(default_factory=list)
    source: str = "default"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "writes": self.writes,
            "calls_back": self.calls_back,
            "assumptions": self.assumptions,
            "source": self.source,
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


def _parse(path: Path, source: str) -> dict[str, EffectModel]:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read effect models {path}: {e}") from e
    return _entries(raw.get("functions") or {}, source)


def _entries(funcs: dict[str, Any], source: str) -> dict[str, EffectModel]:
    out = {}
    for name, spec in funcs.items():
        spec = spec or {}
        writes = spec.get("writes", "any")
        if writes != "any" and not (isinstance(writes, list) and all(isinstance(i, int) for i in writes)):
            raise ConfigError(f"model {name}: 'writes' must be a list of argument indices or 'any'")
        out[name] = EffectModel(
            name, writes, bool(spec.get("calls_back", True)), [str(a) for a in spec.get("assumptions", [])], source
        )
    return out


def load_models(project: Any) -> Models:
    entries = _parse(DEFAULTS, "weaver-default")
    flow = getattr(project, "flow", None)
    if flow is not None:
        for p in flow.model_files:
            entries.update(_parse(p, str(p)))
        entries.update(_entries(flow.externals, "weaver.yaml"))
    return Models(entries)
