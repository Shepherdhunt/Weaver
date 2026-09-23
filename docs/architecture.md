# Weaver architecture (milestones 1-3, task ownership)

This document maps the planning documents to code, records design decisions, and lists known limits
and next steps. "Tracker" refers to `pointer-tracker-plan.md`; "artifact" refers to
`compiler-artifact-plan.md`.

## Data flow

```
build ──(shims)──▶ capture log ──▶ compile_commands.json / links.json / tools.json
                                          │
         weaver.yaml (profiles, target/platform facts, contract, CLite model, policy)
                                          │
                     ┌────────────────────┴────────────────────┐
                     ▼                                         ▼
          toolchain.collect (production recipes)     toolchain.probe (capability matrix)
          + secondary Clang (translated options)
                     │  .weaver/evidence/<profile>/<unit>/{manifest.json, unit.i, unit.macros.txt,
                     │                                    unit.d, unit.ast.json.gz, secondary.*, …}
                     ▼
          fidelity (macros, headers, active lines, ELF layout probes) ──▶ evidence status per unit
                     │
                     ▼
          analysis.inventory ──▶ inventory.json (findings, uses, operations, coverage)
                     │                   │
                     │                   └──▶ analysis.graph ──▶ graph.json (provenance on every edge)
                     ▼
          recipes.<recipe>.evaluate ──▶ preconditions + byte-range edits
                     │
                     ▼
          ledger.propose ──▶ validate (workspaces: compile, re-check, build, tests, diff) ──▶ accept / skip / revert
                     ▲
          llm.slice / llm.client (explain only; read-only evidence tools)
```

Milestone 2 adds three branches to that pipeline:

```
  per-unit flow bitcode ──llvm-link──▶ program.bc ──wpa (separate process, time/memory limits)──▶ wpa.out
                                                                                                      │
       flow.parse (points-to sets, indirect-call targets, source locations) ──▶ .weaver/flow/<profile>/flow.json
                                                                                                      │
  inventory function summaries + flow.models (reviewed library effects) ──▶ flow.program.may_modify ◀─┘
                                                                                     │
                                                                           recipes.scalar_input

  snapshot (working tree or git revision: inventory + verdicts + source tree)
        └──▶ impact.diff (facts by stable ID, edited hunks, verdict flips) + contracts + transaction overlaps
                   └──▶ report / `weaver check` exit status / optional revalidation of both trees

  web.server (stdlib HTTP, token + Host checks, background jobs) ──▶ web.api view models ──▶ static SPA
```

Milestone 3 (the cFS pilot) makes these whole-program and production-compiler aware:

```
  links.json ──▶ link.LinkModel: images (executable / shared / relocatable), archive members the linker
                 really loads (replayed with -Wl,-t,-t), -l libraries, dynamic exports and imports
                      └──▶ programs: declared in weaver.yaml (closed, entry points) or one per image
                                │
       ┌────────────────────────┴───────────────────────────┐
       ▼                                                    ▼
  flow.svf: one wpa job per program                   flow.gcc_pta: production GCC recompiles each unit
  (.weaver/flow/<profile>/programs/<p>/)              with -flto -fipa-pta, replays each image's link
       │                                              (version script = the program's entry points and
       │                                              imports), parses GCC's own points-to solution
       ▼                                                    ▼
  flow.program.may_modify (+ boundary models) ──▶ scalar-input may-modify ◀── GccPta.may_modify
                                        (a "yes" from any backend wins; flow.agreement: all | any)

  analysis.borrow ──▶ "borrowed" contracts (never written, never kept) over the same use graph
  report ──▶ `weaver report`: per-scope evidence, recipe verdicts, blockers, sole blockers, contracts
  testdetect + settings ──▶ validation commands at setup and in the settings editor
  validate: per-test comparison (CTest, Meson) and a recorded strength (behavioural / compile-only)
```

Task ownership and scale make the interface recipes usable on multi-task programs:

```
  weaver.yaml preservation.concurrency: tasks, interrupts, dispatchers, indirect-call targets
        │  checked against thread starts (models with `spawns`), the link model's entry points,
        │  SVF's unresolved indirect calls, and `unless_called` guards
        ▼
  flow.tasks.TaskModel: contexts ──▶ call-graph closures ──▶ per-context write summaries (SVF objects)
        │                            thread-escape closure from shared roots (globals, heap, unknown
        │                            memory, thread-start arguments, pointers converted to integers)
        ▼
  SI.no-concurrent-writers: concurrent(function, targets) ──▶ established / violated / unresolved

  analysis.identindex (identifier -> files, cached on disk) + bounded lexer and AST caches + slotted,
  interned locations ──▶ whole-program evaluation of cFS in under 1 GB
  web.export ──▶ `weaver export-ui`: the interface's recorded answers as one read-only page
```

