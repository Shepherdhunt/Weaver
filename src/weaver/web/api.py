"""View models for the web interface.

These functions shape inventory, flow and ledger data for the browser.  They
compute nothing new: every fact shown in the UI comes from the same evidence
files the CLI uses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from weaver.analysis.inventory import load_inventory
from weaver.analysis.uses import describe_use_parts
from weaver.config import CONFIG_NAME, Project
from weaver.impact import access_class, list_snapshots, load_contracts
from weaver.store import Store
from weaver.util import read_json, sha256_file


class Cache:
    """Per-inventory cache of recipe verdicts (evaluating recipes is the expensive part)."""

    def __init__(self) -> None:
        self.key: tuple[str, str] | None = None
        self.verdicts: dict[str, Any] = {}
        self.inv: dict[str, Any] | None = None

    def get(self, project: Project) -> tuple[dict[str, Any], dict[str, Any]]:
        inv = load_inventory(project)
        key = (str(project.root), inv["generated_at"] + _flow_stamp(project) + _config_stamp(project))
        if key != self.key:
            from weaver.recipes import RecipeContext, recipes_for_finding

            ctx = RecipeContext(project, inv)
            verdicts: dict[str, Any] = {}
            for f in inv["findings"]:
                for r in recipes_for_finding(f):
                    res = r.evaluate(ctx, f)
                    verdicts.setdefault(f["id"], {})[r.id] = {
                        "eligible": res.eligible,
                        "edits": len(res.edits),
                        "preconditions": [p.to_json() for p in res.preconditions],
                    }
            self.key, self.verdicts, self.inv = key, verdicts, inv
        assert self.inv is not None
        return self.inv, self.verdicts


def _flow_stamp(project: Project) -> str:
    root = Store(project.state_dir).root / "flow"
    stamps = []
    for p in project.profiles:
        f = root / p.id / "run.json"
        if f.exists():
            stamps.append(str(f.stat().st_mtime))
    return ":".join(stamps)


def _config_stamp(project: Project) -> str:
    parts = [str(project.config_path.stat().st_mtime)]
    if project.contracts_path and project.contracts_path.exists():
        parts.append(str(project.contracts_path.stat().st_mtime))
    return ":".join(parts)


def project_state(project: Project) -> dict[str, Any]:
    from weaver.flow.evidence import flow_status
    from weaver.flow.svf import find_wpa
    from weaver.ledger import Ledger

    store = Store(project.state_dir)
    inv = None
    if store.inventory_path.exists():
        inv = read_json(store.inventory_path)
    stale = []
    if inv:
        for f, h in inv["files"].items():
            p = project.root / f
            if p.exists() and sha256_file(p) != h:
                stale.append(f)
    txns = Ledger(project).all()
    counts: dict[str, int] = {}
    for t in txns:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return {
        "project": {
            "name": project.name,
            "root": str(project.root),
            "config": str(project.config_path),
            "profiles": [
                {
                    "id": p.id,
                    "compile_commands": str(p.compile_commands),
                    "compile_commands_exist": p.compile_commands.exists(),
                    "capture": bool(p.capture),
                    "secondary": p.secondary_frontend.compiler if p.secondary_frontend else None,
                    "target": p.target,
                }
                for p in project.profiles
            ],
            "preservation": project.preservation,
            "acceptance": {"require": project.acceptance.require, "min_evidence": project.acceptance.min_evidence},
        },
        "inventory": {"generated_at": inv["generated_at"], "summary": inv["summary"]} if inv else None,
        "stale_files": stale,
        "flow": flow_status(project, inv) if inv else {},
        "svf_available": bool(find_wpa(project)),
        "snapshots": list_snapshots(project),
        "transactions": counts,
    }


def pointer_list(project: Project, cache: Cache) -> dict[str, Any]:
    from weaver.ledger import Ledger

    inv, verdicts = cache.get(project)
    rows = []
    for f in inv["findings"]:
        v = verdicts.get(f["id"], {})
        rows.append(
            {
                "id": f["id"],
                "kind": f["kind"],
                "name": f.get("name"),
                "type": f.get("type"),
                "function": f.get("function"),
                "record": f.get("record"),
                "file": f.get("file"),
                "line": f.get("line"),
                "class": access_class(f),
                "evidence": f.get("evidence_status"),
                "uses": len(f.get("uses", [])),
                "recipes": {
                    k: {
                        "eligible": x["eligible"],
                        "edits": x["edits"],
                        "blockers": [p["id"] for p in x["preconditions"] if p["status"] != "established"],
                    }
                    for k, x in v.items()
                },
            }
        )
    removed = [
        {
            "txn": t["id"],
            "recipe": t["recipe"],
            "name": t["finding"].get("name"),
            "function": t["finding"].get("function"),
            "file": t["finding"].get("file"),
            "at": t["history"][-1]["at"],
        }
        for t in Ledger(project).all()
        if t["state"] == "accepted"
    ]
    return {"pointers": rows, "removed": removed, "generated_at": inv["generated_at"]}


def pointer_detail(project: Project, fid: str, cache: Cache) -> dict[str, Any]:
    from weaver.analysis.inventory import find_finding
    from weaver.flow.evidence import load_flow
    from weaver.llm.slice import source_lines

    inv, verdicts = cache.get(project)
    f = find_finding(inv, fid)
    rel = f.get("file") or ""
    lines = (project.root / rel).read_text(errors="replace").splitlines() if (project.root / rel).exists() else []
    uses = []
    for u in f.get("uses", []):
        ln = u.get("line") or 0
        uses.append(
            {
                **u,
                "text": describe_use_parts(u["kind"], u.get("access"), u.get("detail") or {}),
                "source": lines[ln - 1].strip() if 0 < ln <= len(lines) else None,
            }
        )
    svf_targets = []
    for occ_profile in sorted({o["profile"] for o in f.get("occurrences", [])}):
        fe = load_flow(project, occ_profile, inv)
        if fe is None:
            continue
        objs = _svf_targets_for(fe, f)
        if objs is not None:
            svf_targets.append({"profile": occ_profile, "complete": fe.complete, "targets": objs})
    lo = max(1, (f.get("line") or 1) - 3)
    fsum = inv.get("functions", {}).get(f"{rel}::{f.get('function')}")
    hi = (fsum or {}).get("end_line") or (f.get("line") or 1) + 12
    if fsum:
        lo = min(lo, fsum.get("line") or lo)
    contracts = [c for c in load_contracts(project) if c.get("finding") == f["id"]]
    callers = []
    call_args: list[dict[str, Any]] = []
    if f.get("kind") == "parameter" and f.get("function"):
        from weaver.llm.slice import callers_of

        callers = callers_of(inv, f["function"])
        call_args = _call_arguments(project, inv, f)
    return {
        "finding": {k: v for k, v in f.items() if k != "uses"},
        "class": access_class(f),
        "uses": uses,
        "excerpt": {"file": rel, "start": lo, "lines": source_lines(project.root, rel, lo, min(hi, lo + 80))},
        "svf_targets": svf_targets,
        "recipes": verdicts.get(f["id"], {}),
        "contracts": contracts,
        "callers": callers,
        "call_args": call_args,
    }


def _call_arguments(project: Project, inv: dict[str, Any], f: dict[str, Any]) -> list[dict[str, Any]]:
    """What each analysed call site passes for a parameter: the syntactic hypothesis for its targets."""
    idx = f.get("param_index", 0)
    out: list[dict[str, Any]] = []
    texts: dict[str, list[str]] = {}
    for key, fsum in inv.get("functions", {}).items():
        for c in fsum.get("calls", []):
            if c.get("callee") != f["function"] or idx >= len(c.get("args", [])):
                continue
            a = c["args"][idx]
            site = c.get("site") or {}
            text = None
            sp = a.get("span")
            if sp:
                if sp["file"] not in texts:
                    path = project.root / sp["file"]
                    texts[sp["file"]] = [path.read_bytes().decode("latin-1")] if path.exists() else [""]
                text = texts[sp["file"]][0][sp["start"] : sp["end"]]
            target = (a.get("addr_of") or {}).get("name")
            path_ = "".join((a.get("addr_of") or {}).get("path") or [])
            out.append(
                {
                    "caller": key.partition("::")[2],
                    "file": site.get("file"),
                    "line": site.get("line"),
                    "text": text,
                    "object": (target + path_) if target else None,
                    "null": bool(a.get("null")),
                }
            )
    out.sort(key=lambda x: (x["file"] or "", x["line"] or 0))
    return out


def _svf_targets_for(fe: Any, f: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Points-to targets of a pointer declaration from SVF evidence (None if not mapped)."""
    if f["kind"] == "parameter" and f.get("function") is not None:
        t = fe.param_targets(f["function"], f.get("param_index", 0))
        return [fe.describe(o) for o in sorted(t)] if t is not None else None
    # A local/global pointer variable: its stack/global object holds the pointer values.
    holders = fe.objects_for_decl(f.get("file"), f.get("line"), f.get("name"))
    if not holders:
        return None
    pts: set[int] = set()
    for h in holders:
        pts.update(fe.nodes.get(h, {}).get("pts", []))
    return [fe.describe(o) for o in sorted(pts)]


