"""Parse ``wpa -print-all-pts -print-fp`` output into Weaver's flow schema.

Each points-to block looks like::

    ##<p> Source Loc: { 0th arg get "ln": 3, "file": "t.c" }
    Ptr 10          PointsTo: { 6 48 }
    !!Target NodeID 6    [<g> Source Loc: { Glob "ln": 1, "fl": "t.c" }]

and each resolved indirect call site like::

    CallSite: CallICFGNode17 {fun: use{ "ln": 7, "cl": 5, "fl": "u.c" }}
       call void %0(...) ... with Targets:
        set2

Lines the parser does not recognise inside those sections are counted as parse
errors, which makes the run incomplete rather than silently lossy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_SEP = re.compile(r"^-{20,}\s*$")
_HEADER = re.compile(r"^##<(?P<name>[^>]*)>\s*(?:Source Loc:\s*(?P<loc>.*)|id:(?P<id>\d+))\s*$")
_PTR = re.compile(r"^Ptr (?P<id>\d+)\s+PointsTo:\s*\{(?P<pts>[^}]*)\}\s*$")
_TARGET = re.compile(r"^!!Target NodeID (?P<id>\d+)\s+\[(?P<desc>.*)\]\s*$")
_TDESC = re.compile(r"^<(?P<name>[^>]*)>\s*Source Loc:\s*(?P<loc>.*)$")
_DUMMY = re.compile(r"^Dummy Obj id: ?(?P<id>\d+)")
_ARG = re.compile(r"(?P<n>\d+)(?:st|nd|rd|th) arg (?P<fn>\S+)")
_CALLSITE = re.compile(r"^CallSite: CallICFGNode\d+ \{fun: (?P<fn>[^{]+?)\{(?P<loc>[^}]*)\}\}\s*$")


def parse_loc(text: str | None) -> dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        t = t[1:-1].strip()
    out: dict[str, Any] = {"raw": t}
    if not t:
        out["kind"] = "none"
        return out
    if t == "constant data":
        out["kind"] = "const"
        return out
    ln = re.search(r'"ln":\s*(\d+)', t)
    cl = re.search(r'"cl":\s*(\d+)', t)
    fl = re.search(r'"(?:fl|file)":\s*"([^"]*)"', t)
    out.update(
        line=int(ln.group(1)) if ln else None,
        col=int(cl.group(1)) if cl else None,
        file_raw=fl.group(1) if fl else None,
    )
    arg = _ARG.search(t)
    if arg:
        out.update(kind="arg", arg_index=int(arg.group("n")), function=arg.group("fn"))
    elif t.startswith("Glob"):
        out["kind"] = "global"
    elif '"file"' in t and cl is None:
        out["kind"] = "function"
    elif cl is not None:
        out["kind"] = "inst"
    elif fl is not None:
        out["kind"] = "decl"  # an alloca: the stack object of a local or parameter
    else:
        out["kind"] = "other"
    return out


def parse_wpa(text: str, root: Path, dirs: list[str]) -> dict[str, Any]:
    from weaver.flow.svf import resolve_source_file

    cache: dict[str, str | None] = {}
    nodes: dict[int, dict[str, Any]] = {}
    objects: dict[int, dict[str, Any]] = {}
    indirect: list[dict[str, Any]] = []
    errors: list[str] = []

    def place(loc: dict[str, Any]) -> dict[str, Any]:
        f = loc.get("file_raw")
        loc["file"] = resolve_source_file(f, root, dirs, cache) if f else None
        loc.pop("raw", None)
        return loc

    lines = text.splitlines()
    i = 0
    in_pts = False
    cur: dict[str, Any] | None = None
    while i < len(lines):
        line = lines[i].rstrip("\n")
        s = line.strip()
        if _SEP.match(s):
            in_pts = True
            cur = None
            i += 1
            continue
        m = _CALLSITE.match(s)
        if m:
            loc = place(parse_loc("{" + m.group("loc") + "}"))
            targets: list[str] = []
            j = i + 1
            while j < len(lines) and "with Targets:" not in lines[j] and not _CALLSITE.match(lines[j].strip()):
                j += 1
            j += 1
            while j < len(lines) and lines[j].startswith("\t") and lines[j].strip():
                targets.append(lines[j].strip())
                j += 1
            indirect.append(
                {
                    "function": m.group("fn").strip(),
                    "file": loc.get("file"),
                    "line": loc.get("line"),
                    "col": loc.get("col"),
                    "targets": targets,
                }
            )
            i = j
            continue
        if re.match(r"^=+[^=]*=+$", s) or re.match(r"^NodeID: \d+$", s):
            # section banner of the indirect-call listing, and the node id preceding each call site
            in_pts = False
            i += 1
            continue
        if not in_pts or not s:
            i += 1
            continue
        m = _HEADER.match(s)
        if m:
            cur = {"name": m.group("name").strip(), "loc": place(parse_loc(m.group("loc")))}
            if m.group("id") is not None:
                cur["loc"] = {"kind": "dummy"}
            i += 1
            continue
        m = _PTR.match(s)
        if m:
            nid = int(m.group("id"))
            body = m.group("pts").strip()
            pts = [] if body in ("", "empty") else [int(x) for x in body.split()]
            node = nodes.setdefault(nid, {"id": nid})
            if cur is not None:
                node.update(name=cur["name"], loc=cur["loc"])
            node["pts"] = pts
            i += 1
            continue
        m = _TARGET.match(s)
        if m:
            oid = int(m.group("id"))
            desc = m.group("desc").strip()
            if oid not in objects:
                d = _TDESC.match(desc)
                if d:
                    objects[oid] = {
                        "id": oid,
                        "name": d.group("name").strip(),
                        "loc": place(parse_loc(d.group("loc"))),
                        "kind": None,
                    }
                elif _DUMMY.match(desc):
                    objects[oid] = {"id": oid, "name": None, "loc": {"kind": "dummy"}, "kind": "dummy"}
                else:
                    objects[oid] = {"id": oid, "name": None, "loc": {"kind": "other"}, "kind": "other"}
            i += 1
            continue
        if s.startswith("*" * 4) or s.startswith("#" * 4) or re.match(r"^[A-Za-z][\w/ ]*\s+[-\d.e+]+$", s):
            # statistics sections between blocks
            in_pts = False if s.startswith("****") else in_pts
            i += 1
            continue
        errors.append(s[:200])
        i += 1

    for o in objects.values():
        if o["kind"] is None:
            k = o["loc"].get("kind")
            o["kind"] = {
                "global": "global",
                "function": "function",
                "decl": "stack",
                "inst": "heap",
                "const": "constant",
                "none": "field-or-internal",
                "arg": "argument",
            }.get(k, "other")
    diagnostics = {
        "nodes": len(nodes),
        "objects": len(objects),
        "indirect_call_sites": len(indirect),
        "unresolved_indirect_call_sites": sum(1 for c in indirect if not c["targets"]),
        "parse_errors": len(errors),
        "parse_error_samples": errors[:10],
        "time_limit_hit": bool(re.search(r"time limit|timed out|budget exhausted", text, re.I)),
        "unmapped_files": sorted(k for k, v in cache.items() if v is None),
    }
    return {
        "nodes": [n for n in nodes.values() if n.get("pts") or n.get("loc", {}).get("kind") == "arg"],
        "objects": list(objects.values()),
        "indirect_calls": indirect,
        "diagnostics": diagnostics,
    }
