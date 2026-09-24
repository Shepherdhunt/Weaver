"""A ratchet for CI: pointers, high-risk pointers and profile violations may only go down.

The baseline is a small JSON file committed next to ``weaver.yaml``
(``weaver-ratchet.json`` unless ``ratchet.baseline`` says otherwise).  For every
file it records how many pointers Weaver finds there, how many of them are
high-risk, and how many sites violate each rule of a simplification profile.
``weaver ratchet`` recomputes the same numbers from the current inventory and
fails when any of them went up in a file, naming the new pointers.  With
``--base REV`` only the files changed since that revision are compared, which is
what a merge-request job wants.

Counts per file, not pointer IDs, decide: renaming a pointer or moving a
function changes IDs but not the work left.  IDs are kept only to name what is
new.  Numbers that went down are reported as progress; ``--update`` writes them
into the baseline (and ``--strict`` fails until it is updated, so the committed
file stays tight).  ``--update`` also records a deliberate increase: the
change to the committed file is then visible in review.

Risk levels depend on the evidence (points-to results raise or lower factors),
so the baseline records whether points-to evidence was present; if that differs,
the high-risk comparison is skipped with a warning instead of failing falsely.
Run the same ``weaver refresh`` (with or without points-to) for the baseline and
in CI.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from weaver import __version__
from weaver.config import Project
from weaver.errors import WeaverError
from weaver.util import now_iso, read_json, run, sha256_file, write_json

SCHEMA = "weaver.ratchet/1"
DIMENSIONS = ("pointers", "high", "violations")


def settings(project: Project) -> dict[str, Any]:
    raw = project.raw.get("ratchet") or {}
    if not isinstance(raw, dict):
        raise WeaverError("ratchet: must be a mapping (baseline, profile, enforce)")
    enforce = [str(x) for x in raw.get("enforce") or DIMENSIONS]
    if bad := [x for x in enforce if x not in DIMENSIONS]:
        raise WeaverError(f"ratchet.enforce: unknown {bad}; use {', '.join(DIMENSIONS)}")
    return {
        "baseline": (project.config_path.parent / str(raw.get("baseline") or "weaver-ratchet.json")).resolve(),
        "profile": raw.get("profile"),
        "enforce": enforce,
    }


def measure(project: Project, inv: dict[str, Any], profile: str | None = None) -> dict[str, Any]:
    """The ratchet's numbers for the current inventory."""
    from weaver.risk import report as risk_report
    from weaver.simplify import check as simplify_check
    from weaver.simplify import default_profile

    stale = [f for f, h in inv["files"].items() if (project.root / f).exists() and sha256_file(project.root / f) != h]
    if stale:
        raise WeaverError(f"the inventory is stale for {len(stale)} file(s) (e.g. {stale[0]}); run 'weaver refresh'")
    rep = risk_report(project, inv)
    files: dict[str, dict[str, Any]] = {}

    def entry(rel: str) -> dict[str, Any]:
        return files.setdefault(rel, {"pointers": 0, "high": 0, "violations": {}, "ids": [], "high_ids": []})

    names: dict[str, dict[str, Any]] = {}
    no_flow = 0
    for r in rep["pointers"]:
        e = entry(r["file"] or "?")
        e["pointers"] += 1
        e["ids"].append(r["id"])
        if r["level"] == "high":
            e["high"] += 1
            e["high_ids"].append(r["id"])
        names[r["id"]] = {k: r.get(k) for k in ("name", "kind", "function", "line", "level", "score")}
        no_flow += any(x["id"] == "no-flow-evidence" for x in r["factors"])
    pid = profile or default_profile(project)
    sim = simplify_check(project, inv, pid)
    for row in [*sim["functions"], *sim["file_scope"]]:
        counts = Counter(v["rule"] for v in row["violations"] if v["rule"] != "pointer")  # pointers counted above
        if counts:
            e = entry(row["file"])
            for rule, n in counts.items():
                e["violations"][rule] = e["violations"].get(rule, 0) + n
    for e in files.values():
        e["ids"].sort()
        e["high_ids"].sort()
    return {
        "schema": SCHEMA,
        "weaver": __version__,
        "profile": pid,
        "flow_evidence": bool(rep["pointers"]) and no_flow < len(rep["pointers"]),
        "totals": {
            "pointers": sum(e["pointers"] for e in files.values()),
            "high": sum(e["high"] for e in files.values()),
            "violations": sum(sum(e["violations"].values()) for e in files.values()),
        },
        "files": dict(sorted(files.items())),
        "names": names,
    }


