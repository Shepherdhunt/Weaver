"""The analysis pipeline as one callable: capture → collect → fidelity → inventory → flow.

Used by ``weaver refresh``, ``weaver auto`` and the web interface, so every
entry point re-analyses the same way after sources change.  Each stage reports
through ``log`` and returns a summary; a failed stage stops the pipeline.
"""

from __future__ import annotations

from typing import Any, Callable

from weaver.config import Project

Log = Callable[[str], None]


def _print(msg: str) -> None:
    print(msg, flush=True)


def build_capture(project: Project, log: Log = _print) -> dict[str, Any]:
    """Run each profile's configured capture build through shims and finalize its compile database."""
    import os
    import subprocess

    from weaver.capture.shims import finalize, make_shim
    from weaver.errors import WeaverError
    from weaver.store import Store

    out: dict[str, Any] = {}
    for prof in project.profiles:
        cap = prof.capture
        if cap is None:
            log(f"[{prof.id}] no capture build configured; using {prof.compile_commands}")
            continue
        cdir = Store(project.state_dir).capture_dir() / prof.id
        log_path = cdir / "log.jsonl"
        if log_path.exists():
            log_path.unlink()
        subst: dict[str, str] = {"root": str(project.root)}
        for name, real in cap.tools.items():
            subst[name] = str(make_shim(cdir / "shims", name, real, log_path))
        if cap.clean:
            argv, cwd, env = cap.clean.render(**subst)
            log(f"[{prof.id}] clean: {' '.join(argv)}")
            subprocess.run(argv, cwd=cwd, env={**os.environ, **env}, check=False)
        argv, cwd, env = cap.command.render(**subst)
        log(f"[{prof.id}] build: {' '.join(argv)}")
        p = subprocess.run(
            argv,
            cwd=cwd,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=cap.command.timeout,
        )
        for line in (p.stdout + p.stderr).splitlines()[-40:]:
            log(f"    {line}")
        if p.returncode != 0:
            raise WeaverError(f"profile {prof.id}: capture build failed (exit {p.returncode})")
        res = finalize(log_path, prof.compile_commands.parent, cap.exclude, str(project.root))
        extra = f"; {res['excluded_invocations']} build-system probe(s) or excluded source(s) set aside"
        log(
            f"[{prof.id}] captured {res['compile_entries']} compile command(s), {res['links']} link(s)"
            + (extra if res["excluded_invocations"] else "")
        )
        out[prof.id] = res
    return out


def refresh(
    project: Project,
    log: Log = _print,
    jobs: int | None = None,
    flow: bool | None = None,
    fidelity: bool = True,
    capture: bool = False,
) -> dict[str, Any]:
    from weaver.analysis.inventory import build_inventory
    from weaver.fidelity import run_fidelity
    from weaver.flow.svf import find_wpa, run_flow
    from weaver.toolchain.collect import collect_profile

    summary: dict[str, Any] = {}
    if capture:
        summary["capture"] = build_capture(project, log)
    for prof in project.profiles:
        res = collect_profile(project, prof, jobs=jobs)
        log(f"[{prof.id}] collect: {res['collected']} collected, {res['cached']} cached, {len(res['failed'])} failed")
        for f in res["failed"]:
            log(f"    failed: {f['file']}: {f['detail']}")
        summary.setdefault("collect", {})[prof.id] = res
        if fidelity and prof.secondary_frontend is not None:
            fr = run_fidelity(project, prof, jobs=jobs)
            counts: dict[str, int] = {}
            for u in fr.get("units", []):
                counts[u["evidence_status"]] = counts.get(u["evidence_status"], 0) + 1
            log(f"[{prof.id}] fidelity: {counts or 'no secondary units'}")
            summary.setdefault("fidelity", {})[prof.id] = counts
    inv = build_inventory(project, jobs=jobs)
    s = inv["summary"]
    log(f"inventory: {s['findings']} finding(s) in {s['units']} unit(s); {s['unexamined_lines']} unexamined line(s)")
    summary["inventory"] = s
    run_it = flow if flow is not None else bool(project.flow.svf_enabled and find_wpa(project))
    if run_it:
        for prof in project.profiles:
            fr = run_flow(project, prof, log=log)
            log(f"[{prof.id}] flow: {fr['status']}" + (f" ({fr['reason']})" if fr.get("reason") else ""))
            summary.setdefault("flow", {})[prof.id] = fr["status"]
    else:
        log(
            "flow: SVF skipped ("
            + ("not selected in flow.backend" if not project.flow.uses("svf") else "unavailable or disabled")
            + ")"
        )
    if flow is not False and project.flow.uses("gcc"):
        from weaver.flow.gcc_pta import gcc_profile, run_gcc_pta

        for prof in project.profiles:
            if not gcc_profile(project, prof):
                continue
            gr = run_gcc_pta(project, prof, jobs=jobs)
            log(f"[{prof.id}] gcc points-to: {gr['status']}" + (f" ({gr['reason']})" if gr.get("reason") else ""))
            summary.setdefault("gcc_pta", {})[prof.id] = gr["status"]
    return summary
