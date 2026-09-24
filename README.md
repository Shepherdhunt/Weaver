# Weaver

Weaver is a compiler-assisted tool for removing C pointers in small, reversible, evidence-backed
steps, toward a pointer-free CLite program. It also explains, after anyone edits the code, how
pointer behavior changed and whether an earlier refactor still holds. It implements the first three
milestones of the planning documents in this repository, the third being a pilot on NASA's core
Flight System ([`pilots/cfs`](pilots/cfs/README.md)):

- [`pointer-tracker-plan.md`](pointer-tracker-plan.md): preservation contract, semantic pointer
  inventory, recipes with explicit preconditions, one reviewable transaction at a time, layered
  validation.
- [`compiler-artifact-plan.md`](compiler-artifact-plan.md): evidence from the actual production
  compiler, an optional secondary Clang frontend with fidelity checks, target profiles, flow
  evidence from a separate SVF job and from the production GCC itself, and a normalized,
  source-linked evidence graph.

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
| **Link model and programs** | Images (executables, shared objects), the archive members a link really loads (from a traced replay), `-l` libraries, and dynamic exports and imports. Programs group the images that run together: declared in `weaver.yaml` with their entry points (closed), or one per image. "Who else can call this function?" is answered per program. |
| **Flow evidence** (artifact §§7, 10) | `weaver flow` builds frontend-only bitcode for every unit (with the secondary Clang for GCC profiles), links it per program, and runs SVF's Andersen analysis as a separate, time- and memory-limited `wpa` job. Points-to sets and indirect-call targets are mapped back to source. Runs are `complete`, `incomplete` or `failed`, and only complete, current evidence is used. |
| **GCC flow evidence** | For GCC profiles, `weaver flow --backend gcc` asks the production compiler: each unit is recompiled with `-flto -fipa-pta`, each image's link is replayed, and GCC's own points-to and clobber sets answer may-modify. SVF and GCC are cross-checked (`flow.backend`, `flow.agreement`); a write found by either blocks. SVF is optional. |
| **May-modify query** | Can a call write what a parameter points to? It takes the closure over the call graph (with indirect calls resolved by SVF), named writes, writes through pointers intersected with points-to sets, and reviewed models of library functions. An unmodelled external call answers `unknown`, never `no`. |
| **Effect models** | Reviewed packs: POSIX/glibc (always loaded) and `builtin:cfs` for cFE and OSAL APIs, as boundary models that stand in for the framework's code, with framework-owned state and retained arguments. |
| **Task ownership** | For multi-task programs, `preservation.concurrency` declares the tasks, interrupt contexts, dispatchers and unresolved indirect-call targets. Weaver checks the declaration against every thread start and entry point, summarises each context's writes, and exempts stack objects whose address never escapes their task. `SI.no-concurrent-writers` then names the task and line of a possible concurrent write, or establishes that none exists. `weaver tasks` shows the contexts, what was checked and which contexts may write each global. |
| Recipe `local-alias` (tracker §§4-5) | Replaces a local alias of one known object with direct access to that object. 13 preconditions. |
| **Recipe `scalar-input`** (tracker §5) | Turns a read-only pointer-to-scalar parameter into a value parameter, and rewrites every declaration, dereference and call site (`&x` → `x`, `p` → `*p`). 10 preconditions, including SVF-backed may-modify, a complete caller set, sequencing at each call site, and no concurrent writer (single-threaded, or decided by the task model). |
| **Recipe `output-param`** (tracker roadmap) | Turns a pointer parameter the function only writes into a return value. A `void` function returns the value (`get(a, &v);` → `v = get(a);`); a function that returns a status returns a small result record declared next to its prototype (`s = read(&v);` → the record's `status` and `value`). When the value is written on some paths only (an early error return), the record also carries `has_value`, and the caller assigns the value only when it was written. Each call must pass the address of a local variable (or a field of one) whose address is taken nowhere else, or pass on the caller's own pointer parameter from callers that do: converting the callee first then makes the caller a candidate (leaf-first; `weaver auto --recipe output-param` runs the chain, validating each step). A caller that never reads its variable drops the value and the variable. |
| **Your own change** | `weaver patch change.diff --removes P-…` (or **Check my change…** in the interface) opens a transaction for a unified diff you wrote. Hunks are placed by their content, so approximate line numbers are fine. Validation runs the recipe checks: every affected configuration compiles, the configured tests and differential runs pass on both trees, and the ledger keeps a checkpoint for revert. The mechanical re-check differs: every affected unit is analysed before and after the patch and its pointer facts compared with the rules of change impact. It fails when a named pointer still exists or a pinned or implied contract breaks. Every other change (a pointer added, a read-only pointer now written, a new escape) is listed for review before acceptance. |
| **AI drafts** | A second switch, `ai.drafts`, on top of AI explanations. `weaver draft P-…` (or **Draft a change with AI**) asks the configured model for a patch that removes the pointer. The model receives its own drafting guide (the same for every provider), the evidence slice, and the exact source of the function, its callers and its declarations. If the draft does not apply, Weaver asks once more with the reason, then opens a transaction exactly as for your own change. Nothing is applied until the draft validates and you accept it. |
| Transactions (tracker §6) | Candidate cards. States: discovered → analyzed → blocked / proposed → validated / provisional / rejected → accepted / skipped → reverted. Patches are bound to source hashes. Revert uses a three-way merge so later unrelated edits survive. |
| Validation (tracker §7) | Isolated baseline and candidate workspaces. The production compiler rebuilds every unit whose main file *or included headers* were edited. A mechanical re-check parses the patched AST again. Configured builds, tests and differential comparisons run in both workspaces; CTest and Meson results are compared test by test, so a test that already fails on the baseline is not blamed on the patch. An acceptance policy judges the results. |
| **Validation strength** | Each validation and acceptance records whether anything ran the patched program (`behavioural`) or not (`compile-only`), shown on the card, the ledger, impact reports and the web interface. **Coverage of the changed lines** (on by default when tests are configured): after the judged runs, a second build of the patched tree through compiler shims that add `--coverage` runs the same tests, and gcov or `llvm-cov` says which changed lines they executed. A change the tests never ran is `unexercised`, one they ran in part is `partly-exercised`; `acceptance.require: [coverage]` makes such a transaction provisional. `validation.coverage: false` turns the extra build off. `weaver tests` and the setup form detect the project's test commands (Make `test`/`check`, CTest, Meson, test scripts); the web settings editor changes validation commands and the acceptance policy. |
| **Contracts on borrowed pointers** | `--expect borrowed`: the target is never written and the pointer is never kept past the call, checked through casts, copies and callees. The cFS software-bus buffer (`SBBufPtr`) holds through nine pointers in `sample_app`. |
| **Risk view** | `weaver risk` and the **Risk** tab rank every pointer by the risk factors Weaver established for it: conversions to and from integers, concurrent writers (task model), unknown targets, pointer arithmetic, reinterpreting casts, escapes, writes, heap targets, globals, function pointers, many targets, reassignment, null tests, identity comparisons and weak evidence. Each factor has a weight and its evidence; the score orders the work (it is not a probability of failure). Totals per function, file and module; the Map can be coloured by risk; `weaver report` includes the top pointers. |
| **Simplification checker** | `weaver simplify` and the **Simplify** tab: every analysed function against a target profile, listing each construct the target excludes with its line (pointers, addresses, pointer arithmetic, pointer/integer casts, function pointers, heap allocation, raw memory functions, `goto`, unions, variadic functions, recursion, `setjmp`/`longjmp`, inline assembly, global writes, static locals), and how many functions already meet it. Built-in profiles: CLite (provisional), no pointers, and ready for a modular redesign; projects adjust them or define their own. A guide for manual work, not a certification. |
| **Rejection report** | `weaver report --scope apps/sample_app`: evidence, pointers by class, each recipe's eligible and blocked candidates with the failing preconditions, the precondition that alone blocks the most, and contract status. |
| **Change impact** | Snapshots of pointer facts (the working tree, or any git revision). `weaver impact` explains each pointer whose behavior changed since a snapshot, which edited line caused it, which recipe verdicts flipped, which pinned or transaction-implied contracts broke, and optionally whether builds and differential runs still agree. `weaver check` exits 1 on high risk, for CI. |
| **Web interface** | `weaver serve`: load or set up a project, compile, then explore a map of every pointer colored by what it does to its target. Graphs show points-to and call relationships, and a source view has inline marks. Refactors can be proposed, validated and accepted, contracts pinned, and change impact compared. `--scope` shows one subsystem of a large project. `weaver export-ui` records the interface as one read-only HTML page for sharing. |
| **AI explanations** (tracker §10) | Off by default; switched on per project. Bring your own key: Claude through Anthropic's SDK, or any OpenAI-style Chat Completions server, hosted or local. One explanation guide (Weaver's vocabulary, evidence rules, eight fixed answer sections) is given to every provider, with the same focused evidence slice and read-only evidence tools; answers are checked against it. `weaver ai`, `weaver explain`. |

