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
    # fnmatch patterns (relative to the project root, or absolute) for compiled sources that are not
    # part of the program, e.g. generated test harnesses; build-system probes are always excluded.
    exclude: list[str] = field(default_factory=list)


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
    # Points-to backends (``flow.backend``): 'svf' (separate AGPL tool, optional) and/or 'gcc' (the production
    # GCC's own -fipa-pta).  'auto' uses every backend that is available for a profile.
    backend: str = "auto"
    backends: list[str] = field(default_factory=lambda: ["svf", "gcc"])
    # 'all': every backend with evidence must say "no write" before a may-modify precondition holds;
    # 'any': one backend's "no" suffices (disagreements are still recorded).  A "yes" always wins.
    agreement: str = "all"

    def uses(self, backend: str) -> bool:
        return backend in self.backends


# AI providers for explanations (``ai.provider``): Claude through Anthropic's SDK, or any server that
# speaks the OpenAI-style Chat Completions API (hosted services and local model servers alike).
AI_PROVIDERS: dict[str, dict[str, str | None]] = {
    "anthropic": {"model": "claude-opus-5", "key_env": "ANTHROPIC_API_KEY", "base_url": None},
    "openai-compatible": {"model": None, "key_env": "OPENAI_API_KEY", "base_url": "https://api.openai.com/v1"},
}


@dataclass
class AIConfig:
    """AI explanations (``ai:`` in weaver.yaml).  Off unless enabled; keys never live in this file."""

    enabled: bool = False
    provider: str = "anthropic"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None  # the environment variable holding the key (default per provider)
    tools: bool = True  # let the model call Weaver's read-only evidence tools
    drafts: bool = False  # let the model draft patches (sends the affected functions' source)
    effort: str = "high"  # Claude only
    notes: Path | None = None  # project-specific notes appended to the explanation guide

    @property
    def effective_model(self) -> str | None:
        return self.model or AI_PROVIDERS[self.provider]["model"]

    @property
    def effective_base_url(self) -> str | None:
        return (self.base_url or AI_PROVIDERS[self.provider]["base_url"] or "").rstrip("/") or None

    @property
    def key_env(self) -> str:
        return self.api_key_env or str(AI_PROVIDERS[self.provider]["key_env"])

    @property
    def key_id(self) -> str:
        """Which stored key this configuration uses: one per provider, or per endpoint host."""
        if self.provider == "anthropic":
            return "anthropic"
        from urllib.parse import urlparse

        return "openai-compatible:" + (urlparse(self.effective_base_url or "").netloc or "default")


def _ai(raw: Any, base: Path) -> AIConfig:
    if not raw:
        return AIConfig()
    if not isinstance(raw, dict):
        raise ConfigError(
            "ai: must be a mapping (enabled, provider, model, base_url, api_key_env, tools, drafts, notes)"
        )
    provider = str(raw.get("provider") or "anthropic")
    if provider not in AI_PROVIDERS:
        raise ConfigError(f"ai.provider: unknown provider {provider!r}; use {' or '.join(AI_PROVIDERS)}")
    effort = str(raw.get("effort") or "high")
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("ai.effort must be low, medium, high, xhigh or max")
    return AIConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=provider,
        model=str(raw["model"]) if raw.get("model") else None,
        base_url=str(raw["base_url"]) if raw.get("base_url") else None,
        api_key_env=str(raw["api_key_env"]) if raw.get("api_key_env") else None,
        tools=bool(raw.get("tools", True)),
        drafts=bool(raw.get("drafts", False)),
        effort=effort,
        notes=(base / str(raw["notes"])).resolve() if raw.get("notes") else None,
    )


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
    # Images that run together (see weaver.link): [{name, images, entry_points, closed, profile}]
    programs: list[dict[str, Any]] = field(default_factory=list)
    ai: AIConfig = field(default_factory=AIConfig)

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


def _model_path(base: Path, ref: str) -> Path:
    """A reviewed effect-model file: ``builtin:<name>`` ships with Weaver, anything else is a path."""
    if ref.startswith("builtin:"):
        p = Path(__file__).resolve().parent / "data" / "models" / f"{ref.removeprefix('builtin:')}.yaml"
        if not p.exists():
            raise ConfigError(f"no built-in effect models named {ref!r}")
        return p
    return (base / ref).resolve()


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
                exclude=[str(x) for x in ((cap.get("exclude") if isinstance(cap, dict) else None) or [])],
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
    backend = fl.get("backend", "auto")
    if isinstance(backend, list):
        backends = [str(b) for b in backend]
        backend = ",".join(backends)
    else:
        backend = str(backend)
        backends = {"auto": ["svf", "gcc"], "none": []}.get(
            backend, [b.strip() for b in backend.split(",") if b.strip()]
        )
    if unknown := [b for b in backends if b not in ("svf", "gcc")]:
        raise ConfigError(f"flow.backend: unknown backend(s) {unknown}; use auto, svf, gcc or none")
    agreement = str(fl.get("agreement", "all"))
    if agreement not in ("all", "any"):
        raise ConfigError("flow.agreement must be 'all' or 'any'")
    flow = FlowConfig(
        backend=backend,
        backends=backends,
        agreement=agreement,
        svf_enabled=bool(svf.get("enabled", True)) and "svf" in backends,
        wpa=str(svf["wpa"]) if svf.get("wpa") else None,
        timeout=float(svf.get("timeout", 900.0)),
        memory_mb=int(svf.get("memory_mb", 8192)),
        options=[str(o) for o in svf.get("options", ["-ander", "-field-limit=0"])],
        model_files=[_model_path(base, str(m)) for m in fl.get("models", [])],
        externals=dict(fl.get("externals") or {}),
    )
    return Project(
        ai=_ai(raw.get("ai"), base),
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
        programs=_programs(raw.get("programs") or []),
    )


def _programs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ConfigError("'programs' must be a list of {name, images, entry_points}")
    out = []
    for i, p in enumerate(raw):
        if not isinstance(p, dict) or not p.get("name") or not isinstance(p.get("images"), list):
            raise ConfigError(f"programs[{i}] needs a 'name' and a list of 'images'")
        out.append(
            {
                "name": str(p["name"]),
                "images": [str(x) for x in p["images"]],
                "entry_points": [str(x) for x in p.get("entry_points") or []],
                "closed": bool(p.get("closed", True)),
                "profile": str(p["profile"]) if p.get("profile") else None,
                "notes": str(p.get("notes", "")),
            }
        )
    return out


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

# AI explanations and drafts: optional and off by default.  Every provider gets the
# same guides ('weaver ai guide').  Keys never go in this file: set the provider's
# environment variable, or store one for your account ('weaver ai key').
ai:
  enabled: false
  drafts: false                # also let the model draft patches (sends whole functions' source)
  provider: anthropic          # or openai-compatible: OpenAI, Ollama, vLLM, LM Studio...
  # model: claude-opus-5
  # base_url: http://localhost:11434/v1   # openai-compatible only; a local server keeps code on this machine

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
