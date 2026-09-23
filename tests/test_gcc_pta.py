"""GCC's own interprocedural points-to (-fipa-pta) as flow evidence, and its cross-check with SVF."""

from __future__ import annotations

import yaml
from conftest import RAND_MODEL, SINGLE_THREADED, build_project, needs_gcc, needs_svf, run_cli

from weaver.analysis.inventory import load_inventory
from weaver.config import load_project
from weaver.flow.evidence import flow_status
from weaver.flow.gcc_pta import GccPta, load_gcc_pta, parse_pta_dump
from weaver.pipeline import refresh
from weaver.recipes import CATALOG, RecipeContext

# Trimmed from a real ltrans0.ltrans.092i.pta2 (GCC 12): two files each define a static helper();
# LTO renames them helper.lto_priv.0 and helper.lto_priv.1.
DUMP = """\
Symbol table:

main/6 (main)
  Type: function definition analyzed
  Visibility: externally_visible semantic_interposition prevailing_def public
  Availability: overwritable
helper.lto_priv.1/5 (helper)
  Type: function definition analyzed
  Visibility: semantic_interposition prevailing_def_ironly
  Availability: local
writer/3 (writer)
  Type: function definition analyzed
  Visibility: semantic_interposition prevailing_def_ironly
  Availability: local
g/0 (g)
  Type: variable definition analyzed
  Visibility: semantic_interposition prevailing_def_ironly
  Availability: available

;; Function main (main, funcdef_no=0, decl_uid=1, cgraph_uid=1, symbol_order=6)

Points-to sets

ANYTHING = { ANYTHING }
ESCAPED = { buf }
NONLOCAL = { ESCAPED NONLOCAL }
k = { NONLOCAL } same as _1
main.clobber = { k m }
helper.lto_priv.1.clobber = { k } same as helper.lto_priv.1.arg0
helper.lto_priv.1.use = { k } same as helper.lto_priv.1.arg0
helper.lto_priv.1.arg0 = { k }
writer.clobber = { m } same as writer.arg0
writer.use = { }
writer.arg0 = { m }
reader.clobber = { }
reader.use = { g k }
reader.arg0 = { k } same as helper.lto_priv.1.arg0
helper.lto_priv.0.clobber = { }
helper.lto_priv.0.use = { g } same as helper.lto_priv.0.arg0
helper.lto_priv.0.arg0 = { g }
split.part.0.clobber = { }
split.part.0.arg0 = { k }
ext.arg0 = { NONLOCAL }
ext.clobber = { ESCAPED NONLOCAL }
passes.arg0 = { buf }
passes.clobber = { ESCAPED NONLOCAL buf }
any.arg0 = { ANYTHING }
any.clobber = { }

;; Function writer (writer, funcdef_no=2)
"""


def _pta():
    data = parse_pta_dump(DUMP)
    return data, GccPta({**data, "image": "prog", "program": "prog"})


def test_parse_symbol_table_and_sets():
    data, _ = _pta()
    assert data["symbols"]["main"]["visibility"].startswith("externally_visible")
    assert data["symbols"]["writer"]["visibility"] == "semantic_interposition prevailing_def_ironly"
    assert data["variables"] == ["g"]
    assert data["functions"]["writer"]["args"] == {"0": ["m"]}
    assert data["functions"]["reader"]["clobber"] == []
    # the two static helpers are united under their source name: sound, possibly coarser
    h = data["functions"]["helper"]
    assert sorted(h["symbols"]) == ["helper.lto_priv.0", "helper.lto_priv.1"]
    assert h["args"]["0"] == ["g", "k"] and h["clobber"] == ["k"] and not h["renumbered"]
    assert data["functions"]["split"]["renumbered"]


def test_may_modify_answers():
    _, g = _pta()
    assert g.may_modify("reader", 0)[0] == "no"  # reads *r, writes nothing
    status, text = g.may_modify("writer", 0)
    assert status == "yes" and "may write m" in text
    assert g.may_modify("helper", 0)[0] == "yes"  # union of both statics: conservative
    # external memory and a call that writes external memory: GCC cannot tell, so unknown, not yes
    assert g.may_modify("ext", 0)[0] == "unknown"
    assert "ANYTHING" in g.may_modify("any", 0)[1]
    # buf escaped into external code the call reaches: listed in the clobber set, but not a traced write
    status, text = g.may_modify("passes", 0)
    assert status == "unknown" and "escaped buf" in text
    assert g.may_modify("split", 0)[0] == "unknown"  # parameters may be renumbered in a .part clone
    assert g.may_modify("missing", 0) == (
        "unknown",
        "GCC (prog): missing() is not in the linked image (unreachable from its exports)",
    )
    assert g.may_modify("writer", 3)[0] == "unknown"  # no such argument set
    assert g.visibility("main").startswith("externally_visible")