def write_baseline(project: Project, current: dict[str, Any], path: Path) -> dict[str, Any]:
    from weaver.toolchain.collect import _source_revision

    base = {k: v for k, v in current.items() if k != "names"}
    base["created_at"] = now_iso()
    base["source_revision"] = _source_revision(project.root)
    write_json(path, base)
    return base


def changed_files(project: Project, rev: str) -> set[str]:
    """Files changed since ``rev`` (committed on this branch, or in the working tree), project-relative."""
    root = project.root.resolve()
    top = run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], timeout=30)
    if not top.ok:
        raise WeaverError("--base needs the project to be in a git repository")
    top_dir = Path(top.stdout.decode().strip())
    out: set[str] = set()
    for argv in (["diff", "--name-only", f"{rev}...HEAD"], ["diff", "--name-only", "HEAD"]):
        r = run(["git", "-C", str(root), *argv], timeout=120)
        if not r.ok:
            raise WeaverError(f"git {' '.join(argv)} failed: {r.stderr_text(300)}")
        for line in r.stdout.decode().splitlines():
            p = (top_dir / line.strip()).resolve()
            try:
                out.add(str(p.relative_to(root)))
            except ValueError:
                continue  # outside the project
    return out


def compare(
    base: dict[str, Any], cur: dict[str, Any], enforce: list[str], only: set[str] | None = None
) -> dict[str, Any]:
    """Failures (numbers that went up), progress (numbers that went down) and warnings."""
    warnings: list[str] = []
    if base.get("profile") != cur.get("profile"):
        warnings.append(
            f"the baseline counted violations of profile {base.get('profile')!r}, this run {cur.get('profile')!r}; "
            "violations are not compared (run 'weaver ratchet --update' after changing the profile)"
        )
        enforce = [d for d in enforce if d != "violations"]
    if bool(base.get("flow_evidence")) != bool(cur.get("flow_evidence")):
        warnings.append(
            "risk levels were computed with different evidence (points-to results present in one run and not the "
            "other); high-risk pointers are not compared. Run the same 'weaver refresh' for the baseline and in CI"
        )
        enforce = [d for d in enforce if d != "high"]
    if base.get("weaver") != cur.get("weaver"):
        warnings.append(
            f"the baseline was written by Weaver {base.get('weaver')}, this is {cur.get('weaver')}; if counts moved "
            "for files nobody changed, refresh the baseline with 'weaver ratchet --update'"
        )
    names = cur.get("names", {})
    empty = {"pointers": 0, "high": 0, "violations": {}, "ids": [], "high_ids": []}
    keys = sorted(set(base["files"]) | set(cur["files"]))
    if only is not None:
        keys = [k for k in keys if k in only]
    failures: list[dict[str, Any]] = []
    progress: list[dict[str, Any]] = []

    def named(ids: list[str]) -> list[dict[str, Any]]:
        return [{"id": i, **names.get(i, {})} for i in ids]

    for rel in keys:
        b, c = base["files"].get(rel, empty), cur["files"].get(rel, empty)
        for dim in ("pointers", "high"):
            if c[dim] == b[dim]:
                continue
            ids = "ids" if dim == "pointers" else "high_ids"
            item = {"file": rel, "what": dim, "before": b[dim], "after": c[dim]}
            if c[dim] > b[dim] and dim in enforce:
                failures.append({**item, "new": named(sorted(set(c[ids]) - set(b[ids])))})
            elif c[dim] < b[dim]:
                progress.append(item)
        rules = sorted(set(b["violations"]) | set(c["violations"]))
        for rule in rules:
            nb, nc = b["violations"].get(rule, 0), c["violations"].get(rule, 0)
            item = {"file": rel, "what": f"violations:{rule}", "before": nb, "after": nc}
            if nc > nb and "violations" in enforce:
                failures.append(item)
            elif nc < nb:
                progress.append(item)
    return {
        "ok": not failures,
        "failures": failures,
        "progress": progress,
        "warnings": warnings,
        "files_compared": len(keys),
        "scope": "changed files" if only is not None else "all files",
        "enforce": enforce,
        "totals": {"before": base.get("totals"), "after": cur.get("totals")},
    }


