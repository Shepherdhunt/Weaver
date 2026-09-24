"""Coverage of the changed lines: did the tests execute what the patch changed?

A test suite that passes says little about a change it never runs.  After the
judged runs, validation builds the patched tree once more, in its own copy,
through compiler shims that add ``--coverage`` to every compile and link.  It
runs the same tests and differential commands there and reads the counts with
gcov (GCC, JSON output) or ``llvm-cov gcov`` (Clang, gcov text).  Only the lines
the patch replaced or inserted are looked at; of those, only lines that carry
code count ("executable").  The judged builds keep their production flags:
instrumentation never touches what decides pass or fail.

The result is one record per profile:

* passed: every changed line that has code ran at least once (or none has code);
* not evaluated: some changed lines never ran, or coverage could not be measured
  (no tool, the instrumented build failed, no data was written).  The detail
  says which, and names the lines.

``validation_strength`` then stops calling a change "behavioural" when the tests
executed none of it (``unexercised``) or only part of it (``partly-exercised``).
``acceptance.require: [coverage]`` makes such a transaction provisional.

Limits: shims work through ``PATH``, so a build that names its compiler by
absolute path is not instrumented; code in a changed macro is counted at the
lines that expand it, not in the header; a line counts as executed when any
test ran it once, whatever it checked.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from weaver.util import run

COMPILERS = ("cc", "gcc", "clang", "c++", "g++", "clang++")
SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".C", ".c++")

SHIM = """#!/bin/sh
# Weaver coverage shim: runs {real} with --coverage when it compiles or links
cov=0
for a in "$@"; do
  case "$a" in
    -E|-M|-MM) exec {real} "$@" ;;
    -c|*.c|*.cc|*.cpp|*.cxx|*.C|*.c++|*.s|*.S|*.o|*.a|*.so) cov=1 ;;
  esac
done
if [ "$cov" = 1 ]; then
  printf '%s\\n' {real} >> {log} 2>/dev/null
  exec {real} "$@" --coverage
fi
exec {real} "$@"
"""


def changed_lines(changes: dict[str, tuple[bytes, bytes]]) -> tuple[dict[str, list[int]], int]:
    """Lines (1-based, in the patched text) each file's patch replaced or inserted, and the number of hunks
    that only delete (they leave no line to execute)."""
    out: dict[str, list[int]] = {}
    deletions = 0
    for rel, (old, new) in changes.items():
        a = old.decode("latin-1").splitlines()
        b = new.decode("latin-1").splitlines()
        lines: list[int] = []
        for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
            if tag in ("replace", "insert"):
                lines += [j for j in range(j1 + 1, j2 + 1) if b[j - 1].strip()]
            elif tag == "delete":
                deletions += 1
        if lines:
            out[rel] = sorted(set(lines))
    return out, deletions


def make_shims(bin_dir: Path, log: Path, names: list[str]) -> list[str]:
    """One shim per compiler name found on PATH (outside ``bin_dir``); returns the names shimmed."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if Path(p) != bin_dir)
    made = []
    for name in dict.fromkeys(names):
        real = shutil.which(name, path=path)
        if real is None or "/" in name:
            continue
        s = bin_dir / name
        s.write_text(SHIM.format(real=shlex.quote(real), log=shlex.quote(str(log))))
        s.chmod(0o755)
        made.append(name)
    return made


def _family(compiler: str) -> tuple[str, str]:
    """('gcc' | 'clang', major version) of a real compiler."""
    r = run([compiler, "--version"], timeout=30)
    text = r.stdout_text(2000) if r.ok else ""
    family = "clang" if "clang" in text.lower() or "clang" in Path(compiler).name else "gcc"
    v = run([compiler, "-dumpversion"], timeout=30)
    major = (v.stdout_text(100).strip().split(".")[0] if v.ok else "") or ""
    return family, major