## Module map

| Module | Plan section | Responsibility |
|---|---|---|
| `capture/wrapper.py`, `capture/shims.py` | artifact §3 | Record argv, working directory, relevant environment, response-file contents and exit status. Split multi-source invocations. Separate link invocations. |
| `capture/compdb.py` | artifact §3 | Compilation database (`arguments` / `command`) and GNU-style response files. One unit per entry. |
| `capture/toolid.py` | artifact §2 | Executable identity: resolved path, SHA-256, banner, family and version from predefined macros, `-dumpmachine`. |
| `toolchain/sanitize.py` | artifact §4 | Remove the source, output, action, dependency, LTO and link-only options. Every removal is logged with a reason. |
| `toolchain/recipes.py` | artifact §§4-5 | Command templates, deviations, frontend-interface flags, and the probe-time artifact checks. |
| `toolchain/translate.py` | artifact §§6, 8 | Explicit GCC→Clang translation, with each option logged as kept, dropped, `UNRESOLVED` or substituted. |
| `toolchain/collect.py` | artifact §§3, 10 | Per-unit collection. Cache keyed by source and dependency hashes, command, tool hashes, recipe list and translation version. Also writes the profile manifest. |
| `toolchain/probe.py` | artifact §2 | Capability status per recipe for the production compiler and the secondary frontend. |
| `fidelity/` | artifact §9 | Secondary-frontend compatibility. Findings are kept separate from transformation verification. |
| `frontend/clang_ast.py` | artifact §4 | Replays the JSON dumper's elided `file`/`line` state and resolves macro spelling and expansion locations. |
| `frontend/lexer.py` | tracker §§3, 9 | Raw lexer over all source text, including inactive conditional groups and directives. |
| `frontend/typestr.py` | tracker §3 | Parses C type spellings into a tree: pointer, array, function, qualifiers, `_Atomic`, typedef resolution. |
| `frontend/preproc.py`, `analysis/coverage.py` | tracker §2 | Token-bearing source lines that each configuration compiled, and unexamined ranges. |
| `analysis/uses.py` | tracker §3 | Syntactic use classification. Access shape only; says nothing about aliasing. |
| `analysis/inventory.py` | tracker §3 | Findings with stable IDs, occurrences per configuration, operations, and pointer-bearing records. |
| `analysis/graph.py` | artifact §10 | Normalized graph with provenance and explicit `unknown`. |
| `recipes/` | tracker §§4-5 | Recipe interface, the evaluation context (with lazy whole-program and flow views), `local-alias` and `scalar-input`. |
| `analysis/functions.py` | tracker §3 | Per-function summaries: direct calls with argument spans, `&designator`s, side effects and null constants; indirect calls; writes by name and through pointers; address-taken functions; declarations with parameter spans. |
| `frontend/wrappers.py` | artifact §9 | Secondary-only forwarding wrapper macros (glibc fortify under Clang), and the location unwrapping that makes their arguments plain file text. |
| `flow/svf.py`, `flow/parse.py` | artifact §§7, 10 | Flow bitcode, linking, the `wpa` job (runtime library fix-ups, limits, provenance), and the parser for its text output. |
| `flow/evidence.py` | artifact §10 | Points-to queries by parameter, declaration and source span; staleness against the inventory. |
| `flow/models.py`, `data/external_models.yaml` | artifact §7 | Reviewed effect models for library functions (argument indices written, callbacks, assumptions), normalizing `_chk`/`__builtin_` spellings. |
| `flow/program.py` | tracker §5 | Call-graph closure and the may-modify query (`no` / `yes` / `unknown`, with reasons). |
| `pipeline.py` | — | Capture → collect → fidelity → inventory → flow, shared by `refresh`, `auto` and the web interface. |
| `impact.py` | — | Snapshots, fact diff, pinned and transaction-implied contracts, transaction overlap, revalidation, report. |
| `web/` | — | `weaver serve`: HTTP server, jobs, view models (`api.py`) and the static interface. |
| `rewrite.py` | tracker §§6, 9 | Deterministic byte-range edits bound to file hashes, overlap and stale checks, diffs, offset maps. |
| `validate.py` | tracker §7 | Isolated workspaces, path remapping, compile, mechanical re-check, builds, tests, differential runs, and judgement under the acceptance policy. |
| `ledger.py`, `card.py` | tracker §6 | Transaction states, event log, checkpoints, three-way revert, and candidate cards. |
| `llm/` | tracker §10, artifact §§10-11 | Evidence slice, planner instruction, and an optional Claude tool loop (read-only tools, transcripts saved). |
| `link.py` | artifact §§3, 7 | Link model: images, archive members from a traced link replay, libraries and their undefined symbols, dynamic exports and imports, programs, and who outside the analysed code can call a function. |
| `flow/gcc_pta.py` | artifact §§7, 10 | GCC-native flow evidence: LTO objects with `-fipa-pta`, image link replays, the `pta2` dump parser, and `may_modify` over GCC's points-to and clobber sets. |
| `data/models/posix.yaml`, `data/models/cfs.yaml` | artifact §7 | Reviewed effect-model packs: POSIX/glibc (always loaded) and `builtin:cfs` (cFE/OSAL APIs as boundary models, framework-owned state, retained arguments). |
| `analysis/borrow.py` | tracker §3 | The borrowed-pointer contract: follows a pointer through casts, copies and calls, and reports writes, retention and unknown sinks. |
| `report.py` | tracker roadmap | `weaver report`: inventory and rejection report for a scope of the project. |
| `flow/tasks.py` | tracker §5 | Task model: declared threads of control checked against thread starts and entry points, per-context write summaries, thread-escape analysis, and the concurrent-writer query behind `weaver tasks` and `SI.no-concurrent-writers`. |
| `analysis/identindex.py` | tracker §§3, 9 | Identifier-to-files index for whole-tree textual reference scans, cached on disk by path, size and modification time. |
| `web/export.py` | — | `weaver export-ui`: records the interface's read-only answers (optionally for a scope) into one HTML page that needs no server. |
| `testdetect.py`, `settings.py` | tracker §7 | Detecting a project's test commands (Make, CTest, Meson, scripts), and editing validation commands and the acceptance policy in `weaver.yaml` with a backup and reload check. |

