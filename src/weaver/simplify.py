"""Simplification checker: which constructs stand between each function and a target.

A *target profile* names the C constructs a project wants gone.  CLite, a C subset
without pointers and many other constructs, is one profile.  Others describe a
simplification goal: code without pointers, or code whose hidden and shared
state no longer stops it from being split into modules.  For every analysed
function the checker lists each construct the profile excludes, with its line,
and a function with none *meets* the profile.

This is a guide for manual work, not a certification.  The facts come from the
inventory (pointer declarations and operations, calls, writes, and constructs
recorded from each function's AST in the analysed configurations); code no
configuration compiled is not checked.

Projects choose and adjust profiles in ``weaver.yaml``::

    simplify:
      profile: clite-provisional      # the default profile
      add: [static-local]             # extra rules for the default profile
      remove: [recursion]
      profiles:                       # the project's own profiles
        app-layer: {title: "Application layer", rules: [pointer, global-write, goto]}
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from weaver.config import Project
from weaver.errors import ConfigError

RULES: dict[str, dict[str, str]] = {
    "pointer": {
        "title": "Pointer variables and parameters",
        "why": "Each pointer is an alias whose target, lifetime and ownership must be traced by hand.",
    },
    "address-of": {
        "title": "Taking an address (&x)",
        "why": "Taking an address creates a pointer to the object, often to pass it on.",
    },
    "pointer-arithmetic": {
        "title": "Pointer arithmetic and subscripts on pointers",
        "why": "The bounds of the underlying array are not visible where the pointer moves.",
    },
    "integer-pointer-cast": {
        "title": "Conversions between pointers and integers",
        "why": "An address held as an integer escapes every points-to analysis.",
    },
    "reinterpret-cast": {
        "title": "Casts between unrelated pointer types",
        "why": "The same memory is read as another type.",
    },
    "function-pointer": {
        "title": "Function pointers and indirect calls",
        "why": "Which code runs is decided at run time, so callers and effects are not visible statically.",
    },
    "dynamic-allocation": {
        "title": "Heap allocation and release",
        "why": "Lifetime and ownership are managed by hand (malloc, calloc, realloc, free).",
    },
    "raw-memory": {
        "title": "Raw memory functions",
        "why": "memcpy, memset and similar treat typed objects as untyped bytes.",
    },
    "array-decay": {
        "title": "Arrays passed as pointers",
        "why": "The array's length is lost when it decays to a pointer.",
    },
    "goto": {"title": "goto", "why": "Jumps make control flow and initialisation harder to follow and to move."},
    "union": {"title": "Unions", "why": "The same storage holds values of different types."},
    "varargs": {
        "title": "Variadic functions and va_arg",
        "why": "Argument types are not checked by the compiler.",
    },
    "recursion": {
        "title": "Recursion (direct or mutual)",
        "why": "Stack depth depends on the data, not on the code.",
    },
    "setjmp-longjmp": {"title": "setjmp and longjmp", "why": "Non-local jumps bypass normal returns and cleanup."},
    "inline-asm": {"title": "Inline assembly", "why": "Outside the language; its effects are not analysed."},
    "global-write": {
        "title": "Writes to global state",
        "why": "Shared mutable state couples functions that look independent.",
    },
    "static-local": {
        "title": "Static local variables",
        "why": "Hidden state that persists between calls.",
    },
}

PROFILES: dict[str, dict[str, Any]] = {
    "clite-provisional": {
        "title": "CLite (provisional)",
        "provisional": True,
        "note": "CLite has no written specification yet. This rule list is a working assumption about what it "
        "excludes; replace it when the definition exists.",
        "rules": [
            "pointer",
            "address-of",
            "pointer-arithmetic",
            "integer-pointer-cast",
            "reinterpret-cast",
            "function-pointer",
            "dynamic-allocation",
            "raw-memory",
            "goto",
            "union",
            "varargs",
            "recursion",
            "setjmp-longjmp",
            "inline-asm",
        ],
    },
    "pointer-free": {
        "title": "No pointers",
        "note": "Code with no pointers of any kind: no pointer variables, addresses, pointer arithmetic, casts or "
        "function pointers.",
        "rules": [
            "pointer",
            "address-of",
            "pointer-arithmetic",
            "integer-pointer-cast",
            "reinterpret-cast",
            "function-pointer",
            "raw-memory",
        ],
    },
    "modular": {
        "title": "Ready for a modular redesign",
        "note": "Hidden and shared state and non-local control flow that make code hard to split into modules.",
        "rules": ["global-write", "static-local", "goto", "setjmp-longjmp", "inline-asm"],
    },
}

# inventory operation kind -> rule
OP_RULES = {
    "address-of": "address-of",
    "pointer-arithmetic": "pointer-arithmetic",
    "pointer-subscript": "pointer-arithmetic",
    "integer-pointer-conversion": "integer-pointer-cast",
    "pointer-reinterpret-cast": "reinterpret-cast",
    "function-address": "function-pointer",
    "indirect-call": "function-pointer",
    "allocation": "dynamic-allocation",
    "release": "dynamic-allocation",
    "memory-library-call": "raw-memory",
    "array-decay": "array-decay",
    "inline-assembly": "inline-asm",
}
OP_TEXT = {
    "address-of": "takes an address",
    "pointer-arithmetic": "pointer arithmetic",
    "pointer-subscript": "subscript on a pointer",
    "integer-pointer-conversion": "pointer/integer conversion",
    "pointer-reinterpret-cast": "cast to an unrelated pointer type",
    "function-address": "takes a function's address",
    "indirect-call": "calls through a function pointer",
    "array-decay": "array passed as a pointer",
    "inline-assembly": "inline assembly",
}
AST_TEXT = {
    "goto": "goto",
    "varargs": "va_arg",
    "static-local": "static local variable",
    "function-pointer": "function-pointer variable",
    "union": "union object or member",
}
SETJMP = {
    "setjmp",
    "_setjmp",
    "sigsetjmp",
    "__sigsetjmp",
    "longjmp",
    "_longjmp",
    "siglongjmp",
    "__builtin_setjmp",
    "__builtin_longjmp",
}
AST_RULES = {"goto", "union", "static-local"}  # need the constructs the inventory records per function
POINTER_KINDS = {"local", "parameter", "global", "static-global", "return"}


def _config(project: Project) -> dict[str, Any]:
    raw = project.raw.get("simplify") or {}
    if not isinstance(raw, dict):
        raise ConfigError("simplify: must be a mapping (profile, add, remove, profiles)")
    return raw


def profiles(project: Project) -> dict[str, dict[str, Any]]:
    """Built-in profiles plus the project's own, each with its rule list checked."""
    raw = _config(project)
    out = {k: dict(v) for k, v in PROFILES.items()}
    for pid, p in (raw.get("profiles") or {}).items():
        if not isinstance(p, dict) or not isinstance(p.get("rules"), list):
            raise ConfigError(f"simplify.profiles.{pid}: needs a 'rules' list")
        out[str(pid)] = {"title": str(p.get("title") or pid), "note": str(p.get("note") or ""), "rules": p["rules"]}
    default = default_profile(project)
    if default not in out:
        raise ConfigError(f"simplify.profile: unknown profile {default!r} (have: {', '.join(out)})")
    out[default] = {**out[default], "rules": _adjust(out[default]["rules"], raw)}
    for pid, p in out.items():
        if bad := [r for r in p["rules"] if r not in RULES]:
            raise ConfigError(f"simplify profile {pid}: unknown rule(s) {bad}; known: {', '.join(RULES)}")
    return out


