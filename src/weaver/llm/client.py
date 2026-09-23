"""AI explanations: off unless the project turns them on, with the customer's own key.

The model receives the explanation guide (``weaver.llm.prompt``) and a focused
evidence slice, and may call read-only evidence tools to request more context.
It cannot edit or validate anything.  Every request and response is saved under
``<state>/llm/`` for audit.  ``--dry-run`` prints the assembled request instead
and sends nothing.

Providers (``ai.provider``):

* ``anthropic``: Claude through Anthropic's official SDK (``pip install
  weaver[llm]``), with adaptive thinking, an effort level and server-side refusal
  fallbacks.
* ``openai-compatible``: any server that speaks the OpenAI-style Chat Completions
  API, hosted or local (``weaver.llm.openai_compat``; standard library only).

Every provider gets the same guide, slice and tools, and its answer is checked
for the guide's sections, so explanations read the same whichever model gives
them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from weaver.analysis.coverage import unexamined_in_range
from weaver.analysis.inventory import find_finding, load_inventory
from weaver.config import Project
from weaver.errors import WeaverError
from weaver.llm.prompt import GUIDE_VERSION, SECTIONS, missing_sections, system_prompt
from weaver.llm.slice import build_slice, callers_of, source_lines
from weaver.recipes import RecipeContext, recipes_for_finding
from weaver.util import is_within, now_iso, write_json

DEFAULT_MODEL = "claude-opus-5"  # Claude; other providers name their model in ai.model
MAX_TURNS = 8
MAX_SOURCE_LINES = 200


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# The read-only evidence tools, described once for every provider.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_finding",
        "description": "Return one inventory finding (declaration, type, uses with lines, possible targets, "
        "configurations and evidence status) by its ID, e.g. 'P-1a2b3c4d5e'.",
        "parameters": _schema({"finding_id": {"type": "string"}}, ["finding_id"]),
    },
    {
        "name": "find_findings",
        "description": "List pointer findings filtered by file (project-relative), function, name or kind "
        "(local, parameter, global, static-global, field, return, typedef). Use empty strings "
        "for filters you do not need. At most 50 results.",
        "parameters": _schema(
            {
                "file": {"type": "string"},
                "function": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string"},
            },
            ["file", "function", "name", "kind"],
        ),
    },
    {
        "name": "get_source",
        "description": "Return numbered source lines [start_line, end_line] of a project file "
        f"(at most {MAX_SOURCE_LINES} lines).",
        "parameters": _schema(
            {
                "file": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            ["file", "start_line", "end_line"],
        ),
    },
    {
        "name": "get_callers",
        "description": "Return the functions (file::name) whose analysed bodies call the named function "
        "directly. Calls through function pointers are not included and remain unknown.",
        "parameters": _schema({"function": {"type": "string"}}, ["function"]),
    },
    {
        "name": "evaluate_recipes",
        "description": "Evaluate every applicable recipe on a finding and return each precondition's status "
        "(established / violated / unresolved) with evidence.",
        "parameters": _schema({"finding_id": {"type": "string"}}, ["finding_id"]),
    },
    {
        "name": "get_coverage",
        "description": "Return which code lines of a project file no analysed configuration compiled "
        "(unexamined code is unknown evidence, not pointer-free).",
        "parameters": _schema({"file": {"type": "string"}}, ["file"]),
    },
]


# Claude's tool format (strict: inputs always match the schema)
TOOL_DEFS: list[dict[str, Any]] = [
    {"name": t["name"], "description": t["description"], "input_schema": t["parameters"], "strict": True}
    for t in TOOL_SPECS
]


class EvidenceTools:
    """Read-only evidence access for the model."""

    def __init__(self, project: Project, inv: dict[str, Any]):
        self.project = project
        self.inv = inv
        self.ctx = RecipeContext(project, inv)

    def call(self, name: str, args: dict[str, Any]) -> str:
        fn: Callable[..., Any] | None = getattr(self, "t_" + name, None)
        if fn is None:
            raise WeaverError(f"unknown tool {name!r}")
        return json.dumps(fn(**args), default=str)

    def t_get_finding(self, finding_id: str) -> Any:
        f = find_finding(self.inv, finding_id)
        return {k: v for k, v in f.items() if k not in ("decl_span",)}

    def t_find_findings(self, file: str, function: str, name: str, kind: str) -> Any:
        out = []
        for f in self.inv["findings"]:
            if (
                (file and f.get("file") != file)
                or (function and f.get("function") != function)
                or (name and f.get("name") != name)
                or (kind and f.get("kind") != kind)
            ):
                continue
            out.append(
                {
                    k: f.get(k)
                    for k in (
                        "id",
                        "kind",
                        "name",
                        "type",
                        "file",
                        "line",
                        "function",
                        "record",
                        "use_summary",
                        "evidence_status",
                    )
                }
            )
            if len(out) >= 50:
                break
        return out

    def t_get_source(self, file: str, start_line: int, end_line: int) -> Any:
        p = (self.project.root / file).resolve()
        if not is_within(p, self.project.root) or not p.is_file():
            return {"error": f"{file} is not a file inside the project root"}
        end_line = min(end_line, start_line + MAX_SOURCE_LINES - 1)
        return {"file": file, "lines": source_lines(self.project.root, file, start_line, end_line)}

    def t_get_callers(self, function: str) -> Any:
        return {
            "function": function,
            "direct_callers": callers_of(self.inv, function),
            "note": "indirect calls are unresolved and not listed",
        }

    def t_evaluate_recipes(self, finding_id: str) -> Any:
        f = find_finding(self.inv, finding_id)
        return [
            {
                "recipe": r.id,
                **{
                    k: v
                    for k, v in r.evaluate(self.ctx, f).to_json().items()
                    if k in ("eligible", "preconditions", "capabilities_required")
                },
            }
            for r in recipes_for_finding(f)
        ]

    def t_get_coverage(self, file: str) -> Any:
        info = self.inv["coverage"]["files"].get(file)
        if info is None:
            return {"file": file, "status": "no analysed unit compiled or included this file (unexamined)"}
        return {"file": file, **info, "unexamined_in_file": unexamined_in_range(self.inv["coverage"], file, 1, 1 << 30)}


def build_request(project: Project, finding_id: str, model: str) -> tuple[str, str, dict[str, Any]]:
    inv = load_inventory(project)
    sl = build_slice(project, finding_id, inv)
    user = (
        "Explain this pointer for the reviewer, following the explanation guide and its answer format "
        f"({'; '.join(SECTIONS)}). Evidence slice (JSON, produced by Weaver from compiler artifacts):\n\n"
        + json.dumps(sl, indent=1, default=str)
    )
    return system_prompt(project.ai.notes), user, {"inventory": inv, "slice": sl, "model": model}


def _text(content: Any) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text")


def run_planner(
    client: Any,
    model: str,
    system: str,
    user: str,
    tools: EvidenceTools | None,
    fallbacks: bool = True,
    effort: str = "high",
    max_turns: int = MAX_TURNS,
) -> tuple[str, list[dict[str, Any]], str]:
    """Claude's manual tool loop; returns (final text, transcript, stop reason)."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    transcript: list[dict[str, Any]] = []
    extra: dict[str, Any] = {"tools": TOOL_DEFS} if tools is not None else {}
    betas: list[str] = []
    if fallbacks:
        # Server-side refusal fallback (Claude API): re-run a declined request on
        # Anthropic's recommended model for that refusal category.
        betas.append("server-side-fallback-2026-07-01")
        extra["extra_body"] = {"fallbacks": "default"}
    stop = "max_turns"
    last_text = ""
    for _ in range(max_turns):
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            betas=betas,
            **extra,
        )
        transcript.append(resp.to_dict() if hasattr(resp, "to_dict") else dict(resp))
        stop = resp.stop_reason
        last_text = _text(resp.content) or last_text
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            return f"[the model declined this request (category: {cat})]", transcript, stop
        if stop in ("tool_use", "pause_turn"):
            messages.append({"role": "assistant", "content": resp.content})
            if stop == "pause_turn":
                continue
            results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                try:
                    if tools is None:
                        raise WeaverError("evidence tools are turned off for this project (ai.tools)")
                    out = tools.call(block.name, dict(block.input))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                except (WeaverError, TypeError, ValueError, KeyError) as e:
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {e}", "is_error": True}
                    )
            messages.append({"role": "user", "content": results})
            continue
        return last_text, transcript, stop
    return last_text + f"\n[stopped after {max_turns} model turns]", transcript, stop