def _tools(compilers: list[str]) -> list[tuple[str, list[str]]]:
    """The coverage readers to try, in order: (label, argv prefix)."""
    out: list[tuple[str, list[str]]] = []
    for c in dict.fromkeys(compilers):
        family, major = _family(c)
        if family == "gcc":
            for name in ([f"gcov-{major}"] if major else []) + ["gcov"]:
                p = shutil.which(name)
                if p:
                    out.append(("gcov", [p, "--json-format", "--stdout"]))
                    break
        else:
            for name in ([f"llvm-cov-{major}"] if major else []) + ["llvm-cov"]:
                p = shutil.which(name)
                if p:
                    out.append(("llvm-cov", [p, "gcov", "-t"]))
                    break
    return list(dict.fromkeys((label, tuple(argv)) for label, argv in out))  # type: ignore[arg-type]


def _read_json(stdout: str, gcda: Path) -> list[tuple[Path, list[tuple[int, int]]]]:
    out = []
    for doc in stdout.splitlines():
        doc = doc.strip()
        if not doc.startswith("{"):
            continue
        d = json.loads(doc)
        cwd = Path(d.get("current_working_directory") or gcda.parent)
        for f in d.get("files", []):
            p = Path(f["file"])
            p = p if p.is_absolute() else cwd / p
            out.append((p, [(int(ln["line_number"]), int(ln["count"])) for ln in f.get("lines", [])]))
    return out


def _read_text(stdout: str, gcda: Path, root: Path) -> list[tuple[Path, list[tuple[int, int]]]]:
    """gcov's annotated text (``llvm-cov gcov -t``): '-' has no code, '#####' / '=====' never ran."""
    out: list[tuple[Path, list[tuple[int, int]]]] = []
    cur: list[tuple[int, int]] | None = None
    for line in stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        count, num = parts[0].strip(), parts[1].strip()
        if num == "0" and parts[2].startswith("Source:"):
            src = Path(parts[2][len("Source:") :].strip())
            if not src.is_absolute():  # relative to where it was compiled: the object's directory or above
                for base in [gcda.parent, *gcda.parent.parents]:
                    if (base / src).exists() or base == root:
                        src = base / src
                        break
            cur = []
            out.append((src, cur))
            continue
        if cur is None or not num.isdigit() or num == "0" or count == "-":
            continue
        n = 0 if count.startswith(("#", "=")) else int(count.rstrip("*") or 0)
        cur.append((int(num), n))
    return out


def collect(
    ws: Path, compilers: list[str], interesting: set[str], log: Any = None
) -> tuple[dict[str, dict[int, int]], str, int]:
    """Line counts per project-relative file from every ``.gcda`` under ``ws``: (counts, tool, files read)."""
    say = log or (lambda _m: None)
    tools = _tools(compilers)
    if not tools:
        return {}, "", 0
    wsr = ws.resolve()
    stems = {Path(r).name for r in interesting}
    only_sources = all(r.endswith(SOURCE_SUFFIXES) for r in interesting)
    gcdas = [
        g
        for g in wsr.rglob("*.gcda")
        if not only_sources or any(g.name == s + ".gcda" or g.name.startswith(Path(s).stem + ".") for s in stems)
    ]
    counts: dict[str, dict[int, int]] = {}
    used: set[str] = set()

    def one(g: Path) -> list[tuple[Path, list[tuple[int, int]]]]:
        with tempfile.TemporaryDirectory() as tmp:
            for label, argv in tools:
                r = run([*argv, str(g)], cwd=tmp, timeout=300)
                if not r.ok:
                    continue
                text = r.stdout_text(None)
                files = _read_json(text, g) if label == "gcov" else _read_text(text, g, wsr)
                if files:
                    used.add(label)
                    return files
        return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        for files in pool.map(one, gcdas):
            for p, lines in files:
                try:
                    rel = str(Path(os.path.realpath(p)).relative_to(wsr))
                except ValueError:
                    continue  # a system header or generated file outside the tree
                if rel not in interesting:
                    continue
                per = counts.setdefault(rel, {})
                for n, c in lines:
                    per[n] = max(per.get(n, 0), c)
    say(f"coverage: read {len(gcdas)} data file(s) with {', '.join(sorted(used)) or 'no tool'}")
    return counts, ", ".join(sorted(used)), len(gcdas)


