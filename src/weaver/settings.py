"""Read and edit the parts of ``weaver.yaml`` a user tunes after setup: validation commands and the acceptance policy.

The editor rewrites ``weaver.yaml`` from its parsed form, so YAML comments are
not kept; the previous file is saved next to it as ``weaver.yaml.bak`` first,
and the new file is loaded back before the write is reported as done (an
invalid result restores the previous file).  Only the keys edited here change:
``acceptance``, ``preservation.concurrency`` and each profile's
``validation``.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Any

import yaml

from weaver.config import Project, load_project
from weaver.errors import ConfigError
from weaver.evidence import ValidationKind

POLICY_KINDS = [
    ValidationKind.COMPILE.value,
    ValidationKind.MECHANICAL_RECHECK.value,
    ValidationKind.TEST.value,
    ValidationKind.DIFFERENTIAL_TEST.value,
    ValidationKind.COVERAGE.value,
    ValidationKind.CONFIGURATION.value,
]
EVIDENCE_LEVELS = ["primary", "secondary-checked", "secondary-partial"]


def _cmd_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("run", "")
    if isinstance(raw, list):
        if len(raw) == 3 and raw[:2] == ["/bin/sh", "-c"]:
            return str(raw[2])
        return shlex.join(str(x) for x in raw)
    return str(raw)


def _cmd(raw: Any, default_name: str) -> dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"name": str(d.get("name", default_name)), "run": _cmd_text(raw)}
    if d.get("timeout") is not None:
        out["timeout"] = float(d["timeout"])
    if d.get("cwd") and d["cwd"] != "{workspace}":
        out["cwd"] = str(d["cwd"])
    return out


def _capture_text(pr: dict[str, Any]) -> str | None:
    cap = pr.get("capture")
    if not cap:
        return None
    cmd = _cmd_text(cap.get("command") if isinstance(cap, dict) else cap)
    tools = (cap.get("tools") if isinstance(cap, dict) else None) or {"cc": "cc"}
    for k, v in tools.items():
        cmd = cmd.replace("{" + str(k) + "}", str(v))
    return cmd


def read_settings(project: Project) -> dict[str, Any]:
    from weaver.testdetect import detect_tests
    from weaver.validate import configured_strength

    raw = yaml.safe_load(project.config_path.read_text()) or {}
    profiles = []
    for pr in raw.get("profiles") or []:
        val = pr.get("validation") or {}
        build = _capture_text(pr)
        profiles.append(
            {
                "id": str(pr.get("id")),
                "build": _cmd(val["build"], "build") if val.get("build") else None,
                "tests": [_cmd(t, f"test{j}") for j, t in enumerate(val.get("tests") or [])],
                "compare": [_cmd(t, f"compare{j}") for j, t in enumerate(val.get("compare") or [])],
                "coverage": bool(val.get("coverage", True)),
                "capture_build": build,
                "suggestions": detect_tests(project.root, build),
            }
        )
    acc = raw.get("acceptance") or {}
    return {
        "config": str(project.config_path),
        "acceptance": {
            "require": list(project.acceptance.require),
            "allow_provisional": project.acceptance.allow_provisional,
            "min_evidence": project.acceptance.min_evidence,
            "explicit": bool(acc),
        },
        "concurrency": (raw.get("preservation") or {}).get("concurrency"),
        "flow": {"backend": ((raw.get("flow") or {}).get("backend")) or "auto"},
        "profiles": profiles,
        "ai": ai_settings(project),
        "strength": configured_strength(project),
        "kinds": POLICY_KINDS,
        "evidence_levels": EVIDENCE_LEVELS,
    }


def ai_settings(project: Project) -> dict[str, Any]:
    """The AI section as the settings editor shows it.  Never contains a key, only where one comes from."""
    from weaver.config import AI_PROVIDERS
    from weaver.llm.keys import key_status
    from weaver.llm.prompt import GUIDE_VERSION

    ai = project.ai
    return {
        "enabled": ai.enabled,
        "provider": ai.provider,
        "model": ai.model or "",
        "default_model": AI_PROVIDERS[ai.provider]["model"] or "",
        "base_url": ai.base_url or "",
        "default_base_url": AI_PROVIDERS[ai.provider]["base_url"] or "",
        "key_env": ai.key_env,
        "key": key_status(ai),
        "tools": ai.tools,
        "drafts": ai.drafts,
        "guide_version": GUIDE_VERSION,
        "providers": list(AI_PROVIDERS),
    }


def _merge_cmd(new: dict[str, Any], old: list[Any], default_name: str) -> Any:
    """The YAML entry for an edited command, keeping fields the editor does not show (env, argv form)."""
    name = str(new.get("name") or default_name).strip() or default_name
    run = str(new.get("run") or "").strip()
    prev = next((o for o in old if isinstance(o, dict) and str(o.get("name")) == name), None)
    if prev is not None and _cmd_text(prev) == run and prev.get("timeout") == new.get("timeout"):
        return prev
    out: dict[str, Any] = {
        "name": name,
        "run": run,
        "cwd": str(new.get("cwd") or (prev or {}).get("cwd") or "{workspace}"),
    }
    if new.get("timeout"):
        out["timeout"] = float(new["timeout"])
    if prev and prev.get("env"):
        out["env"] = prev["env"]
    return out


def write_settings(project: Project, changes: dict[str, Any]) -> tuple[Project, list[str]]:
    """Apply edited settings to ``weaver.yaml``; returns the reloaded project and warnings."""
    path = project.config_path
    text = path.read_text()
    raw = yaml.safe_load(text) or {}
    warnings: list[str] = []

    if "acceptance" in changes:
        a = changes["acceptance"] or {}
        req = [k for k in a.get("require", []) if k in POLICY_KINDS]
        if ValidationKind.COMPILE.value not in req:
            raise ConfigError("the acceptance policy must require 'compile'")
        acc = dict(raw.get("acceptance") or {})
        acc["require"] = req
        if "allow_provisional" in a:
            acc["allow_provisional"] = bool(a["allow_provisional"])
        if a.get("min_evidence"):
            if a["min_evidence"] not in EVIDENCE_LEVELS:
                raise ConfigError(f"min_evidence must be one of {', '.join(EVIDENCE_LEVELS)}")
            acc["min_evidence"] = a["min_evidence"]
        raw["acceptance"] = acc
    if "concurrency" in changes:
        pres = dict(raw.get("preservation") or {})
        if changes["concurrency"]:
            pres["concurrency"] = str(changes["concurrency"])
        else:
            pres.pop("concurrency", None)
        raw["preservation"] = pres
    if "ai" in changes:
        from weaver.config import AI_PROVIDERS

        a = changes["ai"] or {}
        ai = dict(raw.get("ai") or {})
        if "enabled" in a:
            ai["enabled"] = bool(a["enabled"])
        if "drafts" in a:
            ai["drafts"] = bool(a["drafts"])
        if a.get("provider"):
            if a["provider"] not in AI_PROVIDERS:
                raise ConfigError(f"ai.provider must be one of {', '.join(AI_PROVIDERS)}")
            ai["provider"] = a["provider"]
        for k in ("model", "base_url"):
            if k in a:
                v = str(a[k] or "").strip()
                if v:
                    ai[k] = v
                else:
                    ai.pop(k, None)
        if ai.get("enabled") and ai.get("provider") == "openai-compatible" and not ai.get("model"):
            raise ConfigError("an OpenAI-compatible provider needs a model name")
        raw["ai"] = ai
    if (changes.get("flow") or {}).get("backend"):
        fl = dict(raw.get("flow") or {})
        fl["backend"] = changes["flow"]["backend"]
        raw["flow"] = fl

    by_id = {str(p.get("id")): p for p in raw.get("profiles") or []}
    for pc in changes.get("profiles") or []:
        pr = by_id.get(str(pc.get("id")))
        if pr is None:
            raise ConfigError(f"unknown profile {pc.get('id')!r}")
        val = dict(pr.get("validation") or {})
        if "build" in pc:
            b = pc["build"] or {}
            if str(b.get("run") or "").strip():
                val["build"] = _merge_cmd(b, [val.get("build")] if val.get("build") else [], "build")
            else:
                val.pop("build", None)
        for key in ("tests", "compare"):
            if key in pc:
                old = list(val.get(key) or [])
                items = [c for c in pc[key] or [] if str(c.get("run") or "").strip()]
                names = [str(c.get("name") or f"{key}{i}") for i, c in enumerate(items)]
                if len(set(names)) != len(names):
                    raise ConfigError(f"profile {pr['id']}: {key} names must be unique")
                val[key] = [_merge_cmd(c, old, f"{key}{i}") for i, c in enumerate(items)]
                if not val[key]:
                    val.pop(key)
        if "coverage" in pc:
            if pc["coverage"]:
                val.pop("coverage", None)  # on is the default
            else:
                val["coverage"] = False
        if (val.get("tests") or val.get("compare")) and not val.get("build"):
            warnings.append(
                f"profile {pr['id']}: tests are configured without a validation build; they will run against "
                "whatever the workspace copy contains unless the test command builds the program itself"
            )
        if val:
            pr["validation"] = val
        else:
            pr.pop("validation", None)

    new_text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    if new_text == yaml.safe_dump(yaml.safe_load(text) or {}, sort_keys=False, allow_unicode=True):
        return project, warnings
    backup = Path(str(path) + ".bak")
    shutil.copy2(path, backup)
    path.write_text(new_text)
    try:
        proj = load_project(path)
    except Exception:
        shutil.copy2(backup, path)
        raise
    from weaver.validate import configured_strength

    st = configured_strength(proj)
    warnings.extend(n for n in st["notes"] if n not in warnings and "without a validation build" not in n)
    return proj, warnings
