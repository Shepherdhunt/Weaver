"""Project configuration (``weaver.yaml``).

The configuration records what the plans ask to be made explicit before any
edit: the profiles (compiler + target + build configuration), the preservation
contract, the provisional CLite capability model and the acceptance policy.
Values under ``target``/``platform`` are *recorded* facts supplied by the user;
Weaver never infers a target from an OS or product name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from weaver.errors import ConfigError

CONFIG_NAME = "weaver.yaml"
SCHEMA = "weaver.project/1"

DEFAULT_ACCEPTANCE_REQUIRE = ["compile", "mechanical-recheck"]


@dataclass
class CommandSpec:
    name: str
    run: list[str]
    cwd: str = "{workspace}"
    timeout: float = 600.0
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any, default_name: str) -> "CommandSpec":
        if isinstance(raw, list):
            return cls(name=default_name, run=[str(x) for x in raw])
        if isinstance(raw, str):
            return cls(name=default_name, run=["/bin/sh", "-c", raw])
        if not isinstance(raw, dict) or "run" not in raw:
            raise ConfigError(f"command spec {default_name!r} needs a 'run' entry")
        run = raw["run"]
        if isinstance(run, str):
            run = ["/bin/sh", "-c", run]
        return cls(
            name=str(raw.get("name", default_name)),
            run=[str(x) for x in run],
            cwd=str(raw.get("cwd", "{workspace}")),
            timeout=float(raw.get("timeout", 600.0)),
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        )

    def render(self, **subst: str) -> tuple[list[str], str, dict[str, str]]:
        def r(s: str) -> str:
            for k, v in subst.items():
                s = s.replace("{" + k + "}", v)
            return s

        return [r(a) for a in self.run], r(self.cwd), {k: r(v) for k, v in self.env.items()}


@dataclass
class ValidationSpec:
    build: CommandSpec | None = None
    tests: list[CommandSpec] = field(default_factory=list)
    compare: list[CommandSpec] = field(default_factory=list)


@dataclass
class SecondaryFrontend:
    compiler: str  # path or name of the analysis Clang
    extra_args: list[str] = field(default_factory=list)


@dataclass
class CaptureSpec:
    """How to rebuild the project through recording shims (``capture:`` in a profile).

    ``tools`` maps placeholder names to real executables, e.g. ``{cc: gcc}``;
    ``command`` may reference ``{cc}`` (replaced by the shim path) and ``{root}``.
    """

    command: CommandSpec
    tools: dict[str, str] = field(default_factory=lambda: {"cc": "cc"})
    clean: CommandSpec | None = None


@dataclass
class Profile:
    id: str
    compile_commands: Path
    description: str = ""
    link_manifest: Path | None = None
    target: dict[str, Any] = field(default_factory=dict)
    platform: dict[str, Any] = field(default_factory=dict)
    secondary_frontend: SecondaryFrontend | None = None
    recipes: list[str] | str = "default"
    validation: ValidationSpec = field(default_factory=ValidationSpec)
    capture: CaptureSpec | None = None


@dataclass
class AcceptancePolicy:
    require: list[str] = field(default_factory=lambda: list(DEFAULT_ACCEPTANCE_REQUIRE))
    allow_provisional: bool = False
    # Minimum evidence status a candidate's source facts must have.
    min_evidence: str = "secondary-checked"


@dataclass
class FlowConfig:
    """Flow-evidence settings (``flow:`` in weaver.yaml)."""

    svf_enabled: bool = True
    wpa: str | None = None  # explicit path to SVF's wpa; default: pysvf's bundled binary, then PATH
    timeout: float = 900.0
    memory_mb: int = 8192
    # Andersen with field-insensitive objects: fields are merged into their base object,
    # so a write to any part of an object is seen by queries about any other part.
    options: list[str] = field(default_factory=lambda: ["-ander", "-field-limit=0"])
    model_files: list[Path] = field(default_factory=list)
    externals: dict[str, Any] = field(default_factory=dict)


@dataclass
class Project:
    config_path: Path
    name: str
    root: Path
    state_dir: Path
    profiles: list[Profile]
    workspace_exclude: list[str]
    preservation: dict[str, Any]
    clite: dict[str, Any]
    acceptance: AcceptancePolicy
    raw: dict[str, Any]
    flow: FlowConfig = field(default_factory=FlowConfig)
    contracts_path: Path | None = None

    def profile(self, pid: str) -> Profile:
        for p in self.profiles:
            if p.id == pid:
                return p
        raise ConfigError(f"unknown profile {pid!r}; known: {', '.join(p.id for p in self.profiles)}")

    def select_profiles(self, ids: list[str] | None) -> list[Profile]:
        if not ids:
            return list(self.profiles)
        return [self.profile(i) for i in ids]


def find_config(start: str | os.PathLike[str] | None = None) -> Path:
    cur = Path(start or os.getcwd()).resolve()
    if cur.is_file():
        return cur
    for d in [cur, *cur.parents]:
        cand = d / CONFIG_NAME
        if cand.exists():
            return cand
    raise ConfigError(f"no {CONFIG_NAME} found in {cur} or its parents (run 'weaver init')")


def load_project(path: str | os.PathLike[str] | None = None) -> Project:
    cfg_path = find_config(path)
    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{cfg_path}: invalid YAML: {e}") from e
    if raw.get("schema", SCHEMA) != SCHEMA:
        raise ConfigError(f"{cfg_path}: unsupported schema {raw.get('schema')!r} (expected {SCHEMA})")
    base = cfg_path.parent
    proj = raw.get("project") or {}
    root = (base / proj.get("root", ".")).resolve()
    state_dir = (base / proj.get("state_dir", ".weaver")).resolve()

    profiles = []
    seen = set()
    for i, pr in enumerate(raw.get("profiles") or []):
        if "id" not in pr or "compile_commands" not in pr:
            raise ConfigError(f"profile #{i} needs 'id' and 'compile_commands'")
        pid = str(pr["id"])
        if pid in seen:
            raise ConfigError(f"duplicate profile id {pid!r}")
        seen.add(pid)
        sec = pr.get("secondary_frontend")
        secondary = None
        if sec:
            if isinstance(sec, str):
                sec = {"compiler": sec}
            secondary = SecondaryFrontend(
                compiler=str(sec.get("compiler", "clang")),
                extra_args=[str(a) for a in sec.get("extra_args", [])],
            )
        val = pr.get("validation") or {}
        validation = ValidationSpec(
            build=CommandSpec.parse(val["build"], "build") if val.get("build") else None,
            tests=[CommandSpec.parse(t, f"test{j}") for j, t in enumerate(val.get("tests") or [])],
            compare=[CommandSpec.parse(t, f"compare{j}") for j, t in enumerate(val.get("compare") or [])],
        )
        cap = pr.get("capture")
        capture = None
        if cap:
            cmd = CommandSpec.parse(cap.get("command") if isinstance(cap, dict) else cap, "capture")
            if cmd.cwd == "{workspace}":
                cmd.cwd = "{root}"
            clean = None
            if isinstance(cap, dict) and cap.get("clean"):
                clean = CommandSpec.parse(cap["clean"], "clean")
                if clean.cwd == "{workspace}":
                    clean.cwd = "{root}"
            capture = CaptureSpec(
                command=cmd,
                clean=clean,
                tools={
                    str(k): str(v)
                    for k, v in ((cap.get("tools") if isinstance(cap, dict) else None) or {"cc": "cc"}).items()
                },
            )
        profiles.append(
            Profile(
                capture=capture,
                id=pid,
                compile_commands=(base / pr["compile_commands"]).resolve(),
                description=str(pr.get("description", "")),
                link_manifest=(base / pr["link_manifest"]).resolve() if pr.get("link_manifest") else None,
                target=dict(pr.get("target") or {}),
                platform=dict(pr.get("platform") or {}),
                secondary_frontend=secondary,
                recipes=pr.get("recipes", "default"),
                validation=validation,
            )
        )
    acc = raw.get("acceptance") or {}
    acceptance = AcceptancePolicy(
        require=[str(x) for x in acc.get("require", DEFAULT_ACCEPTANCE_REQUIRE)],
        allow_provisional=bool(acc.get("allow_provisional", False)),
        min_evidence=str(acc.get("min_evidence", "secondary-checked")),
    )
    fl = raw.get("flow") or {}
    svf = fl.get("svf") or {}
    flow = FlowConfig(
        svf_enabled=bool(svf.get("enabled", True)),
        wpa=str(svf["wpa"]) if svf.get("wpa") else None,
        timeout=float(svf.get("timeout", 900.0)),
        memory_mb=int(svf.get("memory_mb", 8192)),
        options=[str(o) for o in svf.get("options", ["-ander", "-field-limit=0"])],
        model_files=[(base / m).resolve() for m in fl.get("models", [])],
        externals=dict(fl.get("externals") or {}),
    )
    return Project(
        flow=flow,
        contracts_path=(base / proj.get("contracts", "weaver-contracts.yaml")).resolve(),
        config_path=cfg_path,
        name=str(proj.get("name", root.name)),
        root=root,
        state_dir=state_dir,
        profiles=profiles,
        workspace_exclude=[str(x) for x in proj.get("workspace_exclude", [".git"])],
        preservation=dict(raw.get("preservation") or {}),
        clite=dict(raw.get("clite") or {}),
        acceptance=acceptance,
        raw=raw,
    )


TEMPLATE = """\
schema: weaver.project/1
project:
  name: {name}
  root: .
  state_dir: .weaver
  # Paths copied into isolated validation workspaces are everything under root
  # except these (the state dir is always excluded).
  workspace_exclude: [".git"]

