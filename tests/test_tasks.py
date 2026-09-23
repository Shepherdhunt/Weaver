"""Task ownership: the concurrency precondition decided from declared, checked threads of control."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import HAVE_CLANG, HAVE_MAKE, HAVE_SVF, run_cli

from weaver.analysis.inventory import load_inventory
from weaver.config import load_project
from weaver.recipes import CATALOG, RecipeContext

needs_svf_tasks = pytest.mark.skipif(
    not (HAVE_CLANG and HAVE_MAKE and HAVE_SVF), reason="clang, make and pysvf required"
)

SRC = """\
#include <pthread.h>

int shared_counter;             /* written by the worker thread */
static int main_only;

static int scale(const int *f, int x) { return x * *f; }
static int peek(const int *p) { return *p + 1; }
static int twice(const int *h) { return 2 * *h; }

static void *worker(void *arg)
{
    (void)arg;
    for (int i = 0; i < 3; i++)
        shared_counter++;
    return 0;
}

static void *filler(void *arg)
{
    *(int *)arg = 7;            /* writes the object main handed over */
    return 0;
}

int main(void)
{
    pthread_t t1, t2;
    int local = 3;              /* main's own stack: no other thread has its address */
    int handoff = 0;            /* its address is given to filler */
    pthread_create(&t1, 0, worker, 0);
    pthread_create(&t2, 0, filler, &handoff);
    main_only = scale(&local, 2);
    int r = peek(&shared_counter);
    int s = twice(&handoff);
    pthread_join(t1, 0);
    pthread_join(t2, 0);
    return r + s + main_only;
}
"""

TASKS = {
    "model": "tasks",
    "tasks": [
        {"name": "main", "entry": "main"},
        {"name": "worker", "entry": "worker"},
        {"name": "filler", "entry": "filler"},
    ],
}


def _project(tmp: Path, concurrency) -> Path:
    from weaver.capture.shims import finalize, make_shim

    root = tmp / "threads"
    root.mkdir()
    (root / "tasks.c").write_text(SRC)
    (root / "Makefile").write_text("app: tasks.c\n\t$(CC) -O0 -g -pthread -o app tasks.c\n")
    cap = root / ".weaver" / "capture"
    shim = make_shim(cap / "shims", "cc", shutil.which("clang"), cap / "log.jsonl")
    subprocess.run(["make", "-s", f"CC={shim}"], cwd=root, check=True)
    finalize(cap / "log.jsonl", root / "build")
    cfg = {
        "schema": "weaver.project/1",
        "project": {"name": "threads", "workspace_exclude": [".git", "build"]},
        "preservation": {"behaviors": ["exit-status"], "concurrency": concurrency},
        "profiles": [{"id": "clang", "compile_commands": "build/compile_commands.json"}],
    }
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert run_cli(root, "collect") == 0 and run_cli(root, "inventory") == 0 and run_cli(root, "flow") == 0
    return root


def _set_concurrency(root: Path, concurrency) -> None:
    cfg = yaml.safe_load((root / "weaver.yaml").read_text())
    cfg["preservation"]["concurrency"] = concurrency
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))


def _verdicts(root: Path) -> dict[str, tuple[bool, object]]:
    proj = load_project(root)
    inv = load_inventory(proj)
    ctx = RecipeContext(proj, inv)
    out = {}
    for f in inv["findings"]:
        if CATALOG["scalar-input"].applicable(f) and f.get("function") in ("scale", "peek", "twice"):
            res = CATALOG["scalar-input"].evaluate(ctx, f)
            conc = next(p for p in res.preconditions if p.id == "SI.no-concurrent-writers")
            out[f["function"]] = (res.eligible, conc)
    return out


@pytest.fixture(scope="module")
def threads(tmp_path_factory):
    return _project(tmp_path_factory.mktemp("tasks"), TASKS)


@needs_svf_tasks
def test_task_model_decides_concurrent_writers(threads):
    proj = load_project(threads)
    ctx = RecipeContext(proj, load_inventory(proj))
    tm = ctx.tasks("clang", "app")
    assert tm.problems == []
    assert {c.name: c.kind for c in tm.contexts.values()} == {"main": "task", "worker": "task", "filler": "task"}
    v = _verdicts(threads)
    eligible, conc = v["scale"]
    assert conc.status == "established" and eligible, conc
    assert any("runs only in task main" in e for e in conc.evidence)
    assert any("not held by any other context's pointers: local" in e for e in conc.evidence)
    _, conc = v["peek"]
    assert conc.status == "violated" and any("task worker may write shared_counter" in e for e in conc.evidence)
    _, conc = v["twice"]
    assert conc.status == "violated" and any("task filler may write handoff" in e for e in conc.evidence)


@needs_svf_tasks
def test_declarations_are_checked(threads):
    # a thread the declaration forgets: the model is incomplete, so nothing is established
    _set_concurrency(threads, {**TASKS, "tasks": TASKS["tasks"][:2]})
    try:
        proj = load_project(threads)
        tm = RecipeContext(proj, load_inventory(proj)).tasks("clang", "app")
        assert any("starts filler(), which is not a declared task" in p for p in tm.problems)
        eligible, conc = _verdicts(threads)["scale"]
        assert not eligible and conc.status == "unresolved"
        # 'single-threaded' is contradicted by the pthread_create calls
        _set_concurrency(threads, "single-threaded")
        _, conc = _verdicts(threads)["scale"]
        assert conc.status == "violated" and any("pthread_create() starts a thread" in e for e in conc.evidence)
    finally:
        _set_concurrency(threads, TASKS)


def test_declared_indirect_call_targets(tmp_path):
    """A declared resolution is refused when the program calls the function that would install more targets."""
    from weaver.flow.models import Models
    from weaver.flow.program import Program
    from weaver.flow.tasks import TaskModel

    def fn(name, calls=(), indirect=()):
        return {
            "name": name, "file": "a.c", "line": 1, "end_line": 9, "static": False, "units": ["u"],
            "calls": [{"callee": c, "site": {"file": "a.c", "line": 2}, "args": []} for c in calls],
            "indirect_calls": [{"site": {"file": "a.c", "line": ln, "col": 5}, "args": []} for ln in indirect],
            "named_writes": [], "pointer_writes": [], "function_refs": [], "asm": [],
        }  # fmt: skip

    inv = {"functions": {"a.c::main": fn("main", indirect=[3]), "a.c::cb": fn("cb"), "a.c::reg": fn("reg")}}
    prog = Program(inv, Models({}))
    spec = {"model": "tasks", "tasks": [{"name": "main", "entry": "main"}],
            "indirect_calls": [{"at": "a.c:3", "targets": ["cb"]}]}  # fmt: skip
    tm = TaskModel(prog, None, "p", spec)
    assert tm.problems == [] and "a.c::cb" in tm.contexts["main"].funcs
    assert tm.contexts["main"].unresolved == []
    spec["indirect_calls"][0].update(targets=[], unless_called=["reg"])
    inv["functions"]["a.c::main"]["calls"].append({"callee": "reg", "site": {"file": "a.c", "line": 4}, "args": []})
    tm = TaskModel(Program(inv, Models({})), None, "p", spec)
    assert any("but the program calls reg" in p for p in tm.problems)
    assert tm.contexts["main"].unresolved  # the declaration is not used