## Design decisions

**Evidence comes from artifacts, not an in-process frontend.** The analyzer reads the JSON AST that
the production Clang (or the labelled secondary Clang) emitted during collection. Every fact can
therefore be traced to a stored artifact, a command and a tool hash. The plan prefers a LibTooling
exporter for a durable tool. This container has no Clang development headers, and the JSON dump
already carries resolved types, `referencedDecl` links, byte offsets and macro locations, so
milestone 1 uses the pinned dump. The schema is treated as version-specific: the producer version
is recorded and probed. A LibTooling exporter that writes Weaver's own schema is a roadmap item.

**The raw lexer complements the AST.** The AST shows only code that was active in an analysed
configuration. The lexer sees everything, so a recipe can require that every textual occurrence of
a name in scope, including in `#if 0`, macro bodies and inactive branches, is explained by some
analysed AST. The same lexer gives token-exact checks of each edit range.

**Validation includes a mechanical re-check.** After patching, the file is parsed again in the
workspace with the same frontend. The re-check confirms that the pointer's declaration is gone,
every replaced site binds to the original target declaration (catching shadowing and macro
capture), and no reference to the old name remains in its scope.

**Unknown stays unknown.** Missing artifacts, unparsed files, unexamined lines, untranslated
options, unchecked secondary evidence and unresolved callees are all reported explicitly. None of
them can make a precondition established.

**An extra evidence status.** `secondary-unchecked` is added to the plan's four statuses so that
"not yet checked" is never read as "partially checked".

**Identifiers.** Finding IDs hash the file, scope, name, kind and ordinal. They stay stable across
unrelated edits, and are tied to a revision through the recorded `file_sha256` and occurrences.
Unit IDs hash the profile, directory, file and exact command.

**SVF runs as a separate job and is read from its text output.** pysvf 1.0.0.43 bundles LLVM 21 and
the `wpa` binary. Its Python `AndersenBase` produced empty points-to sets, and `wpa -dump-json`
crashed, so Weaver runs `wpa -ander -field-limit=0 -print-all-pts -print-fp` under a timeout and
an address-space limit, then parses the text. Weaver locates the binary without importing pysvf,
so no AGPL code runs in its process. `-field-limit=0` makes the analysis field-insensitive: SVF's
text output does not link field objects to their base object, and merging fields into the base is
the sound choice. Frontend-only bitcode (`-disable-llvm-passes`, `-g`, value names kept) keeps
source locations and variable names. For GCC profiles the bitcode comes from the secondary Clang,
and the flow evidence carries that unit's evidence status. A run with parse errors or missing units
is `incomplete` and is not used.