LABEL = {"pointers": "pointer(s)", "high": "high-risk pointer(s)"}


def _what(item: dict[str, Any]) -> str:
    w = item["what"]
    return f"'{w.split(':', 1)[1]}' violation(s)" if w.startswith("violations:") else LABEL[w]


def render_text(res: dict[str, Any], baseline: Path) -> str:
    out = []
    for w in res["warnings"]:
        out.append(f"warning: {w}")
    for f in res["failures"]:
        out.append(f"FAIL {f['file']}: {_what(f)} {f['before']} -> {f['after']}")
        for n in f.get("new", [])[:10]:
            where = f"{n.get('function')}() " if n.get("function") else ""
            out.append(
                f"       new: {n.get('kind')} '{n.get('name')}' {where}line {n.get('line')} ({n.get('level')} risk)"
            )
    for p in res["progress"]:
        out.append(f"down {p['file']}: {_what(p)} {p['before']} -> {p['after']}")
    t = res["totals"]
    out.append(
        f"ratchet: {'FAILED' if res['failures'] else 'ok'} — {res['files_compared']} file(s) compared "
        f"({res['scope']}); totals {t['before']} -> {t['after']}"
    )
    if res["failures"]:
        out.append(
            "Remove the new pointers or violations, or, if the increase is intended, run 'weaver ratchet --update' "
            f"and commit {baseline.name} so the increase is reviewed."
        )
    elif res["progress"]:
        out.append(f"Progress: run 'weaver ratchet --update' and commit {baseline.name} to lock it in.")
    return "\n".join(out)


def render_github(res: dict[str, Any], baseline: Path) -> str:
    """GitHub Actions workflow commands: an error annotation per new pointer or failing file."""

    def esc(s: str) -> str:
        return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    out = [f"::warning::{esc(w)}" for w in res["warnings"]]
    for f in res["failures"]:
        news = f.get("new") or []
        if news:
            for n in news:
                where = f"{n.get('function')}() " if n.get("function") else ""
                count = f"{_what(f)} {f['before']} -> {f['after']}"
                msg = f"new {n.get('kind')} pointer '{n.get('name')}' in {where}({count})"
                out.append(f"::error file={f['file']},line={n.get('line') or 1}::{esc(msg)}")
        else:
            msg = f"{_what(f)} went up: {f['before']} -> {f['after']}"
            out.append(f"::error file={f['file']}::{esc(msg)}")
    out.append(render_text(res, baseline))
    return "\n".join(out)


def ratchet(
    project: Project,
    update: bool = False,
    base_rev: str | None = None,
    profile: str | None = None,
    strict: bool = False,
    log: Any = None,
) -> tuple[dict[str, Any], Path]:
    """Compare with (or, with ``update``, write) the baseline.  Returns (result, baseline path)."""
    from weaver.analysis.inventory import load_inventory

    say = log or (lambda _m: None)
    cfg = settings(project)
    path: Path = cfg["baseline"]
    inv = load_inventory(project)
    base = read_json(path) if path.exists() else None
    cur = measure(project, inv, profile or cfg["profile"] or (base or {}).get("profile"))
    if update or base is None:
        if base is None and not update:
            raise WeaverError(f"no ratchet baseline at {path}; create it with 'weaver ratchet --update' and commit it")
        write_baseline(project, cur, path)
        say(f"wrote {path}: {cur['totals']}")
        res = compare(base, cur, cfg["enforce"]) if base else None
        return {"ok": True, "updated": str(path), "totals": cur["totals"], "previous": res}, path
    if base.get("schema") != SCHEMA:
        raise WeaverError(f"{path} is not a ratchet baseline ({SCHEMA})")
    only = changed_files(project, base_rev) if base_rev else None
    res = compare(base, cur, cfg["enforce"], only)
    if strict and res["ok"] and res["progress"]:
        res["ok"] = False
        res["warnings"].append(
            f"--strict: the baseline is not tight; run 'weaver ratchet --update' and commit {path.name}"
        )
    return res, path
