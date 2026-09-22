"""Run collection recipes for every unit of a profile.

Outputs go to ``<state>/evidence/<profile>/<unit>/``.  Each unit directory has
a ``manifest.json`` binding every artifact to the source hash, the exact
command, the producing tool identity and the recipe's recorded deviations.
Evidence is cached by source/dependency hashes, per-file command, tool identity
and profile, and invalidated when any of them changes (compiler plan §10).
"""

from __future__ import annotations

import gzip
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from weaver import SCHEMA_VERSION
from weaver.capture.compdb import CompileCommand, load_compdb, quote_gnu_response
from weaver.capture.toolid import ToolIdentity, identify
from weaver.config import Profile, Project
from weaver.errors import WeaverError
from weaver.evidence import EvidenceStatus
from weaver.store import Store
from weaver.toolchain.recipes import RECIPES, SECONDARY_DEFAULT, recipes_for
from weaver.toolchain.sanitize import SanitizedCommand, sanitize
from weaver.toolchain.translate import TRANSLATION_VERSION, translate_gcc_to_clang
from weaver.util import now_iso, read_json, rel_or_abs, run, sha256_bytes, sha256_file, short_hash, write_json

MANIFEST = "manifest.json"


def parse_depfile(text: str, directory: str) -> list[str]:
    """Parse a make-style dependency file into absolute paths (targets dropped)."""
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    deps: list[str] = []
    for rule in text.splitlines():
        if ":" not in rule:
            continue
        # Split on the first unescaped ': ' that separates targets from prerequisites.
        idx = rule.find(": ")
        if idx < 0:
            idx = rule.rfind(":")
        body = rule[idx + 1 :]
        cur: list[str] = []
        i = 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body) and body[i + 1] in " #":
                cur.append(body[i + 1])
                i += 2
                continue
            if c == "$" and i + 1 < len(body) and body[i + 1] == "$":
                cur.append("$")
                i += 2
                continue
            if c.isspace():
                if cur:
                    deps.append("".join(cur))
                    cur = []
            else:
                cur.append(c)
            i += 1
        if cur:
            deps.append("".join(cur))
    out = []
    for d in deps:
        p = d if os.path.isabs(d) else os.path.join(directory, d)
        out.append(os.path.normpath(p))
    return list(dict.fromkeys(out))