def map_model(project: Project, cache: Cache) -> dict[str, Any]:
    """Files → functions → pointers, plus call edges, for the overview map."""
    inv, verdicts = cache.get(project)
    files: dict[str, dict[str, Any]] = {}

    def fn_entry(file: str, fn: str | None) -> dict[str, Any]:
        fe = files.setdefault(file, {"file": file, "functions": {}, "counts": {}})
        key = fn or "(file scope)"
        return fe["functions"].setdefault(key, {"name": key, "pointers": [], "calls": [], "callers": []})

    for f in inv["findings"]:
        if not f.get("file"):
            continue
        cls = access_class(f)
        e = fn_entry(f["file"], f.get("function"))
        v = verdicts.get(f["id"], {})
        e["pointers"].append(
            {
                "id": f["id"],
                "name": f.get("name"),
                "kind": f["kind"],
                "class": cls,
                "eligible": any(x["eligible"] for x in v.values()),
                "line": f.get("line"),
                "type": f.get("type"),
            }
        )
        c = files[f["file"]]["counts"]
        c[cls] = c.get(cls, 0) + 1
    for key, fsum in inv.get("functions", {}).items():
        file, _, name = key.partition("::")
        e = fn_entry(file, name)
        e["line"] = fsum.get("line")
        e["calls"] = sorted({c["callee"] for c in fsum.get("calls", []) if c.get("callee")})
        e["indirect_calls"] = len(fsum.get("indirect_calls", []))
    names: dict[str, list[tuple[str, str]]] = {}
    for file, fe in files.items():
        for name in fe["functions"]:
            names.setdefault(name, []).append((file, name))
    for file, fe in files.items():
        for name, e in fe["functions"].items():
            for callee in e.get("calls", []):
                for tf, tn in names.get(callee, []):
                    files[tf]["functions"][tn]["callers"].append(f"{file}::{name}")
    out = []
    for file in sorted(files):
        fe = files[file]
        fns = sorted(fe["functions"].values(), key=lambda x: (x.get("line") or 0, x["name"]))
        out.append({"file": file, "counts": fe["counts"], "functions": fns})
    return {"files": out}


