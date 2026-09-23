"""Task ownership: which thread of control can run which code, and who may write an object concurrently.

An interface recipe that moves a read (or a write) from inside a callee to its
call site is only equivalent if nothing else writes the object in between.  In a
multi-task program such as cFS that is a question about tasks: the call runs in
one task, and the object must not be written by any other task, interrupt or
second instance of the same task.  ``single-threaded`` answers it for programs
that have one thread; this module answers it for programs that declare theirs:

    preservation:
      concurrency:
        model: tasks
        tasks:                                   # one instance each unless 'instances: many'
          - {name: SAMPLE_APP, entry: SAMPLE_APP_Main}
          - {name: startup, entry: [main, SAMPLE_LIB_Init]}   # several entries, one context
        interrupts:
          - {name: SIGHUP, entry: OS_NoopSigHandler}
        dispatchers: [OS_PthreadTaskEntry, CFE_ES_TaskEntryPoint]
        indirect_calls:                          # targets points-to analysis cannot resolve
          - {at: "cfe/modules/es/fsw/src/cfe_es_apps.c:1016", targets: [SAMPLE_LIB_Init]}
          - {at: "cfe/modules/time/fsw/src/cfe_time_tone.c:1329", targets: [],
             unless_called: [CFE_TIME_RegisterSynchCallback]}

A *dispatcher* is code that starts a declared task through a function pointer
(an OS task trampoline); it runs in every task started through it, so it is a
context with many instances, and its indirect calls to declared entries are not
followed into it.

The declaration is checked, not trusted:

* every entry must exist in the program, and every program entry point (the
  link model's) must belong to a declared context, or it becomes a context of
  its own that may run concurrently with everything;
* every call to a function whose effect model *spawns* a thread of control
  (``pthread_create``, ``OS_TaskCreate``, ``signal``...) must start a declared
  entry or dispatcher, identified by the function named in the argument or by
  the points-to set of the argument;
* indirect calls without resolved targets make a context's code unbounded.

Each context's code is the call-graph closure of its entries (indirect calls
through SVF's targets, boundary calls judged by their models), and its writes
are summarised once as points-to objects.  ``concurrent`` then decides, for the
targets of a parameter, whether any context other than the one running the call
(or a second instance of it) may write them.  A write Weaver cannot bound (an
unmodelled call, a write through unknown memory) only matters for objects that
code can reach: a stack object whose address no pointer in that context holds is
out of its reach, under the pointer-provenance rule compilers already assume.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

from weaver.flow.evidence import FlowEvidence


@dataclass
class Context:
    name: str
    entries: list[str]
    kind: str  # task | interrupt | dispatcher | implicit
    many: bool
    funcs: set[str] = field(default_factory=set)
    unresolved: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entries": self.entries,
            "kind": self.kind,
            "instances": "many" if self.many else "one",
            "functions": len(self.funcs),
            "unresolved_indirect_calls": len(self.unresolved),
        }


@dataclass
class WriteSummary:
    objs: dict[int, dict[str, Any]] = field(default_factory=dict)  # object -> first write that may reach it
    unbounded: list[dict[str, Any]] = field(default_factory=list)  # writes Weaver cannot bound
    owned: list[dict[str, Any]] = field(default_factory=list)  # boundary calls writing framework-owned state


def spec_of(preservation: dict[str, Any]) -> dict[str, Any] | None:
    """The task declaration in ``preservation.concurrency``, if it uses the task model."""
    c = preservation.get("concurrency")
    return c if isinstance(c, dict) and c.get("model") == "tasks" else None


def _names(v: Any) -> list[str]:
    return [str(x) for x in (v if isinstance(v, list) else [v] if v else [])]


class TaskModel:
    def __init__(
        self,
        program: Any,
        fe: FlowEvidence | None,
        prog_key: str,
        spec: dict[str, Any],
        entry_points: list[str] | None = None,
    ):
        self.program = program
        self.fe = fe
        self.prog_key = prog_key
        self.problems: list[str] = []
        self.assumptions: list[str] = []
        self.funcs = {k for k in program.funcs if prog_key in program.programs_of(k)} or set(program.funcs)
        self._by_name: dict[str, list[str]] = {}
        for k in self.funcs:
            self._by_name.setdefault(program.funcs[k]["name"], []).append(k)
        self.contexts: dict[str, Context] = {}
        for t in spec.get("tasks") or []:
            self._declare(t, "task")
        for t in spec.get("interrupts") or []:
            self._declare(t, "interrupt")
        self.dispatchers = set(_names(spec.get("dispatchers")))
        for d in sorted(self.dispatchers):
            self._declare({"name": d, "entry": d, "instances": "many"}, "dispatcher")
        self.entry_names = {e for c in self.contexts.values() for e in c.entries}
        self.resolved: dict[tuple[str, int], list[str]] = {}
        called = {c["callee"] for k in self.funcs for c in program.funcs[k]["calls"]}
        for d in spec.get("indirect_calls") or []:
            file, _, line = str(d.get("at", "")).rpartition(":")
            if not file or not line.isdigit():
                self.problems.append(f"indirect_calls: 'at' must be 'file:line', got {d.get('at')!r}")
                continue
            targets = _names(d.get("targets"))
            unknown = [t for t in targets if t not in self._by_name]
            if unknown:
                self.problems.append(f"indirect call at {d['at']}: {', '.join(unknown)} not defined in {prog_key}")
            used = sorted(set(_names(d.get("unless_called"))) & called)
            if used:
                self.problems.append(
                    f"indirect call at {d['at']} is declared to reach {targets or 'nothing'}, but the program calls "
                    f"{', '.join(used)}, which installs more targets"
                )
                continue
            self.resolved[(file, int(line))] = targets
            self.assumptions.append(
                f"indirect call at {d['at']} reaches only {', '.join(targets) or 'no function'}"
                + (f" ({d['reason']})" if d.get("reason") else "")
            )
        for ep in entry_points or []:
            if ep not in self.entry_names and ep in self._by_name:
                self.problems.append(
                    f"program entry point {ep}() belongs to no declared task; treated as a context of its own "
                    "that may run concurrently with every task"
                )
                self.contexts[f"entry:{ep}"] = Context(f"entry:{ep}", [ep], "implicit", True)
                self.entry_names.add(ep)
        for c in self.contexts.values():
            self._closure(c)
        self._check_spawns()
        covered = set().union(*(c.funcs for c in self.contexts.values())) if self.contexts else set()
        self.uncovered = self.funcs - covered
        self._ctx_of: dict[str, list[Context]] = {}
        for c in self.contexts.values():
            for k in c.funcs:
                self._ctx_of.setdefault(k, []).append(c)
        self._summaries: dict[str, WriteSummary] = {}
        self._exposure: dict[int, set[str]] | None = None
        self._spans: dict[str, list[tuple[int, int, str]]] | None = None

    # -- declaration ---------------------------------------------------------
    def _declare(self, t: Any, kind: str) -> None:
        if isinstance(t, str):
            t = {"name": t, "entry": t}
        name = str(t.get("name") or _names(t.get("entry"))[0])
        entries = _names(t.get("entry"))
        missing = [e for e in entries if e not in self._by_name]
        if missing:
            self.problems.append(f"{kind} {name}: entry {', '.join(missing)} is not defined in {self.prog_key}")
        many = str(t.get("instances", "one")) not in ("one", "1")
        self.contexts[name] = Context(name, entries, kind, many)

    # -- reachability ----------------------------------------------------------
    def _closure(self, c: Context) -> None:
        stack = [k for e in c.entries for k in self._by_name.get(e, [])]
        models = self.program.models
        while stack:
            k = stack.pop()
            if k in c.funcs or k not in self.funcs:
                continue
            c.funcs.add(k)
            f = self.program.funcs[k]
            for call in f["calls"]:
                if models.is_boundary(call["callee"]):
                    continue  # its effects come from the reviewed model (see summary)
                stack.extend(t for t in self.program.resolve(k, call["callee"]) if t not in c.funcs)
            for call in f["indirect_calls"]:
                site = call.get("site") or {}
                targets = (
                    self.fe.indirect_targets(site.get("file"), site.get("line"), site.get("col")) if self.fe else None
                )
                if not targets:
                    declared = self.resolved.get((site.get("file"), site.get("line")))
                    if declared is None:
                        c.unresolved.append({"function": f["name"], "site": site})
                        continue
                    targets = declared
                for name in targets:
                    if c.kind == "dispatcher" and name in self.entry_names and name not in c.entries:
                        continue  # the dispatched task runs as its own context
                    stack.extend(t for t in self.program.resolve(k, name) if t not in c.funcs)

    def _check_spawns(self) -> None:
        models = self.program.models
        for k in sorted(self.funcs):
            f = self.program.funcs[k]
            for call in f["calls"]:
                m = models.lookup(call["callee"])
                spawns = getattr(m, "spawns", None) if m is not None else None
                if spawns is None:
                    continue
                site = call.get("site") or {}
                where = f"{site.get('file')}:{site.get('line')}"
                if spawns == "unknown":
                    if any(c.kind == "interrupt" for c in self.contexts.values()):
                        self.assumptions.append(
                            f"{call['callee']}() at {where} installs a handler Weaver cannot identify; "
                            "assumed to be one of the declared interrupt contexts"
                        )
                    else:
                        self.problems.append(
                            f"{call['callee']}() at {where} installs a handler Weaver cannot identify; declare it "
                            "under interrupts"
                        )
                    continue
                entries = self._entries_of(f, call, int(spawns))
                if not entries:
                    self.problems.append(
                        f"{call['callee']}() at {where} starts a thread of control whose entry Weaver cannot identify"
                    )
                    continue
                for e in sorted(entries - self.entry_names):
                    self.problems.append(
                        f"{call['callee']}() at {where} starts {e}(), which is not a declared task, interrupt or "
                        "dispatcher"
                    )

    def _entries_of(self, f: dict[str, Any], call: dict[str, Any], i: int) -> set[str]:
        """Functions an entry-point argument may name: a function named in it, or SVF's function objects."""
        if i >= len(call["args"]):
            return set()
        a = call["args"][i]
        lc = a.get("lc")
        out: set[str] = set()
        if lc:
            l0, c0, l1, c1 = lc
            for r in f.get("function_refs", []):
                if (l0, c0) <= (r.get("line") or 0, r.get("col") or 0) <= (l1, c1):
                    out.add(r["name"])
            if self.fe is not None:
                pts, n = self.fe.pts_in_span(f["file"], lc)
                for o in pts:
                    d = self.fe.objects.get(o) or {}
                    if d.get("kind") == "function" and d.get("name"):
                        out.add(d["name"])
        return out

    # -- queries ----------------------------------------------------------------
    def contexts_of(self, key: str) -> list[Context]:
        return self._ctx_of.get(key, [])

    def summary(self, c: Context) -> WriteSummary:
        s = self._summaries.get(c.name)
        if s is None:
            s = self._summaries[c.name] = self._summarise(c)
        return s

    def _summarise(self, c: Context) -> WriteSummary:
        s = WriteSummary()
        fe, models = self.fe, self.program.models
        for u in c.unresolved:
            s.unbounded.append({**u, "why": "indirect call without resolved targets"})

        def add(objs: set[int], f: dict[str, Any], site: Any, what: str) -> None:
            unknown = fe.unknown_objects(objs) if fe else objs
            if unknown:
                s.unbounded.append({"function": f["name"], "site": site, "why": what + " through unknown memory"})
            for o in objs - unknown:
                s.objs.setdefault(o, {"function": f["name"], "site": site, "what": what})

        for k in sorted(c.funcs):
            f = self.program.funcs[k]
            for w in f["named_writes"]:
                if not w.get("global"):
                    continue
                objs = fe.objects_for_decl(w.get("decl_file"), w.get("decl_line"), w["name"], True) if fe else set()
                if not objs:
                    s.unbounded.append({"function": f["name"], "site": w.get("site"), "why": f"writes {w['name']}"})
                add(objs, f, w.get("site"), f"writes {w['name']}")
            for w in f["pointer_writes"]:
                if fe is None or not w.get("lc") or w.get("unknown_base"):
                    s.unbounded.append({"function": f["name"], "site": w.get("site"), "why": "write through a pointer"})
                    continue
                pts, n = fe.pts_in_span(w["lvalue"]["file"] if w.get("lvalue") else f["file"], w["lc"])
                if n == 0:
                    s.unbounded.append(
                        {"function": f["name"], "site": w.get("site"), "why": "write through an unmapped pointer"}
                    )
                    continue
                add(pts, f, w.get("site"), "writes through a pointer")
            for call in f["calls"]:
                boundary = models.is_boundary(call["callee"])
                if self.program.resolve(k, call["callee"]) and not boundary:
                    continue  # analysed callee: part of this context's closure
                m = models.lookup(call["callee"])
                site = call.get("site")
                if m is None or m.calls_back or m.writes == "any":
                    why = (
                        "no reviewed model"
                        if m is None
                        else "may run callbacks"
                        if m.calls_back
                        else "may write any object"
                    )
                    s.unbounded.append({"function": f["name"], "site": site, "why": f"calls {call['callee']}(): {why}"})
                    continue
                if m.writes_owned:
                    s.owned.append({"function": f["name"], "site": site, "callee": call["callee"], "model": m})
                for i in m.writes:
                    if i >= len(call["args"]):
                        continue
                    a = call["args"][i]
                    what = f"{call['callee']}() writes through argument {i + 1}"
                    if a.get("addr_of") and fe is not None:
                        d = a["addr_of"]
                        objs = fe.objects_for_decl(
                            d.get("decl_file"), d.get("decl_line"), d.get("name"), bool(d.get("global_"))
                        )
                        if objs:
                            add(objs, f, site, what)
                            continue
                    if fe is None or not a.get("lc"):
                        s.unbounded.append({"function": f["name"], "site": site, "why": what})
                        continue
                    pts, n = fe.pts_in_span(f["file"], a["lc"])
                    if n == 0:
                        s.unbounded.append({"function": f["name"], "site": site, "why": what + " (unmapped)"})
                        continue
                    add(pts, f, site, what)
            for a in f["asm"]:
                s.unbounded.append({"function": f["name"], "site": a, "why": "inline assembly"})
        return s

    def _function_at(self, file: str | None, line: int | None) -> str | None:
        if self._spans is None:
            self._spans = {}
            for k in self.funcs:
                f = self.program.funcs[k]
                if f.get("line") is not None:
                    self._spans.setdefault(f["file"], []).append((f["line"], f.get("end_line") or f["line"], k))
            for v in self._spans.values():
                v.sort()
        spans = self._spans.get(file or "", [])
        i = bisect.bisect_right(spans, (line or 0, 1 << 30, "")) - 1
        if i >= 0 and spans[i][0] <= (line or 0) <= spans[i][1]:
            return spans[i][2]
        return None

    def exposure(self, oid: int) -> set[str]:
        """Functions with a pointer that may point to stack object ``oid`` (whose code can reach it)."""
        if self._exposure is None:
            self._exposure = {}
            if self.fe is not None:
                stack = {o for o, d in self.fe.objects.items() if d.get("kind") == "stack"}
                for n in self.fe.nodes.values():
                    loc = n.get("loc") or {}
                    hit = stack.intersection(n.get("pts") or ())
                    if not hit:
                        continue
                    if loc.get("kind") == "inst":
                        k = self._function_at(loc.get("file"), loc.get("line"))
                    elif loc.get("kind") == "arg":
                        k = next(iter(self._by_name.get(loc.get("function") or "", [])), None)
                    else:
                        k = None
                    for o in hit:
                        self._exposure.setdefault(o, set()).add(k or "?")
        return self._exposure.get(oid, set())

    def owner_function(self, oid: int) -> str | None:
        d = (self.fe.objects.get(oid) if self.fe else None) or {}
        if d.get("kind") != "stack":
            return None
        loc = d.get("loc") or {}
        return self._function_at(loc.get("file"), loc.get("line"))

    def concurrent(self, key: str, targets: set[int]) -> tuple[str, list[str], list[str]]:
        """(established | violated | unresolved, evidence, counter-evidence) for writes to ``targets``
        by any context that can run while the call to ``key`` is in progress."""
        pos: list[str] = []
        neg: list[tuple[str, str]] = []
        running = self.contexts_of(key)
        fname = self.program.funcs.get(key, {}).get("name", key)
        if not running:
            return "unresolved", [], [f"{fname}() is reached from no declared task (entry points: see weaver tasks)"]
        if self.problems:
            neg.append(("unresolved", "the task declaration is incomplete: " + self.problems[0]))
        conflicting = [c for c in self.contexts.values() if c not in running or c.many or len(running) > 1]
        names = {o: (self.fe.describe(o).get("name") if self.fe else str(o)) for o in targets}
        for c in conflicting:
            s = self.summary(c)
            hit = sorted(targets & set(s.objs))
            for o in hit[:3]:
                w = s.objs[o]
                site = w.get("site") or {}
                neg.append(
                    (
                        "violated",
                        f"{self._label(c)} may write {names[o]}: {w['function']}() at "
                        f"{site.get('file')}:{site.get('line')} ({w['what']})",
                    )
                )
            if hit:
                continue
            if s.owned:
                own = sorted(
                    str(names[o])
                    for o in targets
                    if any(w["model"].owned((self.fe.describe(o) if self.fe else {}).get("file")) for w in s.owned)
                )
                if own:
                    w = s.owned[0]
                    neg.append(
                        (
                            "violated",
                            f"{self._label(c)} calls {w['callee']}(), which may write framework-owned {', '.join(own)}",
                        )
                    )
                    continue
            if s.unbounded:
                reach = [o for o in targets if self._reachable(o, c)]
                if reach:
                    u = s.unbounded[0]
                    site = u.get("site") or {}
                    neg.append(
                        (
                            "unresolved",
                            f"{self._label(c)} has writes Weaver cannot bound, and {names[reach[0]]} is within its "
                            f"reach: {u['function']}() at {site.get('file')}:{site.get('line')} ({u['why']}; "
                            f"{len(s.unbounded)} such write(s))",
                        )
                    )
        if not neg:
            run = ", ".join(self._label(c) for c in running)
            pos.append(f"{fname}() runs only in {run}; {len(conflicting)} other context(s) checked")
            pos.append("possible targets: " + ", ".join(sorted(str(n) for n in names.values())))
            stack = [o for o in targets if (self.fe.objects.get(o) or {}).get("kind") == "stack"] if self.fe else []
            if stack:
                pos.append(
                    "stack targets not held by any other context's pointers: "
                    + ", ".join(str(names[o]) for o in stack)
                    + " (pointer provenance: code without the address cannot reach the object)"
                )
            return "established", pos + [f"assumption: {a}" for a in self.assumptions[:3]], []
        status = "violated" if any(s == "violated" for s, _ in neg) else "unresolved"
        return status, [], [t for _, t in neg]

    def _reachable(self, oid: int, c: Context) -> bool:
        """Whether unbounded writes in context ``c`` could reach object ``oid``."""
        d = (self.fe.objects.get(oid) if self.fe else None) or {}
        if d.get("kind") != "stack":
            return True  # globals and heap objects are reachable by name or through any exposed pointer
        return bool(self.exposure(oid) & (c.funcs | {"?"}))

    @staticmethod
    def _label(c: Context) -> str:
        kind = {"task": "task", "interrupt": "interrupt", "dispatcher": "dispatcher", "implicit": "entry point"}[c.kind]
        return f"{kind} {c.name}" + (" (many instances)" if c.many else "")

    def to_json(self) -> dict[str, Any]:
        return {
            "program": self.prog_key,
            "contexts": [c.to_json() for c in self.contexts.values()],
            "problems": self.problems,
            "assumptions": self.assumptions,
            "uncovered_functions": len(self.uncovered),
        }


def spawn_sites(program: Any, prog_keys: set[str] | None = None) -> list[dict[str, Any]]:
    """Calls that start a thread of control, optionally only in functions linked into ``prog_keys``."""
    out = []
    for k, f in program.funcs.items():
        if prog_keys and not prog_keys & program.programs_of(k):
            continue
        for call in f["calls"]:
            m = program.models.lookup(call["callee"])
            if m is not None and getattr(m, "spawns", None) is not None:
                out.append({"key": k, "function": f["name"], "callee": call["callee"], "site": call.get("site") or {}})
    return out
