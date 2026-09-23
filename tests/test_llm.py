"""AI explanations: the Claude loop with a fake client, the OpenAI-compatible loop against a fake server,
the shared guide, the on/off switch and bring-your-own-key storage."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from conftest import build_project, needs_clang, run_cli

from weaver.config import load_project
from weaver.llm.client import TOOL_DEFS, EvidenceTools, build_request, run_planner


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def msg(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks), stop_details=None, to_dict=lambda: {"stop_reason": stop})


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = build_project(tmp_path_factory.mktemp("llm"), [{"id": "clang", "cc": "clang"}])
    run_cli(root, "collect")
    run_cli(root, "inventory")
    return load_project(root)


def _fid(project, function, name):
    from weaver.analysis.inventory import load_inventory

    inv = load_inventory(project)
    return next(f["id"] for f in inv["findings"] if f.get("function") == function and f.get("name") == name)


@needs_clang
def test_slice_is_focused_and_source_linked(project):
    system, user, meta = build_request(project, _fid(project, "la_escape", "ep"), "claude-opus-5")
    assert system.startswith("# Weaver explanation guide")
    sl = meta["slice"]
    assert sl["finding"]["name"] == "ep" and sl["source_excerpt"]["lines"]
    call = next(u for u in sl["uses"] if u["kind"] == "call-arg")
    assert call["source"] == "util_touch(ep);"
    assert sl["recipes"][0]["eligible"] is False
    assert sl["configurations"][0]["production_compiler"]["family"] == "clang"


@needs_clang
def test_tool_loop_dispatches_and_confines_reads(project):
    fid = _fid(project, "la_escape", "ep")
    system, user, meta = build_request(project, fid, "claude-opus-5")
    fake = FakeMessages(
        [
            msg(
                "tool_use",
                NS(type="tool_use", id="a", name="get_callers", input={"function": "util_touch"}),
                NS(
                    type="tool_use",
                    id="b",
                    name="get_source",
                    input={"file": "../../etc/passwd", "start_line": 1, "end_line": 5},
                ),
                NS(type="tool_use", id="c", name="no_such_tool", input={}),
            ),
            msg("end_turn", NS(type="text", text="Recommendation: blocked.")),
        ]
    )
    client = NS(beta=NS(messages=fake))
    text, transcript, stop = run_planner(
        client, "claude-opus-5", system, user, EvidenceTools(project, meta["inventory"])
    )
    assert (text, stop) == ("Recommendation: blocked.", "end_turn")
    first = fake.calls[0]
    assert first["tools"] == TOOL_DEFS and first["thinking"] == {"type": "adaptive"}
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["extra_body"] == {"fallbacks": "default"}
    results = fake.calls[1]["messages"][-1]["content"]  # all results in ONE user message
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert json.loads(results[0]["content"])["direct_callers"] == ["src/alias.c::la_escape"]
    assert "not a file inside the project root" in results[1]["content"]
    assert results[2]["is_error"] is True
    assert {t["name"] for t in TOOL_DEFS}.isdisjoint({"write_file", "apply_patch", "run"})  # read-only


@needs_clang
def test_refusal_is_reported(project):
    system, user, meta = build_request(project, _fid(project, "la_basic", "p"), "claude-opus-5")
    fake = FakeMessages([NS(stop_reason="refusal", content=[], stop_details=NS(category="cyber"), to_dict=lambda: {})])
    text, _, stop = run_planner(
        NS(beta=NS(messages=fake)),
        "claude-opus-5",
        system,
        user,
        EvidenceTools(project, meta["inventory"]),
        fallbacks=False,
    )
    assert stop == "refusal" and "declined" in text
    assert "betas" in fake.calls[0] and fake.calls[0]["betas"] == [] and "extra_body" not in fake.calls[0]


@needs_clang
def test_dry_run_prints_request(project):
    from weaver.llm.client import explain

    out = explain(project, _fid(project, "la_basic", "p"), dry_run=True)
    req = json.loads(out[out.index("{") :])
    assert req["model"] == "claude-opus-5" and "get_finding" in req["tools"]


# ---------------------------------------------------------------------------------- provider-neutral feature
import dataclasses  # noqa: E402
import http.server  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import threading  # noqa: E402

from weaver.config import AIConfig  # noqa: E402
from weaver.errors import WeaverError  # noqa: E402
from weaver.llm.prompt import SECTIONS, missing_sections, system_prompt  # noqa: E402

ANSWER = "\n".join(f"### {s}\nNone." for s in SECTIONS)


@pytest.fixture()
def keydir(tmp_path, monkeypatch):
    monkeypatch.setenv("WEAVER_CONFIG_DIR", str(tmp_path / "cfg"))
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MY_KEY"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path / "cfg"


class FakeChat:
    """A Chat Completions server on loopback that replays scripted replies and records requests."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                data = json.dumps(outer.replies.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def reply(content=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish}]}


