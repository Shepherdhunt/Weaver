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


def cmd_doctor(args: argparse.Namespace) -> int:
    from weaver.config import load_project
    from weaver.doctor import doctor, render
    from weaver.errors import ConfigError

    project, error = None, None
    if not args.machine:
        try:
            project = load_project(args.config)
        except ConfigError as e:
            if args.config or "no weaver.yaml found" not in str(e):
                error = str(e)  # a project was named, or one exists but does not load
    res = doctor(project, error)
    res["machine_only"] = bool(args.machine)
    if args.json:
        _print_json(res)
    else:
        print(render(res))
    return 0 if res["ok"] else 1


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
    want = {"all": ["svf", "gcc"], "svf": ["svf"], "gcc": ["gcc"]}[args.backend] if args.backend else proj.flow.backends
    if "gcc" in want:
        from weaver.flow.gcc_pta import run_gcc_pta

        for prof in proj.select_profiles(args.profile):
            g = run_gcc_pta(proj, prof, log=None if args.json else print)
            if args.json:
                _print_json(g)
                continue
            print(f"profile {prof.id}: GCC points-to {g['status']}" + (f" ({g['reason']})" if g.get("reason") else ""))
            if g.get("deviations"):
                print(f"  analysis deviations from production flags: {' '.join(g['deviations'])}")
            if g["status"] not in ("complete", "unavailable"):
                rc |= 1
    if "svf" not in want:
        return rc
    for prof in proj.select_profiles(args.profile):
        res = run_flow(proj, prof, force=args.force)
        if args.json:
            _print_json(res)
            continue
        print(
            f"profile {prof.id}: flow evidence {res['status']}" + (f" ({res['reason']})" if res.get("reason") else "")
        )
        from weaver.flow.evidence import load_flows

        flows = load_flows(proj, prof.id)
        for name, p in (res.get("programs") or {}).items():
            fe = flows.get(name) or flows.get(name.replace("/", "_"))
            diag = (fe.run.get("diagnostics") if fe else None) or {}
            n_units = len(p["units"]) if isinstance(p.get("units"), list) else p.get("units")
            print(
                f"  {name:<28} {p['status']:<10} {n_units} unit(s)"
                + ("" if p.get("closed", True) else ", open")
                + (f"; {diag.get('nodes')} pointer node(s), {diag.get('objects')} object(s)" if diag else "")
                + (f"  ({p['reason']})" if p.get("reason") else "")
            )
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
        for n in r.notes:
            if n.startswith("then:"):
                print(f"           then {n[5:].strip()}")
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


def cmd_patch(args: argparse.Namespace) -> int:
    from weaver.card import render_card
    from weaver.ledger import Ledger

    data = sys.stdin.buffer.read() if args.diff == "-" else Path(args.diff).read_bytes()
    txn = Ledger(_project(args)).propose_patch(data, args.removes or [], args.title or "", {"kind": "manual"})
    print(render_card(txn))
    return 0 if txn["state"] == "proposed" else 3


def cmd_draft(args: argparse.Namespace) -> int:
    from weaver.card import render_card
    from weaver.ledger import Ledger
    from weaver.llm.draft import draft

    proj = _project(args)
    res = draft(
        proj,
        args.finding,
        dry_run=args.dry_run,
        propose=not args.no_propose,
        log=lambda m: print(m, file=sys.stderr),
    )
    if args.dry_run:
        _print_json(res)
        return 0
    if "txn" in res:
        print(render_card(Ledger(proj).load(res["txn"])))
        return 0 if res["state"] == "proposed" else 3
    print(res.get("text", ""))
    if res.get("patch"):
        print("\n--- the draft's patch (not proposed) ---\n" + res["patch"])
        return 0
    print(f"weaver: {res.get('error')}", file=sys.stderr)
    return 3


