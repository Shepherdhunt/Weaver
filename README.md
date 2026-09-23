# Weaver

Weaver is a compiler-assisted tool for removing C pointers in small, reversible, evidence-backed
steps, toward a pointer-free CLite program. It also explains, after anyone edits the code, how
pointer behavior changed and whether an earlier refactor still holds. It implements the first two
milestones of the planning documents in this repository:

- [`pointer-tracker-plan.md`](pointer-tracker-plan.md): preservation contract, semantic pointer
  inventory, recipes with explicit preconditions, one reviewable transaction at a time, layered
  validation.
- [`compiler-artifact-plan.md`](compiler-artifact-plan.md): evidence from the actual production
  compiler, an optional secondary Clang frontend with fidelity checks, target profiles, flow
  evidence from a separate SVF job, and a normalized, source-linked evidence graph.

The central rule applies throughout: **preserve what each pointer does before changing how the
program represents it.** Compiler artifacts and points-to analysis supply the facts. Deterministic
recipes and the rewriter produce patches. An independent validation runner decides whether a patch
can be accepted. The optional LLM explains and recommends; it never edits or validates.

## Status

| Plan item | What exists |
|---|---|
| Build capture (artifact plan §3) | Compiler wrapper and shims that preserve the exit status. The capture log is turned into `compile_commands.json`, `links.json` and `tools.json`. Response files are expanded and hashed. Tool identity comes from path, SHA-256, `--version` and predefined macros. A profile can name its build command so `weaver refresh --capture` (and the web interface) rebuild through the shims. |
| Collection recipes (§§4-5) | Clang: `-E`, `-dM`, `-M`, JSON AST, LLVM IR (optimized and frontend-only), bitcode, record layouts. GCC: `-E`, `-dM`, `-M`, `-fdump-passes`, GENERIC/GIMPLE/SSA/alias/cgraph/RTL dumps, `-fstack-usage`, assembly, `-fcallgraph-info`. Each removed or added option is recorded. |
| Capability matrix (§2) | `weaver probe` runs every recipe on a fixture and validates the resulting artifact. Each capability is reported as `documented`, `probe-passed`, `unverified` or `unavailable-in-this-profile`. |
| Secondary frontend (§§6, 9) | Explicit GCC→Clang option translation. The target triple, dialect, system include list, implicit pre-includes and feature macros all come from the production compiler. Fidelity checks compare observable macros, the included header set, which conditional groups each compiler compiled, and ELF layout probes. Secondary-only *forwarding wrappers* (glibc's fortified `printf` under Clang) are recognized precisely rather than counted as differences. Each unit is labelled `native`, `secondary-checked`, `secondary-partial`, `secondary-unchecked` or `unsupported`. |
| Pointer inventory (tracker §3) | Pointer variables, parameters, fields, returns and typedef-hidden pointers, each with a stable ID. Every use is classified (dereference read/write, copy, call argument, return, comparison, cast, arithmetic, capture…). Per-function summaries record calls with argument designators, writes by name and through pointers, address-taken functions and declarations. Unresolved facts stay `unknown`. |
| Configuration coverage (tracker §2) | Code lines that no analysed configuration compiled, and project files no unit compiled or included. Both are reported as unexamined, never as pointer-free. |
| **Flow evidence** (artifact §§7, 10) | `weaver flow` builds frontend-only bitcode for every unit (with the secondary Clang for GCC profiles), links it, and runs SVF's Andersen analysis as a separate, time- and memory-limited `wpa` job. Points-to sets and indirect-call targets are mapped back to source. Runs are `complete`, `incomplete` or `failed`, and only complete, current evidence is used. |
| **May-modify query** | Can a call write what a parameter points to? It takes the closure over the call graph (with indirect calls resolved by SVF), named writes, writes through pointers intersected with points-to sets, and reviewed models of library functions. An unmodelled external call answers `unknown`, never `no`. |
| Recipe `local-alias` (tracker §§4-5) | Replaces a local alias of one known object with direct access to that object. 13 preconditions. |
| **Recipe `scalar-input`** (tracker §5) | Turns a read-only pointer-to-scalar parameter into a value parameter, and rewrites every declaration, dereference and call site (`&x` → `x`, `p` → `*p`). 10 preconditions, including SVF-backed may-modify, a complete caller set, sequencing at each call site, and a declared concurrency model. |
| Transactions (tracker §6) | Candidate cards. States: discovered → analyzed → blocked / proposed → validated / provisional / rejected → accepted / skipped → reverted. Patches are bound to source hashes. Revert uses a three-way merge so later unrelated edits survive. |
| Validation (tracker §7) | Isolated baseline and candidate workspaces. The production compiler rebuilds every unit whose main file *or included headers* were edited. A mechanical re-check parses the patched AST again. Configured builds, tests and differential comparisons run in both workspaces. An acceptance policy judges the results. |
| **Change impact** | Snapshots of pointer facts (the working tree, or any git revision). `weaver impact` explains each pointer whose behavior changed since a snapshot, which edited line caused it, which recipe verdicts flipped, which pinned or transaction-implied contracts broke, and optionally whether builds and differential runs still agree. `weaver check` exits 1 on high risk, for CI. |
| **Web interface** | `weaver serve`: load or set up a project, compile, then explore a map of every pointer colored by what it does to its target. Graphs show points-to and call relationships, and a source view has inline marks. Refactors can be proposed, validated and accepted, contracts pinned, and change impact compared. |
| LLM (tracker §10) | A focused evidence slice for one finding and the planner instruction taken verbatim from the plans. `weaver explain` runs an optional Claude tool loop whose tools can only read evidence. |

Not yet, following the plans' roadmap: the output-parameter, buffer/range, typed-ID and callback
recipes; CBMC equivalence harnesses; GIMPLE-derived flow evidence for GCC; vendor adapters (Diab,
Green Hills); linked-image evidence; and the cFS pilot. See
[`docs/architecture.md`](docs/architecture.md).

## Install

```sh
pip install -e .            # requires Python ≥ 3.10 and PyYAML
pip install -e '.[flow]'    # optional: SVF points-to analysis (pysvf bundles LLVM and wpa)
pip install -e '.[llm]'     # optional: Claude-backed `weaver explain`
pip install -e '.[test]'    # pytest
```

A production compiler (Clang or GCC-compatible) must be installed. For non-Clang profiles you also
need a Clang to act as the secondary frontend. The web interface uses only the standard library.

## Web interface

```sh
weaver serve [PROJECT_DIR]      # http://localhost:8765, loopback only
```

1. **Load**: open a directory that has a `weaver.yaml`, or set one up from the form. Give the build
   command with `{cc}` where the compiler goes (`make -B CC={cc}`), the production compiler, an
   optional analysis Clang, an optional test or run command, and the concurrency model.
2. **Compile**: rebuild through recording shims, collect artifacts, check fidelity, build the
   inventory, and run points-to analysis. Progress streams from a background job.
3. **Explore**:
   - **Map**: one card per file and one row per function. Every pointer is a chip colored by what
     it does to its target: read-only, writes through, escapes, reassigned or unused. A violet ring
     marks an eligible refactor, and pointers already removed appear dashed. The progress bar counts
     pointers removed so far. Hovering a function highlights its callers and callees.
   - **Graph**: for the selected pointer, its targets (SVF solid, syntactic hypotheses dashed), what
     each caller passes, and where it escapes. Or the whole call graph, colored by each function's
     riskiest pointer.
   - **Source**: inline marks for reads, writes, escapes and declarations, with hatching for code no
     configuration compiled.
4. **Refactor**: the details panel lists each recipe's preconditions with their evidence. **Propose**
   opens the transaction with its patch and preservation argument. **Validate** runs the isolated
   checks, and **Accept** applies the patch and re-analyses. Everything is recorded in the ledger
   and can be reverted.
5. **Guard**: pin what must stay true of a pointer ("read-only", "doesn't escape"…). Save a
   baseline, and after anyone changes the code, **Changes → Compare** explains what moved.

The server binds to 127.0.0.1. It rejects foreign `Host` headers (DNS rebinding) and requires a
per-process token on every API call. Modifying operations run one at a time.

## Command-line walkthrough

```sh
weaver init                                   # write weaver.yaml; record profiles, target and platform facts
weaver capture shim --tool cc=/usr/bin/gcc    # generate a recording shim
make clean && make CC=$PWD/.weaver/capture/shims/cc
weaver capture finalize --out build           # -> build/compile_commands.json, links.json, tools.json

weaver probe                                  # capability matrix for each profile
weaver refresh                                # collect -> fidelity -> inventory -> flow, for changed units
weaver coverage                               # code no configuration compiled
weaver candidates --all                       # eligible candidates, and blocked ones with reasons

weaver show P-1a2b3c4d5e                      # one finding: uses, targets, precondition evaluation
weaver explain P-1a2b3c4d5e --dry-run         # the LLM request (or run it with credentials)
weaver propose P-1a2b3c4d5e                   # open a transaction and print its candidate card and patch
weaver validate T-0bb5fce6                    # isolated compile, re-check, build, tests, differential run
weaver accept T-0bb5fce6                      # apply under the acceptance policy (or: skip)
weaver revert T-0bb5fce6                      # undo an accepted transaction
weaver auto --max 10                          # propose/validate/accept eligible candidates under the policy

weaver contract pin P-1a2b3c4d5e --expect read-only,no-escape --reason "callers reuse the buffer"
weaver snapshot save --name baseline          # or: weaver snapshot git origin/main --name main
weaver impact --since baseline --revalidate   # what changed, why, and whether behavior still agrees
weaver check --since main                     # same, exit status 1 on high risk (CI)
```

The individual stages remain available as `weaver collect`, `weaver fidelity`, `weaver inventory`
and `weaver flow`.

`weaver.yaml` records the profiles and their compile databases, the preservation contract, the
provisional CLite capability model, flow settings and reviewed library models, and the acceptance
policy. `weaver init` writes a commented template. Validation commands run inside a workspace copy,
referenced as `{workspace}`:

```yaml
preservation:
  behaviors: [outputs, persistent-state, side-effect-ordering]
  concurrency: single-threaded        # interface recipes stay blocked until this is declared
acceptance:
  require: [compile, mechanical-recheck, differential-testing]
flow:
  externals:                          # reviewed effect models for library calls
    rand: {writes: [], calls_back: false, assumptions: ["only updates its own hidden state"]}
profiles:
  - id: native-dev
    compile_commands: build/compile_commands.json
    capture: {command: "make -B CC={cc}", tools: {cc: gcc}}   # lets `refresh --capture` rebuild
    secondary_frontend: {compiler: clang}       # only when the production compiler is not Clang
    validation:
      build: {run: [make, -C, "{workspace}", BUILD=out], cwd: "{workspace}"}
      tests: [{name: unit, run: ["./out/tests"], cwd: "{workspace}"}]
      compare: [{name: demo, run: ["./out/demo"], cwd: "{workspace}"}]
```

## Example: read-only input becomes a value parameter

`tests/fixtures/demo/src/params.c` holds one positive example or counterexample per precondition.
For `p_scale`, `weaver propose` produces one transaction across the header, the definition and the
caller. The caller's argument sits inside a `printf(...)` that glibc makes a macro under Clang:

```diff
-int p_scale(const int *factor, int x);
+int p_scale(const int factor, int x);
 ...
-        printf("scale=%d sum2=%ld\n", p_scale(&k, 7), p_sum2(&la, &lb));
+        printf("scale=%d sum2=%ld\n", p_scale(k, 7), p_sum2(&la, &lb));
 ...
-int p_scale(const int *factor, int x)
+int p_scale(const int factor, int x)
 {
-    return x * *factor;
+    return x * factor;
 }
```

Each counterexample is rejected for its specific reason:

| Case | Blocking precondition |
|---|---|
| a callee writes the target through another pointer (`p_touch(w)` with `v` and `w` possibly aliased, per SVF) | `SI.no-modification-during-call` |
| a callee writes the target by name (`p_bump()` increments the global passed in) | `SI.no-modification-during-call` |
| a call into `rand()` with no reviewed effect model | `SI.no-modification-during-call` (unresolved) |
| the pointer is null-tested / written through / passed on / copied | `SI.read-only-uses` |
| the target is read on only some paths (`if (c) return *v; return 0;`) | `SI.unconditional-read` |
| a caller passes `0`, or another argument has side effects (`p_pair(&w, w++)`) | `SI.call-sites` |
| the function's address is taken (`int (*p_hook)(const int *) = p_cb;`) | `SI.complete-callers` |
| a reference in code no configuration compiles, or a use as a cleanup attribute | `SI.complete-callers` |
| the target is a struct or a pointer | `SI.parameter-type` |
| no concurrency model declared | `SI.no-concurrent-writers` |

The `local-alias` recipe has the same kind of table: the plan's §5 transformation and 17
counterexamples elsewhere in the fixture (see `tests/test_pipeline.py`).

## Example: someone else changed the code

After a baseline and a pinned contract on `p_sum2`'s `a`, a teammate adds `*a = 0;` and
`util_touch((int *)b);`. `weaver check --since baseline --revalidate` then prints (exit status 1):

```
Change impact since snapshot 'baseline' (2026-09-22T21:08:27+00:00): risk HIGH
  1 changed file(s); 5 high, 2 review, 0 info; 1 contract(s) violated
  changed src/params.c: lines 3-3, lines 16-19
  [HIGH  ] a in p_sum2(): line 17: deref (write) — was read-only; its target is now written
           *a = 0;                 /* teammate: reset the accumulator */
  [HIGH  ] a in p_sum2(): access class changed: read-only → writes
  [HIGH  ] b in p_sum2(): line 18: explicitly converted (BitCast) to int * — its value now leaves the function
           util_touch((int *)b);   /* teammate: record the access */
  [HIGH  ] b in p_sum2(): access class changed: read-only → escapes
  [REVIEW] a in p_sum2(): scalar-input was eligible and is now blocked — SI.read-only-uses: line 17: deref (write); …
  [REVIEW] b in p_sum2(): scalar-input was eligible and is now blocked — SI.read-only-uses: line 18: explicitly …
  contract C-818970c9: VIOLATED — a in p_sum2(): no-escape, read-only: read-only: line 17: deref (write)
  revalidation clang:build: passed (baseline ok, current ok)
  revalidation clang:demo-stdout: passed (identical exit status and stdout)
```

The program's output did not change, so the tests alone would have passed this edit. The
pointer-level explanation is what shows a reviewer that the assumptions behind `p_sum2`'s callers,
and behind any refactor that relied on them, no longer hold. Edits to lines an accepted transaction
produced, or a reintroduced alias of an object a refactor removed, are flagged the same way.

## Tests

```sh
python -m pytest            # unit tests, plus end-to-end tests when clang/gcc/make (and pysvf) are available
ruff check src tests
```

The end-to-end tests capture real builds of the fixture with Clang and GCC. They cover:

- multiple configurations and fidelity checks, including glibc forwarding wrappers;
- the full transaction lifecycle, including revert over a later user edit, and stale-source
  refusal;
- auto mode for both recipes with a differential run of the whole program;
- SVF flow evidence and the may-modify query;
- change impact with contracts, git snapshots and revalidation;
- the web server's request guards, its views, and the propose/validate/accept and setup/capture
  workflows through its jobs;
- the LLM tool loop against a fake client.

## License

MIT. See [LICENSE](LICENSE). SVF is AGPL-3.0-or-later. It is an optional extra, and Weaver never
imports it: the `wpa` binary runs as a separate process on bitcode files, and Weaver reads its text
output. Review that arrangement against your distribution plans before shipping SVF alongside
Weaver.
