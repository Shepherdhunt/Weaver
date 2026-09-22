# Weaver architecture (milestone 1)

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
| `recipes/` | tracker §§4-5 | Recipe interface, the evaluation context, and `local-alias`. |
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

## Known limits of milestone 1

- Inventory facts are syntactic. Possible targets are flow-insensitive, intraprocedural hypotheses
  and are labelled that way.
- `local-alias` covers automatic locals in a unit's main file. Pointers declared in headers need
  coverage across translation units and are reported as unresolved.
- Coverage treats lines inside a parenthesized group opened on a covered line as covered, because
  `-E` folds multi-line macro invocations. A conditional directive in between disables that rule.
- Layout probes need ELF objects. Bit-field layout, aggregate calling conventions, interrupt and
  atomic interfaces are listed as not covered.
- The fidelity checks and translation tables are reviewed for GCC-compatible drivers only. Vendor
  drivers get `unverified` capabilities until an adapter exists.
- Validation can say "testing", "differential testing" or "mechanical re-check". It never claims a
  universal proof.

## Next milestones (from the plans' roadmaps)

1. **Flow evidence**: pin an LLVM + SVF analysis job (Andersen first) on the collected bitcode.
   Export SVF results into the graph with incomplete-run diagnostics, external API models (cFS,
   OSAL), and GIMPLE-derived evidence for GCC profiles. Review SVF's AGPL licensing first.
2. **Interface recipes**: read-only scalar input to value parameter, and isolated output parameter
   to return value. Both need complete caller sets and alias analysis (the plan's `update(&x, &x)`
   rejection case is already in the fixture).
3. **cFS pilot**: pin cFS and its submodules, capture the native configuration, and produce the
   inventory and rejection report for `sample_app`. Then run `local-alias`. Study `SBBufPtr` as a
   borrowed-buffer contract.
4. **Bounded checking**: CBMC equivalence harnesses as a `bounded-check` validation kind, recording
   unwinding bounds and assumptions.
5. **Durable frontend**: a LibTooling exporter with Weaver's own schema and preprocessor callbacks
   for macro provenance.
6. **Vendor adapters**: Diab and Green Hills capability adapters, driven by the installed manuals
   and probes.
