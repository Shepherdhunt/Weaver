"""End-to-end tests on the demo fixture: capture -> collect -> inventory -> recipe -> ledger.

The fixture (tests/fixtures/demo) contains positive examples and deliberate
counterexamples for the local-alias recipe; a successful rejection is part of
correctness.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path

import pytest
from conftest import FIXTURE, build_project, needs_clang, needs_gcc, run_cli, validation_for

from weaver.analysis.inventory import load_inventory
from weaver.config import load_project
from weaver.ledger import Ledger

ELIGIBLE = {
    ("la_basic", "p"),
    ("la_struct", "pp"),
    ("la_global", "gp"),
    ("e_member", "ip"),
    ("e_element", "ep"),
    ("e_const_view", "vp"),
}
BLOCKED = {
    ("la_escape", "ep"): "LA.dereference-only",
    ("la_reassign", "rp"): "LA.dereference-only",
    ("la_compare", "cp"): "LA.dereference-only",
    ("la_compare", "dp"): "LA.dereference-only",
    ("la_macro", "mp"): "LA.edits-in-source",
    ("la_shadow", "sp"): "LA.name-resolution",
    ("la_inactive", "qp"): "LA.all-references-explained",
    ("la_config", "tp"): "LA.all-references-explained",
    ("la_volatile", "wp"): "LA.access-qualifiers",
    ("e_through_pointer", "bp"): "LA.target-stable",
    ("e_subscript", "kp"): "LA.dereference-only",
    ("e_multi", "xp"): "LA.decl-shape",
    ("e_multi", "yp"): "LA.decl-shape",
    ("e_for_init", "np"): "LA.decl-shape",
    ("e_goto", "gp"): "LA.initialization-dominates",
    ("e_cleanup", "cp"): "LA.decl-shape",
    ("main", "kp"): "LA.dereference-only",
}


def cli_json(root: Path, *args: str):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_cli(root, *args, "--json")
    assert rc == 0
    return json.loads(buf.getvalue())


def verdicts(root: Path, recipe: str = "local-alias") -> dict[tuple[str, str], list[str]]:
    """(function, name) -> ids of the preconditions not established, for one recipe."""
    inv = load_inventory(load_project(root))
    by_id = {f["id"]: f for f in inv["findings"]}
    out = {}
    for c in cli_json(root, "candidates", "--recipe", recipe):
        f = by_id[c["finding"]]
        out[(f["function"], f["name"])] = [p["id"] for p in c["preconditions"] if p["status"] != "established"]
    return out


def finding_id(root: Path, function: str, name: str) -> str:
    inv = load_inventory(load_project(root))
    return next(f["id"] for f in inv["findings"] if f.get("function") == function and f.get("name") == name)


@pytest.fixture(scope="module")
def clang_project(tmp_path_factory):
    root = build_project(tmp_path_factory.mktemp("clang"), [{"id": "clang", "cc": "clang"}])
    assert run_cli(root, "collect") == 0
    assert run_cli(root, "inventory") == 0
    return root


@needs_clang
def test_inventory_and_coverage(clang_project):
    inv = load_inventory(load_project(clang_project))
    kinds = inv["summary"]["by_kind"]
    assert kinds["local"] == 23 and kinds["parameter"] == 19 and kinds["static-global"] == 1
    assert kinds["global"] == 1 and kinds["extern-decl"] == 1  # the function-pointer hook in params
    # Lines inside '#ifdef NEVER_DEFINED' and '#ifdef TRACE' were compiled by no configuration.
    alias = inv["coverage"]["files"]["src/alias.c"]
    lines = (clang_project / "src/alias.c").read_text().splitlines()
    unexamined = [lines[a - 1].strip() for a, b in alias["unexamined_ranges"] for _ in range(a, b + 1)]
    assert unexamined == ["util_touch(qp);", "util_touch(tp);"]
    # Every finding records provenance and native evidence for a Clang production compiler.
    assert all(f["evidence_status"] == "native" and f["occurrences"] for f in inv["findings"])


@needs_clang
def test_recipe_verdicts(clang_project):
    v = verdicts(clang_project)
    assert {k for k, bl in v.items() if not bl} == ELIGIBLE
    for key, pre in BLOCKED.items():
        assert v[key] == [pre], (key, v[key])


@needs_clang
def test_graph_edges_carry_provenance(clang_project):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert run_cli(clang_project, "graph") == 0
    graph = json.loads(buf.getvalue())
    escapes = [e for e in graph["edges"] if e["type"] == "escapes-to-call"]
    assert any(e["dst"] == "callee:util_touch" for e in escapes)
    prov = escapes[0]["provenance"][0]
    assert prov["producer"]["family"] == "clang" and prov["evidence_status"] == "native"


@needs_clang
def test_transaction_lifecycle(tmp_path):
    root = build_project(
        tmp_path,
        [{"id": "clang", "cc": "clang", "validation": validation_for("clang")}],
        extra={"acceptance": {"require": ["compile", "mechanical-recheck", "differential-testing"]}},
    )
    run_cli(root, "collect")
    run_cli(root, "inventory")
    original = (root / "src/alias.c").read_bytes()
    fid = finding_id(root, "la_basic", "p")
    other = finding_id(root, "la_struct", "pp")
    led = Ledger(load_project(root))

    txn = led.propose(fid)
    assert txn["state"] == "proposed"
    assert "-    unsigned *p = &total;\n-    *p += 2;\n+    total += 2;" in txn["patch"]["diff"]
    assert (root / "src/alias.c").read_bytes() == original  # proposing never edits the tree

    txn = led.validate(txn["id"])
    assert txn["state"] == "validated", txn["validation"]["judgement"]
    kinds = {(r["kind"], r["outcome"]) for r in txn["validation"]["records"]}
    assert {("compile", "passed"), ("mechanical-recheck", "passed"), ("differential-testing", "passed")} <= kinds

    led.accept(txn["id"])
    patched = (root / "src/alias.c").read_text()
    assert "unsigned *p" not in patched and "    total += 2;\n" in patched

    run_cli(root, "refresh")
    inv = load_inventory(load_project(root))
    ids = {f["id"] for f in inv["findings"]}
    assert fid not in ids and other in ids  # IDs survive unrelated edits

    # A later, unrelated user edit is preserved by revert (three-way inverse merge).
    edited = (root / "src/alias.c").read_text().replace("/* Aliased parameters", "/* USER EDIT: aliased")
    (root / "src/alias.c").write_text(edited)
    led.revert(txn["id"])
    reverted = (root / "src/alias.c").read_text()
    assert "unsigned *p = &total;" in reverted and "USER EDIT" in reverted
    states = [e["state"] for e in led.events() if e["txn"] == txn["id"]]
    assert states == ["discovered", "analyzed", "proposed", "validated", "accepted", "reverted"]


@needs_clang
def test_stale_sources_block_validation(tmp_path):
    root = build_project(tmp_path, [{"id": "clang", "cc": "clang"}])
    run_cli(root, "collect")
    run_cli(root, "inventory")
    led = Ledger(load_project(root))
    txn = led.propose(finding_id(root, "la_basic", "p"))
    with open(root / "src/alias.c", "a") as f:
        f.write("/* concurrent edit */\n")
    txn = led.validate(txn["id"])
    assert txn["state"] == "blocked" and "changed since it was analysed" in txn["validation"]["error"]


@needs_clang
def test_blocked_candidates_are_recorded(clang_project):
    led = Ledger(load_project(clang_project))
    txn = led.propose(finding_id(clang_project, "la_escape", "ep"))
    assert txn["state"] == "blocked" and "patch" not in txn
    assert "LA.dereference-only violated" in txn["history"][-1]["note"]


@needs_clang
def test_alternate_configuration_changes_the_verdict(tmp_path):
    root = build_project(
        tmp_path,
        [
            {"id": "clang", "cc": "clang"},
            {"id": "clang-trace", "cc": "clang", "cflags": "-O2 -std=c11 -DTRACE"},
        ],
    )
    run_cli(root, "collect")
    # Inventory from one configuration only: the other profile is a missing configuration.
    run_cli(root, "inventory", "--profile", "clang")
    v = verdicts(root)
    assert "LA.configurations" in v[("la_basic", "p")]
    # With both configurations the TRACE branch is analysed and the escape is explicit.
    run_cli(root, "inventory")
    v = verdicts(root)
    assert v[("la_config", "tp")] == ["LA.dereference-only"]
    assert v[("la_basic", "p")] == []
    inv = load_inventory(load_project(root))
    lines = (root / "src/alias.c").read_text().splitlines()
    alias = inv["coverage"]["files"]["src/alias.c"]
    assert [lines[a - 1].strip() for a, _ in alias["unexamined_ranges"]] == ["util_touch(qp);"]


@needs_clang
def test_auto_mode_preserves_behavior(tmp_path):
    root = build_project(
        tmp_path,
        [{"id": "clang", "cc": "clang", "validation": validation_for("clang")}],
        extra={"acceptance": {"require": ["compile", "mechanical-recheck", "differential-testing"]}},
    )
    run_cli(root, "collect")
    run_cli(root, "inventory")
    before = subprocess.run(["./build/clang/demo"], cwd=root, capture_output=True, text=True).stdout
    assert run_cli(root, "auto", "--max", "10") == 0
    led = Ledger(load_project(root))
    accepted = [t for t in led.all() if t["state"] == "accepted"]
    assert {(t["finding"]["function"], t["finding"]["name"]) for t in accepted} == ELIGIBLE
    subprocess.run(["make", "-s", "CC=clang", "BUILD=after"], cwd=root, check=True)
    after = subprocess.run(["./after/demo"], cwd=root, capture_output=True, text=True).stdout
    assert after == before
    src = (root / "src/edge.c").read_text()
    assert "w.in.b = 7;" in src and "arr[1] *= 10;" in src and "return v + (int)sizeof(v);" in src


@needs_gcc
def test_gcc_profile_with_secondary_frontend(tmp_path):
    root = build_project(tmp_path, [{"id": "gcc", "cc": "gcc", "secondary": {"compiler": "clang"}}])
    run_cli(root, "collect")
    # Before fidelity checks, secondary evidence is 'unchecked' and cannot authorise a rewrite.
    run_cli(root, "inventory")
    assert "LA.configurations" in verdicts(root)[("la_basic", "p")]
    fid = cli_json(root, "fidelity")
    status = {u["file"]: u["evidence_status"] for u in fid["units"]}
    # main.c calls printf: under glibc fortification Clang sees a forwarding macro that GCC does not;
    # it is recognised as transparent rather than counted as a difference.
    assert set(status.values()) == {"secondary-checked"}, fid["units"]
    main = next(u for u in fid["units"] if u["file"] == "src/main.c")
    for fwd in main["macro_differences"]["forwarding"]:
        assert fwd.startswith("printf -> ") and "_chk" in fwd
    run_cli(root, "inventory")
    assert verdicts(root)[("la_basic", "p")] == []


@needs_gcc
def test_fidelity_detects_observable_macro_difference(tmp_path):
    root = build_project(
        tmp_path, [{"id": "gcc", "cc": "gcc", "secondary": {"compiler": "clang", "extra_args": ["-DSCALE=2"]}}]
    )
    run_cli(root, "collect")
    fid = cli_json(root, "fidelity")
    alias = next(u for u in fid["units"] if u["file"] == "src/alias.c")
    assert alias["evidence_status"] == "secondary-partial"
    assert any("SCALE" in f for f in alias["findings"])
    edge = next(u for u in fid["units"] if u["file"] == "src/edge.c")
    assert edge["evidence_status"] == "secondary-checked"  # SCALE is not visible to edge.c


@needs_clang
def test_probe_reports_capabilities(clang_project):
    res = cli_json(clang_project, "probe")
    caps = res["capabilities"]
    for name in ("preprocess", "macros", "deps", "ast_json", "llvm_ir", "bitcode", "record_layouts"):
        assert caps[name]["status"] == "probe-passed", (name, caps[name])


def test_fixture_is_pristine():
    # Guard against tests mutating the checked-in fixture.
    assert "unsigned *p = &total;" in (FIXTURE / "src/alias.c").read_text()
