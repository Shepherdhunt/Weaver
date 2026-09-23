"""Flow evidence from the production GCC: interprocedural points-to (``-fipa-pta``) at link time.

For GCC profiles the SVF job analyses bitcode from the secondary Clang.  This job
asks the production compiler itself.  Every unit linked into a program's images
is recompiled with its production command plus LTO and ``-fipa-pta``; each
image's captured link is replayed with LTO, and GCC's whole-image points-to
solution is read from its ``pta2`` dump:

* ``F.argN``   what parameter N of F may point to (union over all call sites),
* ``F.clobber`` what a call to F may write, including callees and resolved
  indirect calls,
* ``ESCAPED`` / ``NONLOCAL`` the memory external code can reach.

Deviations from the production command are recorded: the optimization level is
raised to ``-O1`` (IPA passes do not run at ``-O0``), inlining and IPA passes
that rewrite parameters are disabled so functions keep their source shape, and
``-Werror`` is dropped.  For images of a declared closed program the replay
exports only the program's entry points and the symbols its other images import,
so GCC knows which functions no outside code can call.

Names in GCC's sets are declaration names; two variables of the same name are
treated as one, which can only add apparent overlaps (conservative).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from weaver.capture.compdb import load_compdb
from weaver.config import Profile, Project
from weaver.store import Store
from weaver.toolchain.collect import MANIFEST
from weaver.toolchain.sanitize import sanitize
from weaver.util import now_iso, read_json, run, sha256_file, short_hash, write_json

GCC_PTA_SCHEMA = "weaver.gcc-pta/1"
PTA_FLAGS = [
    "-O1",
    "-g",
    "-flto",
    "-flto-partition=one",
    "-fipa-pta",
    "-fno-inline",
    "-fno-inline-small-functions",
    "-fno-inline-functions-called-once",
    "-fno-ipa-sra",
    "-fno-ipa-cp",
    "-fno-ipa-icf",
    "-fno-ipa-modref",
    "-fno-ipa-pure-const",
    "-fno-ipa-reference",
    "-fno-ipa-bit-cp",
    "-fno-ipa-vrp",
]
SPECIAL = {"NULL", "STRING", "ESCAPED", "NONLOCAL", "ANYTHING", "INTEGER", "READONLY", "STOREDANYTHING", "UNKNOWN"}
_SET = re.compile(r"^(?P<name>\S+) = \{ ?(?P<body>[^}]*?) ?\}(?P<rest>.*)$")
_SYM = re.compile(r"^(?P<name>\S+)/\d+ \((?P<asm>[^)]*)\)")
_CLONE = re.compile(r"\.(lto_priv|constprop|isra|part|cold)\.\d+$")
_RENUMBERING = re.compile(r"\.(constprop|isra|part)\.\d+")


def gcc_profile(project: Project, profile: Profile) -> bool:
    """Whether the profile's production compiler is GCC (units say so in their manifests)."""
    store = Store(project.state_dir)
    for c in load_compdb(profile.compile_commands)[:20]:
        m = store.unit_dir(profile.id, c.unit_id(profile.id)) / MANIFEST
        if m.exists():
            return read_json(m).get("production_tool", {}).get("family") == "gcc"
    return False


def _analysis_options(options: list[str]) -> tuple[list[str], list[str]]:
    kept, dropped = [], []
    for o in options:
        if (
            re.fullmatch(r"-O[0-3sgz]?|-Ofast", o)
            or o == "-Werror"
            or o.startswith("-Werror=")
            or o.startswith("-flto")
        ):
            dropped.append(o)
        else:
            kept.append(o)
    return kept, dropped