Not yet, following the plans' roadmap: the buffer/range, typed-ID and callback recipes; field-sensitive and lock-aware ownership; CBMC equivalence harnesses; and vendor
adapters (Diab, Green Hills). See
[`docs/architecture.md`](docs/architecture.md).

## Install

```sh
pip install -e '.[flow]'    # Weaver with SVF points-to analysis (pysvf bundles LLVM and wpa; AGPL, see below)
pip install -e '.[llm]'     # optional: Claude for AI explanations (other providers need nothing extra)
pip install -e '.[test]'    # pytest
```

Requires Python ≥ 3.10. Points-to evidence comes from two analyses that Weaver shows side by side:
SVF (installed by `[flow]`) and GCC's own interprocedural points-to, which needs GCC with LTO support
(`gcc-ar` or `ar` with the LTO plugin) and binutils (`nm`). `pip install -e .` alone works too, with
GCC's analysis only. A production compiler (Clang or GCC-compatible) must be installed. For
non-Clang profiles you also need a Clang to act as the secondary frontend. The web interface uses
only the standard library.

## Web interface

```sh
weaver serve [PROJECT_DIR]      # loopback only, port 61847 (or the next free one); prints a sign-in link
weaver serve --scope apps/sample_app            # large projects: show one subsystem at a time
weaver export-ui -o weaver.html                 # a read-only copy of the interface, no server needed
```

