"""Link model (images, archives, programs), capture exclusions, effect-model packs, macro token
comparison, the borrowed-pointer check and the rejection report."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import build_project, needs_clang

from weaver.analysis.borrow import check_borrow
from weaver.capture.shims import finalize, make_shim
from weaver.config import load_project
from weaver.errors import ConfigError
from weaver.fidelity import _tokens
from weaver.flow.models import Models, _entries, load_models
from weaver.link import link_model

HAVE_TOOLS = all(shutil.which(t) for t in ("gcc", "ar", "nm"))
needs_binutils = pytest.mark.skipif(not HAVE_TOOLS, reason="gcc, ar and nm required")

SOURCES = {
    "lib/util.c": "int util_add(int *a) { return *a + 1; }\n",
    "lib/unused.c": "int util_unused(void) { return 0; }\n",
    "plug/plugin.c": "int util_add(int *a);\nint plugin_entry(int *x) { return util_add(x); }\n"
    "int plugin_helper(int *y) { return *y; }\n",
    "app/main.c": "int util_add(int *a);\nint main(void) { int k = 1; return util_add(&k) - 2; }\n",
    "CMakeFiles/CMakeScratch/TryCompile-1/probe.c": "int main(void) { return 0; }\n",
    "gen/harness.c": "int harness(void) { return 0; }\n",
}
BUILD = """set -e
mkdir -p out
$CC -c lib/util.c -o out/util.o
$CC -c lib/unused.c -o out/unused.o
ar rcs out/libutil.a out/util.o out/unused.o
$CC -fPIC -c plug/plugin.c -o out/plugin.o
$CC -shared -o out/plugin.so out/plugin.o out/libutil.a
$CC -c app/main.c -o out/main.o
$CC -o out/app out/main.o out/libutil.a
$CC -c CMakeFiles/CMakeScratch/TryCompile-1/probe.c -o out/probe.o
$CC -c gen/harness.c -o out/harness.o
"""


def _mini(tmp: Path, programs: list | None = None) -> Path:
    root = tmp / "mini"
    for rel, text in SOURCES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    cap = root / ".weaver" / "capture"
    shim = make_shim(cap / "shims", "cc", shutil.which("gcc"), cap / "log.jsonl")
    subprocess.run(["sh", "-c", BUILD], cwd=root, env={"CC": str(shim), "PATH": "/usr/bin:/bin"}, check=True)
    res = finalize(cap / "log.jsonl", root / "build", exclude=["gen/*"], root=str(root))
    cfg = {
        "schema": "weaver.project/1",
        "project": {"name": "mini"},
        "profiles": [{"id": "gcc", "compile_commands": "build/compile_commands.json"}],
    }
    if programs:
        cfg["programs"] = programs
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg))
    return root, res


@needs_binutils
def test_capture_excludes_probes_and_configured_paths(tmp_path):
    root, res = _mini(tmp_path)
    files = sorted(Path(e["file"]).as_posix() for e in json.loads((root / "build/compile_commands.json").read_text()))
    assert files == ["app/main.c", "lib/unused.c", "lib/util.c", "plug/plugin.c"]
    other = json.loads((root / "build/other-invocations.json").read_text())
    matched = sorted(m for x in other["excluded"] for m in x["matched"])
    assert matched == ["*/CMakeFiles/CMakeScratch/*", "gen/*"]
    assert res["excluded_invocations"] == 2


@needs_binutils
def test_link_model_images_archives_and_programs(tmp_path):
    root, _ = _mini(tmp_path)
    proj = load_project(root)
    lm = link_model(proj, proj.profiles[0])
    unit = {Path(f).name: u for u, f in lm.unit_file.items()}
    assert set(lm.images) == {"plugin.so", "app"}
    app, plug = lm.images["app"], lm.images["plugin.so"]
    assert app.kind == "executable" and plug.kind == "shared"
    # the traced link loads only the archive member that resolves a symbol
    assert unit["util.c"] in app.units and unit["unused.c"] not in app.units
    assert {"plugin_entry", "plugin_helper"} <= plug.exports
    progs = {p.name: p for p in lm.programs}
    assert progs["app"].closed and progs["app"].entry_points == ["main"]
    assert not progs["plugin.so"].closed  # an unconfigured shared object is an open program

    # util.c is linked into the app and, through the archive, into plugin.so, which exports it
    (status, why), *rest = lm.external_callers("util_add", [unit["util.c"]], False)
    assert not rest and status == "unresolved" and why.startswith("plugin.so: util_add is exported")
    assert lm.external_callers("util_add", [unit["util.c"]], True) == []  # a static function has no outside callers
    (status, why), *_ = lm.external_callers("main", [unit["main.c"]], False)
    assert status == "violated" and "entry point" in why
    (status, why), *_ = lm.external_callers("plugin_helper", [unit["plugin.c"]], False)
    assert status == "unresolved" and "exported from a shared object" in why

    # declared as one closed program, only the declared entry points remain callable from outside
    root2, _ = _mini(
        tmp_path / "b",
        [{"name": "sys", "images": ["app", "plugin.so"], "entry_points": ["main", "plugin_entry"]}],
    )
    proj2 = load_project(root2)
    lm2 = link_model(proj2, proj2.profiles[0])
    unit2 = {Path(f).name: u for u, f in lm2.unit_file.items()}
    assert [p.name for p in lm2.programs] == ["sys"]
    assert lm2.external_callers("plugin_helper", [unit2["plugin.c"]], False) == []
    assert lm2.external_callers("util_add", [unit2["util.c"]], False) == []
    assert lm2.external_callers("plugin_entry", [unit2["plugin.c"]], False)[0][0] == "violated"


def test_programs_config_is_validated(tmp_path):
    (tmp_path / "weaver.yaml").write_text(
        yaml.safe_dump({"schema": "weaver.project/1", "profiles": [], "programs": [{"name": "x"}]})
    )
    with pytest.raises(ConfigError):
        load_project(tmp_path)


def test_effect_model_packs(tmp_path):
    cfg = {"schema": "weaver.project/1", "profiles": [], "flow": {"models": ["builtin:cfs"]}}
    (tmp_path / "weaver.yaml").write_text(yaml.safe_dump(cfg))
    m = load_models(load_project(tmp_path))
    # POSIX pack is always loaded
    assert m.lookup("pthread_create").retains == [3] and m.lookup("pthread_create").calls_back
    assert m.lookup("memcpy") is not None
    # the cFS pack: boundary models judged by their reviewed contract, not the analysed body
    rb = m.lookup("CFE_SB_ReceiveBuffer")
    assert m.is_boundary("CFE_SB_ReceiveBuffer") and rb.writes == [0]
    assert m.lookup("CFE_SB_TransmitMsg").writes == []
    assert m.lookup("OS_TimerCreate").writes == [0, 2]
    assert rb.owned("cfe/modules/sb/fsw/src/cfe_sb_api.c") and not rb.owned("apps/sample_app/fsw/src/sample_app.c")

    cfg["flow"]["models"] = ["builtin:nope"]
    (tmp_path / "weaver.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError):
        load_project(tmp_path)
    with pytest.raises(ConfigError):
        _entries({"f": {"writes_owned": True}}, "test")  # owned-state writes need the pack's 'owns'


def test_macro_tokens_ignore_spacing_and_parameter_names():
    assert _tokens("(fn)(TYPE,MEMBER) __builtin_offsetof (TYPE, MEMBER)") == _tokens(
        "(fn)(t,d) __builtin_offsetof(t, d)"
    )
    assert _tokens("((void *)0)") == _tokens("((void*)0)")
    assert _tokens("(fn)(a,b) a ## b") == _tokens("(fn)(x,y) x##y")
    assert _tokens("(fn)(a,b) a - b") != _tokens("(fn)(a,b) b - a")  # order matters
    assert _tokens("(fn)(a,...) f(a, __VA_ARGS__)") != _tokens("(fn)(a) f(a, __VA_ARGS__)")
    assert _tokens(None) is None


# -- borrowed-pointer check on a synthetic inventory ---------------------------------------------------


def _f(fid, kind, function, name, uses, **extra):
    return {
        "id": fid,
        "kind": kind,
        "function": function,
        "name": name,
        "file": "a.c",
        "line": 1,
        "uses": uses,
        **extra,
    }


def _use(kind, line, access=None, **detail):
    return {"kind": kind, "line": line, "access": access, "detail": detail}


class _Prog:
    """The two things check_borrow asks of a Program: effect models and definition lookup."""

    def __init__(self, analysed, models):
        self.analysed, self.models = set(analysed), models

    def resolve(self, key, callee):
        return [f"a.c::{callee}"] if callee in self.analysed else []


MODELS = Models(
    _entries(
        {
            "log_event": {"writes": [], "calls_back": False},
            "keep": {"writes": [], "calls_back": False, "retains": [0]},
            "fill": {"writes": [0], "calls_back": False},
        },
        "test",
    )
)


def test_borrow_check_follows_the_pointer_interprocedurally():
    buf = _f(
        "P0",
        "local",
        "Main",
        "buf",
        [
            _use("address-of-pointer", 3),  # ReceiveBuffer(&buf, ...)
            _use("arrow", 4, "member-read"),
            _use("call-arg", 5, callee="Handle", arg=0),
            _use("call-arg", 6, callee="log_event", arg=0),
            _use("null-test", 7),
        ],
    )
    msg = _f(
        "P1",
        "parameter",
        "Handle",
        "msg",
        [_use("cast", 10, sink={"kind": "copy", "into": {"target": "variable", "name": "cmd"}})],
        param_index=0,
    )
    cmd = _f("P2", "local", "Handle", "cmd", [_use("arrow", 11, "member-read")])
    inv = {"findings": [buf, msg, cmd]}
    prog = _Prog({"Handle"}, MODELS)
    res = check_borrow(inv, prog, buf)
    assert res.status == "held", res.reasons
    assert [v["name"] for v in res.visited] == ["buf", "msg", "cmd"]

    def verdict(*extra_uses, analysed=("Handle",)):
        cmd2 = {**cmd, "uses": [*cmd["uses"], *extra_uses]}
        return check_borrow({"findings": [buf, msg, cmd2]}, _Prog(analysed, MODELS), buf)

    assert verdict(_use("arrow", 12, "member-write")).status == "violated"
    assert verdict(_use("copy", 12, into={"target": "global", "name": "Saved"})).status == "violated"
    assert verdict(_use("return", 12)).status == "violated"
    assert verdict(_use("call-arg", 12, callee="keep", arg=0)).status == "violated"  # retained after return
    assert verdict(_use("call-arg", 12, callee="fill", arg=0)).status == "violated"  # written by the callee
    assert verdict(_use("call-arg", 12, callee="mystery", arg=0)).status == "unknown"  # no model
    assert verdict(_use("call-arg", 12, callee=None, arg=0)).status == "unknown"  # indirect call


# -- report ------------------------------------------------------------------------------------------


@needs_clang
def test_report_on_the_demo(tmp_path):
    from weaver.analysis.inventory import build_inventory
    from weaver.report import build_report, render_markdown
    from weaver.toolchain.collect import collect_profile

    root = build_project(tmp_path, [{"id": "clang", "cc": "clang"}])
    proj = load_project(root)
    collect_profile(proj, proj.profiles[0])
    build_inventory(proj)
    rep = build_report(proj, ["src/alias.c"])
    assert rep["scope"] == ["src/alias.c"] and rep["evidence"]["units_in_scope"] == 1
    assert all(c["file"] == "src/alias.c" for c in rep["candidates"])
    la = rep["recipes"]["local-alias"]
    assert la["applicable"] == la["eligible"] + sum(
        1 for c in rep["candidates"] if c["recipe"] == "local-alias" and not c["eligible"]
    )
    md = render_markdown(rep)
    for heading in ("# Weaver report: demo (src/alias.c)", "## Evidence", "## Recipes", "### Eligible", "### Blocked"):
        assert heading in md