def run_gcc_pta(project: Project, profile: Profile, jobs: int | None = None, log: Any = None) -> dict[str, Any]:
    from weaver.link import link_model

    store = Store(project.state_dir)
    out = store.root / "flow" / profile.id / "gcc"
    out.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {"schema": GCC_PTA_SCHEMA, "profile": profile.id, "started_at": now_iso(), "flags": PTA_FLAGS}
    if not gcc_profile(project, profile):
        rec.update(status="unavailable", reason="the production compiler is not GCC")
        write_json(out / "run.json", rec)
        return rec
    lm = link_model(project, profile)
    if lm.manifest is None or not lm.programs:
        rec.update(status="unavailable", reason="no link manifest: GCC's analysis needs the captured links")
        write_json(out / "run.json", rec)
        return rec
    cmds = {c.unit_id(profile.id): c for c in load_compdb(profile.compile_commands)}
    inv_path = store.inventory_path
    inv_units = (
        {u["unit_id"]: u["file_sha256"] for u in read_json(inv_path)["units"] if u["profile"] == profile.id}
        if inv_path.exists()
        else {}
    )

    # 1. LTO objects for every unit in a program ------------------------------------------
    wanted = sorted({u for p in lm.programs for u in p.units})
    objs_dir = out / "objs"
    objs_dir.mkdir(exist_ok=True)

    def compile_one(uid: str) -> tuple[str, dict[str, Any]]:
        c = cmds[uid]
        opts, dropped = _analysis_options(sanitize(c).options)
        obj = objs_dir / f"{uid}.o"
        key = short_hash(c.arguments, PTA_FLAGS, sha256_file(c.file) if os.path.exists(c.file) else "")
        stamp = objs_dir / f"{uid}.key"
        if obj.exists() and stamp.exists() and stamp.read_text() == key:
            return uid, {"object": str(obj), "status": "cached", "dropped": dropped}
        r = run([c.compiler, *opts, *PTA_FLAGS, "-c", c.file, "-o", str(obj)], cwd=c.directory, timeout=1800)
        if not r.ok:
            return uid, {"status": "failed", "stderr": r.stderr_text(2000), "dropped": dropped}
        stamp.write_text(key)
        return uid, {"object": str(obj), "status": "ok", "dropped": dropped}

    with ThreadPoolExecutor(max_workers=jobs or min(8, os.cpu_count() or 2)) as ex:
        objects = dict(ex.map(compile_one, wanted))
    failed = {u: o for u, o in objects.items() if o["status"] == "failed"}
    rec["objects"] = {"compiled": len(objects) - len(failed), "failed": len(failed)}
    rec["deviations"] = sorted({d for o in objects.values() for d in o.get("dropped", [])})

    # 2. one LTO link replay per image of each program ------------------------------------
    rec["programs"] = {}
    for prog in lm.programs:
        prec: dict[str, Any] = {"images": {}, "closed": prog.closed, "configured": prog.configured}
        for name in prog.images:
            img = lm.images[name]
            t0 = time.time()
            res = _replay(project, lm, prog, img, objects, out / "programs" / _safe(prog.name) / _safe(name))
            res["inputs"] = [{"unit": u, "file_sha256": inv_units.get(u)} for u in sorted(img.units)]
            if (out / "programs" / _safe(prog.name) / _safe(name) / "pta.json").exists():
                pta = out / "programs" / _safe(prog.name) / _safe(name) / "pta.json"
                data = read_json(pta)
                data["inputs"] = res["inputs"]
                write_json(pta, data)
            res["duration_s"] = round(time.time() - t0, 2)
            prec["images"][name] = {k: res.get(k) for k in ("status", "reason", "functions", "duration_s", "dump")}
            if log:
                log(
                    f"[{profile.id}] gcc-pta {prog.name}/{name}: {res['status']}"
                    + (f" ({res['reason']})" if res.get("reason") else "")
                )
        st = [i["status"] for i in prec["images"].values()]
        prec["status"] = "complete" if st and all(s == "complete" for s in st) else "incomplete"
        rec["programs"][prog.name] = prec
    st = [p["status"] for p in rec["programs"].values()]
    rec["status"] = "complete" if st and all(s == "complete" for s in st) else "incomplete"
    rec["finished_at"] = now_iso()
    write_json(out / "run.json", rec)
    return rec


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", name)