**May-modify is a conservative closure.** For a parameter `p` of `f`, every function reachable from
`f` is checked. Indirect calls use SVF targets, or are `unknown` without them. Writes by name to an
object in `pts(p)` answer `yes`. So do writes through a pointer whose points-to set meets `pts(p)`.
External calls are `unknown` unless a reviewed model lists which arguments they write. When every
call site passes `&object`, those designators bound `pts(p)` even without SVF. The answer is `no`
only when nothing is unknown.

**Interface recipes need declared assumptions.** Reading `*p` once at the call site instead of
inside the callee is only equivalent if no other thread writes the object during the call. The
recipe therefore requires a concurrency declaration: `single-threaded` (checked: no call in the
program starts a thread) or a task model (below), and otherwise stays unresolved. It also requires
a complete caller set. That means
the address is never taken, every textual reference to the name, including inactive code, is an
analysed call or declaration, every linked object was analysed, and the interface is not frozen.

**Forwarding wrappers are recognized, not ignored.** With `_FORTIFY_SOURCE`, glibc defines
`printf(...)` as a macro forwarding to `__printf_chk` for Clang, while GCC sees an inline function.
A macro is transparent only when it is secondary-only and function-like, its whole body is a
single call to the fortified or builtin spelling of the same function, and every parameter appears
exactly once as a whole argument, with no `#` or `##`. Fidelity records such macros under
`forwarding`. The AST loader then reads tokens inside those invocations' arguments at their
spelling locations, unless the argument text names a function-like macro, whose arguments would
share the same expansion location. Active code is compared per conditional segment (the lines
between conditional directives), because activity can only change at a directive. This also makes
the comparison independent of how each compiler lays out expanded macros in `-E` output.

**Impact compares facts, not text.** Findings are matched by stable ID. A new use is judged against
the old access class: a write through a formerly read-only pointer, or a new escape, is high
severity. Changes are tied to edited hunks. Contracts come from two sources: pinned expectations
in `weaver-contracts.yaml`, and implications of accepted transactions (no reintroduced alias of
the target, the parameter is still by value). Revalidation rebuilds the snapshot tree and the
current tree and compares the configured runs. The report says plainly when the runs agree but
the pointer facts do not.

**The web interface is a view over the same evidence.** It binds to loopback. It requires a
per-process token and a loopback `Host` header, accepts only JSON POSTs, and sends a strict CSP.
It inserts project text only through `textContent`. Long operations are background jobs with
streamed logs, and only one modifying operation runs at a time.

**Programs, not files, are the unit of whole-program claims.** A cFS build links cFE, OSAL and
PSP into one executable, loads applications as shared objects by name, and builds host tools and
test modules from the same tree. Linking every unit into one bitcode module fails (several `main`s)
and would be wrong anyway. The link model groups images into programs. A declared program is
closed over its images: only its entry points, and symbols another of its images imports, are
callable from outside. An undeclared shared object is an open program whose every export may be
called. Archive membership comes from replaying the captured link with `-Wl,-t,-t`, because a static
link loads only the members that resolve a symbol.

**Boundary models stand in for framework internals.** For an application, a cFE API call is a
contract, not code to be re-analysed: `CFE_SB_TransmitMsg` copies the message, and
`CFE_SB_ReceiveBuffer` writes only its first argument. A model marked `boundary: true` is used
instead of the analysed body, even when that body is in the program. `writes_owned` models writes
to framework-owned state (paths the pack `owns`), which can only matter for a pointer that may
point there. Each boundary model in `data/models/cfs.yaml` was checked against the cFS v7.0.1
source; three needed care: `CFE_TBL_Load`/`Manage`/`Validate` can call the application's
validation callback, `OS_TimerCreate` writes arguments 1 and 3, and `OS_TaskCreate` keeps its stack
pointer.