1. **Load**: open a directory that has a `weaver.yaml`, or set one up from the form. Give the build
   command with `{cc}` where the compiler goes (`make -B CC={cc}`), the production compiler, an
   optional analysis Clang, the tests to run (**Detect test commands** finds `make check`, CTest,
   Meson or test scripts), an optional program run to compare, and the concurrency model.
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

The details panel shows the points-to evidence of both analyses side by side, per program: what
SVF and GCC each say the pointer may point to and, for a parameter, whether a call may write that
target, each with its reason. When they disagree, the recipe takes the stricter answer
(`flow.agreement: all`).

The **Risk** tab shows where pointer risk concentrates: how many pointers are high, medium or low
risk, which factors contribute, which modules or files carry the most, and a ranked list with
every factor's evidence. Selecting a pointer opens its details, which list the same factors. The
**Map** can switch from colouring pointers by behaviour to colouring them by risk.

The **Simplify** tab measures progress toward a target: CLite, or simply simpler code ready for a
modular redesign. Choose a profile to see how many functions already meet it and which constructs
remain, by rule and by function. Open a function to jump to each construct's line. Profiles are
set in `weaver.yaml`:

```yaml
simplify:
  profile: clite-provisional   # or pointer-free, modular, or your own
  add: [static-local]          # adjust the default profile
  remove: [recursion]
  profiles:
    app-layer: {title: "Application layer", rules: [pointer, global-write, goto]}
```

CLite has no written specification yet, so its profile is marked provisional. Code no analysed
configuration compiled is not checked.

**AI explanations** are optional and off by default. When a project turns them on (Settings, or
`weaver ai enable`), each pointer gets an **Explain with AI** button. The model receives that
pointer's evidence and nearby source, may call Weaver's read-only evidence tools, and never edits
anything. Bring your own key: Claude (`provider: anthropic`), or any server that speaks the
OpenAI-style Chat Completions API, hosted or local (`provider: openai-compatible`, for example Ollama
at `http://localhost:11434/v1`, which keeps the code on your machine). Keys come from the provider's
environment variable or are stored for your user account outside the project (`weaver ai key`, or
Settings); they never go into `weaver.yaml`. Every provider receives the same explanation guide
(`weaver ai guide` prints it) and must answer in the same eight sections, so explanations read the
same whichever model gives them. Answers that skip a section are flagged.