def _replay(
    project: Project, lm: Any, prog: Any, img: Any, objects: dict[str, dict[str, Any]], out: Path
) -> dict[str, Any]:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    link = img.link
    cwd = link["cwd"]
    by_input: dict[str, str] = {}
    missing: list[str] = []
    for inp in link.get("inputs", []):
        path = os.path.realpath(os.path.join(cwd, inp))
        if path in img.archives:
            members = [objects.get(u, {}).get("object") for u in img.archives[path]]
            if not all(members):
                missing.append(inp)
                continue
            ar = out / "archives" / f"{len(by_input)}_{os.path.basename(path)}"
            ar.parent.mkdir(exist_ok=True)
            gcc_ar = shutil.which("gcc-ar") or shutil.which("ar")
            r = run([gcc_ar, "rcs", str(ar), *members], timeout=600)
            if not r.ok:
                return {"status": "failed", "reason": f"could not build LTO archive for {inp}"}
            by_input[inp] = str(ar)
        else:
            uid = lm.output_unit.get(path)
            o = objects.get(uid or "", {}).get("object")
            if o:
                by_input[inp] = o
            else:
                missing.append(inp)
    argv = [by_input.get(a, a) for a in link["argv"]]
    if "-o" in argv and argv.index("-o") + 1 < len(argv):
        argv[argv.index("-o") + 1] = str(out / img.name)
    else:
        argv += ["-o", str(out / img.name)]
    argv = [a for a in argv if not re.fullmatch(r"-O[0-3sgz]?|-Ofast", a)]
    extra = [*PTA_FLAGS, "-fdump-ipa-pta2-details", "-dumpdir", str(out) + "/"]
    exports = _exports(lm, prog, img)
    if exports is not None:
        vs = out / "exports.map"
        body = "".join(f"    {s};\n" for s in sorted(exports))
        vs.write_text("{\n  global:\n" + body + "  local: *;\n};\n")
        extra.append(f"-Wl,--version-script={vs}")
    r = run([*argv, *extra], cwd=cwd, timeout=3600)
    res: dict[str, Any] = {"argv": [*argv, *extra], "returncode": r.returncode}
    if not r.ok:
        res.update(
            status="failed", reason="LTO link replay failed: " + (r.stderr_text(400).strip().splitlines() or [""])[-1]
        )
        write_json(out / "result.json", res)
        return res
    dumps = sorted(out.glob("*pta2"))
    if not dumps:
        res.update(status="failed", reason="GCC wrote no ipa-pta dump")
        write_json(out / "result.json", res)
        return res
    parsed = parse_pta_dump(dumps[0].read_text(errors="replace"))
    for d in dumps:
        d.unlink()  # large; the parsed solution is kept
    status = "complete" if not missing else "incomplete"
    res.update(
        status=status,
        reason=f"{len(missing)} input(s) not recompiled with LTO: {missing[:5]}" if missing else None,
        functions=len(parsed["functions"]),
        exports=sorted(exports) if exports is not None else None,
        dump=dumps[0].name,
    )
    write_json(out / "pta.json", {**parsed, "image": img.name, "program": prog.name, "result": res})
    write_json(out / "result.json", res)
    return res


def _exports(lm: Any, prog: Any, img: Any) -> set[str] | None:
    """Symbols the image must keep visible: entry points and what other images of the program import."""
    if not (prog.closed and prog.configured):
        return None
    defined = img.exports or set()
    need = set(prog.entry_points) & (defined | {"main"})
    for other in prog.images:
        if other != img.name:
            need |= (lm.images[other].imports or set()) & defined
    return need


# ---------------------------------------------------------------------------
# Dump parsing and queries
# ---------------------------------------------------------------------------