**Borrowed pointers are a contract, not a recipe.** `SBBufPtr` points into a software-bus buffer
that stays valid only until the next receive on the pipe. The `borrowed` expectation holds when no
path writes through the pointer or anything derived from it, and none stores it anywhere that
outlives the call (a global, the heap, a struct field, a return value). The check follows casts,
`&p->field`, local copies and analysed callees' parameters. For modelled callees it uses their
`writes` and `retains`. Any other sink is unknown, and a contract with unknowns cannot be pinned.

**GCC's own points-to is the second backend.** SVF analyses Clang bitcode. For GCC profiles that
is the secondary frontend's view of the code. `flow.gcc_pta` asks the production compiler instead:
it recompiles each unit with its production command plus `-O1 -flto -fipa-pta`, with inlining and
parameter-rewriting IPA passes off so functions keep their source shape. It then replays each
image's captured link with LTO and reads GCC's `pta2` dump. `F.clobber` (what a call may write,
callees and resolved indirect calls included) is intersected with `F.argN` (what the parameter may
point to over all call sites). Only a named object in both sets counts as a write (`yes`). An
overlap through memory GCC does not track (`NONLOCAL`, `ESCAPED`: what external code such as the C
library may write, for which GCC has no model), or an `ANYTHING` set, answers `unknown`. That is
"cannot tell", not counter-evidence. GCC names objects
by declaration name, so two objects with the same name are merged. Two `static` functions of one
name (LTO's `.lto_priv.N`) are united. Clones that may renumber parameters (`.isra`, `.constprop`,
`.part`) make answers `unknown`. Each deviation from the production flags is recorded. Both merges
can only add apparent overlaps. Solutions are keyed to the inventory's source hashes, and stale
ones are not used.

**Backends combine conservatively.** `flow.backend` selects `svf`, `gcc`, both (`auto`, the
default: whatever is available) or `none`. A "may write" from any backend blocks the candidate.
Under `flow.agreement: all` (the default), every backend that produced evidence must say "no
write". With `any`, one "no" suffices and the disagreement is recorded in the precondition's
evidence. Without SVF evidence, GCC's whole-image answer replaces the designator-only reasoning.
SVF is therefore optional: a GCC project gets flow evidence with no AGPL component installed.

**Validation strength is recorded, not implied.** A transaction that compiled and re-checked has
not run. Validation records `strength: behavioural` only when a test or differential run passed on
the patched tree, and acceptance copies it. The card, the ledger, the impact report and the web
interface flag `compile-only` acceptances. The project's configured strength (compile-only,
tests optional, or tests required) is shown in the top bar. Tests from runners that report
individual results (CTest, Meson) are compared test by test against the unpatched baseline. A test
that passes there and fails with the patch rejects the change. A test that fails on both is
recorded as pre-existing and not attributed to the patch. Before this, one sandbox-dependent OSAL
test made every cFS transaction fail validation.

**Concurrency is a declared, checked task model.** Which code runs in which thread of control is
a property of the whole system, often decided by a start-up script or a table, so Weaver does not
guess it. The project declares its tasks, interrupt contexts, dispatchers (trampolines such as
`OS_PthreadTaskEntry` that run in every task they start) and targets for indirect calls SVF cannot
resolve. Weaver then checks the declaration against the code. Every call whose effect model
`spawns` a thread (`pthread_create`, `OS_TaskCreate`, `signal`) must start a declared entry,
identified by name or by the argument's points-to set. Every entry point of the link model must
belong to a context. A declared indirect-call resolution is refused when the program calls a
function listed in its `unless_called` (the one that would install more targets). Anything missing
makes the model incomplete, and an incomplete model establishes nothing. Programs the declaration
does not cover must start no thread.