**Your own changes** go through the same checks. **Check my change…** (on a pointer, or in
Transactions) takes a unified diff and opens a transaction; validation compiles, compares every
affected unit's pointer facts before and after, re-checks contracts and runs the tests on both
trees. The transaction lists the pointers removed and added, and every change a reviewer should look
at before accepting. With **AI drafts** switched on as well (Settings), **Draft a change with AI**
asks the model for such a patch and checks it the same way: the model proposes; nothing is
applied until the draft validates and you accept it.

The top bar shows the validation strength. **validation: compile only** means no test or
differential run is configured, so an accepted change has been compiled and re-checked but never
run. **Settings** edits validation commands, the acceptance policy, the concurrency declaration and
the flow backend. It saves `weaver.yaml` and keeps the previous file as `weaver.yaml.bak`.

The server binds to 127.0.0.1 on port 61847, or the next free port if that one is taken
(`--port N` binds exactly N, `--port 0` any free port). The port is not a secret. The session is
protected by a random key that changes on every start and appears only in the terminal: open the
link `weaver serve` prints (`--open` does it for you). The link sets an HttpOnly, SameSite=Strict
cookie for that browser and drops the key from the address bar. The page itself never contains the
key, so other accounts on a shared machine cannot drive your session. The server also rejects
foreign `Host` headers (DNS rebinding) and API calls without the page's own request header (CSRF).
Modifying operations run one at a time.

## Command-line walkthrough