def neighborhood(project: Project, fid: str, cache: Cache) -> dict[str, Any]:
    """Relationship graph around one pointer: targets (AST and SVF), escapes, function, callers."""
    d = pointer_detail(project, fid, cache)
    f = d["finding"]
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    pid = f["id"]
    nodes[pid] = {"id": pid, "type": "pointer", "label": f.get("name"), "class": d["class"], "main": True}
    if f.get("function"):
        fk = f"fn:{f['function']}"
        nodes[fk] = {"id": fk, "type": "function", "label": f["function"] + "()"}
        edges.append({"from": pid, "to": fk, "type": "declared-in"})
        for c in d["callers"]:
            ck = f"fn:{c.split('::')[-1]}"
            nodes.setdefault(ck, {"id": ck, "type": "function", "label": c.split("::")[-1] + "()"})
            edges.append({"from": ck, "to": fk, "type": "calls"})
    for a in d.get("call_args", []):
        name = a["object"] or a["text"] or "?"
        ok = f"obj:{name}"
        nodes.setdefault(ok, {"id": ok, "type": "object", "label": a["object"] or f"value of {name}", "source": "ast"})
        edges.append(
            {
                "from": pid,
                "to": ok,
                "type": "may-point-to",
                "evidence": "ast",
                "via": f"argument at {a['file']}:{a['line']} in {a['caller']}()",
            }
        )
    for t in f.get("possible_targets", []) if not d.get("call_args") else []:
        obj = t.get("object") or {}
        name = obj.get("name") or t.get("name") or t.get("callee") or t.get("source")
        ok = f"obj:{name}"
        nodes.setdefault(ok, {"id": ok, "type": "object", "label": name, "source": "ast"})
        edges.append({"from": pid, "to": ok, "type": "may-point-to", "evidence": "ast", "via": t.get("via")})
    for s in d["svf_targets"]:
        for o in s["targets"]:
            name = o.get("name") or f"#{o['id']}"
            ok = f"obj:{name}"
            node = nodes.setdefault(
                ok, {"id": ok, "type": "object", "label": name, "kind": o.get("kind"), "source": "svf"}
            )
            if node.get("source") == "ast":
                node["source"] = "both"
            if not any(e["from"] == pid and e["to"] == ok and e.get("evidence") == "svf" for e in edges):
                edges.append({"from": pid, "to": ok, "type": "may-point-to", "evidence": "svf"})
    for u in d["uses"]:
        det = u.get("detail") or {}
        if u["kind"] == "call-arg" and det.get("callee"):
            ck = f"fn:{det['callee']}"
            nodes.setdefault(ck, {"id": ck, "type": "function", "label": det["callee"] + "()"})
            edges.append({"from": pid, "to": ck, "type": "escapes-to", "line": u["line"]})
        elif u["kind"] == "copy" and (det.get("into") or {}).get("name"):
            nk = f"var:{det['into']['name']}"
            nodes.setdefault(nk, {"id": nk, "type": "pointer", "label": det["into"]["name"], "class": "unknown"})
            edges.append({"from": pid, "to": nk, "type": "copied-into", "line": u["line"]})
    return {"nodes": list(nodes.values()), "edges": edges, "focus": pid}