**Stack objects are private unless their address escapes.** Write summaries are context-insensitive:
a helper called from two tasks appears to write, in each, everything any caller passes it. Without
more, every caller's local would look shared. A local lives in the frame of the task running its
function, and another task, or another instance of the same task, can reach it only through its
address. Weaver computes the closure of object contents in SVF's points-to graph from the shared
roots: globals, heap and unknown objects, arguments handed to a thread start (a model's `shares`),
and pointers converted to integers (recorded with their operand, so `(uintptr_t)&x` exposes `x`). A
stack target outside that closure cannot be written concurrently. This relies on pointer
provenance, which optimizing compilers already assume. If a conversion's operand cannot be mapped,
no local is exempted.

**Memory scales with the analysed program, not the source tree.** Whole-program evaluation of cFS
peaked at 6.8 GB. Most of it was whole-tree token lists for complete-caller scans (1.6 million
tokens) and AST dictionaries kept after their locations were resolved. The scans now use an
identifier-to-files index, cached on disk, and re-lex only the few files that mention a name.
Lexed files and ASTs live in bounded LRU caches. Locations and tokens are slotted and interned, raw
location dictionaries are dropped once resolved, and documentation-comment nodes are skipped while
their locations still advance the resolver's state. Peak memory is now under 1 GB for the same
run.

**A read-only snapshot shares the interface without a server.** `weaver export-ui` asks the same
routes the browser asks, for every view that changes nothing, and embeds the answers next to the
unchanged `app.js`, which serves them in snapshot mode. Actions that would change the project show
the command that runs them locally. Paths of the exporting machine are replaced by the project
name. The page contains the analysed source code, so it is shared like the source tree.

## Known limits

- Inventory facts are syntactic. Their possible targets are intraprocedural hypotheses, labelled as
  such. SVF points-to sets are flow- and context-insensitive and field-insensitive (Andersen).
- GCC's points-to solution comes from an `-O1` LTO build, not the production `-O*` level, and
  names objects by declaration name. On cFE it saturates: 759 of the 1,068 functions left in
  `core-cpu1` have `ANYTHING` in their clobber set, so GCC decides far fewer candidates than SVF
  (see [`pilots/cfs`](../pilots/cfs/README.md)). It is most useful as an independent cross-check,
  and as the only backend where SVF cannot be used. Scalar-input is the only recipe that consults
  it so far.
- Whole-program recipe evaluation on cFS takes about 4 minutes per recipe with under 1 GB peak
  memory. Function summaries and flow evidence for the whole program stay in memory, so memory
  still grows with the size of the analysed program; the rest of the source tree costs only its
  identifier index.
- `scalar-input` handles pointers to scalars that are spelled as plain pointer declarators. Struct
  targets, pointer-to-pointer, array parameters and typedef'd pointer parameters are blocked.
- Effect models cover POSIX/glibc and the cFE/OSAL APIs `sample_app` uses. Other cFS
  applications will call APIs without a reviewed model, and those calls answer `unknown`.
- The task model is only as precise as SVF's field- and context-insensitive points-to sets. A write
  to one field of a global table counts as a write to the whole table, and writes through pointers
  into memory SVF cannot identify make a context's writes unbounded. On cFE no global has a single
  owner at this precision.
- Locks are not modelled. A write under the same mutex as the call still counts as concurrent, so
  the answer can only be conservative. Instance counts are declared (`instances: many`), not read
  from start-up scripts.
- Only programs with current SVF evidence get a task model. GCC's points-to is not used for it yet.
- `local-alias` covers automatic locals in a unit's main file. Pointers declared in headers need
  coverage across translation units and are reported as unresolved.
- Coverage treats lines inside a parenthesized group opened on a covered line as covered, because
  `-E` folds multi-line macro invocations. A conditional directive in between disables that rule.
- Change impact matches findings by stable ID. Renaming a pointer or moving it to another function
  shows up as one removed finding and one added finding.
- Layout probes need ELF objects. Bit-field layout, aggregate calling conventions, interrupt and
  atomic interfaces are listed as not covered.
- The fidelity checks and translation tables are reviewed for GCC-compatible drivers only. Vendor
  drivers get `unverified` capabilities until an adapter exists.
- Validation can say "testing", "differential testing" or "mechanical re-check". It never claims a
  universal proof.

## Next milestones (from the plans' roadmaps)

1. **Output parameter to return value**: the second interface recipe, and the one cFE's idioms call
   for (most scalar pointer parameters there return a value next to a status code). It needs
   "written on every path before any read" and the same complete-caller, may-modify and task checks.
2. **Field-sensitive, lock-aware ownership**: field objects linked to their base in the points-to
   evidence, so a task's write to one field of `CFE_ES_Global` does not cover the others, and lock
   regions from OSAL mutex models, so writes under the call's own lock are not concurrent.
3. **More GCC evidence**: use GCC's points-to in `local-alias` and in the borrow check, and read
   modref summaries at the production optimization level.
4. **Bounded checking**: CBMC equivalence harnesses as a `bounded-check` validation kind, recording
   unwinding bounds and assumptions.
5. **Durable frontend**: a LibTooling exporter with Weaver's own schema and preprocessor callbacks
   for macro provenance.
6. **Vendor adapters**: Diab and Green Hills capability adapters, driven by the installed manuals
   and probes.
