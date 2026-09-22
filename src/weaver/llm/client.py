"""Optional Claude-backed explainer (install with ``pip install weaver[llm]``).

The model receives the planner instruction and a focused evidence slice, and
may call read-only evidence tools to request more context.  It cannot edit or
validate anything.  Every request and response is saved under
``<state>/llm/`` for audit.  Without the ``anthropic`` package or with
``--dry-run``, the assembled request is printed instead.
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
from weaver.llm.prompt import system_prompt
from weaver.llm.slice import build_slice, callers_of, source_lines
from weaver.recipes import RecipeContext, recipes_for_finding
from weaver.util import is_within, now_iso, write_json

DEFAULT_MODEL = "claude-opus-5"
MAX_TURNS = 8
MAX_SOURCE_LINES = 200


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "get_finding",
        "description": "Return one inventory finding (declaration, type, uses with lines, possible targets, "
        "configurations and evidence status) by its ID, e.g. 'P-1a2b3c4d5e'.",
        "input_schema": _schema({"finding_id": {"type": "string"}}, ["finding_id"]),
        "strict": True,
    },
    {
        "name": "find_findings",
        "description": "List pointer findings filtered by file (project-relative), function, name or kind "
        "(local, parameter, global, static-global, field, return, typedef). Use empty strings "
        "for filters you do not need. At most 50 results.",
        "input_schema": _schema(
            {
                "file": {"type": "string"},
                "function": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string"},
            },
            ["file", "function", "name", "kind"],
        ),
        "strict": True,
    },
    {
        "name": "get_source",
        "description": "Return numbered source lines [start_line, end_line] of a project file "
        f"(at most {MAX_SOURCE_LINES} lines).",
        "input_schema": _schema(
            {
                "file": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            ["file", "start_line", "end_line"],
        ),
        "strict": True,
    },
    {
        "name": "get_callers",
        "description": "Return the functions (file::name) whose analysed bodies call the named function "
        "directly. Calls through function pointers are not included and remain unknown.",
        "input_schema": _schema({"function": {"type": "string"}}, ["function"]),
        "strict": True,
    },
    {
        "name": "evaluate_recipes",
        "description": "Evaluate every applicable recipe on a finding and return each precondition's status "
        "(established / violated / unresolved) with evidence.",
        "input_schema": _schema({"finding_id": {"type": "string"}}, ["finding_id"]),
        "strict": True,
    },
    {
        "name": "get_coverage",
        "description": "Return which code lines of a project file no analysed configuration compiled "
        "(unexamined code is unknown evidence, not pointer-free).",
        "input_schema": _schema({"file": {"type": "string"}}, ["file"]),
        "strict": True,
    },
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
        "Explain this candidate for the reviewer. Evidence slice (JSON, produced by Weaver from compiler "
        "artifacts):\n\n" + json.dumps(sl, indent=1, default=str)
    )
    return system_prompt(), user, {"inventory": inv, "slice": sl, "model": model}


def _text(content: Any) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text")


def run_planner(
    client: Any,
    model: str,
    system: str,
    user: str,
    tools: EvidenceTools,
    fallbacks: bool = True,
    effort: str = "high",
    max_turns: int = MAX_TURNS,
) -> tuple[str, list[dict[str, Any]], str]:
    """Manual tool loop; returns (final text, transcript, stop reason)."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    transcript: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}
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
            tools=TOOL_DEFS,
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


def explain(
    project: Project, finding_id: str, dry_run: bool = False, model: str | None = None, fallbacks: bool | None = None
) -> str:
    model = model or os.environ.get("WEAVER_MODEL") or DEFAULT_MODEL
    if fallbacks is None:
        fallbacks = os.environ.get("WEAVER_LLM_FALLBACKS", "1") != "0"
    system, user, meta = build_request(project, finding_id, model)
    try:
        import anthropic  # noqa: F401
    except ImportError:
        anthropic = None  # type: ignore[assignment]
    if dry_run or anthropic is None:
        head = (
            ""
            if anthropic is not None
            else "# anthropic package not installed (pip install 'weaver[llm]'); showing the request instead\n"
        )
        return head + json.dumps(
            {"model": model, "system": system, "tools": [t["name"] for t in TOOL_DEFS], "user": user}, indent=1
        )

    client = anthropic.Anthropic()
    tools = EvidenceTools(project, meta["inventory"])
    try:
        text, transcript, stop = run_planner(client, model, system, user, tools, fallbacks=fallbacks)
    except anthropic.AuthenticationError as e:
        raise WeaverError(f"authentication failed ({e.message}); set ANTHROPIC_API_KEY or log in") from e
    except anthropic.RateLimitError as e:
        raise WeaverError(f"rate limited: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise WeaverError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise WeaverError(f"cannot reach the API: {e}") from e
    f = find_finding(meta["inventory"], finding_id)
    log = Path(project.state_dir) / "llm" / f"{f['id']}-{now_iso().replace(':', '')}.json"
    write_json(
        log,
        {
            "model": model,
            "finding": f["id"],
            "stop_reason": stop,
            "system": system,
            "user": user,
            "responses": transcript,
        },
    )
    return text + f"\n\n[advisory explanation from {model}; transcript: {log}]"