def _gcc_project(tmp, flow=None):
    extra = {"preservation": SINGLE_THREADED, "flow": {**RAND_MODEL, **(flow or {})}}
    root = build_project(tmp, [{"id": "gcc", "cc": "gcc", "secondary": {"compiler": "clang"}}], extra=extra)
    return root


def _scalar(root):
    """(function, name) -> (eligible, may-modify precondition) under the scalar-input recipe."""
    proj = load_project(root)
    inv = load_inventory(proj)
    ctx = RecipeContext(proj, inv)
    out = {}
    for f in inv["findings"]:
        if CATALOG["scalar-input"].applicable(f):
            res = CATALOG["scalar-input"].evaluate(ctx, f)
            mod = next(p for p in res.preconditions if p.id == "SI.no-modification-during-call")
            out[(f.get("function"), f.get("name"))] = (res.eligible, mod)
    return out


@needs_gcc
def test_gcc_backend_alone_establishes_and_blocks(tmp_path):
    """With SVF deselected, GCC's own points-to decides the may-modify precondition."""
    root = _gcc_project(tmp_path, {"backend": "gcc"})
    summary = refresh(load_project(root), log=lambda m: None)
    assert summary["gcc_pta"] == {"gcc": "complete"}
    assert "flow" not in summary  # SVF not run
    proj = load_project(root)
    inv = load_inventory(proj)
    gp = load_gcc_pta(proj, "gcc", inventory=inv)
    assert list(gp) == ["demo/demo"]
    st = flow_status(proj, inv)["gcc"]["gcc"]
    assert st["complete"] and st["current"] == st["images"] == 1

    v = _scalar(root)
    for key in [("p_scale", "factor"), ("p_sum2", "a"), ("p_via_ptr", "v"), ("p_noisy", "v")]:
        eligible, mod = v[key]
        assert eligible, (key, mod)
        assert any(e.startswith("GCC (demo)") for e in mod.evidence)
    for key in [("p_snapshot", "c"), ("p_read_after_touch", "v")]:
        assert v[key][1].status == "violated", (key, v[key][1])

    # the solution goes stale when the source changes
    src = root / "src" / "params.c"
    src.write_text(src.read_text() + "\nint p_extra(void) { return 0; }\n")
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0
    proj = load_project(root)
    assert load_gcc_pta(proj, "gcc", inventory=load_inventory(proj)) == {}


@needs_gcc
def test_designator_matches_extern_declaration(tmp_path):
    """Without any points-to backend, a by-name write to the global passed as &global still blocks."""
    root = _gcc_project(tmp_path, {"backend": "none"})
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0
    _, mod = _scalar(root)[("p_snapshot", "c")]
    assert mod.status == "violated" and "p_counter" in " ".join(mod.evidence)


@needs_svf
@needs_gcc
def test_backends_cross_check(tmp_path):
    root = _gcc_project(tmp_path)
    refresh(load_project(root), log=lambda m: None)  # auto: SVF and GCC
    v = _scalar(root)
    eligible, mod = v[("p_scale", "factor")]
    ev = " ".join(mod.evidence)
    assert eligible and "SVF points-to evidence" in ev and "GCC (demo)" in ev
    # e_release writes **pp: SVF says yes, GCC says no; a yes from any backend wins
    assert v[("e_release", "pp")][1].status == "violated"

    # GCC cannot decide p_scale (its argument may point to ANYTHING) while SVF says no write
    import json

    pta = next((root / ".weaver" / "flow" / "gcc" / "gcc" / "programs").glob("*/*/pta.json"))
    data = json.loads(pta.read_text())
    data["functions"]["p_scale"]["args"]["0"] = ["ANYTHING"]
    pta.write_text(json.dumps(data))
    eligible, mod = _scalar(root)[("p_scale", "factor")]
    assert not eligible and mod.status == "unresolved"
    assert any("backends disagree" in e for e in mod.evidence)
    cfg = yaml.safe_load((root / "weaver.yaml").read_text())
    cfg["flow"]["agreement"] = "any"
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg))
    eligible, mod = _scalar(root)[("p_scale", "factor")]
    assert eligible and any("established because flow.agreement is 'any'" in e for e in mod.evidence)