def _adjust(rules: list[str], raw: dict[str, Any]) -> list[str]:
    add = [str(r) for r in raw.get("add") or []]
    remove = {str(r) for r in raw.get("remove") or []}
    return [r for r in dict.fromkeys([*rules, *add]) if r not in remove]


def default_profile(project: Project) -> str:
    return str(_config(project).get("profile") or "clite-provisional")


def _recursive(funcs: dict[str, dict[str, Any]]) -> dict[str, int | None]:
    """Functions on a call cycle (direct or mutual) -> line of a call that stays on the cycle."""
    by_name: dict[str, list[str]] = {}
    for k, f in funcs.items():
        by_name.setdefault(f["name"], []).append(k)

    def callees(k: str) -> list[tuple[str, int | None]]:
        f = funcs[k]
        out = []
        for c in f.get("calls", []):
            ks = by_name.get(c.get("callee") or "", [])
            same = [x for x in ks if funcs[x]["file"] == f["file"]]
            for t in same or [x for x in ks if not funcs[x].get("static")]:
                out.append((t, (c.get("site") or {}).get("line")))
        return out

    # iterative Tarjan: strongly connected components of the direct call graph
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    comp: dict[str, int] = {}
    counter = 0
    for root in funcs:
        if root in index:
            continue
        work = [(root, iter(callees(root)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                w = nxt[0]
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(callees(w))))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp[w] = index[v]
                    if w == v:
                        break
    size = Counter(comp.values())
    out: dict[str, int | None] = {}
    for k in funcs:
        cyc = [(t, ln) for t, ln in callees(k) if comp.get(t) == comp[k] and (size[comp[k]] > 1 or t == k)]
        if cyc:
            out[k] = cyc[0][1]
    return out


def check(
    project: Project,
    inv: dict[str, Any],
    profile: str | None = None,
    in_scope: Callable[[str], bool] = lambda _path: True,
) -> dict[str, Any]:
    """Every analysed function (in scope) against one profile."""
    profs = profiles(project)
    pid = profile or default_profile(project)
    if pid not in profs:
        raise ConfigError(f"unknown simplification profile {pid!r} (have: {', '.join(profs)})")
    prof = profs[pid]
    rules = set(prof["rules"])
    funcs: dict[str, dict[str, Any]] = inv.get("functions", {})
    ops: dict[str, Any] = inv.get("operations") or {}
    pointers: dict[str, list[dict[str, Any]]] = {}
    for f in inv["findings"]:
        if f.get("kind") in POINTER_KINDS and f.get("file"):
            pointers.setdefault(f"{f['file']}::{f.get('function') or '(file scope)'}", []).append(f)
    recursive = _recursive(funcs) if "recursion" in rules else {}

    def violations(key: str, fs: dict[str, Any] | None) -> list[dict[str, Any]]:
        v: dict[tuple[str, int | None, str], dict[str, Any]] = {}

        def add(rule: str, line: int | None, text: str) -> None:
            if rule in rules:
                v.setdefault((rule, line, text), {"rule": rule, "line": line, "text": text})

        for f in pointers.get(key, []):
            add("pointer", f.get("line"), f"{f['kind']} '{f.get('name')}' ({f.get('type')})")
        for rec in (ops.get(key) or {}).values():
            for site in rec.get("sites", []):
                rule = OP_RULES.get(site["kind"])
                if rule is None:
                    continue
                text = OP_TEXT.get(site["kind"]) or site["kind"]
                if site.get("callee"):
                    text = f"{site['callee']}()"
                add(rule, site.get("line"), text)
        if fs is None:
            return list(v.values())
        for c in fs.get("calls", []):
            if c.get("callee") in SETJMP:
                add("setjmp-longjmp", (c.get("site") or {}).get("line"), f"{c['callee']}()")
        for w in fs.get("named_writes", []):
            local = w.get("decl_file") == fs.get("file") and (fs.get("line") or 0) <= (w.get("decl_line") or -1) <= (
                fs.get("end_line") or 0
            )  # a static local: its own rule
            if w.get("global") and not local:
                add("global-write", (w.get("site") or {}).get("line"), f"writes {w.get('name')}")
        if fs.get("variadic"):
            add("varargs", fs.get("line"), "variadic function")
        if key in recursive:
            add("recursion", recursive[key], "on a call cycle")
        for c in fs.get("constructs", []):
            add(c["kind"], c.get("line"), AST_TEXT.get(c["kind"], c["kind"]))
        return sorted(v.values(), key=lambda x: (x["line"] or 0, x["rule"]))

    rows = []
    for key, fs in funcs.items():
        if not fs.get("file") or not in_scope(fs["file"]):
            continue
        vs = violations(key, fs)
        rows.append(
            {
                "file": fs["file"],
                "function": fs["name"],
                "line": fs.get("line"),
                "end_line": fs.get("end_line"),
                "violations": vs,
                "counts": dict(Counter(x["rule"] for x in vs)),
            }
        )
    file_scope = []
    for key in sorted(k for k in pointers if k.endswith("::(file scope)")):
        file = key.rsplit("::", 1)[0]
        if in_scope(file):
            vs = violations(key, None)
            if vs:
                file_scope.append({"file": file, "violations": vs, "counts": dict(Counter(x["rule"] for x in vs))})
    rows.sort(key=lambda r: (-len(r["violations"]), r["file"], r["line"] or 0))
    by_rule = {
        r: {
            "functions": sum(1 for row in rows if row["counts"].get(r)),
            "sites": sum(row["counts"].get(r, 0) for row in rows),
        }
        for r in prof["rules"]
    }
    notes = []
    if rules & AST_RULES and any("constructs" not in fs for fs in funcs.values()):
        notes.append("The inventory predates construct recording: run 'weaver inventory' to check goto, unions "
                     "and static locals.")  # fmt: skip
    if prof.get("provisional"):
        notes.append(prof.get("note") or "This profile is provisional.")
    ready = sum(1 for r in rows if not r["violations"])
    return {
        "profile": {
            "id": pid,
            **{k: prof.get(k) for k in ("title", "note", "rules")},
            "provisional": bool(prof.get("provisional")),
        },
        "profiles": [
            {"id": k, "title": p["title"], "provisional": bool(p.get("provisional"))} for k, p in profs.items()
        ],
        "rules": {r: RULES[r] for r in prof["rules"]},
        "summary": {
            "functions": len(rows),
            "ready": ready,
            "percent": round(100 * ready / len(rows)) if rows else 0,
            "by_rule": by_rule,
        },
        "functions": rows,
        "file_scope": file_scope,
        "notes": notes,
    }
