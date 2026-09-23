"""SVF as a separate, pinned analysis job (artifact plan §12).

``run_flow`` builds whole-program bitcode for one profile from the frontend IR
of every unit, links it with the producer's own ``llvm-link``, runs SVF's
``wpa`` under time and memory limits, and parses its text output into
Weaver's versioned ``flow.json``.  The run record keeps the input bitcode
hashes, SVF identity and options, diagnostics and completion status.  SVF's
own ``-dump-json`` exporter is deliberately not used: it crashed on the
reviewed build (see ``docs/architecture.md``).
"""

from __future__ import annotations

import os
import re
import resource
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from weaver import SCHEMA_VERSION
from weaver.capture.compdb import load_compdb
from weaver.capture.toolid import identify
from weaver.config import Profile, Project
from weaver.store import Store
from weaver.toolchain.collect import MANIFEST, _run_recipe
from weaver.toolchain.sanitize import sanitize
from weaver.util import is_within, now_iso, read_json, rel_or_abs, run, sha256_file, short_hash, write_json

FLOW_SCHEMA = f"weaver.flow/{SCHEMA_VERSION}"
PRINT_OPTIONS = ["-print-all-pts", "-print-fp"]


# ---------------------------------------------------------------------------
# Locating SVF
# ---------------------------------------------------------------------------


def find_wpa(project: Project) -> dict[str, Any]:
    """Locate ``wpa`` and the environment it needs.  Returns {} when SVF is unavailable."""
    if project.flow.wpa:
        p = Path(project.flow.wpa)
        return {"wpa": str(p), "env": {}, "source": "weaver.yaml"} if p.exists() else {}
    import importlib.util

    # Locate the package without importing it: SVF (AGPL-3.0-or-later) only ever runs as a
    # separate `wpa` process, never inside Weaver.
    spec = importlib.util.find_spec("pysvf")
    if spec is not None and spec.origin:
        base = Path(spec.origin).resolve().parent
        wpa = base / "SVF" / "Release-build" / "bin" / "wpa"
        if wpa.exists():
            llvm = next(iter(sorted((base / "SVF").glob("llvm-*.obj"))), None)
            libs = [base / "SVF" / "Release-build" / "lib", base / "SVF" / "z3.obj" / "bin"]
            if llvm is not None:
                libs.append(llvm / "lib")
            try:
                from importlib.metadata import version

                ver = version("pysvf")
            except Exception:  # noqa: BLE001
                ver = "unknown"
            return {
                "wpa": str(wpa),
                "env": {
                    "LD_LIBRARY_PATH": ":".join(str(x) for x in libs),
                    "SVF_EXTAPI_DIR": str(base / "SVF" / "Release-build" / "lib"),
                },
                "source": f"pysvf {ver}",
                "llvm": llvm.name.replace(".obj", "") if llvm is not None else None,
                "lib_dirs": [str(x) for x in libs],
            }
    w = shutil.which("wpa")
    return {"wpa": w, "env": {}, "source": "PATH"} if w else {}


def _runtime(project: Project, svf: dict[str, Any]) -> dict[str, Any]:
    """Resolve missing shared-library names (e.g. a versioned SONAME) via links in the state dir."""
    env = dict(svf.get("env", {}))
    fixes: list[str] = []
    rt = Store(project.state_dir).root / "flow" / "runtime-lib"
    for _ in range(4):
        r = run([svf["wpa"], "-help"], env=env, timeout=60)
        m = re.search(r"error while loading shared libraries: (\S+?):", r.stderr_text())
        if not m:
            return {**svf, "env": env, "runtime_fixes": fixes, "help_ok": r.returncode in (0, 1)}
        missing = m.group(1)
        stem = missing.split(".so")[0] + ".so"
        cand = None
        for d in svf.get("lib_dirs", []):
            if (Path(d) / stem).exists():
                cand = Path(d) / stem
                break
        if cand is None:
            return {**svf, "env": env, "runtime_fixes": fixes, "help_ok": False, "error": r.stderr_text(500)}
        rt.mkdir(parents=True, exist_ok=True)
        link = rt / missing
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(cand)
        fixes.append(f"{missing} -> {cand}")
        env["LD_LIBRARY_PATH"] = f"{rt}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
    return {**svf, "env": env, "runtime_fixes": fixes, "help_ok": False}


