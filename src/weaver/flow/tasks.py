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
(or a second instance of it) may write them.

Stack objects get a thread-escape analysis first.  A local lives in the frame of
the task that runs its function; another task (or another instance of the same
task, which has its own frame) can reach it only if its address escapes: is
stored, directly or through other objects, into a global, heap or unknown
object, is handed to a thread start (a model's ``shares`` argument), or is
converted to an integer.  SVF's points-to sets of object nodes are the objects'
contents, so escape is the closure of those contents from the shared roots.  A
target that does not escape cannot be written concurrently, whatever the
context-insensitive write summaries say (a helper called from several tasks
writes, in each, only the objects its caller passed).  This relies on pointer
provenance, which compilers already assume: code that never obtains an object's
address cannot reach it, even through a pointer made from an integer.
"""

from __future__ import annotations

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
        self._escaped: set[int] | None = None
        self._escape_unknown: list[str] = []
        self._escape_why: dict[int, str] = {}

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
        # Code a task calls runs in that task, framework APIs included: unlike may_modify, which judges
        # an API call by its reviewed contract, reachability and write summaries follow the analysed
        # body of every callee (so callbacks an API runs, e.g. a table validation function, are found).
        stack = [k for e in c.entries for k in self._by_name.get(e, [])]
        while stack:
            k = stack.pop()
            if k in c.funcs or k not in self.funcs:
                continue
            c.funcs.add(k)
            f = self.program.funcs[k]
            for call in f["calls"]:
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
                wfile = w["lvalue"]["file"] if w.get("lvalue") else f["file"]
                pts, n = fe.pts_in_span(wfile, w["lc"])
                if n == 0:
                    # the store's debug location lies outside the lvalue's columns: every pointer on the
                    # statement's lines is a sound superset of the one written through
                    pts, n = fe.pts_in_span(wfile, [w["lc"][0], 0, w["lc"][2], 1 << 20])
                if n == 0:
                    s.unbounded.append(
                        {"function": f["name"], "site": w.get("site"), "why": "write through an unmapped pointer"}
                    )
                    continue
                add(pts, f, w.get("site"), "writes through a pointer")
            for call in f["calls"]:
                if self.program.resolve(k, call["callee"]):
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

    def escaped(self) -> set[int]:
        """Objects another thread of control may reach: the shared roots and everything their contents reach."""
        if self._escaped is not None:
            return self._escaped
        fe = self.fe
        if fe is None:
            self._escaped = set()
            return self._escaped
        roots: set[int] = {
            o
            for o, d in fe.objects.items()
            if d.get("kind") in ("global", "heap", "external") or o in fe.unknown_objects({o})
        }
        why: dict[int, str] = {}
        self._escape_unknown = []

        def exposed(file: str, designator: Any, lc: Any, what: str, loads: Any = None) -> None:
            """Add what an expression may point to (the object '&x' names, or the content of a variable
            read) to the shared roots."""
            objs: set[int] = set()
            mapped = False
            if loads:
                holders = fe.objects_for_decl(
                    loads.get("decl_file"), loads.get("decl_line"), loads.get("name"), bool(loads.get("global_"))
                ) or fe.objects_for_decl(loads.get("decl_file"), loads.get("decl_line"), f"{loads.get('name')}.addr")
                objs = {p for h in holders for p in (fe.nodes.get(h) or {}).get("pts", [])}
                mapped = bool(holders)
            elif designator:
                objs = fe.objects_for_decl(
                    designator.get("decl_file"),
                    designator.get("decl_line"),
                    designator.get("name"),
                    bool(designator.get("global_")),
                )
                mapped = bool(objs)
            elif lc:
                objs, n = fe.pts_in_span(file, lc)
                if n == 0:  # the value's debug location may be on a neighbouring line of the statement
                    objs, n = fe.pts_in_span(file, [max(1, lc[0] - 2), 0, lc[2] + 2, 1 << 20])
                mapped = n > 0  # pointer nodes found: their (possibly empty) points-to set is what is exposed
            if not mapped:
                self._escape_unknown.append(what)
            for o in objs:
                why.setdefault(o, what)
            roots.update(objs)

        # thread-start arguments and pointers converted to integers expose what they point to
        for k in self.funcs:
            f = self.program.funcs[k]
            for call in f["calls"]:
                m = self.program.models.lookup(call["callee"])
                for i in getattr(m, "shares", None) or []:
                    if i < len(call["args"]) and not call["args"][i].get("null"):
                        a = call["args"][i]
                        exposed(
                            f["file"], a.get("addr_of"), a.get("lc"), f"handed to {call['callee']}() in {f['name']}()"
                        )
        for fkey, per_unit in (self.program.inv.get("operations") or {}).items():
            if fkey not in self.funcs:
                continue
            file = self.program.funcs[fkey]["file"]
            for rec in per_unit.values():
                for site in rec.get("sites", []):
                    if site.get("kind") != "integer-pointer-conversion" or site.get("cast_kind") != "PointerToIntegral":
                        continue
                    what = f"converted to an integer in {fkey.split('::')[-1]}() at line {site.get('line')}"
                    if "operand_lc" not in site:
                        self._escape_unknown.append(what + " (inventory predates operand records)")
                        continue
                    if site.get("operand_null_based") or site.get("operand_function"):
                        continue
                    exposed(file, site.get("operand"), site.get("operand_lc"), what, site.get("operand_loads"))
        escaped = set(roots)
        stack = list(roots)
        while stack:
            m = stack.pop()
            for o in (fe.nodes.get(m) or {}).get("pts", []):  # an object node's points-to set: its contents
                if o not in escaped:
                    escaped.add(o)
                    stack.append(o)
        self._escaped = escaped
        self._escape_why = why
        return escaped

    @property
    def escape_unknown(self) -> list[str]:
        """Exposures Weaver could not map to objects; while any exist, no local is known not to escape."""
        self.escaped()
        return self._escape_unknown

    def concurrent(self, key: str, targets: set[int]) -> tuple[str, list[str], list[str]]:
        """(established | violated | unresolved, evidence, counter-evidence) for writes to ``targets``
        by any context that can run while the call to ``key`` is in progress."""
        pos: list[str] = []
        neg: list[tuple[str, str]] = []
        running = self.contexts_of(key)
        fname = self.program.funcs.get(key, {}).get("name", key)
        if not targets:
            return "established", [f"the parameter of {fname}() points to no object in any analysed call"], []
        unknown = self.fe.unknown_objects(targets) if self.fe else set()
        if unknown:
            return (
                "unresolved",
                [],
                [f"the parameter of {fname}() may point to memory points-to analysis cannot identify"],
            )
        if not running:
            if self.problems or any(c.unresolved for c in self.contexts.values()):
                return (
                    "unresolved",
                    [],
                    [f"{fname}() is reached from no declared task, and the declaration is incomplete"],
                )
            return (
                "established",
                [
                    f"no declared task runs {fname}() in {self.prog_key}: every thread of control is declared and "
                    "every indirect call resolved, and none reaches it"
                ],
                [],
            )
        if self.problems:
            neg.append(("unresolved", "the task declaration is incomplete: " + self.problems[0]))
        conflicting = [c for c in self.contexts.values() if c not in running or c.many or len(running) > 1]
        names = {o: self._name(o) for o in targets}
        esc = self.escaped()
        local = (
            set()
            if self.escape_unknown  # an exposure Weaver could not map: any local may be exposed
            else {o for o in targets if (self.fe.objects.get(o) or {}).get("kind") == "stack" and o not in esc}
        )
        checked = targets - local
        for c in conflicting:
            s = self.summary(c)
            hit = sorted(checked & set(s.objs))
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
                    names[o]
                    for o in checked
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
                reach = sorted(checked)
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
            pos.append("possible targets: " + ", ".join(sorted(names.values())))
            if local:
                pos.append(
                    "stack targets that never escape their task: "
                    + ", ".join(sorted(names[o] for o in local))
                    + " (their address is never stored in shared memory, handed to a thread or converted to an "
                    "integer; pointer provenance)"
                )
            return "established", pos + [f"assumption: {a}" for a in self.assumptions[:3]], []
        status = "violated" if any(s == "violated" for s, _ in neg) else "unresolved"
        # the writes that decide the verdict come first: callers show only the first few
        return status, [], [t for _, t in sorted(neg, key=lambda x: x[0] != "violated")]

    def _name(self, oid: int) -> str:
        d = self.fe.describe(oid) if self.fe else {"kind": "object"}
        if d.get("name"):
            return str(d["name"])
        where = f" at {d['file']}:{d['line']}" if d.get("file") else ""
        return f"a {d.get('kind') or 'unknown'} object{where}"

    @staticmethod
    def _label(c: Context) -> str:
        kind = {"task": "task", "interrupt": "interrupt", "dispatcher": "dispatcher", "implicit": "entry point"}[c.kind]
        return f"{kind} {c.name}" + (" (many instances)" if c.many else "")

    def ownership(self) -> list[dict[str, Any]]:
        """For each global object: the contexts whose code may write it, and its owner if there is one.

        An object is *owned* by a context when that context (one instance) is the only one that may
        write it and no other context has writes Weaver cannot bound.
        """
        if self.fe is None:
            return []
        writers: dict[int, list[str]] = {}
        for c in self.contexts.values():
            for o in self.summary(c).objs:
                writers.setdefault(o, []).append(c.name)
        unbounded = [c.name for c in self.contexts.values() if self.summary(c).unbounded]
        out = []
        for o, d in sorted(self.fe.objects.items()):
            if d.get("kind") != "global" or not d.get("name") or str(d["name"]).startswith("."):
                continue
            w = writers.get(o, [])
            maybe = [u for u in unbounded if u not in w]
            owner = w[0] if len(w) == 1 and not self.contexts[w[0]].many and not maybe else None
            loc = d.get("loc") or {}
            out.append(
                {
                    "object": d["name"],
                    "file": loc.get("file"),
                    "line": loc.get("line"),
                    "writers": w,
                    "unbounded": maybe,
                    "owner": owner,
                    "state": "owned"
                    if owner
                    else "unwritten"
                    if not w and not maybe
                    else "shared"
                    if len(w) > 1
                    else "open",
                }
            )
        return out

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