def _anthropic(
    project: Project, key: str | None, model: str, system: str, user: str, tools: EvidenceTools | None, fallbacks: bool
) -> tuple[str, list[dict[str, Any]], str]:
    try:
        import anthropic
    except ImportError as e:
        raise WeaverError("the anthropic package is not installed: pip install 'weaver[llm]'") from e
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    try:
        return run_planner(client, model, system, user, tools, fallbacks=fallbacks, effort=project.ai.effort)
    except anthropic.AuthenticationError as e:
        raise WeaverError(
            f"Anthropic rejected the key ({e.message}); set {project.ai.key_env} or save a key in Settings"
        ) from e
    except anthropic.NotFoundError as e:
        raise WeaverError(f"Anthropic does not know model {model!r} ({e.message}); check ai.model") from e
    except anthropic.RateLimitError as e:
        raise WeaverError(f"rate limited: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise WeaverError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise WeaverError(f"cannot reach the Anthropic API: {e}") from e


def explain(
    project: Project, finding_id: str, dry_run: bool = False, model: str | None = None, fallbacks: bool | None = None
) -> str:
    """Ask the project's configured AI provider to explain one finding (advisory only)."""
    from weaver.llm import openai_compat
    from weaver.llm.keys import resolve_key

    ai = project.ai
    model = model or os.environ.get("WEAVER_MODEL") or ai.effective_model
    if fallbacks is None:
        fallbacks = os.environ.get("WEAVER_LLM_FALLBACKS", "1") != "0"
    if dry_run:
        system, user, _ = build_request(project, finding_id, model or "(no model configured)")
        return json.dumps(
            {
                "enabled": ai.enabled,
                "provider": ai.provider,
                "model": model,
                "base_url": ai.effective_base_url,
                "guide_version": GUIDE_VERSION,
                "system": system,
                "tools": [t["name"] for t in TOOL_SPECS] if ai.tools else [],
                "user": user,
            },
            indent=1,
        )
    if not ai.enabled:
        raise WeaverError(
            "AI explanations are off for this project; turn them on in Settings or with 'weaver ai enable'"
        )
    if not model:
        raise WeaverError(f"ai.model is required for provider {ai.provider!r}")
    key, source = resolve_key(ai)
    if key is None and source == "missing" and ai.provider != "anthropic":
        raise WeaverError(
            f"no API key for {ai.effective_base_url}: set {ai.key_env} or save a key in Settings ('weaver ai key')"
        )
    system, user, meta = build_request(project, finding_id, model)
    tools = EvidenceTools(project, meta["inventory"]) if ai.tools else None
    if ai.provider == "anthropic":
        text, transcript, stop = _anthropic(project, key, model, system, user, tools, fallbacks)
    else:
        text, transcript, stop = openai_compat.run_chat(ai.effective_base_url or "", key, model, system, user, tools)
    missing = missing_sections(text) if stop not in ("refusal", "content_filter") else []
    if missing:
        text += "\n\n[Weaver: this answer does not follow the explanation guide; missing sections: " + (
            ", ".join(missing) + "]"
        )
    f = find_finding(meta["inventory"], finding_id)
    log = Path(project.state_dir) / "llm" / f"{f['id']}-{now_iso().replace(':', '')}.json"
    write_json(
        log,
        {
            "provider": ai.provider,
            "base_url": ai.effective_base_url,
            "model": model,
            "guide_version": GUIDE_VERSION,
            "finding": f["id"],
            "stop_reason": stop,
            "missing_sections": missing,
            "system": system,
            "user": user,
            "responses": transcript,
        },
    )
    return text + f"\n\n[advisory explanation from {ai.provider}/{model}; transcript: {log}]"