# The behavior to preserve (pointer-tracker plan §1).  Recorded into every
# candidate card; edit to describe the real contract.
preservation:
  dialect: recorded_dialect
  behaviors: [outputs, persistent-state, side-effect-ordering, termination, errors, shared-state]
  interfaces: []        # binary interfaces / serialized layouts that must not change
  resources: {{}}         # memory, throughput and deadline requirements

# Provisional CLite capabilities.  A recipe that needs a capability that is not
# 'confirmed' stays blocked for CLite export (C-to-C simplification may proceed).
clite:
  value_records: provisional
  indexed_collections: provisional
  dynamic_storage: unknown
  typed_object_ids: unknown
  shared_mutable_storage: unknown
  platform_apis: unknown

acceptance:
  require: [compile, mechanical-recheck]   # add 'testing' / 'differential-testing' when configured
  allow_provisional: false
  min_evidence: secondary-checked

profiles:
  - id: native-dev
    description: pinned native development configuration
    compile_commands: build/compile_commands.json
    target:            # recorded facts, never inferred
      architecture: recorded_architecture
      endian: recorded_endian
      abi: recorded_abi
      cpu_features: []
      address_spaces: []
    platform:
      os_version: recorded_os_version
      runtime_mode: recorded_kernel_process_partition_or_bare_metal
    # For a non-Clang production compiler, name the analysis Clang here:
    # secondary_frontend: {{compiler: clang}}
    validation:
      # build: {{run: [make, -C, "{{workspace}}"], cwd: "{{workspace}}"}}
      # tests:   [{{name: unit, run: ["./build/tests"], cwd: "{{workspace}}"}}]
      # compare: [{{name: demo, run: ["./build/demo"], cwd: "{{workspace}}"}}]
"""
