"""Flow evidence (SVF), the may-modify query and the scalar-input interface recipe.

src/params.c holds one positive example or counterexample per precondition of
the scalar-input recipe (read-only scalar input becomes a value parameter).
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import (
    RAND_MODEL,
    SINGLE_THREADED,
    build_project,
    needs_clang,
    needs_gcc,
    needs_svf,
    run_cli,
    validation_for,
)
from test_pipeline import finding_id, verdicts

from weaver.analysis.inventory import load_inventory
from weaver.config import load_project
from weaver.flow.evidence import load_flow
from weaver.flow.models import load_models
from weaver.flow.program import Program, may_modify
from weaver.ledger import Ledger

SCALAR_ELIGIBLE = {
    ("p_scale", "factor"),
    ("p_sum2", "a"),
    ("p_sum2", "b"),
    ("p_noisy", "v"),
    ("p_via_ptr", "v"),
}
# Counterexamples whose only failing precondition is the one they illustrate.
SCALAR_BLOCKED_ONLY = {
    ("p_read_after_touch", "v"): "SI.no-modification-during-call",  # p_touch() writes *w, and w may alias v
    ("p_snapshot", "c"): "SI.no-modification-during-call",  # p_bump() writes p_counter by name
    ("p_cb", "v"): "SI.complete-callers",  # address taken: calls through p_hook are not analysable
    ("p_pair", "a"): "SI.call-sites",  # p_pair(&w, w++): reading at the call site would be unsequenced
}
# Counterexamples that fail (at least) the named precondition.
SCALAR_BLOCKED_BY = {
    ("p_maybe", "m"): "SI.read-only-uses",  # null test
    ("p_cond", "v"): "SI.unconditional-read",
    ("p_set", "out"): "SI.read-only-uses",  # write through
    ("p_touch", "q"): "SI.read-only-uses",
    ("p_read_after_touch", "w"): "SI.read-only-uses",  # passed on
    ("e_through_pointer", "pp"): "SI.parameter-type",  # struct target
    ("e_release", "pp"): "SI.parameter-type",  # pointer target
    ("util_touch", "p"): "SI.read-only-uses",  # copied into a global
}


def _project(tmp, *, concurrency=True, model=True, differential=False):
    extra = {}
    if concurrency:
        extra["preservation"] = SINGLE_THREADED
    if model:
        extra["flow"] = RAND_MODEL
    prof = {"id": "clang", "cc": "clang"}
    if differential:
        prof["validation"] = validation_for("clang")
        extra["acceptance"] = {"require": ["compile", "mechanical-recheck", "differential-testing"]}
    root = build_project(tmp, [prof], extra=extra)
    assert run_cli(root, "collect") == 0
    assert run_cli(root, "inventory") == 0
    return root


@pytest.fixture(scope="module")
def flow_project(tmp_path_factory):
    root = _project(tmp_path_factory.mktemp("flow"))
    assert run_cli(root, "flow") == 0
    return root


@needs_svf
def test_flow_run_is_complete_and_resolves_indirect_calls(flow_project):
    proj = load_project(flow_project)
    inv = load_inventory(proj)
    fe = load_flow(proj, "clang", inv)
    assert fe is not None and fe.complete, fe and fe.run
    assert fe.provenance()["svf"].startswith("pysvf")
    main = inv["functions"]["src/main.c::main"]
    (call,) = main["indirect_calls"]  # p_hook(&k)
    site = call["site"]
    assert fe.indirect_targets(site["file"], site["line"], site["col"]) == ["p_cb"]


@needs_svf
def test_may_modify(flow_project):
    proj = load_project(flow_project)
    inv = load_inventory(proj)
    prog = Program(inv, load_models(proj))
    flows = {"clang": load_flow(proj, "clang", inv)}

    def mod(fn, idx):
        return may_modify(prog, f"src/params.c::{fn}", idx, flows, None)

    assert mod("p_scale", 0).status == "no"
    assert mod("p_sum2", 0).status == "no" and mod("p_sum2", 1).status == "no"
    r = mod("p_read_after_touch", 0)
    assert r.status == "yes" and any(x["function"] == "p_touch" for x in r.reasons)
    assert mod("p_snapshot", 0).status == "yes"
    noisy = mod("p_noisy", 0)
    assert noisy.status == "no" and any("rand" in a for a in noisy.assumptions)  # reviewed model

    # Without the reviewed model the call into rand() is an unknown effect: never "no".
    bare = Program(inv, load_models(None))  # the shipped defaults only
    assert may_modify(bare, "src/params.c::p_noisy", 0, flows, None).status == "unknown"


@needs_svf
def test_scalar_input_verdicts(flow_project):
    v = verdicts(flow_project, "scalar-input")
    assert {k for k, bl in v.items() if not bl} == SCALAR_ELIGIBLE
    for key, pre in SCALAR_BLOCKED_ONLY.items():
        assert v[key] == [pre], (key, v[key])
    for key, pre in SCALAR_BLOCKED_BY.items():
        assert pre in v[key], (key, v[key])


@needs_clang
def test_interface_recipes_need_declared_concurrency_and_models(tmp_path):
    root = _project(tmp_path, concurrency=False, model=False)
    v = verdicts(root, "scalar-input")
    assert v[("p_scale", "factor")] == ["SI.no-concurrent-writers"]
    assert "SI.no-modification-during-call" in v[("p_noisy", "v")]  # rand() has no reviewed model


@needs_svf
def test_scalar_input_transactions_preserve_behavior(tmp_path):
    root = _project(tmp_path, differential=True)
    assert run_cli(root, "flow") == 0
    before = subprocess.run(["./build/clang/demo"], cwd=root, capture_output=True, text=True).stdout

    led = Ledger(load_project(root))
    txn = led.propose(finding_id(root, "p_scale", "factor"))
    diff = txn["patch"]["diff"]
    assert "+int p_scale(const int factor, int x);" in diff  # header
    assert "+    return x * factor;" in diff  # body
    assert "p_scale(k, 7)" in diff  # call site inside printf(...)
    txn = led.validate(txn["id"])
    assert txn["state"] == "validated", txn["validation"]["judgement"]
    kinds = {(r["kind"], r["outcome"]) for r in txn["validation"]["records"]}
    assert ("mechanical-recheck", "passed") in kinds and ("differential-testing", "passed") in kinds
    # main.c and params.c both include the edited header: both are recompiled
    assert {r["name"].split("#")[0] for r in txn["validation"]["records"] if r["kind"] == "compile"} >= {
        "clang:src/main.c",
        "clang:src/params.c",
    }
    led.accept(txn["id"])
    run_cli(root, "refresh")

    assert run_cli(root, "auto", "--recipe", "scalar-input", "--max", "8") == 0
    accepted = {(t["finding"]["function"], t["finding"]["name"]) for t in led.all() if t["state"] == "accepted"}
    assert accepted == SCALAR_ELIGIBLE
    assert "long p_sum2(long a, long b)" in (root / "src/params.c").read_text()
    assert "p_via_ptr(*kp)" in (root / "src/main.c").read_text()
    subprocess.run(["make", "-s", "CC=clang", "BUILD=after"], cwd=root, check=True)
    after = subprocess.run(["./after/demo"], cwd=root, capture_output=True, text=True).stdout
    assert after == before


@needs_gcc
def test_gcc_call_sites_inside_printf_are_editable(tmp_path):
    # With glibc fortification, Clang (the secondary frontend) sees printf as a forwarding macro while GCC
    # sees a function: the call p_scale(&k, 7) inside printf(...) is still plain source text for production.
    root = build_project(
        tmp_path,
        [{"id": "gcc", "cc": "gcc", "secondary": {"compiler": "clang"}, "validation": validation_for("gcc")}],
        extra={
            "preservation": SINGLE_THREADED,
            "acceptance": {"require": ["compile", "mechanical-recheck", "differential-testing"]},
        },
    )
    for step in ("collect", "fidelity", "inventory"):
        assert run_cli(root, step) == 0
    assert verdicts(root, "scalar-input")[("p_scale", "factor")] == []
    led = Ledger(load_project(root))
    txn = led.propose(finding_id(root, "p_scale", "factor"))
    assert "p_scale(k, 7)" in txn["patch"]["diff"]
    txn = led.validate(txn["id"])
    assert txn["state"] == "validated", txn["validation"]
