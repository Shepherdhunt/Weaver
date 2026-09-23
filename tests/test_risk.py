"""Pointer risk: each factor from the facts behind it, ranking and totals, the CLI and the web view."""

from __future__ import annotations

import json

import pytest
from conftest import HAVE_SVF, run_cli
from test_simplify import _project, ln, needs_build

from weaver.analysis.inventory import load_inventory
from weaver.config import load_project
from weaver.risk import FACTORS, level, pointer_risk, report


@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    root = _project(tmp_path_factory.mktemp("risk"))
    if HAVE_SVF:
        assert run_cli(root, "flow") == 0
    return root


def _by(rep, function, name):
    return next(r for r in rep["pointers"] if r["function"] == function and r["name"] == name)


def _ids(row):
    return {x["id"] for x in row["factors"]}


def test_levels_and_scores_are_the_sum_of_factor_weights():
    f = {
        "kind": "local",
        "uses": [
            {"kind": "cast", "line": 3, "detail": {"cast_kind": "PointerToIntegral"}},
            {"kind": "arith", "line": 4, "detail": {"op": "+"}},
            {"kind": "compare", "line": 5, "detail": {"op": "!=", "null": True}},
            {"kind": "compare", "line": 6, "detail": {"op": "=="}},
        ],
    }
    r = pointer_risk(f, [[{"id": 1, "kind": "heap", "name": "malloc"}, {"id": 2, "kind": "dummy"}]])
    expect = {"integer-conversion", "escapes", "arithmetic", "null-tested", "compared", "heap", "unknown-target"}
    assert _ids(r) == expect
    assert r["score"] == sum(FACTORS[x]["weight"] for x in _ids(r)) and r["level"] == "high"
    assert r["factors"][0]["id"] == "integer-conversion"  # heaviest first
    assert (level(7), level(6), level(4), level(3)) == ("high", "medium", "medium", "low")
    verdicts = {"scalar-input": {"preconditions": [{"id": "SI.no-concurrent-writers", "status": "violated",
                                                    "evidence": ["task T may write x"]}]}}  # fmt: skip
    assert "concurrent-writer" in _ids(pointer_risk({"kind": "parameter", "uses": []}, [], verdicts))
    assert _ids(pointer_risk({"kind": "local", "uses": []}, None)) == {"no-flow-evidence"}


@needs_build
def test_factors_come_from_the_code(proj):
    p = load_project(proj)
    rep = report(p, load_inventory(p))
    q = _by(rep, "addr", "q")
    assert "integer-conversion" in _ids(q)
    assert q["factors"][0]["evidence"][0].startswith(f"line {ln('(unsigned long)q')}")
    fresh = _by(rep, "fresh", "p")
    assert {"arithmetic", "escapes"} <= _ids(fresh)
    assert "function-pointer" in _ids(_by(rep, "apply", "f"))
    if HAVE_SVF:
        assert "heap" in _ids(fresh) and "no-flow-evidence" not in _ids(fresh)
    scores = [r["score"] for r in rep["pointers"]]
    assert scores == sorted(scores, reverse=True)
    sm = rep["summary"]
    assert sm["pointers"] == len(rep["pointers"]) == sm["high"] + sm["medium"] + sm["low"]
    assert rep["files"][0]["name"] == "all.c" and rep["files"][0]["score"] == sum(scores)
    assert rep["factors"]["integer-conversion"]["pointers"] >= 1


@needs_build
def test_risk_cli_and_web_view(proj, capsys):
    from weaver.web.api import Cache, map_model, pointer_detail, pointer_list, risk_view

    p = load_project(proj)
    cache = Cache()
    rep = risk_view(p, cache)
    rows = {r["id"]: r for r in pointer_list(p, cache)["pointers"]}
    top = rep["pointers"][0]
    assert rows[top["id"]]["risk"] == top["score"] and rows[top["id"]]["risk_level"] == top["level"]
    assert pointer_detail(p, top["id"], cache)["risk"]["factors"] == top["factors"]
    assert sum(map_model(p, cache)["files"][0]["risk"].values()) == len(rep["pointers"])
    capsys.readouterr()
    assert run_cli(proj, "risk", "--json", "--level", top["level"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pointers"] and all(r["level"] == top["level"] for r in out["pointers"])