def _llvm_link_for(clang_path: str, version: str) -> str | None:
    sib = Path(os.path.realpath(clang_path)).parent / "llvm-link"
    if sib.exists():
        return str(sib)
    major = version.split(".")[0] if version else ""
    for name in (f"llvm-link-{major}", "llvm-link"):
        w = shutil.which(name)
        if w:
            return w
    return None


# ---------------------------------------------------------------------------
# Running the job
# ---------------------------------------------------------------------------


def _limits(memory_mb: int):
    def apply() -> None:
        if memory_mb > 0:
            b = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (b, b))

    return apply


def run_flow(project: Project, profile: Profile, force: bool = False, log: Any = None) -> dict[str, Any]:
    """Build per-unit bitcode, then run one SVF job per program (see ``weaver.link``)."""
    from weaver.link import link_model

    store = Store(project.state_dir)
    out = store.root / "flow" / profile.id
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    rec: dict[str, Any] = {"schema": FLOW_SCHEMA, "profile": profile.id, "started_at": now_iso()}

    svf = find_wpa(project) if project.flow.svf_enabled else {}
    if not svf:
        rec.update(
            status="unavailable",
            reason="SVF not found (install 'weaver[flow]' or set flow.svf.wpa)"
            if project.flow.svf_enabled
            else "disabled in weaver.yaml",
        )
        write_json(out / "run.json", rec)
        return rec
    svf = _runtime(project, svf)
    rec["svf"] = {k: v for k, v in svf.items() if k not in ("env",)}
    rec["svf"]["wpa_sha256"] = sha256_file(svf["wpa"])
    if not svf.get("help_ok"):
        rec.update(status="unavailable", reason=f"wpa does not start: {svf.get('error', '')}")
        write_json(out / "run.json", rec)
        return rec

    # 1. frontend bitcode for every unit that belongs to a program -----------------
    lm = link_model(project, profile)
    cmds = {c.unit_id(profile.id): c for c in load_compdb(profile.compile_commands)}
    if lm.manifest is None or not lm.programs:
        from weaver.link import Program as LinkProgram

        progs = [LinkProgram("all", [], ["main"], closed=True, configured=False, units=list(cmds))]
        rec["program_note"] = "no link manifest: all units analysed as one program"
    else:
        progs = lm.programs
    wanted = {u for p in progs for u in p.units}
    bitcode: dict[str, dict[str, Any]] = {}
    for uid, c in cmds.items():
        if uid in wanted:
            bitcode[uid] = _unit_bitcode(project, profile, store, c, force)

    # 2. one job per program ------------------------------------------------------
    rec["programs"] = {}
    for prog in progs:
        pdir = out / "programs" / _safe(prog.name)
        pr = _run_program(project, profile, svf, prog, [bitcode[u] for u in prog.units if u in bitcode], pdir)
        rec["programs"][prog.name] = {
            k: pr.get(k)
            for k in ("status", "reason", "closed", "configured", "images", "units", "evidence_status", "duration_s")
        }
        if log:
            log(f"[{profile.id}] flow {prog.name}: {pr['status']}" + (f" ({pr['reason']})" if pr.get("reason") else ""))
    statuses = [p["status"] for p in rec["programs"].values()]
    rec["status"] = (
        "complete"
        if statuses and all(st == "complete" for st in statuses)
        else "failed"
        if statuses and all(st == "failed" for st in statuses)
        else "incomplete"
    )
    bad = [n for n, p in rec["programs"].items() if p["status"] != "complete"]
    if bad:
        rec["reason"] = f"{len(bad)} of {len(statuses)} program(s) not complete: {', '.join(bad[:8])}"
    rec["duration_s"] = round(time.time() - started, 2)
    write_json(out / "run.json", rec)
    return rec


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", name)