def parse_pta_dump(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    symtab: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    i = 0
    if lines and lines[0].startswith("Symbol table"):
        i = 1
        while i < len(lines):
            ln = lines[i]
            if ln.strip() and not ln.startswith(" ") and not _SYM.match(ln):
                break  # the next section
            m = _SYM.match(ln)
            if m and not ln.startswith(" "):
                cur = symtab.setdefault(
                    _CLONE.sub("", m.group("name")), {"kind": None, "visibility": "", "availability": ""}
                )
            elif cur is not None and ln.startswith("  Type:"):
                cur["kind"] = "variable" if "variable" in ln else "function"
                cur["definition"] = "definition" in ln
            elif cur is not None and ln.startswith("  Visibility:"):
                cur["visibility"] = ln.split(":", 1)[1].strip()
            elif cur is not None and ln.startswith("  Availability:"):
                cur["availability"] = ln.split(":", 1)[1].strip()
            i += 1
    sets: dict[str, list[str]] = {}
    in_sets = False
    for ln in lines[i:]:
        if ln.startswith("Points-to sets"):
            in_sets = True
            continue
        if not in_sets:
            continue
        m = _SET.match(ln)
        if m:
            sets[m.group("name")] = [x for x in m.group("body").split() if x]
        elif ln.strip() == "" or ln.startswith(";;"):
            if sets and ln.startswith(";;"):
                in_sets = False
    # Per symbol first, then merged by source name.  Two symbols share a source name when LTO renamed
    # clashing statics (".lto_priv.N") or when GCC cloned a function; their sets are united, which can
    # only add apparent overlaps.  A clone whose parameters may be renumbered (".isra", ".constprop",
    # ".part") marks the record, and queries about its parameters return unknown.
    raw: dict[str, dict[str, Any]] = {}
    for name, body in sets.items():
        m = re.fullmatch(r"(?P<fn>.+)\.(?P<part>arg(?P<n>\d+)|clobber|use|result)", name)
        if not m:
            continue
        rec = raw.setdefault(m.group("fn"), {"args": {}, "clobber": None, "use": None})
        if m.group("n") is not None:
            rec["args"][m.group("n")] = body  # string keys: the record is stored as JSON
        elif m.group("part") in ("clobber", "use"):
            rec[m.group("part")] = body
    functions: dict[str, dict[str, Any]] = {}
    for sym, rec in raw.items():
        fn = _CLONE.sub("", sym)
        while _CLONE.search(fn):
            fn = _CLONE.sub("", fn)
        cur = functions.get(fn)
        if cur is None:
            functions[fn] = {**rec, "symbols": [sym], "renumbered": bool(_RENUMBERING.search(sym))}
            continue
        cur["symbols"].append(sym)
        cur["renumbered"] = cur["renumbered"] or bool(_RENUMBERING.search(sym))
        for n in set(cur["args"]) | set(rec["args"]):
            cur["args"][n] = sorted(set(cur["args"].get(n, [])) | set(rec["args"].get(n, [])))
        for k in ("clobber", "use"):
            # a missing set on either symbol leaves the merged set unknown
            cur[k] = sorted(set(cur[k]) | set(rec[k])) if cur[k] is not None and rec[k] is not None else None
    variables = sorted(n for n, s in symtab.items() if s.get("kind") == "variable")
    return {
        "functions": functions,
        "escaped": sets.get("ESCAPED", []),
        "nonlocal": sets.get("NONLOCAL", []),
        "variables": variables,
        "symbols": {
            n: {k: v for k, v in s.items() if k in ("kind", "visibility", "availability")} for n, s in symtab.items()
        },
    }


class GccPta:
    """GCC's points-to solution for one image of one program."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.functions = data["functions"]
        self.escaped = set(data.get("escaped", [])) - SPECIAL
        self.variables = set(data.get("variables", []))
        self.image = data.get("image")
        self.program = data.get("program")

    def may_modify(self, function: str, index: int) -> tuple[str, str]:
        """(no | yes | unknown, explanation) for parameter ``index`` of ``function``.

        ``yes`` only for a named object in both the parameter's points-to set and the call's
        clobber set: a write GCC traced to that object.  When the overlap goes through memory
        GCC does not track (``NONLOCAL``, ``ESCAPED``: what external code such as the C library
        may write, which GCC has no model of), the answer is ``unknown``, not ``yes``.  ``STRING``
        (string literals) in both sets is not a write: no defined execution modifies a literal.
        """
        img = f"GCC ({self.image})"
        f = self.functions.get(function)
        if f is None:
            if function not in self.data.get("symbols", {}):
                return "unknown", f"{img}: {function}() is not in the linked image (unreachable from its exports)"
            return "unknown", f"{img}: no points-to record for {function}()"
        if f.get("renumbered"):
            return "unknown", f"{img}: {function}() was cloned with changed parameters ({f['symbols']})"
        arg = f["args"].get(str(index))
        clob = f.get("clobber")
        if arg is None or clob is None:
            return "unknown", f"{img}: {function}() argument {index + 1} has no points-to set"
        a, c = set(arg), set(clob)
        if "ANYTHING" in a:
            return "unknown", f"{img}: argument {index + 1} of {function}() may point to ANYTHING"
        ao, co = a - SPECIAL, c - SPECIAL
        both = ao & co
        # A clobber set containing ESCAPED lists escaped objects by name too: the call may reach
        # external code that could write anything escaped.  Such an overlap is not a traced write.
        via_escape = both & self.escaped if "ESCAPED" in c else set()
        traced = both - via_escape
        if traced:
            return (
                "yes",
                f"{img}: {function}() may write {', '.join(sorted(traced))}, which argument {index + 1} may point to",
            )
        if via_escape:
            return (
                "unknown",
                f"{img}: {function}() may write escaped memory, which GCC does not track further; argument "
                f"{index + 1} may point to escaped {', '.join(sorted(via_escape))}",
            )
        if "ANYTHING" in c:
            return "unknown", f"{img}: what a call to {function}() writes is ANYTHING"
        ext_a = bool(a & {"NONLOCAL", "ESCAPED"})
        if ext_a and (co or c & {"NONLOCAL", "ESCAPED"}):
            return (
                "unknown",
                f"{img}: argument {index + 1} may point to memory outside the image, and the call writes memory",
            )
        if "NONLOCAL" in c:
            hit = sorted(o for o in ao if o in self.variables or o.startswith("HEAP"))
            if hit:
                return (
                    "unknown",
                    f"{img}: {function}() may write untracked non-local memory; argument {index + 1} may point to "
                    + ", ".join(hit),
                )
        if "ESCAPED" in c:
            hit = sorted(ao & self.escaped)
            if hit:
                return (
                    "unknown",
                    f"{img}: {function}() may write escaped memory; argument {index + 1} may point to escaped "
                    + ", ".join(hit),
                )
        pts = ", ".join(sorted(a - {"NULL"})) or "nothing"
        writes = ", ".join(sorted(c - {"NULL"}))
        return "no", f"{img}: {function}() writes {{{writes}}}; argument {index + 1} points to {{{pts}}}"

    def visibility(self, function: str) -> str | None:
        s = self.data.get("symbols", {}).get(function)
        return s.get("visibility") if s else None


def load_gcc_pta(
    project: Project, profile_id: str, program: str | None = None, inventory: dict[str, Any] | None = None
) -> dict[str, GccPta]:
    """``program/image`` -> GCC's solution, for every image with a complete, current replay.

    With ``inventory``, an image whose recorded unit sources differ from the
    inventory's (the code changed since the replay) is left out as stale.
    """
    current = (
        {u["unit_id"]: u["file_sha256"] for u in inventory["units"] if u["profile"] == profile_id}
        if inventory is not None
        else None
    )
    d = Store(project.state_dir).root / "flow" / profile_id / "gcc" / "programs"
    out: dict[str, GccPta] = {}
    if not d.is_dir():
        return out
    for pdir in sorted(d.iterdir()):
        if program is not None and pdir.name != _safe(program):
            continue
        for idir in sorted(pdir.iterdir()):
            p = idir / "pta.json"
            if p.exists():
                data = read_json(p)
                if (data.get("result") or {}).get("status") != "complete":
                    continue
                if current is not None and any(
                    current.get(i["unit"]) != i["file_sha256"] for i in data.get("inputs") or []
                ):
                    continue  # stale
                out[f"{data.get('program')}/{data.get('image')}"] = GccPta(data)
    return out