def summarize(changed: dict[str, list[int]], counts: dict[str, dict[int, int]], deletions: int) -> dict[str, Any]:
    executable, executed, missed = 0, 0, []
    per_file = {}
    for rel, lines in sorted(changed.items()):
        c = counts.get(rel, {})
        ex = [n for n in lines if n in c]
        ran = [n for n in ex if c[n] > 0]
        executable += len(ex)
        executed += len(ran)
        missed += [f"{rel}:{n}" for n in ex if c[n] == 0]
        per_file[rel] = {"changed": lines, "executable": ex, "executed": ran}
    return {
        "measured": True,
        "changed_lines": sum(len(v) for v in changed.values()),
        "executable": executable,
        "executed": executed,
        "missed": missed,
        "deletion_only_hunks": deletions,
        "files": per_file,
    }


def describe(cov: dict[str, Any]) -> str:
    if not cov.get("measured"):
        return f"not measured: {cov.get('reason')}"
    n, k = cov["executable"], cov["executed"]
    if n == 0:
        return "no changed line has code of its own (declarations, types, comments or deletions only)"
    if k == n:
        return f"the tests executed every changed line that has code ({k} of {n})"
    missed = ", ".join(cov["missed"][:8]) + (f" (+{len(cov['missed']) - 8} more)" if len(cov["missed"]) > 8 else "")
    if k == 0:
        return f"the tests executed none of the {n} changed line(s) that have code: {missed}"
    return f"the tests executed {k} of {n} changed line(s) that have code; never executed: {missed}"


def measure(
    project: Any,
    prof: Any,
    changes: dict[str, tuple[bytes, bytes]],
    wdir: Path,
    make_ws: Any,
    log: Any = None,
) -> dict[str, Any]:
    """Build the patched tree with coverage, run the profile's tests and comparisons, read the changed lines."""
    say = log or (lambda _m: None)
    started = time.time()
    changed, deletions = changed_lines(changes)
    if not changed:
        return {"measured": True, "changed_lines": 0, "executable": 0, "executed": 0, "missed": [],
                "deletion_only_hunks": deletions, "files": {}, "duration_s": 0.0}  # fmt: skip
    v = prof.validation
    ws = make_ws(project, wdir / f"coverage-{prof.id}")
    for f, (_, new) in changes.items():
        (ws / f).write_bytes(new)
    bin_dir, clog = wdir / f"coverage-bin-{prof.id}", wdir / f"coverage-{prof.id}.log"
    names = list(COMPILERS)
    try:
        from weaver.capture.compdb import load_compdb

        names += [Path(c.compiler).name for c in load_compdb(prof.compile_commands)]
    except Exception:  # noqa: BLE001 - the standard names still apply
        pass
    make_shims(bin_dir, clog, names)
    path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def go(spec: Any) -> Any:
        argv, cwd, env = spec.render(workspace=str(ws), root=str(project.root))
        env = {**env, "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}" if "PATH" in env else path}
        return run(argv, cwd=cwd, env=env, timeout=spec.timeout)

    def fail(reason: str) -> dict[str, Any]:
        return {"measured": False, "reason": reason, "duration_s": round(time.time() - started, 2)}

    if v.build:
        say(f"[{prof.id}] coverage: building the patched tree with --coverage ...")
        r = go(v.build)
        if not r.ok:
            return fail(f"the instrumented build failed: {r.stderr_text(600).strip() or r.stdout_text(600).strip()}")
    for spec in [*v.tests, *v.compare]:
        say(f"[{prof.id}] coverage: running {spec.name} ...")
        go(spec)  # outcomes were judged on the production build; this run only records what executes
    compilers = (
        sorted({line.strip() for line in clog.read_text().splitlines() if line.strip()}) if clog.exists() else []
    )
    if not compilers:
        return fail("no compile went through the coverage shims (does the build name its compiler by absolute path?)")
    counts, tool, n = collect(ws, compilers, set(changed), say)
    if not tool:
        return fail(
            "no coverage data could be read" + ("" if n else " (the tests wrote no .gcda file)")
            + " — gcov (GCC) or llvm-cov (Clang) is needed"
        )  # fmt: skip
    out = summarize(changed, counts, deletions)
    out.update(tool=tool, data_files=n, duration_s=round(time.time() - started, 2))
    return out
