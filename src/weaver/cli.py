"""Command-line interface.

Typical flow (see README):

    weaver init
    weaver capture shim --tool cc=/usr/bin/gcc     # then build with the shim
    weaver capture finalize
    weaver probe
    weaver collect
    weaver fidelity                                  # for non-Clang production compilers
    weaver inventory
    weaver candidates
    weaver propose P-xxxxxxxxxx
    weaver validate T-xxxxxxxx
    weaver accept T-xxxxxxxx                         # or: skip / revert
    weaver refresh
    weaver serve                                     # the same workflow in a local web interface
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from weaver import __version__
from weaver.errors import WeaverError


def _project(args: argparse.Namespace):
    from weaver.config import load_project

    return load_project(args.config)


def _print_json(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    from weaver.config import CONFIG_NAME, TEMPLATE

    target = Path(args.config or ".").resolve()
    if target.is_dir():
        target = target / CONFIG_NAME
    if target.exists() and not args.force:
        raise WeaverError(f"{target} exists (use --force to overwrite)")
    target.write_text(TEMPLATE.format(name=target.parent.name))
    print(f"wrote {target}; record the profile's compile database, target and platform facts before collecting")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from weaver.capture.shims import finalize, make_shim
    from weaver.config import CONFIG_NAME
    from weaver.store import Store

    try:
        proj = _project(args)
        cap = Store(proj.state_dir).capture_dir()
    except WeaverError:
        cap = Path(".weaver/capture").resolve()
        cap.mkdir(parents=True, exist_ok=True)
        print(f"note: no {CONFIG_NAME}; using {cap}", file=sys.stderr)
    log = Path(args.log).resolve() if args.log else cap / "log.jsonl"
    if args.action == "shim":
        shim_dir = Path(args.dir).resolve() if args.dir else cap / "shims"
        for spec in args.tool:
            name, _, real = spec.partition("=")
            if not real:
                raise WeaverError(f"--tool expects NAME=EXECUTABLE, got {spec!r}")
            p = make_shim(shim_dir, name, real, log, role="compiler")
            print(f"{name}: {p}")
        print(
            f"log: {log}\nBuild from a clean configured tree with these shims (e.g. make CC={shim_dir}/cc), "
            "then run 'weaver capture finalize'."
        )
        return 0
    out = Path(args.out).resolve() if args.out else cap
    res = finalize(log, out)
    _print_json(res)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from weaver.toolchain.probe import probe_profile

    proj = _project(args)
    for prof in proj.select_profiles(args.profile):
        res = probe_profile(proj, prof)
        if args.json:
            _print_json(res)
            continue
        tool = res.get("production_tool", {})
        print(
            f"profile {prof.id}: {tool.get('family')} {tool.get('version')} ({tool.get('realpath')}) "
            f"target {tool.get('target')}"
        )
        for name, cap in res.get("capabilities", {}).items():
            print(f"  {cap['status']:<28} {name:<18} {cap.get('detail', '')[:110]}")
        sec = res.get("secondary")
        if sec:
            if sec.get("error"):
                print(f"  secondary frontend: {sec['error']}")
            else:
                print(f"  secondary frontend {sec['tool'].get('realpath')} ({sec['tool'].get('version')}):")
                for name, cap in sec["capabilities"].items():
                    print(f"    {cap['status']:<26} {name:<18} {cap.get('detail', '')[:100]}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from weaver.toolchain.collect import collect_profile

    proj = _project(args)
    recipes = args.recipes
    if recipes not in (None, "default", "all"):
        recipes = [r.strip() for r in recipes.split(",") if r.strip()]
    rc = 0
    for prof in proj.select_profiles(args.profile):
        res = collect_profile(proj, prof, recipes=recipes, files=args.file, force=args.force, jobs=args.jobs)
        if args.json:
            _print_json(res)
        else:
            print(
                f"profile {prof.id}: {res['units']} unit(s): {res['collected']} collected, {res['cached']} cached, "
                f"{len(res['failed'])} failed"
            )
            for f in res["failed"] + res["partial"]:
                print(f"  {f['unit']} {f['file']}: {f['detail']}")
        rc |= 1 if res["failed"] else 0
    return rc


def cmd_fidelity(args: argparse.Namespace) -> int:
    from weaver.fidelity import run_fidelity

    proj = _project(args)
    for prof in proj.select_profiles(args.profile):
        res = run_fidelity(proj, prof, layout=not args.no_layout)
        if args.json:
            _print_json(res)
            continue
        print(f"profile {prof.id}: {res.get('note') or ''}")
        for u in res.get("units", []):
            print(f"  {u['evidence_status']:<20} {u['file']} ({u['unit_id']})")
            for f in u.get("findings", [])[:8]:
                print(f"      - {f}")
    return 0


def cmd_flow(args: argparse.Namespace) -> int:
    from weaver.flow.svf import run_flow

    proj = _project(args)
    rc = 0
    for prof in proj.select_profiles(args.profile):
        res = run_flow(proj, prof, force=args.force)
        if args.json:
            _print_json(res)
            continue
        diag = res.get("diagnostics") or {}
        print(
            f"profile {prof.id}: flow evidence {res['status']}"
            + (f" ({res['reason']})" if res.get("reason") else "")
            + (
                f"; {diag.get('nodes')} pointer node(s), {diag.get('objects')} object(s), "
                f"{diag.get('indirect_call_sites')} indirect call site(s)"
                if diag
                else ""
            )
        )
        for m in res.get("missing_units", []):
            print(f"  missing: {m['file']}: {m['reason']}")
        rc |= 0 if res["status"] == "complete" else 1
    return rc


def cmd_inventory(args: argparse.Namespace) -> int:
    from weaver.analysis.inventory import build_inventory

    proj = _project(args)
    inv = build_inventory(proj, args.profile, jobs=args.jobs)
    if args.json:
        _print_json(inv["summary"])
        return 0
    s = inv["summary"]
    print(
        f"{s['findings']} pointer finding(s) across {s['units']} unit(s) "
        f"({s['units_without_ast']} without AST evidence)"
    )
    print("  by kind:      " + ", ".join(f"{k}={v}" for k, v in s["by_kind"].items()))
    print("  operations:   " + ", ".join(f"{k}={v}" for k, v in s["operations"].items()))
    print(
        f"  unexamined:   {s['unexamined_lines']} code line(s) no configuration compiled; "
        f"{s['unparsed_files']} unparsed file(s)"
    )
    print("Treat unexamined code and unparsed files as unknown, not pointer-free.")
    return 0


def cmd_findings(args: argparse.Namespace) -> int:
    from weaver.analysis.inventory import load_inventory

    inv = load_inventory(_project(args))
    rows = [
        f
        for f in inv["findings"]
        if (not args.kind or f["kind"] in args.kind)
        and (not args.file or f.get("file") == args.file)
        and (not args.function or f.get("function") == args.function)
    ]
    if args.json:
        _print_json(rows)
        return 0
    for f in rows:
        where = f"{f.get('file')}:{f.get('line')}"
        scope = f.get("function") or f.get("record") or ""
        print(
            f"{f['id']:<14} {f['kind']:<13} {where:<28} {scope:<18} {f.get('name') or '':<16} "
            f"{f.get('type') or '':<22} {f.get('evidence_status')}"
        )
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from weaver.analysis.inventory import load_inventory

    cov = load_inventory(_project(args))["coverage"]
    if args.json:
        _print_json(cov)
        return 0
    for f, info in cov["files"].items():
        rng = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in info["unexamined_ranges"])
        print(
            f"{f}: {info['unexamined_lines']}/{info['code_lines']} code lines unexamined"
            + (f" (lines {rng})" if rng else "")
            + f"; units {len(info['units'])}"
        )
    for f in cov["unparsed_files"]:
        print(f"{f}: not compiled or included by any analysed unit (unexamined)")
    return 0


def _evaluate_all(proj: Any, recipe_id: str | None):
    from weaver.analysis.inventory import load_inventory
    from weaver.recipes import CATALOG, RecipeContext

    inv = load_inventory(proj)
    ctx = RecipeContext(proj, inv)
    recipes = [CATALOG[recipe_id]] if recipe_id else list(CATALOG.values())
    out = []
    for f in inv["findings"]:
        for r in recipes:
            if r.applicable(f):
                out.append((f, r.evaluate(ctx, f)))
    return out


def cmd_candidates(args: argparse.Namespace) -> int:
    proj = _project(args)
    results = _evaluate_all(proj, args.recipe)
    if args.json:
        _print_json([{"finding": f["id"], **res.to_json()} for f, res in results])
        return 0
    elig = [(f, r) for f, r in results if r.eligible]
    blocked = [(f, r) for f, r in results if not r.eligible]
    print(f"{len(elig)} eligible, {len(blocked)} blocked")
    for f, r in sorted(elig, key=lambda x: len(x[1].edits)):
        print(
            f"  ELIGIBLE {f['id']:<14} {r.recipe:<12} {f['file']}:{f['line']} {f.get('function')}() "
            f"'{f['name']}' ({len(r.edits)} edit(s))"
        )
    if args.all:
        for f, r in blocked:
            reasons = "; ".join(f"{b['id']}: {b['evidence'][0] if b['evidence'] else b['status']}" for b in r.blockers)
            print(
                f"  BLOCKED  {f['id']:<14} {r.recipe:<12} {f['file']}:{f['line']} {f.get('function')}() "
                f"'{f['name']}': {reasons[:220]}"
            )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from weaver.analysis.inventory import find_finding, load_inventory
    from weaver.recipes import RecipeContext, recipes_for_finding

    proj = _project(args)
    inv = load_inventory(proj)
    f = find_finding(inv, args.finding)
    if args.json:
        _print_json(f)
        return 0
    print(
        f"{f['id']}: {f['kind']} '{f.get('name')}' {f.get('type')!r} at {f.get('file')}:{f.get('line')}:{f.get('col')}"
        f" in {f.get('function') or f.get('record') or '<file scope>'}"
    )
    print(
        f"  evidence: {f.get('evidence_status')}; configurations: "
        + ", ".join(f"{o['profile']}/{o['unit']}" for o in f.get("occurrences", []))
    )
    if f.get("typedef_hidden"):
        print("  pointer hidden behind a typedef")
    print(f"  uses: {f.get('use_summary') or {}}")
    for u in f.get("uses", [])[:40]:
        print(
            f"    line {u['line']}:{u['col']} {u['kind']}{':' + u['access'] if u.get('access') else ''}"
            f"{' (macro)' if u.get('in_macro') else ''} {u.get('detail') or ''}"
        )
    for t in f.get("possible_targets", []):
        print(f"  possible target: {t}")
    ctx = RecipeContext(proj, inv)
    for r in recipes_for_finding(f):
        res = r.evaluate(ctx, f)
        print(f"  recipe {r.id}: {'ELIGIBLE' if res.eligible else 'BLOCKED'}")
        for p in res.preconditions:
            print(f"    [{p.status[:1].upper()}] {p.id}: {'; '.join(p.evidence)[:200]}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    from weaver.card import render_card
    from weaver.ledger import Ledger

    txn = Ledger(_project(args)).propose(args.finding, args.recipe)
    print(render_card(txn))
    return 0 if txn["state"] == "proposed" else 3


def cmd_validate(args: argparse.Namespace) -> int:
    from weaver.card import render_card
    from weaver.ledger import Ledger

    txn = Ledger(_project(args)).validate(args.txn, keep=args.keep)
    print(render_card(txn))
    return 0 if txn["state"] == "validated" else 3


def cmd_accept(args: argparse.Namespace) -> int:
    from weaver.ledger import Ledger

    txn = Ledger(_project(args)).accept(args.txn)
    print(
        f"{txn['id']} accepted: {', '.join(txn['acceptance']['post_hashes'])} updated; checkpoint "
        f"{txn['acceptance']['checkpoint']}.\nRun 'weaver refresh' before selecting the next candidate."
    )
    return 0


def cmd_skip(args: argparse.Namespace) -> int:
    from weaver.ledger import Ledger

    txn = Ledger(_project(args)).skip(args.txn, args.reason or "")
    print(f"{txn['id']} skipped")
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    from weaver.ledger import Ledger

    txn = Ledger(_project(args)).revert(args.txn)
    print(f"{txn['id']} reverted ({', '.join(txn['reversion']['files'])}); run 'weaver refresh'")
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    from weaver.card import render_card
    from weaver.ledger import Ledger

    txn = Ledger(_project(args)).load(args.txn)
    if args.json:
        _print_json(txn)
    else:
        print(render_card(txn))
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    from weaver.ledger import Ledger

    led = Ledger(_project(args))
    if args.json:
        _print_json(led.events())
        return 0
    for t in led.all():
        f = t["finding"]
        print(
            f"{t['id']}  {t['state']:<11} {t['recipe']:<12} {t['finding_id']:<14} {f.get('file')}:{f.get('line')} "
            f"{f.get('function')}() '{f.get('name')}'  {t['created_at']}"
        )
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    from weaver.pipeline import refresh

    proj = _project(args)
    refresh(proj, jobs=args.jobs, flow=False if args.no_flow else None, capture=args.capture)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from weaver.impact import list_snapshots, save_snapshot, snapshot_from_git

    proj = _project(args)
    if args.action == "save":
        m = save_snapshot(proj, args.name)
        print(f"snapshot {m['name']}: {m['findings']} finding(s) at {m['created_at']}")
    elif args.action == "git":
        if not args.rev:
            raise WeaverError("usage: weaver snapshot git REV [--name NAME]")
        m = snapshot_from_git(proj, args.rev, args.name)
        print(f"snapshot {m['name']}: {m['findings']} finding(s) at {m['source']['commit'][:12]}")
    else:
        for m in list_snapshots(proj):
            src = m["source"]
            print(
                f"{m['name']:<28} {m['created_at']}  {m['findings']:>5} finding(s)  "
                f"{src.get('commit', (src.get('revision') or {}).get('commit', ''))[:12]}"
            )
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    from weaver.impact import impact, render_report

    def log(msg: str) -> None:  # progress goes to stderr so --json output stays parseable
        print(msg, file=sys.stderr if args.json else sys.stdout, flush=True)

    rep = impact(_project(args), args.since, revalidate=args.revalidate, log=log if args.revalidate else None)
    if args.json:
        _print_json(rep)
    else:
        print(render_report(rep))
    if args.command == "check":
        return 1 if rep["risk"] == "high" else 0
    return 0


def cmd_contract(args: argparse.Namespace) -> int:
    from weaver.impact import EXPECTATIONS, load_contracts, pin_contract

    proj = _project(args)
    if args.action == "pin":
        if not args.finding or not args.expect:
            raise WeaverError("usage: weaver contract pin FINDING --expect read-only[,no-escape...]")
        c = pin_contract(
            proj, args.finding, [e.strip() for e in args.expect.split(",") if e.strip()], args.reason or ""
        )
        print(
            f"{c['id']}: {c['subject']['name']} in {c['subject']['function']}() must stay {', '.join(c['expect'])}"
            f" (recorded in {proj.contracts_path})"
        )
    else:
        for c in load_contracts(proj):
            print(
                f"{c['id']}  {c['finding']}  {c['subject'].get('name')} in {c['subject'].get('function')}(): "
                f"{', '.join(c['expect'])}  {c.get('reason') or ''}"
            )
        if args.action == "expectations":
            for k, v in EXPECTATIONS.items():
                print(f"  {k:<16} {v}")
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    from weaver.analysis.graph import build_graph

    proj = _project(args)
    g = build_graph(proj)
    out = Path(args.output) if args.output else None
    if out:
        out.write_text(json.dumps(g, indent=1) + "\n")
        print(f"wrote {out}: {len(g['nodes'])} nodes, {len(g['edges'])} edges")
    else:
        _print_json(g)
    return 0


def cmd_slice(args: argparse.Namespace) -> int:
    from weaver.llm.slice import build_slice

    _print_json(build_slice(_project(args), args.finding))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from weaver.llm.client import explain

    text = explain(
        _project(args),
        args.finding,
        dry_run=args.dry_run,
        model=args.model,
        fallbacks=False if args.no_fallbacks else None,
    )
    print(text)
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    from weaver.ledger import Ledger
    from weaver.pipeline import refresh

    proj = _project(args)
    led = Ledger(proj)
    done: set[str] = set()
    accepted = 0
    for _ in range(args.max):
        results = [(f, r) for f, r in _evaluate_all(proj, args.recipe) if r.eligible and f["id"] not in done]
        if not results:
            print("no further eligible candidates")
            break
        f, r = min(results, key=lambda x: (len(x[1].edits), x[0]["file"], x[0]["line"]))
        done.add(f["id"])
        txn = led.propose(f["id"], r.recipe)
        print(f"{txn['id']} {r.recipe} {f['id']} {f['file']}:{f['line']} '{f['name']}': {txn['state']}")
        if txn["state"] != "proposed" or args.dry_run:
            continue
        txn = led.validate(txn["id"])
        print(f"  validation: {txn['state']}")
        if txn["state"] == "validated" or (txn["state"] == "provisional" and proj.acceptance.allow_provisional):
            led.accept(txn["id"])
            accepted += 1
            print("  accepted; re-analysing")
            refresh(proj, log=lambda m: print("    " + m))
        else:
            led.skip(txn["id"], f"auto: validation {txn['state']}")
    print(f"accepted {accepted} transaction(s)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from weaver.config import CONFIG_NAME
    from weaver.web.server import serve

    path = args.project or args.config
    if path is None and Path(CONFIG_NAME).exists():
        path = "."
    serve(path, host=args.host, port=args.port, open_browser=args.open)
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="weaver", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"weaver {__version__}")
    p.add_argument("-C", "--config", help="path to weaver.yaml or a directory containing it")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn: Any, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help)
        sp.set_defaults(func=fn)
        return sp

    sp = add("init", cmd_init, "write a weaver.yaml template")
    sp.add_argument("--force", action="store_true")

    sp = add("capture", cmd_capture, "build capture: generate compiler shims or finalize a capture log")
    sp.add_argument("action", choices=["shim", "finalize"])
    sp.add_argument("--tool", action="append", default=[], help="NAME=EXECUTABLE (repeatable)")
    sp.add_argument("--dir", help="shim directory")
    sp.add_argument("--log", help="capture log (JSONL)")
    sp.add_argument("--out", help="output directory for compile_commands.json/links.json/tools.json")

    def profiles(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--profile", action="append", help="restrict to profile(s)")

    sp = add("probe", cmd_probe, "probe toolchain capabilities with a fixture")
    profiles(sp)
    sp.add_argument("--json", action="store_true")

    sp = add("collect", cmd_collect, "run collection recipes for every unit")
    profiles(sp)
    sp.add_argument("--recipes", help="default | all | comma-separated recipe names")
    sp.add_argument("--file", action="append", help="only units compiling this file")
    sp.add_argument("--force", action="store_true", help="ignore the evidence cache")
    sp.add_argument("-j", "--jobs", type=int)
    sp.add_argument("--json", action="store_true")

    sp = add("fidelity", cmd_fidelity, "check a secondary Clang frontend against the production compiler")
    profiles(sp)
    sp.add_argument("--no-layout", action="store_true", help="skip ABI/layout probes")
    sp.add_argument("--json", action="store_true")

    sp = add("flow", cmd_flow, "run SVF points-to analysis as a separate job and record flow evidence")
    profiles(sp)
    sp.add_argument("--force", action="store_true", help="rebuild bitcode even if cached")
    sp.add_argument("--json", action="store_true")

    sp = add("inventory", cmd_inventory, "build the pointer inventory from collected evidence")
    profiles(sp)
    sp.add_argument("-j", "--jobs", type=int)
    sp.add_argument("--json", action="store_true")

    sp = add("findings", cmd_findings, "list inventory findings")
    sp.add_argument("--kind", action="append")
    sp.add_argument("--file")
    sp.add_argument("--function")
    sp.add_argument("--json", action="store_true")

    sp = add("coverage", cmd_coverage, "show code no analysed configuration compiled")
    sp.add_argument("--json", action="store_true")

    sp = add("candidates", cmd_candidates, "evaluate recipes on all findings")
    sp.add_argument("--recipe")
    sp.add_argument("--all", action="store_true", help="also list blocked candidates with reasons")
    sp.add_argument("--json", action="store_true")

    sp = add("show", cmd_show, "show one finding with its uses and recipe evaluation")
    sp.add_argument("finding")
    sp.add_argument("--json", action="store_true")

    sp = add("propose", cmd_propose, "open a transaction for a finding and preview its patch")
    sp.add_argument("finding")
    sp.add_argument("--recipe")

    sp = add("validate", cmd_validate, "validate a proposed transaction in isolated workspaces")
    sp.add_argument("txn")
    sp.add_argument("--keep", action="store_true", help="keep the validation workspaces")

    sp = add("accept", cmd_accept, "apply a validated transaction to the working tree")
    sp.add_argument("txn")

    sp = add("skip", cmd_skip, "skip a transaction")
    sp.add_argument("txn")
    sp.add_argument("--reason")

    sp = add("revert", cmd_revert, "revert an accepted transaction")
    sp.add_argument("txn")

    sp = add("card", cmd_card, "print a transaction's candidate card")
    sp.add_argument("txn")
    sp.add_argument("--json", action="store_true")

    sp = add("ledger", cmd_ledger, "list transactions")
    sp.add_argument("--json", action="store_true", help="print the raw event log")

    sp = add("refresh", cmd_refresh, "re-collect changed units, re-check fidelity, rebuild inventory and flow")
    sp.add_argument("-j", "--jobs", type=int)
    sp.add_argument("--no-flow", action="store_true", help="skip the SVF flow job")
    sp.add_argument("--capture", action="store_true", help="first rebuild through capture shims (profile 'capture')")

    sp = add("snapshot", cmd_snapshot, "save a baseline of pointer facts (working tree or a git revision)")
    sp.add_argument("action", choices=["save", "git", "list"])
    sp.add_argument("rev", nargs="?", help="git revision (for 'git')")
    sp.add_argument("--name")

    for name, help_ in (
        ("impact", "explain how pointer behavior changed since a snapshot"),
        ("check", "like impact, but exit 1 on high-risk changes (for CI)"),
    ):
        sp = add(name, cmd_impact, help_)
        sp.add_argument("--since", required=True, help="snapshot name")
        sp.add_argument("--revalidate", action="store_true", help="also run builds/tests/comparisons on both trees")
        sp.add_argument("--json", action="store_true")

    sp = add("contract", cmd_contract, "pin expected pointer behavior (checked by impact/check)")
    sp.add_argument("action", choices=["pin", "list", "expectations"])
    sp.add_argument("finding", nargs="?")
    sp.add_argument("--expect", help="comma-separated: read-only, no-escape, no-identity, no-reassign, not-null-tested")
    sp.add_argument("--reason")

    sp = add("graph", cmd_graph, "export the normalized evidence graph")
    sp.add_argument("-o", "--output")

    sp = add("slice", cmd_slice, "print the focused LLM evidence slice for a finding")
    sp.add_argument("finding")

    sp = add("explain", cmd_explain, "ask the LLM planner to explain a candidate (never to edit)")
    sp.add_argument("finding")
    sp.add_argument("--dry-run", action="store_true", help="print the request instead of calling the API")
    sp.add_argument("--model", help="Claude model id (default: $WEAVER_MODEL or claude-opus-5)")
    sp.add_argument(
        "--no-fallbacks",
        action="store_true",
        help="do not request server-side refusal fallbacks (e.g. on platforms without them)",
    )

    sp = add("serve", cmd_serve, "start the local web interface")
    sp.add_argument("project", nargs="?", help="project directory or weaver.yaml to open (default: -C or cwd)")
    sp.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--open", action="store_true", help="open a browser")

    sp = add("auto", cmd_auto, "propose/validate/accept eligible candidates under the acceptance policy")
    sp.add_argument("--recipe", help="restrict to one recipe (default: all)")
    sp.add_argument("--max", type=int, default=10)
    sp.add_argument("--dry-run", action="store_true", help="propose only")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except WeaverError as e:
        print(f"weaver: error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
