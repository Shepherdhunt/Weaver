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
from weaver.frontend.wrappers import unwrapper_for
from weaver.recipes import CATALOG
from weaver.rewrite import Edit, OffsetMap, apply_edits
from weaver.store import Store
from weaver.toolchain.collect import read_macros
from weaver.toolchain.recipes import RECIPES
from weaver.toolchain.sanitize import sanitize
from weaver.toolchain.translate import translate_gcc_to_clang
from weaver.util import atomic_write_bytes, now_iso, run


class Remapper:
    """Rewrite paths under the project root to the same paths under a workspace.

    A path that exists under the root but was not copied into the workspace
    (a directory in ``workspace_exclude``, such as a build tree holding
    generated headers) keeps pointing at the original: it is a read-only input.
    """

    def __init__(self, root: Path, ws: Path):
        roots = {str(root), os.path.realpath(root)}
        self.patterns = [
            (re.compile(re.escape(r) + r"(?=/|$)([^\s:]*)"), r) for r in sorted(roots, key=len, reverse=True)
        ]
        self.ws = ws

    def _sub(self, m: re.Match[str], root: str) -> str:
        rest = m.group(1)
        mapped = str(self.ws) + rest
        if rest and not os.path.lexists(mapped) and os.path.lexists(root + rest):
            return m.group(0)
        return mapped

    def __call__(self, s: str) -> str:
        for pat, root in self.patterns:
            s = pat.sub(lambda m, r=root: self._sub(m, r), s)
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


_CTEST = re.compile(
    r"^\s*\d+/\d+ Test\s+#\d+: (?P<name>\S+) \.*\s*(?:\*\*\*)?(?P<result>Passed|Failed|Not Run|Timeout|"
    r"Exception[^\d]*|SEGFAULT|Skipped|Disabled)"
)


_MESON = re.compile(
    r"^\s*\d+/\d+\s+(?P<name>\S.*?)\s+(?P<result>OK|FAIL|SKIP|EXPECTEDFAIL|UNEXPECTEDPASS|TIMEOUT|ERROR)\s+[\d.]+s\b"
)


