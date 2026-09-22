# Weaver

Weaver is a compiler-assisted tool for removing C pointers in small, reversible, evidence-backed
steps, toward a pointer-free CLite program. It implements the first milestone of the two planning
documents in this repository:

- [`pointer-tracker-plan.md`](pointer-tracker-plan.md): preservation contract, semantic pointer
  inventory, recipes with explicit preconditions, one reviewable transaction at a time, layered
  validation.
- [`compiler-artifact-plan.md`](compiler-artifact-plan.md): evidence from the actual production
  compiler, an optional secondary Clang frontend with fidelity checks, target profiles, and a
  normalized, source-linked evidence graph.

The central rule applies throughout: **preserve what each pointer does before changing how the
program represents it.** Compiler artifacts supply the facts. Deterministic recipes and the
rewriter produce patches. An independent validation runner decides whether a patch can be
accepted. The optional LLM explains and recommends; it never edits or validates.

## Status: milestone 1

| Plan item | What exists |
|---|---|
| Build capture (artifact plan §3) | Compiler wrapper and shims that preserve the exit status. The capture log is turned into `compile_commands.json`, `links.json` and `tools.json`. Response files are expanded and hashed. Tool identity comes from path, SHA-256, `--version` and predefined macros. |
| Collection recipes (§§4-5) | Clang: `-E`, `-dM`, `-M`, JSON AST, LLVM IR (optimized and frontend-only), bitcode, record layouts. GCC: `-E`, `-dM`, `-M`, `-fdump-passes`, GENERIC/GIMPLE/SSA/alias/cgraph/RTL dumps, `-fstack-usage`, assembly, `-fcallgraph-info`. Each removed or added option is recorded. |
| Capability matrix (§2) | `weaver probe` runs every recipe on a fixture and validates the resulting artifact. Each capability is reported as `documented`, `probe-passed`, `unverified` or `unavailable-in-this-profile`. |
| Secondary frontend (§§6, 9) | Explicit GCC→Clang option translation. The target triple, dialect, system include list, implicit pre-includes and feature macros all come from the production compiler. Fidelity checks compare observable macros, the included header set, the conditional lines each compiler compiled, and ELF layout probes (sizes, alignments, offsets, char signedness). Each unit is labelled `native`, `secondary-checked`, `secondary-partial`, `secondary-unchecked` or `unsupported`. |
| Pointer inventory (tracker §3) | Pointer variables, parameters, fields, returns and typedef-hidden pointers, each with a stable ID. Every use is classified (dereference read/write, copy, call argument, return, comparison, cast, arithmetic, capture…). Per-function pointer operations are counted. Unresolved facts stay `unknown`. |
| Configuration coverage (tracker §2) | Code lines that no analysed configuration compiled, and project files no unit compiled or included. Both are reported as unexamined, never as pointer-free. |
| Evidence graph (artifact §10) | `weaver graph`: nodes and edges with provenance (profile, unit, file hash, producing tool, evidence status, fact kind). Unresolved targets and callees point to an explicit `unknown` node. |
| Recipe `local-alias` (tracker §§4-5) | Replaces a local alias of one known object with direct access to that object. 13 preconditions are each reported as established, violated or unresolved, with evidence. |
| Transactions (tracker §6) | Candidate cards. States: discovered → analyzed → blocked / proposed → validated / provisional / rejected → accepted / skipped → reverted. Patches are bound to source hashes. Acceptance keeps a checkpoint. Revert uses a three-way merge so later unrelated edits survive. |
| Validation (tracker §7) | Isolated baseline and candidate workspaces. The production compiler rebuilds every configuration that compiles the edited file. A mechanical re-check parses the patched AST again. Configured builds, tests and differential comparisons run in both workspaces. An acceptance policy judges the results, and a required check that could not run leaves the result provisional. |
| LLM (tracker §10) | A focused evidence slice for one finding and the planner instruction taken verbatim from the plans. `weaver explain` runs an optional Claude tool loop whose tools can only read evidence. |

Not in this milestone, following the plans' roadmap: LLVM/SVF flow and alias analysis, interprocedural
recipes (scalar parameters, output parameters, buffers and ranges, typed IDs, callbacks), CBMC
equivalence harnesses, vendor adapters (Diab, Green Hills), and linked-image evidence. See
[`docs/architecture.md`](docs/architecture.md).

## Install