def cmd_ratchet(args: argparse.Namespace) -> int:
    from weaver.ratchet import ratchet, render_github, render_text

    res, path = ratchet(
        _project(args),
        update=args.update,
        base_rev=args.base,
        profile=args.profile,
        strict=args.strict,
        log=lambda m: print(m, file=sys.stderr),
    )
    if args.update:
        if args.format == "json":
            _print_json(res)
        else:
            print(f"ratchet baseline written to {path}; commit it. Totals: {res['totals']}")
        return 0
    if args.format == "json":
        _print_json(res)
    elif args.format == "github":
        print(render_github(res, path))
    else:
        print(render_text(res, path))
    return 0 if res["ok"] else 1


def cmd_validate(args: argparse.Namespace) -> int:
    from weaver.card import render_card
    from weaver.ledger import Ledger

    txn = Ledger(_project(args)).validate(args.txn, keep=args.keep)
    print(render_card(txn))
    return 0 if txn["state"] == "validated" else 3


def cmd_accept(args: argparse.Namespace) -> int:
    from weaver.ledger import Ledger
    from weaver.patch import review_items

    txn = Ledger(_project(args)).accept(args.txn)
    print(
        f"{txn['id']} accepted: {', '.join(txn['acceptance']['post_hashes'])} updated; checkpoint "
        f"{txn['acceptance']['checkpoint']}.\nRun 'weaver refresh' before selecting the next candidate."
    )
    review = review_items(txn)
    if review:
        print(f"note: validation listed {len(review)} change(s) to pointer facts for review:", file=sys.stderr)
        for x in review[:20]:
            print(f"  [{x['severity']}] {x.get('name') or ''}: {x['text']}", file=sys.stderr)
    strength = txn["acceptance"].get("strength")
    if strength == "compile-only":
        print(
            "warning: accepted on compile and re-check evidence only; no test or differential run passed. "
            "Configure tests under validation in weaver.yaml (or the web UI's settings) to check behaviour.",
            file=sys.stderr,
        )
    elif strength in ("unexercised", "partly-exercised"):
        cov = next((r for r in txn["validation"]["records"] if r["kind"] == "coverage"), {})
        what = "never ran" if strength == "unexercised" else "did not run all of"
        print(f"warning: accepted although the tests {what} the change: {cov.get('detail', '')}", file=sys.stderr)
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
        strength = (t.get("acceptance") or {}).get("strength")
        weak = strength in ("compile-only", "unexercised", "partly-exercised")
        print(
            f"{t['id']}  {t['state']:<11} {t['recipe']:<12} {t['finding_id']:<14} {f.get('file')}:{f.get('line')} "
            f"{f.get('function')}() '{f.get('name')}'  {t['created_at']}" + (f"  [{strength}]" if weak else "")
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


def cmd_ai(args: argparse.Namespace) -> int:
    """Switch AI explanations on or off, choose the provider, store this user's key, show the guide."""
    import getpass

    from weaver.llm.keys import forget_key, keys_path, store_key
    from weaver.llm.prompt import GUIDE_PATH, GUIDE_VERSION, system_prompt
    from weaver.settings import ai_settings, write_settings

    proj = _project(args)
    if args.action == "guide":
        print(system_prompt(proj.ai.notes), end="")
        return 0
    if args.action in ("enable", "disable"):
        change: dict[str, Any] = {"enabled": args.action == "enable"}
        if args.drafts is not None:
            change["drafts"] = args.drafts
        for k in ("provider", "model", "base_url"):
            if getattr(args, k):
                change[k] = getattr(args, k)
        proj, _ = write_settings(proj, {"ai": change})
    elif args.action == "key":
        if args.forget:
            print("key removed" if forget_key(proj.ai.key_id) else "no stored key for this provider")
        else:
            key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("API key: ")
            store_key(proj.ai.key_id, key)
            print(f"key stored for {proj.ai.key_id} in {keys_path()} (readable only by you)")
    st = ai_settings(proj)
    print(f"AI explanations: {'on' if st['enabled'] else 'off'}")
    print(f"AI drafts: {'on' if st['enabled'] and st['drafts'] else 'off'}" + ("" if st["drafts"] else " (--drafts)"))
    print(f"  provider: {st['provider']}   model: {st['model'] or st['default_model'] or '(set ai.model)'}")
    if st["provider"] != "anthropic":
        print(f"  endpoint: {st['base_url'] or st['default_base_url']}")
    print(f"  key: {st['key']}")
    notes = f"; project notes: {proj.ai.notes}" if proj.ai.notes else ""
    print(f"  guide: version {GUIDE_VERSION} ({GUIDE_PATH}){notes}")
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    """Every pointer scored by the risk factors Weaver established for it, ranked."""
    from weaver.analysis.inventory import load_inventory
    from weaver.risk import RISK_NOTE, report

    proj = _project(args)
    scope = args.scope or []
    rep = report(proj, load_inventory(proj), lambda f: not scope or any(f.startswith(s) for s in scope))
    rows = [r for r in rep["pointers"] if not args.level or r["level"] == args.level]
    if args.json:
        _print_json({**rep, "pointers": rows})
        return 0
    sm = rep["summary"]
    print(f"{sm['pointers']} pointer(s): {sm['high']} high, {sm['medium']} medium, {sm['low']} low risk")
    print(f"  {RISK_NOTE}")
    print("\nFactors (pointers, weight):")
    for f in sorted(rep["factors"].values(), key=lambda v: -v["pointers"]):
        if f["pointers"]:
            print(f"  {f['pointers']:>6}  +{f['weight']}  {f['title']}")
    print(f"\nMost at risk (first {min(args.top, len(rows))} of {len(rows)}):")
    for r in rows[: args.top]:
        where = f"{r['file']}:{r['line']}" + (f" {r['function']}()" if r.get("function") else "")
        print(f"  {r['score']:>3} {r['level']:<6} {r['name']:<24} {where}")
        print(f"        {'; '.join(x['title'] + ' (' + x['evidence'][0] + ')' for x in r['factors'])}")
    print("\nBy module (total score, pointers):")
    for g in rep["modules"][:8]:
        print(f"  {g['score']:>6} {g['pointers']:>5}  {g['name']}  ({g['high']} high, {g['medium']} medium)")
    return 0


def cmd_simplify(args: argparse.Namespace) -> int:
    """Which constructs stand between each function and a target profile (CLite or a simplification goal)."""
    from weaver.analysis.inventory import load_inventory
    from weaver.simplify import check

    proj = _project(args)
    scope = args.scope or []
    rep = check(proj, load_inventory(proj), args.profile, lambda f: not scope or any(f.startswith(s) for s in scope))
    if args.json:
        _print_json(rep)
        return 0
    p, sm = rep["profile"], rep["summary"]
    print(f"Profile: {p['title']} ({p['id']}){' — provisional' if p['provisional'] else ''}")
    for n in rep["notes"]:
        print(f"  note: {n}")
    print(f"{sm['ready']} of {sm['functions']} function(s) meet it ({sm['percent']}%)")
    print("\nBy rule (functions / sites):")
    for r, c in sorted(sm["by_rule"].items(), key=lambda kv: -kv[1]["functions"]):
        print(f"  {c['functions']:>6} / {c['sites']:<6} {r:<22} {rep['rules'][r]['title']}")
    rows = [r for r in rep["functions"] if r["violations"]]
    if args.function:
        rows = [r for r in rows if r["function"] == args.function]
        for r in rows:
            print(f"\n{r['file']}:{r['line']} {r['function']}()")
            for v in r["violations"]:
                print(f"  line {v['line']}: {v['rule']}: {v['text']}")
        return 0
    if rows:
        print(f"\nMost constructs to remove (first {min(args.top, len(rows))} of {len(rows)}):")
        for r in rows[: args.top]:
            counts = ", ".join(f"{k} {n}" for k, n in sorted(r["counts"].items(), key=lambda kv: -kv[1]))
            print(f"  {len(r['violations']):>4}  {r['file']}:{r['line']} {r['function']}()  [{counts}]")
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


def cmd_report(args: argparse.Namespace) -> int:
    from weaver.report import build_report, render_markdown

    proj = _project(args)
    rep = build_report(proj, args.scope or [], log=lambda m: print(m, file=sys.stderr, flush=True))
    text = json.dumps(rep, indent=2, default=str) if args.json else render_markdown(rep)
    if args.output:
        Path(args.output).write_text(text + "\n")
        if args.json_output:
            Path(args.json_output).write_text(json.dumps(rep, indent=2, default=str) + "\n")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_tests(args: argparse.Namespace) -> int:
    from weaver.settings import read_settings, write_settings

    proj = _project(args)
    st = read_settings(proj)
    if args.add:
        profs = [p for p in st["profiles"] if not args.profile or p["id"] == args.profile]
        if len(profs) != 1:
            raise WeaverError("choose a profile with --profile" if profs else f"unknown profile {args.profile!r}")
        prof = profs[0]
        tests = list(prof["tests"])
        by_name = {s["name"]: s for s in prof["suggestions"]}
        for a in args.add:
            sug = by_name.get(a)
            cmd = {"name": sug["name"], "run": sug["run"]} if sug else {"name": f"test{len(tests)}", "run": a}
            if any(t["run"] == cmd["run"] for t in tests):
                continue
            tests.append(cmd)
        build = args.build or (prof["build"] or {}).get("run") or prof["capture_build"]
        change: dict[str, Any] = {"profiles": [{"id": prof["id"], "tests": tests, "build": {"run": build or ""}}]}
        if not args.no_require and "testing" not in st["acceptance"]["require"]:
            change["acceptance"] = {"require": [*st["acceptance"]["require"], "testing"]}
        proj, warnings = write_settings(proj, change)
        print(f"updated {proj.config_path} (previous version in {proj.config_path.name}.bak)")
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        st = read_settings(proj)
    if args.json:
        _print_json(st)
        return 0
    s = st["strength"]
    print(
        f"validation strength: {s['level']}"
        + (f" (policy requires {', '.join(s['required'])})" if s["required"] else "")
    )
    for n in s["notes"]:
        print(f"  note: {n}")
    for prof in st["profiles"]:
        print(f"profile {prof['id']}:")
        print(f"  build: {(prof['build'] or {}).get('run') or '(none)'}")
        for t in prof["tests"]:
            print(f"  test {t['name']}: {t['run']}")
        for t in prof["compare"]:
            print(f"  compare {t['name']}: {t['run']}")
        configured = {t["run"].split()[0] for t in prof["tests"] if t["run"].split()}
        for sug in prof["suggestions"]:
            if sug["run"].split()[0] in configured:
                continue  # the same runner is configured already (perhaps with another build directory)
            print(f"  suggested {sug['name']}: {sug['run']}  ({sug['why']})")
        if not prof["suggestions"] and not prof["tests"]:
            print("  no test entry point found; add one with --add 'COMMAND'")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """The declared threads of control, what Weaver checked about them, and who writes which global."""
    import collections

    from weaver.analysis.inventory import load_inventory
    from weaver.flow.tasks import spawn_sites, spec_of
    from weaver.recipes import RecipeContext

    proj = _project(args)
    ctx = RecipeContext(proj, load_inventory(proj))
    spec = spec_of(proj.preservation)
    out: dict[str, Any] = {"declared": spec is not None, "programs": {}}
    for prof in proj.select_profiles(args.profile):
        lm = ctx.link(prof.id)
        for prog in lm.programs:
            if not prog.closed or (args.program and prog.name != args.program):
                continue
            if spec is None or (spec.get("programs") and prog.name not in spec["programs"]):
                sites = spawn_sites(ctx.program, {f"{prof.id}/{prog.name}"})
                out["programs"][f"{prof.id}/{prog.name}"] = {"declared": False, "spawn_sites": sites}
                continue
            tm = ctx.tasks(prof.id, prog.name)
            rec = tm.to_json()
            rec["ownership"] = tm.ownership()
            out["programs"][f"{prof.id}/{prog.name}"] = rec
    if args.json:
        _print_json(out)
        return 0
    for key, rec in out["programs"].items():
        if not rec.get("contexts"):
            sites = rec.get("spawn_sites") or []
            if not sites:
                print(f"{key}: starts no thread of control (single-threaded)")
                continue
            print(f"{key}: starts threads, and no task model covers it; a starting point for weaver.yaml:")
            entries = sorted({e for s in sites for e in _spawn_entries(ctx, s)})
            print("  preservation:\n    concurrency:\n      model: tasks\n      tasks:")
            print("        - {name: main, entry: main}")
            for e in entries:
                print(f"        - {{name: {e}, entry: {e}}}")
            for s in sites[:8]:
                print(f"  # {s['callee']}() at {s['site'].get('file')}:{s['site'].get('line')} in {s['function']}()")
            continue
        status = "complete" if not rec["problems"] else f"{len(rec['problems'])} problem(s)"
        print(f"{key}: {len(rec['contexts'])} context(s), declaration {status}")
        for c in rec["contexts"]:
            print(
                f"  {c['kind']:<10} {c['name']:<24} {c['instances']:<4} {c['functions']:>5} function(s)"
                + (
                    f", {c['unresolved_indirect_calls']} unresolved indirect call(s)"
                    if c["unresolved_indirect_calls"]
                    else ""
                )
            )
        for p in rec["problems"]:
            print(f"  problem: {p}")
        for a in rec["assumptions"]:
            print(f"  assumption: {a}")
        own = rec["ownership"]
        counts = collections.Counter(o["state"] for o in own)
        print(
            f"  globals: {counts.get('owned', 0)} owned by one task, {counts.get('shared', 0)} written by several, "
            f"{counts.get('open', 0)} within reach of writes Weaver cannot bound, "
            f"{counts.get('unwritten', 0)} never written"
        )
        by_owner = collections.defaultdict(list)
        for o in own:
            if o["owner"]:
                by_owner[o["owner"]].append(o["object"])
        for owner, objs in sorted(by_owner.items()):
            print(f"    {owner}: {', '.join(sorted(objs)[:10])}" + (f" (+{len(objs) - 10})" if len(objs) > 10 else ""))
        shared = [o for o in own if o["state"] == "shared"][:10]
        for o in shared:
            print(f"    shared {o['object']}: {', '.join(o['writers'])}")
    return 0


def _spawn_entries(ctx: Any, site: dict[str, Any]) -> set[str]:
    from weaver.flow.tasks import TaskModel

    f = ctx.program.funcs[site["key"]]
    call = next(c for c in f["calls"] if c.get("site") == site["site"])
    m = ctx.program.models.lookup(call["callee"])
    if not isinstance(getattr(m, "spawns", None), int):
        return set()
    probe = TaskModel.__new__(TaskModel)
    probe.fe = None
    return TaskModel._entries_of(probe, f, call, m.spawns)


def cmd_serve(args: argparse.Namespace) -> int:
    from weaver.config import CONFIG_NAME
    from weaver.web.server import serve

    path = args.project or args.config
    if path is None and Path(CONFIG_NAME).exists():
        path = "."
    serve(path, host=args.host, port=args.port, open_browser=args.open, scope=args.scope)
    return 0


def cmd_export_ui(args: argparse.Namespace) -> int:
    from weaver.web.export import load_dataset, render_page, snapshot_dataset

    datasets = [load_dataset(Path(p)) for p in args.add or []]
    if not args.only_added:
        proj = _project(args)
        say = lambda m: print(m, file=sys.stderr)  # noqa: E731
        ds = snapshot_dataset(proj, args.scope, args.label, args.description or "", log=say)
        datasets.insert(0, ds)
    if not datasets:
        raise WeaverError("nothing to export: give a project, or --add a dataset")
    out = Path(args.output)
    if args.json:
        if len(datasets) != 1:
            raise WeaverError("--json writes one dataset; combine datasets into a page with --add")
        out.write_text(json.dumps(datasets[0], separators=(",", ":")))
    else:
        out.write_text(render_page(datasets, args.title, args.fragment, args.repo, args.branch))
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB, {len(datasets)} dataset(s))")
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

    sp = add("doctor", cmd_doctor, "check that this machine and project have what Weaver needs, and say how to fix it")
    sp.add_argument("--machine", action="store_true", help="check this machine only, not the project")
    sp.add_argument("--json", action="store_true")

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

    sp = add("flow", cmd_flow, "run points-to analysis (SVF and/or the production GCC) and record flow evidence")
    profiles(sp)
    sp.add_argument(
        "--backend",
        choices=["svf", "gcc", "all"],
        help="which analysis to run (default: flow.backend in weaver.yaml; 'gcc' needs a GCC profile)",
    )
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
    sp.add_argument(
        "--expect", help="comma-separated: read-only, no-escape, no-identity, no-reassign, not-null-tested, borrowed"
    )
    sp.add_argument("--reason")

    sp = add("graph", cmd_graph, "export the normalized evidence graph")
    sp.add_argument("-o", "--output")

    sp = add("slice", cmd_slice, "print the focused LLM evidence slice for a finding")
    sp.add_argument("finding")

    sp = add("explain", cmd_explain, "ask the configured AI model to explain a pointer (advisory; never edits)")
    sp.add_argument("finding")
    sp.add_argument("--dry-run", action="store_true", help="print the request instead of sending it")
    sp.add_argument("--model", help="model id (default: $WEAVER_MODEL, then ai.model, then the provider's default)")
    sp.add_argument(
        "--no-fallbacks",
        action="store_true",
        help="do not request server-side refusal fallbacks (e.g. on platforms without them)",
    )

    sp = add("report", cmd_report, "inventory and rejection report: what is eligible, what is blocked and why")
    sp.add_argument("--scope", action="append", help="path prefix to report on (repeatable), e.g. apps/sample_app")
    sp.add_argument("-o", "--output", help="write the report to a file")
    sp.add_argument("--json-output", help="also write the full JSON report here (with -o)")
    sp.add_argument("--json", action="store_true", help="print JSON instead of Markdown")

    sp = add("tests", cmd_tests, "show validation strength; detect and configure the project's test commands")
    sp.add_argument(
        "--add",
        action="append",
        metavar="NAME|COMMAND",
        help="add a suggested test (by name) or a shell command to the profile's validation tests (repeatable)",
    )
    sp.add_argument("--profile", help="profile to change (required when there are several)")
    sp.add_argument("--build", help="validation build command (default: the capture command with the real compiler)")
    sp.add_argument("--no-require", action="store_true", help="do not add 'testing' to acceptance.require")
    sp.add_argument("--json", action="store_true")

    sp = add("tasks", cmd_tasks, "threads of control: the checked task declaration and which task writes which global")
    profiles(sp)
    sp.add_argument("--program", help="only this program (default: every closed program)")
    sp.add_argument("--json", action="store_true")

    sp = add("serve", cmd_serve, "start the local web interface")
    sp.add_argument("project", nargs="?", help="project directory or weaver.yaml to open (default: -C or cwd)")
    sp.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    sp.add_argument("--port", type=int, help="port to bind (default: 61847, or the next free one; 0: any free port)")
    sp.add_argument("--open", action="store_true", help="open a browser")
    sp.add_argument(
        "--scope",
        action="append",
        help="only show pointers under this path prefix (repeatable); recipes still see the whole program",
    )

    sp = add("export-ui", cmd_export_ui, "write a read-only snapshot of the web interface as one HTML file")
    sp.add_argument("-o", "--output", required=True, help="the HTML page (or dataset, with --json) to write")
    sp.add_argument("--scope", action="append", help="only pointers under this path prefix (repeatable)")
    sp.add_argument("--label", help="the dataset's name in the page (default: the project name)")
    sp.add_argument("--description", help="one line shown with the dataset")
    sp.add_argument("--add", action="append", help="also include a dataset written earlier with --json (repeatable)")
    sp.add_argument("--only-added", action="store_true", help="do not export the current project, only --add datasets")
    sp.add_argument("--json", action="store_true", help="write this project's dataset as JSON instead of a page")
    sp.add_argument("--title", default="Weaver", help="the page title")
    sp.add_argument("--fragment", action="store_true", help="leave out <html>, <head> and <body> (the host adds them)")
    sp.add_argument("--repo", help="git URL the page's run-it-locally steps clone")
    sp.add_argument("--branch", help="branch the page's run-it-locally steps check out")

    sp = add("risk", cmd_risk, "pointers ranked by the risk factors Weaver established for each, and why")
    sp.add_argument("--scope", action="append", help="only pointers under this path prefix (repeatable)")
    sp.add_argument("--level", choices=["high", "medium", "low"])
    sp.add_argument("--top", type=int, default=15, help="pointers to list (default 15)")
    sp.add_argument("--json", action="store_true")

    sp = add("simplify", cmd_simplify, "constructs standing between each function and a target (CLite or a goal)")
    sp.add_argument("--profile", help="clite-provisional, pointer-free, modular, or a profile from weaver.yaml")
    sp.add_argument("--scope", action="append", help="only functions under this path prefix (repeatable)")
    sp.add_argument("--function", help="list one function's constructs with their lines")
    sp.add_argument("--top", type=int, default=20, help="functions to list (default 20)")
    sp.add_argument("--json", action="store_true")

    sp = add("ai", cmd_ai, "AI explanations and drafts: status, enable/disable, provider, your API key, the guides")
    sp.add_argument("action", nargs="?", default="status", choices=["status", "enable", "disable", "key", "guide"])
    sp.add_argument("--provider", choices=["anthropic", "openai-compatible"])
    sp.add_argument("--model", help="model id (required for openai-compatible)")
    sp.add_argument("--base-url", help="Chat Completions endpoint, e.g. http://localhost:11434/v1 for Ollama")
    sp.add_argument("--forget", action="store_true", help="with 'key': remove the stored key")
    sp.add_argument(
        "--drafts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with enable/disable: also let the model draft patches (--no-drafts turns only drafts off)",
    )

    sp = add("patch", cmd_patch, "open a transaction for your own change (a unified diff) and check it like a recipe's")
    sp.add_argument("diff", help="unified diff against the analysed tree (git diff, diff -u); '-' reads stdin")
    sp.add_argument("--removes", action="append", metavar="FINDING", help="a pointer the change removes (repeatable)")
    sp.add_argument("--title", help="what the change does, for the ledger")

    sp = add("draft", cmd_draft, "ask the project's AI model to draft a patch removing a pointer; check it like yours")
    sp.add_argument("finding")
    sp.add_argument("--dry-run", action="store_true", help="print the request instead of sending it")
    sp.add_argument("--no-propose", action="store_true", help="print the draft instead of opening a transaction")

    sp = add("ratchet", cmd_ratchet, "for CI: fail when pointers, high-risk pointers or profile violations go up")
    sp.add_argument("--update", action="store_true", help="write the baseline from the current inventory")
    sp.add_argument("--base", metavar="REV", help="compare only the files changed since this git revision")
    sp.add_argument("--profile", help="simplification profile to count (default: ratchet.profile, the baseline's)")
    sp.add_argument("--strict", action="store_true", help="also fail when the baseline could be tightened")
    sp.add_argument("--format", choices=["text", "json", "github"], default="text",
                    help="github: error annotations on the new pointers in a pull request")  # fmt: skip

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
