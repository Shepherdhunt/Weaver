"""Is the build that validation runs the build Weaver analysed?"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import yaml
from conftest import SINGLE_THREADED, build_project, needs_gcc, run_cli, validation_for

from weaver import buildcheck
from weaver.capture.compdb import CompileCommand
from weaver.config import load_project


def _project(tmp: Path) -> Path:
    root = tmp / "p"
    (root / "src").mkdir(parents=True)
    (root / "gen").mkdir()
    for f in ("a.c", "b.c", "t.c"):
        (root / "src" / f).write_text("int x;\n")
    (root / "weaver.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "weaver.project/1",
                "project": {"name": "p", "workspace_exclude": [".git", "out"]},
                "profiles": [{"id": "x", "compile_commands": "cc.json"}],
            }
        )
    )
    return root


def _cmd(root: Path, file: str, *flags: str, cc: str = "/usr/bin/gcc") -> CompileCommand:
    args = [cc, *flags, "-c", file]
    return CompileCommand(directory=str(root), file=str(root / file), arguments=args, expanded=args)


def test_only_flags_that_change_the_code_count(tmp_path):
    root = _project(tmp_path)
    proj = load_project(root)
    roots = [(str(root), "project")]

    def flags(*a: str) -> frozenset[str]:
        return buildcheck.semantic_flags(["cc", *a], str(root / "src"), roots, proj)

    assert flags("-D", "X=1", "-UY") == flags("-DX=1", "-U", "Y")
    assert flags("-std=c89") == flags("-ansi") == flags("-std=iso9899:1990")
    assert flags("-O2", "-g", "-Wall", "-Werror", "-MD", "-MF", "x.d", "-fPIC") == frozenset()
    assert flags("-I..", "-I", "../gen") == {"-I .", "-I gen"}  # relative to the compile's directory
    assert flags("-I../out/include") == frozenset()  # a build directory: differs between any two builds
    assert flags("-I/opt/sdk/include") == {"-I /opt/sdk/include"}  # outside the project: compared as is
    assert flags("-m32", "-funsigned-char", "-fwrapv", "-pthread") == {"-m32", "-funsigned-char", "-fwrapv", "-pthread"}


def test_what_differs_is_named(tmp_path):
    root = _project(tmp_path)
    proj = load_project(root)
    ws = tmp_path / "ws"
    analysed = [_cmd(root, "src/a.c", "-std=c99"), _cmd(root, "src/b.c", "-std=c99")]
    same = [(str(ws), ["/usr/bin/gcc", "-O0", "-std=c99", "-c", "src/a.c"]),
            (str(ws), ["/usr/bin/gcc", "-std=c99", "-c", "src/b.c", "-o", "out/b.o"])]  # fmt: skip
    res = buildcheck.compare(proj, analysed, same, ws)
    assert res["same"] and "compiles the 2 analysed source(s) as analysed" in buildcheck.describe(res)

    other = [
        (str(ws), ["/usr/bin/gcc", "-std=c99", "-DEXTRA", "-c", "src/a.c"]),
        (str(ws), ["/usr/bin/gcc", "-std=c99", "-c", "src/t.c"]),  # never analysed
        (str(ws), ["/usr/bin/gcc", "-c", "out/generated.c"]),  # generated in the build tree: ignored
        (str(ws / "out/CMakeFiles/CMakeScratch/TryCompile-1"), ["/usr/bin/gcc", "-c", "probe.c"]),
    ]  # b.c is never compiled
    res = buildcheck.compare(proj, analysed, other, ws)
    assert not res["same"] and res["unanalysed"] == ["src/t.c"] and res["uncompiled"] == ["src/b.c"]
    assert res["differences"] == [
        {"validation_only": ["-DEXTRA"], "analysed_only": [], "compiler": "", "files": ["src/a.c"]}
    ]
    text = buildcheck.describe(res)
    assert "compiles 1 source(s) no analysed configuration compiled (src/t.c)" in text
    assert "-DEXTRA only in the validation build" in text and "never compiles 1 analysed source(s) (src/b.c)" in text
    assert "not observed" in buildcheck.describe(buildcheck.compare(proj, analysed, [], ws))


@needs_gcc
def test_validation_records_the_build_configuration(tmp_path, capsys):
    # the validation build defines EXTRA_CHECKS, and also compiles a source no analysed build compiles
    val = validation_for("gcc", "-O2 -std=c11 -DEXTRA_CHECKS=1")
    val["build"]["run"] = shlex.join(val["build"]["run"]) + " && gcc -c {workspace}/src/selftest.c -o /dev/null"
    root = build_project(tmp_path, [{"id": "gcc", "cc": "gcc", "validation": val}], extra=SINGLE_THREADED)
    (root / "src/selftest.c").write_text("int selftest(void) { return 0; }\n")
    assert run_cli(root, "refresh", "--no-flow") == 0
    capsys.readouterr()

    assert run_cli(root, "doctor", "--build", "--json") in (0, 1)
    out = json.loads(capsys.readouterr().out)
    check = next(c for c in out["checks"] if c["name"] == "build configuration")
    assert check["status"] == "warn" and "src/selftest.c" in check["detail"]
    assert "-DEXTRA_CHECKS=1 (5) only in the validation build" in check["detail"]

    from weaver.ledger import Ledger

    led = Ledger(load_project(root))
    diff = '--- a/src/util.c\n+++ b/src/util.c\n@@ -1,1 +1,1 @@\n-#include "util.h"\n+#include "util.h" /* touched */\n'
    txn = led.propose_patch(diff, [], "touch")
    assert txn["state"] == "proposed", txn["history"]
    txn = led.validate(txn["id"])
    rec = next(r for r in txn["validation"]["records"] if r["kind"] == "configuration")
    assert rec["outcome"] == "not-evaluated" and "src/selftest.c" in rec["detail"]
    assert txn["state"] == "validated"  # a mismatch is reported, not a reason to reject
    cfg = yaml.safe_load((root / "weaver.yaml").read_text())
    cfg["acceptance"] = {"require": ["compile", "mechanical-recheck", "configuration"]}
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    led = Ledger(load_project(root))
    strict = led.validate(led.propose_patch(diff, [], "touch, strictly")["id"])
    assert strict["state"] == "provisional"  # ... unless the policy requires the same configuration

    # the same build as the capture: nothing to report
    cfg["profiles"][0]["validation"] = validation_for("gcc")
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    proj = load_project(root)
    res = buildcheck.check_now(proj, proj.profiles[0])
    assert res["same"], buildcheck.describe(res)
    assert res["files_validated"] == 5

    # a build that names its compiler by absolute path bypasses the shims: said, not guessed
    import shutil

    cfg["profiles"][0]["validation"] = validation_for(shutil.which("gcc"))
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    proj = load_project(root)
    assert "not observed" in buildcheck.describe(buildcheck.check_now(proj, proj.profiles[0]))