def test_ai_is_off_by_default_and_parsed(tmp_path):
    from weaver.config import _ai

    assert AIConfig().enabled is False and AIConfig().effective_model == "claude-opus-5"
    ai = _ai({"enabled": True, "provider": "openai-compatible", "base_url": "http://localhost:11434/v1/",
              "model": "llama3.1", "notes": "ai-notes.md"}, tmp_path)  # fmt: skip
    assert ai.effective_base_url == "http://localhost:11434/v1" and ai.key_id == "openai-compatible:localhost:11434"
    assert ai.key_env == "OPENAI_API_KEY" and ai.notes == (tmp_path / "ai-notes.md").resolve()
    with pytest.raises(Exception, match="unknown provider"):
        _ai({"provider": "someone"}, tmp_path)


def test_keys_live_outside_the_project_and_are_private(keydir, monkeypatch):
    from weaver.llm.keys import forget_key, keys_path, resolve_key, store_key

    ai = AIConfig(provider="anthropic")
    assert resolve_key(ai) == (None, "missing")
    store_key("anthropic", "  sk-stored  ")
    assert keys_path().parent == keydir and stat.S_IMODE(os.stat(keys_path()).st_mode) == 0o600
    assert resolve_key(ai)[0] == "sk-stored" and "stored on this machine" in resolve_key(ai)[1]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")  # the environment wins
    assert resolve_key(ai) == ("sk-env", "environment variable ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert forget_key("anthropic") and resolve_key(ai)[1] == "missing"
    local = AIConfig(provider="openai-compatible", base_url="http://localhost:11434/v1", model="m")
    assert resolve_key(local) == (None, "not needed (local model server)")


def test_guide_sections_and_project_notes(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("SB means software bus.")
    text = system_prompt(notes)
    assert text.startswith("# Weaver explanation guide") and text.rstrip().endswith("SB means software bus.")
    assert all(f"### {s}" in text for s in SECTIONS)
    assert missing_sections(ANSWER) == []
    assert missing_sections("## summary:\n### Risk\ntext") == SECTIONS[1:4] + SECTIONS[5:]


@needs_clang
def test_explain_is_refused_while_off(project, keydir):
    from weaver.llm.client import explain

    with pytest.raises(WeaverError, match="AI explanations are off"):
        explain(project, _fid(project, "la_basic", "p"))
    req = json.loads(explain(project, _fid(project, "la_basic", "p"), dry_run=True))  # a dry run sends nothing
    assert req["enabled"] is False and req["system"] == system_prompt()


@needs_clang
def test_openai_compatible_provider_gets_the_same_guide_and_tools(project, keydir):
    from weaver.llm.client import TOOL_SPECS, explain
    from weaver.llm.keys import store_key

    def call(cid, name, arguments):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}

    calls = [call("c1", "get_callers", '{"function": "util_touch"}'), call("c2", "get_source", "not json")]
    server = FakeChat([reply(tool_calls=calls, finish="tool_calls"), reply(ANSWER)])
    try:
        ai = AIConfig(enabled=True, provider="openai-compatible", base_url=server.url, model="test-model")
        store_key(ai.key_id, "sk-test")
        proj = dataclasses.replace(project, ai=ai)
        out = explain(proj, _fid(project, "la_escape", "ep"))
    finally:
        server.close()
    first, second = server.requests
    assert first["path"] == "/v1/chat/completions" and first["auth"] == "Bearer sk-test"
    body = first["body"]
    assert body["model"] == "test-model" and body["messages"][0] == {"role": "system", "content": system_prompt()}
    assert [t["function"]["name"] for t in body["tools"]] == [t["name"] for t in TOOL_SPECS]
    tool_msgs = [m for m in second["body"]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert json.loads(tool_msgs[0]["content"])["direct_callers"] == ["src/alias.c::la_escape"]
    assert tool_msgs[1]["content"].startswith("Error: the arguments are not valid JSON")
    assert out.startswith("### Summary") and "missing sections" not in out
    log = json.loads(open(out.rsplit("transcript: ", 1)[1].rstrip("]")).read())
    assert log["provider"] == "openai-compatible" and log["guide_version"] == 1 and log["missing_sections"] == []


@needs_clang
def test_answers_outside_the_guide_are_flagged(project, keydir):
    from weaver.llm.client import explain

    server = FakeChat([reply("It is probably fine.")])
    try:
        ai = AIConfig(enabled=True, provider="openai-compatible", base_url=server.url, model="m", tools=False)
        out = explain(dataclasses.replace(project, ai=ai), _fid(project, "la_basic", "p"))
    finally:
        server.close()
    assert "tools" not in server.requests[0]["body"]  # evidence tools switched off
    assert "does not follow the explanation guide; missing sections: Summary" in out


def test_plain_http_only_to_this_machine():
    from weaver.llm.openai_compat import check_endpoint

    assert check_endpoint("http://localhost:11434/v1/") == "http://localhost:11434/v1/chat/completions"
    with pytest.raises(WeaverError, match="plain HTTP"):
        check_endpoint("http://models.example.com/v1")
