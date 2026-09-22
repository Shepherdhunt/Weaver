import json
import os
import subprocess
import sys

from weaver.capture.compdb import (
    expand_response_files,
    load_compdb,
    quote_gnu_response,
    split_gnu_response,
)
from weaver.toolchain.collect import parse_depfile
from weaver.toolchain.options import parse_driver_args
from weaver.toolchain.sanitize import sanitize


def test_response_file_roundtrip():
    args = ['-DMSG="hi there"', "-I/a b/inc", "", "back\\slash", "it's"]
    assert split_gnu_response(quote_gnu_response(args)) == args


def test_expand_nested_response_files(tmp_path):
    (tmp_path / "inner.rsp").write_text("-DINNER=1\n")
    (tmp_path / "outer.rsp").write_text("-I'inc dir' @inner.rsp -c\n")
    argv, rfs = expand_response_files(["cc", "@outer.rsp", "x.c", "@missing.rsp"], str(tmp_path))
    assert argv == ["cc", "-Iinc dir", "-DINNER=1", "-c", "x.c", "@missing.rsp"]
    assert [os.path.basename(r.path) for r in rfs] == ["outer.rsp", "inner.rsp"]


def test_load_compdb_command_and_arguments(tmp_path):
    db = [
        {"directory": str(tmp_path), "file": "a.c", "command": "cc -DX='a b' -c a.c -o a.o"},
        {"directory": str(tmp_path), "file": "a.c", "arguments": ["cc", "-DY", "-c", "a.c"]},
    ]
    (tmp_path / "compile_commands.json").write_text(json.dumps(db))
    cmds = load_compdb(tmp_path / "compile_commands.json")
    assert cmds[0].expanded == ["cc", "-DX=a b", "-c", "a.c", "-o", "a.o"]
    assert cmds[0].file == str(tmp_path / "a.c")
    # one file, two configurations -> two distinct units
    assert cmds[0].unit_id("p") != cmds[1].unit_id("p")


def test_sanitize_logs_every_removal(tmp_path):
    db = [
        {
            "directory": str(tmp_path),
            "file": "src/a.c",
            "arguments": [
                "gcc",
                "-O2",
                "-Iinc",
                "-MD",
                "-MF",
                "a.d",
                "-c",
                "src/a.c",
                "-o",
                "a.o",
                "-flto",
                "-Wl,--gc-sections",
                "-lm",
                "-DX=1",
                "-save-temps",
            ],
        }
    ]
    (tmp_path / "cc.json").write_text(json.dumps(db))
    san = sanitize(load_compdb(tmp_path / "cc.json")[0])
    assert san.options == ["-O2", "-Iinc", "-DX=1"]
    removed = {tuple(r.option): r.reason for r in san.removed}
    assert ("-MF", "a.d") in removed and ("-o", "a.o") in removed and ("src/a.c",) in removed
    assert "LTO" in removed[("-flto",)]
    assert removed[("-lm",)] == "link-only option"


def test_parse_driver_args_languages():
    # -x applies to every later input until '-x none' (GCC semantics)
    pa = parse_driver_args(["cc", "-x", "c", "foo.inc", "bar.S", "-x", "none", "-c", "b.S", "-o", "out.o"])
    assert pa.sources == ["foo.inc", "bar.S"] and pa.asm_sources == ["b.S"] and pa.action == "-c"


def test_depfile_parsing_handles_escapes(tmp_path):
    text = "a.o: src/a.c /usr/include/my\\ header.h \\\n  inc/b.h\n"
    deps = parse_depfile(text, str(tmp_path))
    assert deps == [str(tmp_path / "src/a.c"), "/usr/include/my header.h", str(tmp_path / "inc/b.h")]


def test_wrapper_preserves_exit_status_and_records(tmp_path):
    log = tmp_path / "log.jsonl"
    tool = tmp_path / "tool.sh"
    tool.write_text("#!/bin/sh\necho out; exit 3\n")
    tool.chmod(0o755)
    r = subprocess.run(
        [sys.executable, "-m", "weaver.capture.wrapper", "--real", str(tool), "--log", str(log), "--", "-c", "x.c"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert r.returncode == 3 and r.stdout == "out\n"
    rec = json.loads(log.read_text())
    assert rec["argv"] == [str(tool), "-c", "x.c"] and rec["returncode"] == 3
