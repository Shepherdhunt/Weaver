# Weaver architecture (milestones 1-2)

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
recipe therefore requires `preservation.concurrency: single-threaded` (or an equivalent
declaration) and otherwise stays unresolved. It also requires a complete caller set. That means
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

## Known limits

- Inventory facts are syntactic. Their possible targets are intraprocedural hypotheses, labelled as
  such. SVF points-to sets are flow- and context-insensitive and field-insensitive (Andersen).
- Flow evidence for GCC profiles comes from Clang bitcode of the same sources. GIMPLE-derived
  evidence from the production compiler is not collected yet.
- `scalar-input` handles pointers to scalars that are spelled as plain pointer declarators. Struct
  targets, pointer-to-pointer, array parameters and typedef'd pointer parameters are blocked.
- The fixture's effect models cover common libc functions. OSAL and cFS APIs need reviewed models
  before interface recipes can pass through them.
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

1. **cFS pilot**: pin cFS and its submodules, capture the native configuration, and produce the
   inventory, flow evidence and rejection report for `sample_app`. Add reviewed OSAL and cFE effect
   models. Study `SBBufPtr` as a borrowed-buffer contract.
2. **Output parameter to return value**: the second interface recipe. It needs "written on every
   path before any read" and the same complete-caller and may-modify machinery.
3. **GCC flow evidence**: GIMPLE-derived alias and points-to facts from the production compiler,
   cross-checked against the Clang/SVF evidence.
4. **Bounded checking**: CBMC equivalence harnesses as a `bounded-check` validation kind, recording
   unwinding bounds and assumptions.
5. **Durable frontend**: a LibTooling exporter with Weaver's own schema and preprocessor callbacks
   for macro provenance.
6. **Vendor adapters**: Diab and Green Hills capability adapters, driven by the installed manuals
   and probes.