```sh
pip install -e .            # requires Python ≥ 3.10 and PyYAML
pip install -e '.[llm]'     # optional: Claude-backed `weaver explain`
pip install -e '.[test]'    # pytest
```

A production compiler (Clang or GCC-compatible) must be installed. For non-Clang profiles you also
need a Clang to act as the secondary frontend.

## Walkthrough

```sh
weaver init                                   # write weaver.yaml; record profiles, target and platform facts
weaver capture shim --tool cc=/usr/bin/gcc    # generate a recording shim
make clean && make CC=$PWD/.weaver/capture/shims/cc
weaver capture finalize --out build           # -> build/compile_commands.json, links.json, tools.json

weaver probe                                  # capability matrix for each profile
weaver collect                                # run collection recipes for every unit
weaver fidelity                               # only for non-Clang production compilers
weaver inventory                              # findings, operations, coverage
weaver coverage                               # code no configuration compiled
weaver candidates --all                       # eligible candidates, and blocked ones with reasons

weaver show P-1a2b3c4d5e                      # one finding: uses, targets, precondition evaluation
weaver explain P-1a2b3c4d5e --dry-run         # the LLM request (or run it with credentials)
weaver propose P-1a2b3c4d5e                   # open a transaction and print its candidate card and patch
weaver validate T-0bb5fce6                    # isolated compile, re-check, build, tests, differential run
weaver accept T-0bb5fce6                      # apply under the acceptance policy (or: skip)
weaver refresh                                # re-collect changed units and rebuild the inventory
weaver revert T-0bb5fce6                      # undo an accepted transaction
weaver auto --max 10                          # propose/validate/accept eligible candidates under the policy
```

`weaver.yaml` records the profiles and their compile databases, the preservation contract, the
provisional CLite capability model, and the acceptance policy. `weaver init` writes a commented
template. Validation commands run inside a workspace copy, referenced as `{workspace}`:

```yaml
acceptance:
  require: [compile, mechanical-recheck, differential-testing]
  min_evidence: secondary-checked
profiles:
  - id: native-dev
    compile_commands: build/compile_commands.json
    secondary_frontend: {compiler: clang}       # only when the production compiler is not Clang
    validation:
      build: {run: [make, -C, "{workspace}", BUILD=out], cwd: "{workspace}"}
      tests: [{name: unit, run: ["./out/tests"], cwd: "{workspace}"}]
      compare: [{name: demo, run: ["./out/demo"], cwd: "{workspace}"}]
```

## Example: the plan's §5 transformation

On the fixture in `tests/fixtures/demo`, `weaver propose` produces:

```diff
 unsigned la_basic(void)
 {
     unsigned total = 3;
-    unsigned *p = &total;
-    *p += 2;
+    total += 2;
     return total;
 }
```

The candidate card lists each of the 13 preconditions with its evidence, for example
`LA.dereference-only: clang: 1 use(s): line 17 deref (readwrite)`. The same fixture holds deliberate
counterexamples, and each is rejected for the specific reason:

| Case | Blocking precondition |
|---|---|
| address passed to a callee | `LA.dereference-only`: passed as argument 1 to `util_touch()` |
| pointer reassigned / compared / indexed | `LA.dereference-only` |
| use inside a macro expansion | `LA.edits-in-source` |
| target name shadowed in an inner block | `LA.name-resolution` |
| reference in `#ifdef` code no configuration compiles | `LA.all-references-explained` |
| volatile pointee, non-volatile object | `LA.access-qualifiers` |
| target reached through another pointer (`&pp->b`) | `LA.target-stable` |
| multi-declarator, for-init, cleanup attribute | `LA.decl-shape` |
| `goto` that bypasses the initialization | `LA.initialization-dominates` |
| a configured profile compiles the file but was not analysed | `LA.configurations` |
| GCC profile before fidelity checks pass | `LA.configurations` (evidence `secondary-unchecked`) |

## Tests

```sh
python -m pytest            # unit tests, plus end-to-end tests when clang/gcc/make are available
ruff check src tests
```

The end-to-end tests capture real builds of the fixture with Clang and GCC. They cover multiple
configurations, fidelity checks, the full transaction lifecycle including revert over a later user
edit, stale-source refusal, auto mode with a differential run of the whole program, and the LLM
tool loop against a fake client.

## License

MIT. See [LICENSE](LICENSE). The artifact plan notes that SVF is AGPL-3.0-or-later; review that
before adding it as a dependency.
