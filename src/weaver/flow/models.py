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


# glibc's fortified (_chk) variants add arguments.  (position, count): ``count`` arguments are
# inserted before the library function's argument ``position``; None: appended after the last
# argument, so every index is unchanged.  A variant not listed here is used only when its model
# touches nothing but argument 0.
FORTIFY_INSERTS: dict[str, tuple[int, int] | None] = {
    **dict.fromkeys(
        ["memcpy", "memmove", "mempcpy", "memset", "strcpy", "stpcpy", "strncpy", "stpncpy", "strcat", "strncat"],
        None,
    ),
    **dict.fromkeys(["read", "pread", "readlink", "getcwd", "realpath", "gets", "ttyname_r", "confstr"], None),
    "printf": (0, 1),  # __printf_chk(flag, fmt, ...)
    "vprintf": (0, 1),
    "fprintf": (1, 1),  # __fprintf_chk(fp, flag, fmt, ...)
    "vfprintf": (1, 1),
    "dprintf": (1, 1),
    "vdprintf": (1, 1),
    "sprintf": (1, 2),  # __sprintf_chk(s, flag, slen, fmt, ...)
    "vsprintf": (1, 2),
    "snprintf": (2, 2),  # __snprintf_chk(s, maxlen, flag, slen, fmt, ...)
    "vsnprintf": (2, 2),
    "fread": (1, 1),  # __fread_chk(ptr, ptrlen, size, n, stream)
    "fread_unlocked": (1, 1),
    "fgets": (1, 1),  # __fgets_chk(s, size, n, stream)
    "fgets_unlocked": (1, 1),
    "recv": (3, 1),  # __recv_chk(fd, buf, len, buflen, flags)
    "recvfrom": (3, 1),
}


def _fortified(name: str) -> bool:
    return bool(re.search(r"_chk$", name))


def _remap(model: EffectModel, name: str, base: str) -> EffectModel | None:
    """The model of ``base`` with argument indices moved to where the fortified ``name`` takes them."""
    if base not in FORTIFY_INSERTS:
        touched = set(model.retains) | (set() if model.writes == "any" else set(model.writes))
        return model if touched <= {0} else None
    ins = FORTIFY_INSERTS[base]
    if ins is None:
        return model
    pos, count = ins

    def mv(i: int) -> int:
        return i if i < pos else i + count

    return EffectModel(
        name,
        model.writes if model.writes == "any" else [mv(i) for i in model.writes],
        model.calls_back,
        [*model.assumptions, f"fortified variant of {base}(): argument indices adjusted"],
        model.source,
        boundary=model.boundary,
        writes_owned=model.writes_owned,
        owns=model.owns,
        retains=[mv(i) for i in model.retains],
    )


class Models:
    def __init__(self, entries: dict[str, EffectModel]):
        self.entries = entries

    def lookup(self, name: str | None) -> EffectModel | None:
        if not name:
            return None
        if name in self.entries:
            return self.entries[name]
        base = normalise(name)
        model = self.entries.get(base)
        if model is None or not _fortified(name):
            return model  # __builtin_memcpy and friends have the library signature
        return _remap(model, name, base)

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
