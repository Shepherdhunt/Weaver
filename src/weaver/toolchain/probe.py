"""Capability probes (compiler plan §2).

A successful help-option lookup is insufficient: each capability is exercised
on a small fixture compiled with the profile's own preserved options, and the
artifact is checked for the expected pointer types, records and functions.  A
failed probe is classified as *unavailable* only when the tool rejected the
interface itself; other failures stay *unverified*, because they may reflect
missing SDK configuration rather than an unsupported feature.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from weaver.capture.compdb import load_compdb
from weaver.capture.toolid import identify
from weaver.config import Profile, Project
from weaver.evidence import CapabilityStatus
from weaver.store import Store
from weaver.toolchain.collect import read_macros
from weaver.toolchain.recipes import RECIPES
from weaver.toolchain.sanitize import sanitize
from weaver.toolchain.translate import translate_gcc_to_clang
from weaver.util import now_iso, run, write_json

FIXTURE = """\
#include <stddef.h>
struct weaver_probe_rec { char c; int *ip; void (*fp)(int); };
typedef int *weaver_probe_iptr;
static int weaver_probe_target;
int weaver_probe_fn(struct weaver_probe_rec *r, int v) {
    int *p = &weaver_probe_target;
    weaver_probe_iptr q = p;
    *q = v;
    if (r->fp) r->fp(*p);
    return (int)sizeof(*r) + (int)offsetof(struct weaver_probe_rec, ip);
}
"""

REJECTED_INTERFACE = re.compile(
    r"unknown argument|unrecognized command[- ]line option|unrecognized option|unsupported option|"
    r"unknown option|invalid option|unsupported argument|not supported for target|"
    r"unrecognized debug output",
    re.I,
)

# Documented-but-unprobed capabilities per family, from the compiler plan's matrix.
DOCUMENTED: dict[str, dict[str, str]] = {
    "clang": {
        "debug_objects": "https://clang.llvm.org/docs/CommandGuide/clang.html",
        "linker_map": "https://sourceware.org/binutils/docs/ld/Options.html (GNU ld only)",
    },
    "gcc": {
        "debug_objects": "https://gcc.gnu.org/onlinedocs/gcc/Debugging-Options.html",
        "linker_map": "https://sourceware.org/binutils/docs/ld/Options.html (GNU ld only)",
        "gcc_plugins": "https://gcc.gnu.org/onlinedocs/gccint/Plugins.html (needs a plugin-enabled build)",
    },
}


def _probe_one(name: str, compiler: str, options: list[str], cwd: str, out: Path, stem: str) -> dict[str, Any]:
    recipe = RECIPES[name]
    src = out / "probe.c"
    inv = recipe.build(compiler, options, str(src), out, stem)
    res = run(inv.argv, cwd=cwd, stdout_path=inv.stdout_to, timeout=600)
    stderr = res.stderr.decode(errors="replace")
    outputs = list(inv.outputs)
    for g in inv.collect_globs:
        outputs.extend(sorted(out.glob(g)))
    if not res.ok:
        status = CapabilityStatus.UNAVAILABLE if REJECTED_INTERFACE.search(stderr) else CapabilityStatus.UNVERIFIED
        detail = (
            f"exit {res.returncode}: " + stderr.strip().splitlines()[0][:300]
            if stderr.strip()
            else f"exit {res.returncode}"
        )
        if status is CapabilityStatus.UNVERIFIED:
            detail += " (may reflect SDK/configuration, not an unsupported feature)"
        return {"status": status.value, "detail": detail, "argv": inv.argv}
    ok, detail = recipe.probe_check(outputs, stderr)
    return {
        "status": (CapabilityStatus.PROBE_PASSED if ok else CapabilityStatus.UNVERIFIED).value,
        "detail": detail if ok else f"artifact did not validate: {detail}",
        "argv": inv.argv,
        "frontend_interface": recipe.frontend_interface,
        "doc": recipe.doc,
    }


def probe_profile(project: Project, profile: Profile) -> dict[str, Any]:
    store = Store(project.state_dir)
    cmds = load_compdb(profile.compile_commands)
    if not cmds:
        return {"profile": profile.id, "error": "compilation database is empty"}
    rep = cmds[0]
    tool = identify(rep.compiler, rep.directory)
    san = sanitize(rep)
    out = store.probe_dir(profile.id)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    (out / "probe.c").write_text(FIXTURE)

    result: dict[str, Any] = {
        "profile": profile.id,
        "probed_at": now_iso(),
        "representative_unit": rep.file,
        "production_tool": tool.to_json(),
        "capabilities": {},
        "secondary": None,
    }
    if tool.family == "unknown":
        for name in RECIPES:
            result["capabilities"][name] = {
                "status": CapabilityStatus.UNVERIFIED.value,
                "detail": "no adapter for this compiler family; consult the installed version's manuals",
            }
    else:
        for name, r in RECIPES.items():
            if tool.family in r.families:
                result["capabilities"][name] = _probe_one(name, rep.compiler, san.options, rep.directory, out, "prod")
        for cap, doc in DOCUMENTED.get(tool.family, {}).items():
            result["capabilities"][cap] = {"status": CapabilityStatus.DOCUMENTED.value, "detail": doc}

    sec = profile.secondary_frontend if tool.family != "clang" else None
    if sec:
        stool = identify(sec.compiler)
        if stool.path is None:
            result["secondary"] = {"error": f"secondary frontend {sec.compiler!r} not found"}
        else:
            m = run(
                [rep.compiler, *san.options, "-E", "-dM", str(out / "probe.c"), "-o", str(out / "prod.m.txt")],
                cwd=rep.directory,
                timeout=600,
            )
            macros = read_macros(out / "prod.m.txt") if m.ok else {}
            tr = translate_gcc_to_clang(san.options, tool, macros, stool.path, rep.directory)
            caps = {}
            for name in ("preprocess", "macros", "deps", "ast_json", "llvm_ir", "bitcode", "record_layouts"):
                caps[name] = _probe_one(name, stool.path, tr.options + sec.extra_args, rep.directory, out, "sec")
            result["secondary"] = {"tool": stool.to_json(), "translation": tr.log, "capabilities": caps}

    write_json(store.profile_dir(profile.id) / "capabilities.json", result)
    return result
