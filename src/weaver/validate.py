"""Independent validation of one transaction in isolated workspaces.

Pointer-tracker plan §§6-7: apply the patch in an isolated checkpoint, compile
every affected supported configuration with its production command, re-check
the recipe's preconditions mechanically on the patched source, run the
configured tests, and compare original and transformed behavior under the same
inputs.  Every record states what kind of evidence it is; a required check that
could not be evaluated leaves the result provisional.  Results are bound to the
exact patch and source revision.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from weaver.capture.compdb import CompileCommand, load_compdb
from weaver.capture.toolid import identify
from weaver.config import Project
from weaver.evidence import ValidationKind, ValidationOutcome
from weaver.frontend.clang_ast import TranslationUnit
from weaver.recipes import CATALOG
from weaver.rewrite import Edit, OffsetMap, apply_edits
from weaver.store import Store
from weaver.toolchain.collect import read_macros
from weaver.toolchain.recipes import RECIPES
from weaver.toolchain.sanitize import sanitize
from weaver.toolchain.translate import translate_gcc_to_clang
from weaver.util import atomic_write_bytes, now_iso, run


class Remapper:
    """Rewrite paths under the project root to the same paths under a workspace."""

    def __init__(self, root: Path, ws: Path):
        roots = {str(root), os.path.realpath(root)}
        self.patterns = [(re.compile(re.escape(r) + r"(?=/|$)"), str(ws)) for r in sorted(roots, key=len, reverse=True)]
        self.ws = ws

    def __call__(self, s: str) -> str:
        for pat, repl in self.patterns:
            s = pat.sub(repl, s)
        return s

    def cmd(self, c: CompileCommand) -> CompileCommand:
        return CompileCommand(
            directory=self(c.directory) if os.path.isdir(self(c.directory)) else c.directory,
            file=self(c.file),
            arguments=[self(a) for a in c.arguments],
            expanded=[self(a) for a in c.expanded],
            output=c.output,
            response_files=c.response_files,
            index=c.index,
        )


def make_workspace(project: Project, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    state = project.state_dir.resolve()
    excludes = set(project.workspace_exclude)

    def ignore(d: str, names: list[str]) -> set[str]:
        out = {n for n in names if n in excludes}
        for n in names:
            if Path(d, n).resolve() == state:
                out.add(n)
        return out

    shutil.copytree(project.root, dest, symlinks=True, ignore=ignore)
    return dest


def _record(kind: ValidationKind, name: str, outcome: ValidationOutcome, detail: str = "", **extra: Any) -> dict:
    return {"kind": kind.value, "name": name, "outcome": outcome.value, "detail": detail, **extra}


def _compile(c: CompileCommand, out_dir: Path) -> dict[str, Any]:
    san = sanitize(c)
    obj = out_dir / (Path(c.file).name + f".{c.index}.o")
    r = run([c.compiler, *san.options, "-c", c.file, "-o", str(obj)], cwd=c.directory, timeout=1800)
    return {"returncode": r.returncode, "stderr": r.stderr_text(8000), "argv": r.argv}


def _warnings(stderr: str) -> list[str]:
    out = []
    for line in stderr.splitlines():
        m = re.search(r"(warning|error): (.*)$", line)
        if m:
            out.append(m.group(0))
    return out


def _ast_for(
    c: CompileCommand, manifest: dict[str, Any], out_dir: Path, prod_macros: dict[str, str]
) -> tuple[Path | None, str]:
    """Collect the AST of a (remapped) command with the same frontend as the evidence."""
    san = sanitize(c)
    key = manifest.get("ast_artifact") or ""
    if key.startswith("secondary."):
        tool = identify(c.compiler, c.directory)
        stool = manifest.get("secondary_tool") or {}
        clang = stool.get("path") or "clang"
        tr = translate_gcc_to_clang(san.options, tool, prod_macros, clang, c.directory)
        cc, opts = clang, tr.options
    else:
        cc, opts = c.compiler, san.options
    inv = RECIPES["ast_json"].build(cc, opts, c.file, out_dir, f"recheck{c.index}")
    r = run(inv.argv, cwd=c.directory, stdout_path=inv.stdout_to, timeout=1800)
    if not r.ok:
        return None, r.stderr_text(4000)
    return inv.outputs[0], ""


def run_validation(project: Project, txn: dict[str, Any], keep: bool = False) -> dict[str, Any]:
    store = Store(project.state_dir)
    cand = txn["candidate"]
    edits = [Edit.from_json(e) for e in cand["edits"]]
    changes = apply_edits(project.root, edits, cand["file_hashes"])  # refuses stale sources
    affected = sorted(changes)
    records: list[dict[str, Any]] = []
    started = time.time()

    wdir = store.workspace_dir(txn["id"])
    base_ws = make_workspace(project, wdir / "baseline")
    cand_ws = make_workspace(project, wdir / "candidate")
    for f, (_, new) in changes.items():
        atomic_write_bytes(cand_ws / f, new)
    scratch = wdir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    base_map = Remapper(project.root, base_ws)
    cand_map = Remapper(project.root, cand_ws)
    recipe = CATALOG[cand["recipe"]]

    # 1. compile every configuration that compiles an affected file ---------
    for prof in project.profiles:
        try:
            cmds = load_compdb(prof.compile_commands)
        except Exception as e:  # noqa: BLE001 - reported as not-evaluated
            records.append(
                _record(ValidationKind.COMPILE, prof.id, ValidationOutcome.NOT_EVALUATED, str(e), profile=prof.id)
            )
            continue
        targets = {os.path.realpath(project.root / f): f for f in affected}
        for c in cmds:
            rel = targets.get(os.path.realpath(c.file))
            if rel is None:
                continue
            bc, cc = base_map.cmd(c), cand_map.cmd(c)
            b = _compile(bc, scratch)
            k = _compile(cc, scratch)
            new_diags = sorted(set(_warnings(k["stderr"])) - set(_warnings(b["stderr"])))
            if k["returncode"] != 0:
                outcome = ValidationOutcome.FAILED
                detail = f"patched unit does not compile: {k['stderr'][:2000]}"
            elif b["returncode"] != 0:
                outcome = ValidationOutcome.NOT_EVALUATED
                detail = "baseline does not compile with this command"
            else:
                outcome = ValidationOutcome.PASSED
                detail = "compiles" + (f"; new diagnostics: {new_diags}" if new_diags else "; no new diagnostics")
            records.append(
                _record(
                    ValidationKind.COMPILE,
                    f"{prof.id}:{rel}#{c.index}",
                    outcome,
                    detail,
                    profile=prof.id,
                    file=rel,
                    new_diagnostics=new_diags,
                )
            )

            # 2. mechanical re-check on the patched AST ----------------------
            man = _manifest_for(project, prof.id, c)
            if man is None or not man.get("ast_artifact"):
                records.append(
                    _record(
                        ValidationKind.MECHANICAL_RECHECK,
                        f"{prof.id}:{rel}#{c.index}",
                        ValidationOutcome.NOT_EVALUATED,
                        "no AST evidence for this unit",
                        profile=prof.id,
                        file=rel,
                    )
                )
                continue
            prod_macros = read_macros(store.unit_dir(prof.id, man["unit_id"]) / "unit.macros.txt")
            ast_path, err = _ast_for(cc, man, scratch, prod_macros)
            if ast_path is None:
                records.append(
                    _record(
                        ValidationKind.MECHANICAL_RECHECK,
                        f"{prof.id}:{rel}#{c.index}",
                        ValidationOutcome.FAILED,
                        f"patched file does not parse: {err}",
                        profile=prof.id,
                        file=rel,
                    )
                )
                continue
            tu = TranslationUnit(ast_path, cc.directory, cc.file, str(cand_ws))
            file_edits = [e for e in edits if e.file == rel]
            problems = recipe.recheck(cand, tu, OffsetMap(file_edits), str(cand_ws))
            records.append(
                _record(
                    ValidationKind.MECHANICAL_RECHECK,
                    f"{prof.id}:{rel}#{c.index}",
                    ValidationOutcome.FAILED if problems else ValidationOutcome.PASSED,
                    "; ".join(problems)
                    if problems
                    else "declaration removed; every replaced site resolves to the target",
                    profile=prof.id,
                    file=rel,
                )
            )

    # 3. builds, tests and differential comparisons ---------------------------
    for prof in project.profiles:
        v = prof.validation
        built = {"baseline": True, "candidate": True}
        if v.build:
            for label, ws in (("baseline", base_ws), ("candidate", cand_ws)):
                argv, cwd, env = v.build.render(workspace=str(ws), root=str(project.root))
                r = run(argv, cwd=cwd, env=env, timeout=v.build.timeout)
                built[label] = r.ok
                if label == "candidate":
                    records.append(
                        _record(
                            ValidationKind.COMPILE,
                            f"{prof.id}:build",
                            ValidationOutcome.PASSED
                            if r.ok
                            else (ValidationOutcome.FAILED if built["baseline"] else ValidationOutcome.NOT_EVALUATED),
                            "full build" + ("" if r.ok else f" failed: {r.stderr_text(2000)}"),
                            profile=prof.id,
                        )
                    )
        for t in v.tests:
            if not built["candidate"]:
                records.append(
                    _record(
                        ValidationKind.TEST,
                        f"{prof.id}:{t.name}",
                        ValidationOutcome.NOT_EVALUATED,
                        "candidate build failed",
                        profile=prof.id,
                    )
                )
                continue
            ra = _run_spec(t, base_ws, project) if built["baseline"] else None
            rb = _run_spec(t, cand_ws, project)
            if rb.ok:
                outcome, detail = ValidationOutcome.PASSED, "exit 0"
            elif ra is not None and not ra.ok:
                outcome, detail = ValidationOutcome.NOT_EVALUATED, "test also fails on the unpatched baseline"
            else:
                outcome, detail = ValidationOutcome.FAILED, f"exit {rb.returncode}: {rb.stderr_text(1500)}"
            records.append(_record(ValidationKind.TEST, f"{prof.id}:{t.name}", outcome, detail, profile=prof.id))
        for t in v.compare:
            if not (built["candidate"] and built["baseline"]):
                records.append(
                    _record(
                        ValidationKind.DIFFERENTIAL_TEST,
                        f"{prof.id}:{t.name}",
                        ValidationOutcome.NOT_EVALUATED,
                        "a build failed",
                        profile=prof.id,
                    )
                )
                continue
            ra = _run_spec(t, base_ws, project)
            rb = _run_spec(t, cand_ws, project)
            same = ra.returncode == rb.returncode and ra.stdout == rb.stdout
            detail = (
                "identical exit status and stdout"
                if same
                else (
                    f"baseline exit {ra.returncode}, candidate exit {rb.returncode}; stdout "
                    f"{'identical' if ra.stdout == rb.stdout else 'differs'}"
                )
            )
            records.append(
                _record(
                    ValidationKind.DIFFERENTIAL_TEST,
                    f"{prof.id}:{t.name}",
                    ValidationOutcome.PASSED if same else ValidationOutcome.FAILED,
                    detail,
                    profile=prof.id,
                    **(
                        {}
                        if same
                        else {"baseline_stdout": ra.stdout_text(2000), "candidate_stdout": rb.stdout_text(2000)}
                    ),
                )
            )

    if not keep:
        shutil.rmtree(wdir, ignore_errors=True)
    return {
        "at": now_iso(),
        "duration_s": round(time.time() - started, 2),
        "patch_sha256": txn["patch"]["sha256"],
        "source_hashes": cand["file_hashes"],
        "records": records,
        "workspace": str(wdir) if keep else None,
    }


def _run_spec(spec: Any, ws: Path, project: Project):
    argv, cwd, env = spec.render(workspace=str(ws), root=str(project.root))
    return run(argv, cwd=cwd, env=env, timeout=spec.timeout)


def _manifest_for(project: Project, profile_id: str, c: CompileCommand) -> dict[str, Any] | None:
    from weaver.toolchain.collect import MANIFEST
    from weaver.util import read_json

    p = Store(project.state_dir).unit_dir(profile_id, c.unit_id(profile_id)) / MANIFEST
    return read_json(p) if p.exists() else None


def judge(records: list[dict[str, Any]], require: list[str]) -> tuple[str, list[str]]:
    """Return (state, reasons) from validation records under an acceptance policy."""
    from weaver.evidence import TxnState

    reasons = []
    failed = [r for r in records if r["outcome"] == ValidationOutcome.FAILED.value]
    if failed:
        return TxnState.REJECTED.value, [f"{r['kind']} {r['name']}: {r['detail'][:300]}" for r in failed]
    for kind in require:
        mine = [r for r in records if r["kind"] == kind]
        if not mine:
            reasons.append(f"required '{kind}' validation was not configured or not run")
        elif not any(r["outcome"] == ValidationOutcome.PASSED.value for r in mine):
            reasons.append(f"required '{kind}' validation could not be evaluated")
        elif any(r["outcome"] == ValidationOutcome.NOT_EVALUATED.value for r in mine):
            reasons.append(f"some '{kind}' validations could not be evaluated")
    if reasons:
        return TxnState.PROVISIONAL.value, reasons
    return TxnState.VALIDATED.value, []
