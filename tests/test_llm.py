"""The planner loop with a fake client: tool dispatch, read-only guarantees, refusals."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from conftest import build_project, needs_clang, run_cli

from weaver.config import load_project
from weaver.llm.client import TOOL_DEFS, EvidenceTools, build_request, run_planner
from weaver.llm.prompt import PLANNER_INSTRUCTION


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
    assert system.startswith(PLANNER_INSTRUCTION.splitlines()[0])
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