def _unit_bitcode(project: Project, profile: Profile, store: Store, c: Any, force: bool) -> dict[str, Any]:
    udir = store.unit_dir(profile.id, c.unit_id(profile.id))
    mpath = udir / MANIFEST
    rel = rel_or_abs(c.file, project.root)
    if not mpath.exists():
        return {"missing": True, "unit": c.unit_id(profile.id), "file": rel, "reason": "not collected"}
    m = read_json(mpath)
    san = sanitize(c)
    if m["production_tool"]["family"] == "clang":
        cc, opts, tool, status = c.compiler, san.options, identify(c.compiler, c.directory), "native"
    elif m.get("secondary_tool") and (m.get("translation") or {}).get("options"):
        cc = m["secondary_tool"]["path"]
        opts, tool, status = m["translation"]["options"], identify(cc), m.get("ast_evidence_status")
    else:
        return {"missing": True, "unit": m["unit_id"], "file": m["file_rel"], "reason": "no Clang frontend"}
    key = short_hash(m["cache_key"], "flow_bitcode", 1)
    art = m.get("flow_bitcode")
    bc = udir / "flow.flow.bc"
    if force or not art or art.get("key") != key or not bc.exists():
        res = _run_recipe("flow_bitcode", cc, opts, san, udir, "flow", "flow", tool, _status(status))
        art = {
            "key": key,
            "status": res["status"],
            "returncode": res["returncode"],
            "argv": res["argv"],
            "tool": res["tool"],
        }
        m["flow_bitcode"] = art
        write_json(mpath, m)
    if art["status"] != "ok":
        return {
            "missing": True,
            "unit": m["unit_id"],
            "file": m["file_rel"],
            "reason": f"bitcode failed (exit {art['returncode']})",
        }
    return {
        "unit": m["unit_id"],
        "file": m["file_rel"],
        "file_sha256": m["file_sha256"],
        "bitcode": str(bc),
        "sha256": sha256_file(bc),
        "evidence_status": status,
        "directory": m["directory"],
        "producer": [tool.realpath or tool.requested, tool.version],
    }