def read_macros(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("#define "):
            rest = line[len("#define ") :]
            # Function-like macro names end at '(' with no space.
            j = 0
            while j < len(rest) and (rest[j].isalnum() or rest[j] == "_"):
                j += 1
            name = rest[:j]
            if j < len(rest) and rest[j] == "(":
                k = rest.find(")", j)
                name_sig = rest[: k + 1]
                out[name] = "(fn)" + name_sig[j:] + " " + rest[k + 1 :].strip()
            else:
                out[name] = rest[j:].strip()
    return out


@dataclass
class UnitResult:
    unit_id: str
    file: str
    status: str  # collected | cached | failed
    detail: str = ""


def _run_recipe(
    name: str,
    compiler: str,
    options: list[str],
    san: SanitizedCommand,
    out_dir: Path,
    stem: str,
    producer: str,
    tool: ToolIdentity,
    evidence_status: EvidenceStatus,
) -> dict[str, Any]:
    recipe = RECIPES[name]
    inv = recipe.build(compiler, options, san.source, out_dir, stem)
    before = set(os.listdir(out_dir))
    res = run(inv.argv, cwd=san.directory, stdout_path=inv.stdout_to, timeout=1800)
    stderr_path = inv.stderr_to or out_dir / f"{stem}.{name}.stderr.txt"
    stderr_path.write_bytes(res.stderr)
    outputs = list(inv.outputs)
    for pattern in inv.collect_globs:
        outputs.extend(sorted(out_dir.glob(pattern)))
    if not inv.outputs and not inv.collect_globs:
        # Recipes whose evidence is their stderr (e.g. gcc -fdump-passes).
        outputs.append(stderr_path)
    files = []
    for p in outputs:
        if not p.exists():
            continue
        entry: dict[str, Any] = {"path": p.name, "sha256": sha256_file(p), "size": p.stat().st_size}
        if p.name.endswith(".ast.json") and p.stat().st_size > 0:
            gz = p.with_name(p.name + ".gz")
            with open(p, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
            p.unlink()
            entry["path"] = gz.name
            entry["stored"] = "gzip"
        files.append(entry)
    new_files = sorted(set(os.listdir(out_dir)) - before - {f["path"] for f in files} - {stderr_path.name})
    ok = res.ok and all((out_dir / f["path"]).exists() for f in files) and bool(files)
    return {
        "recipe": name,
        "producer": producer,  # production | secondary
        "tool": tool.ref().to_json(),
        "evidence_status": evidence_status.value,
        "argv": inv.argv,
        "cwd": san.directory,
        "returncode": res.returncode,
        "status": "ok" if ok else "failed",
        "files": files,
        "incidental_files": new_files,
        "stderr": stderr_path.name,
        "deviations": recipe.deviations,
        "frontend_interface": recipe.frontend_interface,
    }


def collect_unit(
    project: Project,
    profile: Profile,
    cmd: CompileCommand,
    recipes: list[str] | str,
    force: bool = False,
) -> UnitResult:
    store = Store(project.state_dir)
    unit_id = cmd.unit_id(profile.id)
    out_dir = store.unit_dir(profile.id, unit_id)
    man_path = out_dir / MANIFEST
    if not os.path.exists(cmd.file):
        return UnitResult(unit_id, cmd.file, "failed", "source file missing (generated file not built?)")

    tool = identify(cmd.compiler, cmd.directory)
    if tool.path is None:
        return UnitResult(unit_id, cmd.file, "failed", f"compiler {cmd.compiler!r} not found")
    san = sanitize(cmd)
    family = tool.family
    selected = recipes_for(family, recipes if recipes else profile.recipes) if family != "unknown" else []
    sec = profile.secondary_frontend if family != "clang" else None
    sec_tool = identify(sec.compiler) if sec else None
    file_hash = sha256_file(cmd.file)
    rsp_hashes = [rf.sha256 for rf in cmd.response_files]
    cache_key = short_hash(
        SCHEMA_VERSION,
        profile.id,
        file_hash,
        cmd.expanded,
        rsp_hashes,
        tool.sha256,
        selected,
        sec_tool.sha256 if sec_tool else None,
        sec.extra_args if sec else None,
        TRANSLATION_VERSION if sec else None,
        length=24,
    )
    if not force and man_path.exists():
        try:
            old = read_json(man_path)
            if old.get("cache_key") == cache_key and all(
                os.path.exists(d["path"]) and sha256_file(d["path"]) == d["sha256"] for d in old.get("dependencies", [])
            ):
                return UnitResult(unit_id, cmd.file, "cached")
        except (OSError, ValueError):
            pass
        # Stale: clear old artifacts so nothing from another configuration survives.
        for p in out_dir.iterdir():
            if p.is_file():
                p.unlink()

    (out_dir / f"unit.{family}.rsp").write_text(quote_gnu_response(san.options))
    artifacts: dict[str, Any] = {}
    for name in selected:
        # Artifacts from the production compiler itself are native evidence.
        artifacts[name] = _run_recipe(
            name, cmd.compiler, san.options, san, out_dir, "unit", "production", tool, EvidenceStatus.NATIVE
        )

    translation = None
    if sec and sec_tool and sec_tool.path:
        prod_macros = read_macros(out_dir / "unit.macros.txt")
        if not prod_macros:
            m = run([cmd.compiler, *san.options, "-E", "-dM", cmd.file], cwd=cmd.directory, timeout=600)
            (out_dir / "unit.macros.txt").write_bytes(m.stdout)
            prod_macros = read_macros(out_dir / "unit.macros.txt")
        translation = translate_gcc_to_clang(san.options, tool, prod_macros, sec_tool.path, cmd.directory)
        sec_opts = translation.options + list(sec.extra_args)
        (out_dir / "secondary.clang.rsp").write_text(quote_gnu_response(sec_opts))
        for name in SECONDARY_DEFAULT:
            artifacts["secondary." + name] = _run_recipe(
                name,
                sec_tool.path,
                sec_opts,
                san,
                out_dir,
                "secondary",
                "secondary",
                sec_tool,
                EvidenceStatus.SECONDARY_UNCHECKED,
            )
    elif sec:
        translation = {"error": f"secondary frontend {sec.compiler!r} not found"}

    deps: list[dict[str, str]] = []
    dep_art = artifacts.get("deps") or artifacts.get("secondary.deps")
    if dep_art and dep_art["status"] == "ok":
        dfile = out_dir / dep_art["files"][0]["path"]
        for d in parse_depfile(dfile.read_text(errors="replace"), cmd.directory):
            if os.path.exists(d) and os.path.normpath(d) != os.path.normpath(cmd.file):
                deps.append({"path": d, "sha256": sha256_file(d)})

    ast_key = (
        "ast_json" if "ast_json" in artifacts else ("secondary.ast_json" if "secondary.ast_json" in artifacts else None)
    )
    if ast_key and artifacts[ast_key]["status"] != "ok":
        # A failed parse can still leave a partial dump; never treat it as complete.
        ast_key = None
    evidence = (
        EvidenceStatus.NATIVE.value
        if ast_key == "ast_json"
        else EvidenceStatus.SECONDARY_UNCHECKED.value
        if ast_key
        else EvidenceStatus.UNSUPPORTED.value
    )
    manifest = {
        "schema": f"weaver.unit/{SCHEMA_VERSION}",
        "collected_at": now_iso(),
        "unit_id": unit_id,
        "profile": profile.id,
        "file": cmd.file,
        "file_rel": rel_or_abs(cmd.file, project.root),
        "file_sha256": file_hash,
        "directory": cmd.directory,
        "command": cmd.to_json(),
        "production_tool": tool.to_json(),
        "sanitized": san.to_json(),
        "secondary_tool": sec_tool.to_json() if sec_tool else None,
        "translation": translation.__dict__ if translation is not None and hasattr(translation, "log") else translation,
        "artifacts": artifacts,
        "ast_artifact": ast_key,
        "ast_evidence_status": evidence,
        "dependencies": deps,
        "cache_key": cache_key,
    }
    write_json(man_path, manifest)
    failed = [k for k, v in artifacts.items() if v["status"] != "ok"]
    return UnitResult(
        unit_id,
        cmd.file,
        "failed" if failed and not ast_key else "collected",
        f"failed recipes: {failed}" if failed else "",
    )


def collect_profile(
    project: Project,
    profile: Profile,
    recipes: list[str] | str | None = None,
    files: list[str] | None = None,
    force: bool = False,
    jobs: int | None = None,
) -> dict[str, Any]:
    cmds = load_compdb(profile.compile_commands)
    if files:
        wanted = {os.path.normpath(os.path.abspath(f)) for f in files}
        cmds = [c for c in cmds if c.file in wanted]
    jobs = jobs or min(8, os.cpu_count() or 2)
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(lambda c: collect_unit(project, profile, c, recipes, force), cmds))
    write_profile_manifest(project, profile, cmds)
    return {
        "profile": profile.id,
        "units": len(results),
        "collected": sum(r.status == "collected" for r in results),
        "cached": sum(r.status == "cached" for r in results),
        "failed": [{"file": r.file, "unit": r.unit_id, "detail": r.detail} for r in results if r.status == "failed"],
        "partial": [
            {"file": r.file, "unit": r.unit_id, "detail": r.detail}
            for r in results
            if r.status == "collected" and r.detail
        ],
    }


def unit_manifests(project: Project, profile_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """All unit manifests currently listed in each profile's compile database."""
    store = Store(project.state_dir)
    out = []
    for prof in project.select_profiles(profile_ids):
        try:
            cmds = load_compdb(prof.compile_commands)
        except WeaverError:
            continue
        for c in cmds:
            p = store.unit_dir(prof.id, c.unit_id(prof.id)) / MANIFEST
            if p.exists():
                out.append(read_json(p))
    return out


def write_profile_manifest(project: Project, profile: Profile, cmds: list[CompileCommand]) -> Path:
    """The per-profile manifest sketched in compiler plan §10."""
    store = Store(project.state_dir)
    pdir = store.profile_dir(profile.id)
    tools: dict[str, Any] = {}
    for c in cmds:
        t = identify(c.compiler, c.directory)
        tools[t.realpath or c.compiler] = t.to_json()
    sec = identify(profile.secondary_frontend.compiler).to_json() if profile.secondary_frontend else None
    write_json(pdir / "tools.json", {"production": tools, "secondary": sec})
    write_json(pdir / "commands.json", [c.to_json() for c in cmds])
    prod = next(iter(tools.values()), {})
    manifest = {
        "profile_id": profile.id,
        "description": profile.description,
        "source_revision": _source_revision(project.root),
        "production": {
            "compiler": {
                "family": prod.get("family"),
                "version": prod.get("version"),
                "hash": prod.get("sha256"),
                "banner": prod.get("version_banner"),
                "target": prod.get("target"),
            },
            "commands_manifest": "commands.json",
            "tools_manifest": "tools.json",
            "link_manifest": str(profile.link_manifest) if profile.link_manifest else None,
        },
        "target": profile.target,
        "platform": profile.platform,
        "analysis": {
            "producer": "native"
            if prod.get("family") == "clang"
            else ("secondary_frontend" if profile.secondary_frontend else "none"),
            "secondary_frontend": sec,
            "capabilities": "capabilities.json",
            "fidelity": f"../../fidelity/{profile.id}/summary.json",
        },
        "artifacts": {"evidence_root": f"../../evidence/{profile.id}"},
    }
    path = pdir / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return path


def _source_revision(root: Path) -> dict[str, Any]:
    r = run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=30)
    if not r.ok:
        return {"vcs": None}
    st = run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], timeout=60)
    sub = run(["git", "-C", str(root), "submodule", "status", "--recursive"], timeout=60)
    return {
        "vcs": "git",
        "commit": r.stdout.decode().strip(),
        "dirty": bool(st.stdout.strip()),
        "dirty_digest": sha256_bytes(st.stdout) if st.stdout.strip() else None,
        "submodules": sub.stdout.decode().splitlines() if sub.ok else [],
    }
