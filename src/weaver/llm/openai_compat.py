"""Explanations from any server that speaks the OpenAI-style Chat Completions API.

This covers hosted services and local model servers (Ollama, vLLM, LM Studio,
llama.cpp's server) through one wire format: ``POST {base_url}/chat/completions``
with ``messages``, optional function ``tools``, and ``tool_calls`` in the reply.
It uses the standard library only.  Claude is never reached through this
adapter; ``ai.provider: anthropic`` uses Anthropic's own SDK.

The model receives exactly what Claude receives: the explanation guide as the
system message, the evidence slice, and Weaver's read-only evidence tools.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from weaver.errors import WeaverError

MAX_TURNS = 8
TIMEOUT = 600.0


def tool_defs() -> list[dict[str, Any]]:
    from weaver.llm.client import TOOL_SPECS

    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
        }
        for t in TOOL_SPECS
    ]


def check_endpoint(base_url: str) -> str:
    """The chat-completions URL; plain HTTP is allowed only to this machine (the key would travel in clear)."""
    u = urlparse(base_url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise WeaverError(f"ai.base_url must be an http(s) URL, not {base_url!r}")
    if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise WeaverError(f"ai.base_url {base_url} is plain HTTP to another machine; use https")
    return base_url.rstrip("/") + "/chat/completions"


def _post(url: str, key: str | None, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - the URL is checked above
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        if e.code in (401, 403):
            raise WeaverError(f"{urlparse(url).netloc} rejected the key (HTTP {e.code}): {detail}") from e
        raise WeaverError(f"{urlparse(url).netloc}: HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise WeaverError(f"cannot reach {url}: {e.reason}") from e
    except (ValueError, TimeoutError) as e:
        raise WeaverError(f"{url}: unreadable reply ({e})") from e


def run_chat(
    base_url: str,
    key: str | None,
    model: str,
    system: str,
    user: str,
    tools: Any,
    max_turns: int = MAX_TURNS,
) -> tuple[str, list[dict[str, Any]], str]:
    """Tool loop over Chat Completions; returns (final text, transcript, finish reason)."""
    url = check_endpoint(base_url)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    transcript: list[dict[str, Any]] = []
    last_text, finish = "", "max_turns"
    for _ in range(max_turns):
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools is not None:
            body["tools"] = tool_defs()
            body["tool_choice"] = "auto"
        resp = _post(url, key, body)
        transcript.append(resp)
        try:
            choice = resp["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise WeaverError(f"{urlparse(url).netloc}: reply has no message: {str(resp)[:300]}") from e
        finish = choice.get("finish_reason") or "stop"
        last_text = msg.get("content") or last_text
        calls = msg.get("tool_calls") or []
        if calls:
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                try:
                    if tools is None:
                        raise WeaverError("evidence tools are turned off for this project (ai.tools)")
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                    out = tools.call(str(fn.get("name")), args)
                except json.JSONDecodeError as e:
                    out = f"Error: the arguments are not valid JSON ({e.msg})"
                except (WeaverError, TypeError, ValueError, KeyError) as e:
                    out = f"Error: {e}"
                messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
            continue
        if finish == "content_filter":
            return "[the provider filtered this response]", transcript, finish
        return last_text, transcript, finish
    return last_text + f"\n[stopped after {max_turns} model turns]", transcript, finish
