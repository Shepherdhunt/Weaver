from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

FIXTURE = Path(__file__).parent / "fixtures" / "demo"

HAVE_CLANG = shutil.which("clang") is not None
HAVE_GCC = shutil.which("gcc") is not None
HAVE_MAKE = shutil.which("make") is not None

needs_clang = pytest.mark.skipif(not (HAVE_CLANG and HAVE_MAKE), reason="clang and make required")
needs_gcc = pytest.mark.skipif(not (HAVE_CLANG and HAVE_GCC and HAVE_MAKE), reason="gcc, clang and make required")


def _have_svf() -> bool:
    import importlib.util

    spec = importlib.util.find_spec("pysvf")
    if spec is None or not spec.origin:
        return False
    return (Path(spec.origin).resolve().parent / "SVF" / "Release-build" / "bin" / "wpa").exists()


HAVE_SVF = _have_svf()
needs_svf = pytest.mark.skipif(not (HAVE_CLANG and HAVE_MAKE and HAVE_SVF), reason="clang, make and pysvf required")

# Declarations the interface recipe needs beyond the default project settings.
SINGLE_THREADED = {"behaviors": ["stdout", "exit-status"], "concurrency": "single-threaded"}
RAND_MODEL = {
    "externals": {
        "rand": {"writes": [], "calls_back": False, "assumptions": ["reviewed: rand() only updates its own state"]}
    }
}


def build_project(tmp: Path, profiles: list[dict], extra: dict | None = None) -> Path:
    """Copy the demo project, capture each profile's build through shims, write weaver.yaml.

    Each profile dict: {id, cc, cflags?, secondary?, validation?}.
    """
    from weaver.capture.shims import finalize, make_shim

    root = tmp / "proj"
    shutil.copytree(FIXTURE, root)
    cfg_profiles = []
    for p in profiles:
        cap = root / ".weaver" / "capture" / p["id"]
        shim = make_shim(cap / "shims", "cc", p["cc"], cap / "log.jsonl")
        build = f"build/{p['id']}"
        subprocess.run(
            ["make", "-s", f"CC={shim}", f"CFLAGS={p.get('cflags', '-O2 -std=c11')}", f"BUILD={build}"],
            cwd=root,
            check=True,
        )
        finalize(cap / "log.jsonl", root / build)
        entry = {
            "id": p["id"],
            "compile_commands": f"{build}/compile_commands.json",
            "target": {"architecture": "host"},
            "platform": {"runtime_mode": "process"},
        }
        if p.get("secondary"):
            entry["secondary_frontend"] = p["secondary"]
        if p.get("validation"):
            entry["validation"] = p["validation"]
        cfg_profiles.append(entry)
    cfg = {
        "schema": "weaver.project/1",
        "project": {"name": "demo", "workspace_exclude": [".git", "build"]},
        "preservation": {"behaviors": ["stdout", "exit-status"]},
        "profiles": cfg_profiles,
    }
    if extra:
        cfg.update(extra)
    (root / "weaver.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return root


def validation_for(cc: str, cflags: str = "-O2 -std=c11") -> dict:
    return {
        "build": {
            "run": ["make", "-s", "-C", "{workspace}", f"CC={cc}", f"CFLAGS={cflags}", "BUILD=out"],
            "cwd": "{workspace}",
        },
        "compare": [{"name": "demo-stdout", "run": ["./out/demo"], "cwd": "{workspace}"}],
    }


def run_cli(root: Path, *args: str) -> int:
    from weaver.cli import main

    return main(["-C", str(root), *args])