```sh
weaver init                                   # write weaver.yaml; record profiles, target and platform facts
weaver capture shim --tool cc=/usr/bin/gcc    # generate a recording shim
make clean && make CC=$PWD/.weaver/capture/shims/cc
weaver capture finalize --out build           # -> build/compile_commands.json, links.json, tools.json

weaver probe                                  # capability matrix for each profile
weaver tests --add make-check                 # validation strength; detect and add the project's tests
weaver refresh                                # collect -> fidelity -> inventory -> flow, for changed units
weaver flow --backend gcc                     # points-to from the production GCC (svf | gcc | all)
weaver coverage                               # code no configuration compiled
weaver candidates --all                       # eligible candidates, and blocked ones with reasons
weaver report --scope src/net -o REPORT.md    # inventory and rejection report for part of the tree
weaver tasks                                  # declared threads of control, what was checked, who writes each global
weaver risk --top 20                          # pointers ranked by risk factors, with the evidence for each
weaver simplify --profile clite-provisional   # constructs standing between each function and the target
weaver simplify --function parse_msg          # one function's constructs, line by line

weaver show P-1a2b3c4d5e                      # one finding: uses, targets, precondition evaluation
weaver ai enable --provider openai-compatible --base-url http://localhost:11434/v1 --model llama3.1
weaver explain P-1a2b3c4d5e --dry-run         # the request every provider would get (nothing is sent)
weaver propose P-1a2b3c4d5e                   # open a transaction and print its candidate card and patch
weaver validate T-0bb5fce6                    # isolated compile, re-check, build, tests, differential run
weaver accept T-0bb5fce6                      # apply under the acceptance policy (or: skip)
weaver revert T-0bb5fce6                      # undo an accepted transaction
weaver auto --max 10                          # propose/validate/accept eligible candidates under the policy
weaver auto --recipe output-param --max 20    # leaf-first: each conversion can make its caller eligible
weaver patch change.diff --removes P-1a2b3c4d5e --title "…"   # your own change, checked like a recipe's
weaver ai enable --drafts                     # also let the model draft patches (sends whole functions)
weaver draft P-1a2b3c4d5e                     # an AI draft, proposed as a transaction; validate as usual

weaver contract pin P-1a2b3c4d5e --expect read-only,no-escape --reason "callers reuse the buffer"
weaver contract pin P-9f8e7d6c5b --expect borrowed --reason "SB buffer: valid until the next receive"
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
  concurrency: single-threaded        # checked: no call starts a thread; interface recipes need this
  # or, for multi-task programs, the threads of control (see pilots/cfs/weaver.yaml):
  # concurrency:
  #   model: tasks
  #   programs: [cpu1]
  #   tasks: [{name: SAMPLE_APP, entry: SAMPLE_APP_Main}, {name: TIMEBASE, entry: OS_TimeBasePthreadEntry, instances: many}]
  #   interrupts: [{name: SIGHUP, entry: OS_NoopSigHandler}]
  #   dispatchers: [OS_PthreadTaskEntry]
  #   indirect_calls: [{at: "src/app.c:120", targets: [on_tick]}]
acceptance:
  require: [compile, mechanical-recheck, differential-testing]
flow:
  backend: auto                       # svf | gcc | none | auto (every available backend)
  agreement: all                      # every backend with evidence must find no write
  models: [builtin:cfs]               # reviewed model packs (POSIX/glibc is always loaded)
  externals:                          # project-specific reviewed effect models
    rand: {writes: [], calls_back: false, assumptions: ["only updates its own hidden state"]}
programs:                             # images that run together; closed over their entry points
  - {name: cpu1, images: [core-cpu1, sample_app.so], entry_points: [main, SAMPLE_APP_Main]}
profiles:
  - id: native-dev
    compile_commands: build/compile_commands.json
    capture: {command: "make -B CC={cc}", tools: {cc: gcc}}   # lets `refresh --capture` rebuild
    secondary_frontend: {compiler: clang}       # only when the production compiler is not Clang
    validation:
      build: {run: [make, -C, "{workspace}", BUILD=out], cwd: "{workspace}"}
      tests: [{name: unit, run: "ctest --test-dir out", cwd: "{workspace}"}]   # compared test by test
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
- GCC's own points-to (parsed from a real dump, and end to end on the fixture), the cross-check
  with SVF, and staleness;
- the link model on a small program with an archive and a shared object, and capture exclusions;
- effect-model packs, macro token comparison, the borrowed-pointer check and the report;
- test detection, per-test comparison, validation strength, and the settings editor;
- the task model on a pthreads program: private locals, shared globals, an address handed to a
  thread, an address published as an integer, incomplete declarations and a contradicted
  `single-threaded`;
- the risk view: each factor from the facts behind it, scores and levels, ranking and totals, the
  CLI and the web view;
- the simplification checker: every construct found on its line in a purpose-built program,
  the built-in and project-defined profiles, and the web view;
- change impact with contracts, git snapshots and revalidation;
- the web server's request guards, its views, scoped views, the read-only export, and the
  propose/validate/accept and setup/capture workflows through its jobs;
- AI explanations: the Claude tool loop against a fake client, the OpenAI-compatible loop against a
  fake local server, the shared guide and its section check, the on/off switch and private key storage.
- coverage of the changed lines: a change the tests run, one they never run, one they run in part,
  the policy that requires it, and a build that cannot be measured;
- your own change and AI drafts: diffs placed by content, blocked proposals that say why, a change
  that keeps its pointer or breaks a contract, one accepted and reverted with identical output; a
  draft that does not apply, is retried once and validates; a model that declines to patch.

## License

MIT. See [LICENSE](LICENSE).

SVF is AGPL-3.0-or-later, and it is optional:

- It is a separate extra (`weaver[flow]`), not a dependency. Weaver never imports it. It finds the
  `wpa` binary without loading `pysvf`, runs it as a separate process on bitcode files, and reads
  its text output. No SVF code runs in Weaver's process or is linked into it.
- Without it, `flow.backend: gcc` gets points-to evidence from the production GCC (GPL-3.0 with the
  runtime library exception, run as an external program like any compiler). Recipes also work from
  reviewed effect models and call-site designators alone, only with more `unknown` answers.
- `flow.backend: none` or `flow.svf.enabled: false` keeps an installed SVF from being run.

This is an engineering arrangement, not legal advice. Review it against your distribution plans
before shipping SVF alongside Weaver or offering Weaver as a network service with SVF installed.