def _run_program(
    project: Project, profile: Profile, svf: dict[str, Any], prog: Any, units: list[dict[str, Any]], out: Path
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    rec: dict[str, Any] = {
        "schema": FLOW_SCHEMA,
        "profile": profile.id,
        "program": prog.name,
        "images": prog.images,
        "entry_points": prog.entry_points,
        "closed": prog.closed,
        "configured": prog.configured,
        "started_at": now_iso(),
        "svf": {k: v for k, v in svf.items() if k not in ("env",)},
    }
    rec["svf"]["wpa_sha256"] = sha256_file(svf["wpa"])
    inputs = [u for u in units if not u.get("missing")]
    missing = [{k: u[k] for k in ("unit", "file", "reason")} for u in units if u.get("missing")]
    rec["inputs"] = inputs
    rec["units"] = len(units)

    def done(status: str, reason: str | None = None) -> dict[str, Any]:
        rec["status"] = status
        if reason:
            rec["reason"] = reason
        rec["missing_units"] = missing
        rec["evidence_status"] = _weakest([i["evidence_status"] for i in inputs]) if inputs else "unsupported"
        rec["duration_s"] = round(time.time() - started, 2)
        write_json(out / "run.json", rec)
        return rec

    if not inputs:
        return done("failed", "no bitcode could be produced")
    files = [i["file"] for i in inputs]
    dup = sorted({f for f in files if files.count(f) > 1})
    if dup:
        return done("failed", f"the program compiles {dup[:3]} more than once; analyse its images separately")
    producers = {tuple(i["producer"]) for i in inputs}
    if len(producers) != 1:
        return done("failed", f"units were produced by different Clang builds: {sorted(producers)}")
    ((clang_path, clang_ver),) = producers
    link = _llvm_link_for(clang_path, clang_ver)
    if link is None:
        return done("failed", f"no llvm-link matching Clang {clang_ver}")
    program = out / "program.bc"
    lr = run([link, "-o", str(program), *[i["bitcode"] for i in inputs]], timeout=1800)
    rec["link"] = {"tool": link, "returncode": lr.returncode, "stderr": lr.stderr_text(4000)}
    if not lr.ok:
        return done(
            "failed",
            "llvm-link failed: " + lr.stderr_text(300).strip().splitlines()[0]
            if lr.stderr_text()
            else "llvm-link failed",
        )
    if re.search(r"different (target triples|data ?layouts)", lr.stderr_text(), re.I):
        return done("failed", "modules have different targets or data layouts; not linked as one program")
    rec["program_bc"] = {"path": str(program), "sha256": sha256_file(program)}

    argv = [svf["wpa"], *project.flow.options, *PRINT_OPTIONS, str(program)]
    stdout_path = out / "wpa.out"
    t0 = time.time()
    try:
        with open(stdout_path, "wb") as so, open(out / "wpa.err", "wb") as se:
            p = subprocess.run(
                argv,
                stdout=so,
                stderr=se,
                env={**os.environ, **svf["env"]},
                timeout=project.flow.timeout,
                preexec_fn=_limits(project.flow.memory_mb),
                cwd=out,
            )
        rc, timed_out = p.returncode, False
    except subprocess.TimeoutExpired:
        rc, timed_out = -1, True
    rec["job"] = {
        "argv": argv,
        "returncode": rc,
        "timed_out": timed_out,
        "duration_s": round(time.time() - t0, 2),
        "memory_mb": project.flow.memory_mb,
        "timeout_s": project.flow.timeout,
        "stderr_tail": (out / "wpa.err").read_text(errors="replace")[-4000:],
    }
    if rc != 0 or timed_out:
        return done("incomplete", "SVF did not complete" + (" (timeout)" if timed_out else f" (exit {rc})"))

    from weaver.flow.parse import parse_wpa

    dirs = sorted({i["directory"] for i in inputs})
    known: set[str] = set()
    store = Store(project.state_dir)
    for i in inputs:
        known.add(i["file"])
        mpath = store.unit_dir(profile.id, i["unit"]) / MANIFEST
        if mpath.exists():
            for d in read_json(mpath).get("dependencies", []):
                path = d.get("path") if isinstance(d, dict) else d
                if path and is_within(path, project.root):
                    known.add(rel_or_abs(path, project.root))
    flow = parse_wpa(stdout_path.read_text(errors="replace"), project.root, dirs, known)
    flow.update(schema=FLOW_SCHEMA, profile=profile.id, program=prog.name, generated_at=now_iso())
    diag = flow.pop("diagnostics")
    rec["diagnostics"] = diag
    reasons = []
    if missing:
        reasons.append(f"{len(missing)} unit(s) missing from the analysed program")
    if diag["parse_errors"]:
        reasons.append(f"{diag['parse_errors']} unparsed output line(s)")
    if diag["time_limit_hit"]:
        reasons.append("an analysis time limit was reached")
    if not prog.closed:
        reasons.append("open program: code outside it may call its exported functions")
    status = (
        "complete"
        if not reasons or reasons == ["open program: code outside it may call its exported functions"]
        else "incomplete"
    )
    res = done(status, "; ".join(reasons) if reasons else None)
    flow["run"] = {k: res[k] for k in ("status", "evidence_status", "inputs", "missing_units", "closed") if k in res}
    write_json(out / "flow.json", flow)
    return res


def _status(s: str | None):
    from weaver.evidence import EvidenceStatus

    try:
        return EvidenceStatus(s or "unsupported")
    except ValueError:
        return EvidenceStatus.UNSUPPORTED


def _weakest(statuses: list[str | None]) -> str:
    from weaver.evidence import EVIDENCE_RANK, EvidenceStatus

    vals = [_status(s) for s in statuses]
    return min(vals, key=lambda s: EVIDENCE_RANK[s]).value if vals else EvidenceStatus.UNSUPPORTED.value


def resolve_source_file(
    name: str, root: Path, dirs: list[str], cache: dict[str, str | None], known: set[str] | None = None
) -> str | None:
    """Map a debug-info file name to a project-relative path (None when outside or ambiguous).

    Debug info records names as the compiler saw them, often relative to a
    directory that is not recorded in SVF's output.  Relative names are tried
    against the compile directories and the project root, then matched by
    path suffix against the program's own sources and headers (``known``).
    """
    if name in cache:
        return cache[name]
    cands = [name] if os.path.isabs(name) else [os.path.join(d, name) for d in [*dirs, str(root)]]
    found = {os.path.realpath(c) for c in cands if os.path.exists(c)}
    out = None
    if len(found) == 1:
        f = found.pop()
        out = rel_or_abs(f, root) if is_within(f, root) else f
    elif not found and known and not os.path.isabs(name):
        norm = os.path.normpath(name).lstrip("./")
        hits = [k for k in known if k == norm or k.endswith("/" + norm)]
        if len(hits) == 1:
            out = hits[0]
    cache[name] = out
    return out