def source_view(project: Project, rel: str, cache: Cache) -> dict[str, Any]:
    root = project.root.resolve()
    p = (root / rel).resolve()
    if not str(p).startswith(str(root) + os.sep) or not p.is_file():
        raise FileNotFoundError(rel)
    inv, verdicts = cache.get(project)
    marks = []
    for f in inv["findings"]:
        if f.get("file") != rel:
            continue
        cls = access_class(f)
        marks.append(
            {
                "line": f.get("line"),
                "col": f.get("col"),
                "len": len(f.get("name") or ""),
                "finding": f["id"],
                "role": "declaration",
                "class": cls,
                "name": f.get("name"),
            }
        )
        for u in f.get("uses", []):
            acc = u.get("access")
            role = (
                "write"
                if acc in ("write", "readwrite")
                else ("escape" if u["kind"] in ("call-arg", "copy", "return", "cast") else "read")
            )
            marks.append(
                {
                    "line": u.get("line"),
                    "col": u.get("col"),
                    "len": len(f.get("name") or ""),
                    "finding": f["id"],
                    "role": role,
                    "class": cls,
                    "name": f.get("name"),
                    "text": describe_use_parts(u["kind"], acc, u.get("detail") or {}),
                }
            )
    cov = inv["coverage"]["files"].get(rel, {})
    return {
        "file": rel,
        "lines": p.read_text(errors="replace").splitlines(),
        "marks": marks,
        "unexamined": cov.get("unexamined_ranges", []),
        "files": sorted(inv["files"]),
    }


def setup_project(
    path: str,
    build: str,
    compiler: str,
    clean: str | None,
    secondary: str | None,
    concurrency: str | None,
    name: str | None,
    run: str | None = None,
) -> Path:
    """Write a weaver.yaml whose profile rebuilds the project through capture shims.

    With ``run`` (a command whose output characterises the program: a test
    suite or the program itself), validation also rebuilds baseline and
    candidate workspaces with the real compiler and compares that command's
    output, and acceptance requires the comparison to pass.
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    cfg_path = root / CONFIG_NAME
    if cfg_path.exists():
        raise FileExistsError(f"{cfg_path} already exists")
    profile: dict[str, Any] = {
        "id": "default",
        "description": f"captured build: {build}",
        "compile_commands": ".weaver/compdb/default/compile_commands.json",
        "capture": {"command": build, "tools": {"cc": compiler}},
        "target": {"architecture": "recorded_architecture"},
        "platform": {"runtime_mode": "recorded_runtime_mode"},
    }
    if clean:
        profile["capture"]["clean"] = clean
    if secondary:
        profile["secondary_frontend"] = {"compiler": secondary}
    require = ["compile", "mechanical-recheck"]
    if run:
        profile["validation"] = {
            "build": {"run": build.replace("{cc}", compiler), "cwd": "{workspace}"},
            "compare": [{"name": "run", "run": run, "cwd": "{workspace}"}],
        }
        require.append("differential-testing")
    cfg = {
        "schema": "weaver.project/1",
        "project": {"name": name or root.name, "root": ".", "workspace_exclude": [".git"]},
        "preservation": {
            "behaviors": ["outputs", "persistent-state", "side-effect-ordering"],
            **({"concurrency": concurrency} if concurrency else {}),
        },
        "acceptance": {"require": require},
        "profiles": [profile],
    }
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return cfg_path