def parse_test_outcomes(stdout: str) -> dict[str, str]:
    """Per-test results from a runner's output (CTest or Meson progress lines); empty if not recognised."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        m = _CTEST.match(line)
        if m:
            r = m.group("result").strip()
            out[m.group("name")] = (
                "passed" if r == "Passed" else "skipped" if r in ("Skipped", "Disabled") else "failed"
            )
            continue
        m = _MESON.match(line)
        if m:
            r = m.group("result")
            out[m.group("name")] = "passed" if r in ("OK", "EXPECTEDFAIL") else "skipped" if r == "SKIP" else "failed"
    return out


def _compare_tests(base: dict[str, str], cand: dict[str, str]) -> tuple[ValidationOutcome, str, dict[str, Any]]:
    regressions = sorted(n for n, r in cand.items() if r == "failed" and base.get(n) == "passed")
    missing = sorted(n for n, r in base.items() if r == "passed" and n not in cand)
    both_fail = sorted(n for n, r in cand.items() if r == "failed" and base.get(n) == "failed")
    passed = sorted(n for n, r in cand.items() if r == "passed" and base.get(n) == "passed")
    extra = {
        "tests": {
            "passed_both": len(passed),
            "regressions": regressions,
            "failing_on_baseline": both_fail,
            "missing": missing,
        }
    }
    if regressions or missing:
        what = []
        if regressions:
            what.append(
                f"{len(regressions)} test(s) pass on the baseline and fail with the patch: "
                + ", ".join(regressions[:10])
            )
        if missing:
            what.append(f"{len(missing)} test(s) did not run with the patch: {', '.join(missing[:10])}")
        return ValidationOutcome.FAILED, "; ".join(what), extra
    if not passed:
        return ValidationOutcome.NOT_EVALUATED, "no test passes on both trees", extra
    detail = f"{len(passed)} test(s) pass on both trees"
    if both_fail:
        detail += f"; {len(both_fail)} fail on both (pre-existing, not attributable): {', '.join(both_fail[:8])}"
    return ValidationOutcome.PASSED, detail, extra


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
        offset_maps = {f: OffsetMap([e for e in edits if e.file == f]) for f in affected}
        for c in cmds:
            man = _manifest_for(project, prof.id, c)
            deps = {os.path.realpath(c.file)} | {
                os.path.realpath(d["path"]) for d in (man or {}).get("dependencies", [])
            }
            hit = sorted(targets[t] for t in deps & set(targets))
            if man is not None and not hit:
                continue  # neither this unit's source nor anything it includes was edited
            rel = os.path.relpath(c.file, project.root)
            name = f"{prof.id}:{rel}#{c.index}"
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
            via = f" (includes {', '.join(h for h in hit if h != rel)})" if any(h != rel for h in hit) else ""
            records.append(
                _record(
                    ValidationKind.COMPILE,
                    name,
                    outcome,
                    detail + via,
                    profile=prof.id,
                    file=rel,
                    new_diagnostics=new_diags,
                )
            )

            # 2. mechanical re-check on the patched AST ----------------------
            if man is None or not man.get("ast_artifact"):
                records.append(
                    _record(
                        ValidationKind.MECHANICAL_RECHECK,
                        name,
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
                        name,
                        ValidationOutcome.FAILED,
                        f"patched unit does not parse: {err}",
                        profile=prof.id,
                        file=rel,
                    )
                )
                continue
            tu = TranslationUnit(
                ast_path,
                cc.directory,
                cc.file,
                str(cand_ws),
                unwrapper_for(man, store.unit_dir(prof.id, man["unit_id"])),
            )
            problems = recipe.recheck(cand, tu, offset_maps, str(cand_ws))
            if problems is None:
                continue  # the transaction's facts do not appear in this unit
            records.append(
                _record(
                    ValidationKind.MECHANICAL_RECHECK,
                    name,
                    ValidationOutcome.FAILED if problems else ValidationOutcome.PASSED,
                    "; ".join(problems) if problems else "patched AST satisfies the recipe's post-conditions",
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
            per_a = parse_test_outcomes(ra.stdout_text(None)) if ra is not None else {}
            per_b = parse_test_outcomes(rb.stdout_text(None))
            extra: dict[str, Any] = {}
            if per_b and per_a:
                # A test runner that reports individual tests (CTest): judge test by test.
                outcome, detail, extra = _compare_tests(per_a, per_b)
            elif rb.ok:
                outcome, detail = ValidationOutcome.PASSED, "exit 0"
            elif ra is not None and not ra.ok:
                outcome, detail = ValidationOutcome.NOT_EVALUATED, "test also fails on the unpatched baseline"
            else:
                outcome, detail = ValidationOutcome.FAILED, f"exit {rb.returncode}: {rb.stderr_text(1500)}"
            records.append(
                _record(ValidationKind.TEST, f"{prof.id}:{t.name}", outcome, detail, profile=prof.id, **extra)
            )
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


BEHAVIOURAL_KINDS = (ValidationKind.TEST.value, ValidationKind.DIFFERENTIAL_TEST.value)


def validation_strength(records: list[dict[str, Any]]) -> str:
    """``behavioural`` when a test or differential run passed; ``compile-only`` otherwise.

    Compile checks and the mechanical re-check establish that the patch builds and
    has the intended shape; only running the program says anything about behaviour.
    """
    ok = any(r["kind"] in BEHAVIOURAL_KINDS and r["outcome"] == ValidationOutcome.PASSED.value for r in records)
    return "behavioural" if ok else "compile-only"


def configured_strength(project: Project) -> dict[str, Any]:
    """How strong validation *can* be under the current configuration, before any transaction runs.

    ``behavioural`` - tests or differential runs are configured and the acceptance
    policy requires them; ``behavioural-optional`` - they are configured, but a
    transaction whose tests could not run may still be accepted provisionally;
    ``compile-only`` - nothing executes the patched program.
    """
    runs = {
        p.id: {
            "build": bool(p.validation.build),
            "tests": len(p.validation.tests),
            "compare": len(p.validation.compare),
        }
        for p in project.profiles
    }
    configured = any(r["tests"] or r["compare"] for r in runs.values())
    required = [k for k in BEHAVIOURAL_KINDS if k in project.acceptance.require]
    notes = []
    if not configured:
        level = "compile-only"
        notes.append(
            "no test or differential command is configured: a transaction is accepted once it compiles and its "
            "patched AST re-checks; nothing runs the changed program"
        )
    elif not required:
        level = "behavioural-optional"
        notes.append(
            "tests are configured but the acceptance policy does not require them (add 'testing' or "
            "'differential-testing' to acceptance.require)"
        )
    else:
        level = "behavioural"
    for pid, r in runs.items():
        if (r["tests"] or r["compare"]) and not r["build"]:
            notes.append(
                f"profile {pid}: tests run without a validation build; they see whatever the workspace copy already "
                "contains unless the test command builds"
            )
    return {"level": level, "required": required, "profiles": runs, "notes": notes}


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
