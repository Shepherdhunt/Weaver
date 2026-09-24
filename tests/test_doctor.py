"""weaver doctor: missing tools and broken projects are found before a run, with a fix for each."""

from __future__ import annotations

import os
import sys

import yaml
from conftest import SINGLE_THREADED, build_project, needs_gcc, run_cli, validation_for

from weaver import doctor as doc
from weaver.config import load_project


def _by_name(res: dict, section_prefix: str = "") -> dict[str, dict]:
    return {c["name"]: c for c in res["checks"] if c["section"].startswith(section_prefix)}


def test_a_machine_without_compilers(tmp_path, monkeypatch):
    bare = tmp_path / "bin"
    bare.mkdir()
    (bare / "sh").symlink_to("/bin/sh")
    monkeypatch.setenv("PATH", str(bare))
    res = doc.doctor(None)
    checks = _by_name(res)
    assert checks["clang"]["status"] == "fail" and "reads C through Clang" in checks["clang"]["detail"]
    assert checks["clang"]["fix"]  # an install command or instructions
    assert checks["gcc"]["status"] == "info"  # only GCC projects need it
    assert checks["binutils"]["status"] == "warn" and checks["git"]["status"] == "warn"
    assert not res["ok"] and res["counts"]["fail"] >= 1
    text = doc.render(res)
    assert "fail  clang" in text and "fix: " in text and "problem(s)" in text


def test_install_hints_follow_the_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(doc, "_os_release", lambda: {"ID": "ubuntu", "ID_LIKE": "debian"})
    assert doc.install_hint("clang-rt", major="18") == "sudo apt-get install libclang-rt-18-dev"
    monkeypatch.setattr(doc, "_os_release", lambda: {"ID": "rocky", "ID_LIKE": "rhel centos fedora"})
    assert doc.install_hint("ninja") == "sudo dnf install ninja-build"
    monkeypatch.setattr(doc, "_os_release", lambda: {"ID": "arch"})
    assert doc.install_hint("clang") == "install clang with your system's package manager"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doc.install_hint("clang") == "brew install llvm"


def test_a_project_that_cannot_work(tmp_path):
    (tmp_path / "weaver.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "weaver.project/1",
                "project": {"name": "broken"},
                "acceptance": {"require": ["compile", "mechanical-recheck", "testing"]},
                "ai": {"enabled": True, "provider": "openai-compatible", "base_url": "https://example.invalid/v1"},
                "profiles": [{"id": "gcc", "compile_commands": "build/compile_commands.json"}],
            }
        )
    )
    res = doc.doctor(load_project(tmp_path), machine=False)
    prof, proj = _by_name(res, "Project broken, profile gcc"), _by_name(res, "Project broken")
    assert prof["compile commands"]["status"] == "fail" and "no capture command" in prof["compile commands"]["detail"]
    assert prof["validation"]["status"] == "warn" and "compile-only" in prof["validation"]["detail"]
    assert proj["acceptance"]["status"] == "fail" and "stay provisional" in proj["acceptance"]["detail"]
    assert proj["concurrency"]["status"] == "info" and proj["analysis"]["detail"] == "not analysed yet"
    if not any(k in os.environ for k in ("OPENAI_API_KEY", "WEAVER_AI_KEY")):
        assert proj["AI key"]["status"] in ("warn", "ok")  # a key stored for this user also counts
    assert not res["ok"] and not any(c["section"] == "Compilers" for c in res["checks"])


def test_the_cli_reports_a_broken_configuration(tmp_path, capsys):
    (tmp_path / "weaver.yaml").write_text("schema: weaver.project/1\nprofiles: [{id: x}]\n")
    assert run_cli(tmp_path, "doctor", "--json") == 1
    import json

    res = json.loads(capsys.readouterr().out)
    bad = [c for c in res["checks"] if c["section"] == "Project"]
    assert bad and bad[0]["status"] == "fail" and "needs 'id' and 'compile_commands'" in bad[0]["detail"]


@needs_gcc
def test_a_gcc_project_ready_to_go_and_one_without_an_ast_reader(tmp_path):
    root = build_project(
        tmp_path, [{"id": "gcc", "cc": "gcc", "validation": validation_for("gcc")}], extra=SINGLE_THREADED
    )
    assert run_cli(root, "refresh", "--no-flow") == 0
    res = doc.doctor(load_project(root), machine=False)
    prof = _by_name(res, "Project demo, profile gcc")
    assert prof["compiler"]["status"] == "ok" and prof["compiler"]["detail"].startswith("gcc ")
    assert prof["AST reader"]["status"] == "ok" and "the clang on PATH" in prof["AST reader"]["detail"]
    assert prof["validation"]["status"] == "ok"
    project_level = [c for c in res["checks"] if "profile" not in c["section"]]
    assert all(c["status"] != "fail" for c in project_level), project_level
    assert next(c for c in project_level if c["name"] == "analysis")["status"] == "ok"

    cfg = yaml.safe_load((root / "weaver.yaml").read_text())
    cfg["profiles"][0]["secondary_frontend"] = False
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    res = doc.doctor(load_project(root), machine=False)
    ast = next(c for c in res["checks"] if c["name"] == "AST reader")
    assert ast["status"] == "fail" and "without pointer facts" in ast["detail"]


def test_the_web_route(tmp_path):
    from test_web import Client

    from weaver.web.server import App

    c = Client(App())
    try:
        res = c.api("doctor")
        assert {"System", "Compilers", "Coverage"} <= {x["section"] for x in res["checks"]}
        assert res["project"] is None and set(res["counts"]) == {"ok", "warn", "fail", "info"}
    finally:
        c.close()
