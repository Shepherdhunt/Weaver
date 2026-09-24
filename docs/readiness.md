# Readiness: what works, what playtesters can use, what is left

This is an assessment of Weaver as a product, as of September 2026. It rests on three kinds of
evidence:

- **The test suite.** 151 tests, passing on the host (Python 3.11) and inside the container image (Python 3.12). The end-to-end tests capture and build real fixtures with GCC and
  Clang.
- **The cFS pilot.** cFE, OSAL, PSP and the sample app, with 3,343 pointer findings in the analysed
  build ([`pilots/cfs`](../pilots/cfs/README.md)).
- **A first run on a library Weaver had never seen.** [cJSON](https://github.com/DaveGamble/cJSON)
  at `6d9f244` (1.7.19): 5,400 lines of C89, built with GCC by a Makefile, and tested by CMake and
  CTest. The run followed the README as a new user would. The run and its fixes are below.

## Summary

- **Ready for guided playtests.** Suitable playtest projects are C code on Linux, built by GCC or
  Clang, with a working Make or CMake build, and with a Weaver person available to the team. All of
  the following held up on cFS and cJSON:
  - the analysis: inventory, points-to by two engines, risk, the simplification checker, the report;
  - the CI ratchet;
  - checked changes: your own patch, and the recipes' patches.
- **Not ready for self-serve paying customers.** Before the fixes below, a new user of cJSON would
  have seen an empty inventory with no warning, and stopped. Several more gaps separate this from a
  $199 product:
  - installation;
  - a health check of the environment;
  - more than one project per server;
  - licensing and sign-in;
  - platforms other than Linux.
- **Automatic pointer removal is a supporting feature, not the headline.** It removes about 26 of
  3,343 pointers on cFS, and 1 of 470 on cJSON. The value playtesters should be shown:
  - the map of the pointers;
  - the risk ranking;
  - the ratchet;
  - validation of the changes people make, by hand or with an AI draft.

## What a first run on cJSON found

Each row is a step of the run. Everything marked *fixed* is fixed in this branch and has a
regression test.

| # | Step | What happened | Now |
|---|---|---|---|
| 1 | `weaver init` | The template holds placeholder target and platform facts (`recorded_architecture`), and its validation commands are commented out. Editing the file by hand was the only way to configure tests. | open |
| 2 | Capture with `make CC=<shim>`, as the README said | cJSON's Makefile says `CC = gcc -std=c89`, so the override dropped `-std=c89`. Weaver analysed C17 code that is really C89. | *fixed*: the shims also go first on `PATH` under the compiler's own name, and the README capture example uses `PATH` |
| 3 | `weaver refresh` | `0 finding(s) in 4 unit(s)`, with no warning. A GCC profile without a configured secondary frontend has no AST that Weaver can read. | *fixed*: a GCC profile uses the `clang` on `PATH` by default (`secondary_frontend: false` turns this off), and `refresh` warns when units have no AST |
| 4 | Points-to | Both shared libraries failed with "no bitcode could be produced". `gcc -c cJSON.c`, with no `-o`, was not recorded as producing `cJSON.o`, so the link could not be mapped to its sources. | *fixed* |
| 5 | Fidelity | 3 of 4 units were `secondary-partial`: GCC spells `INT_MIN` as `(-INT_MAX - 1)` and Clang as `(-__INT_MAX__ -1)`. The whole-program precondition then blocked all 378 candidates. Any GCC project that includes `limits.h` would have hit this. | *fixed*: differently spelled macros are compiled with both compilers and compared by value. The probes also now apply the secondary frontend's `extra_args`, which they had ignored. |
| 6 | `weaver probe` | 5 GCC capabilities were `unverified`. The probe file was compiled with the project's `-Werror` and strict warnings. | *fixed* |
| 7 | `weaver candidates` | 0 of 378 eligible. `decode_array_index_from_pointer()`'s `index` is a textbook output parameter, blocked only because every call sits in `if (!f(p, &index))`. | *fixed*: `output-param` computes such a call in a block before the `if`, and becomes eligible |
| 8 | `weaver propose` | Picked `scalar-input` (blocked) instead of the eligible `output-param`. | *fixed*: `propose` takes the first eligible recipe |
| 9 | `weaver validate` | Rejected: the patch used `_Bool` and compound literals, and the CMake build is `-std=c89 -pedantic -Werror`. The check worked as intended. | *fixed*: C89 units get C89 code. The change then validates: the C89 build, 22 CTest tests on both trees, identical `cJSON_test` output, and 23 of 23 changed lines executed, in 18 s. |
| 10 | `weaver patch` (hand-written) | Removing `buffer_pointer` from `update_offset()`: the target is gone, nothing else changed in the pointer facts, the tests pass and the changed line is covered. | works |
| 11 | Risk, simplify, ratchet, web views | All work. Risk, simplify and ratchet take under a second each. The pointer list takes 3.4 s on first load, and every view is under 0.3 s after that. The accepted change showed up in the ratchet as progress. | works |

Timings on cJSON:

| Step | Time |
|---|---|
| Capture and refresh (collect, fidelity, inventory, SVF and GCC points-to) | 11 s |
| Evaluating every candidate | 4 s |
| Validating a change (two CMake builds, CTest on both trees, a differential run, an instrumented build for coverage) | 18 s |

Found but not fixed:

- **The validated build is not the analysed build, and Weaver does not say so.**
  - The Makefile build was analysed: `ENABLE_LOCALES` off, and the tests not compiled.
  - Validation ran the CMake build: `ENABLE_LOCALES` on, and 22 test programs.
  - `weaver coverage` lists the 14 unexamined lines. Nothing warns that validation compiled code the
    analysis never saw.
  - The coverage shims already see every compile of the validation build. Comparing its defines and
    dialect with the analysed commands would catch this.
- **The tests are outside the analysed program.** The capture did not include cJSON's `tests/*.c`
  and `fuzzing/*.c`. This is the same pattern as cFS's unit-test stubs, which are the largest
  blocker there.
- **Risk over-counts character casts.** "Cast to an unrelated pointer type" counts
  `unsigned char *` ↔ `char *` (107 sites in cJSON). These should be a separate, low-weight factor.
- **A library's public functions are blocked by design.** cJSON's exported functions fail
  "complete callers", and this is correct: code outside the build may call them. A library user has
  to declare `programs:` and entry points, and the documentation does not explain how.
- **The run was not done by a stranger.** The author of the tool did it. A real first-time user
  could not have worked around rows 2–9, so the next pilots should be run by someone else.

## What works, with evidence

| Area | Evidence | For playtesters |
|---|---|---|
| Build capture (Make, CMake; response files; links) | cFS (CMake), cJSON (Make), fixtures | ready on Linux |
| Pointer inventory and unexamined-code report | cFS 3,343 findings in 51 s; cJSON 470 | ready |
| Secondary Clang for GCC profiles, with fidelity checks | every unit `secondary-checked`: all 287 on cFS, all 4 on cJSON | ready |
| Points-to: SVF and the production GCC, side by side | cFS, cJSON, fixtures | ready (SVF needs the AGPL review before it ships) |
| Risk ranking and report | cFS, cJSON | ready; the character-cast noise is known |
| Simplification checker (CLite provisional, no pointers, modular) | cFS, cJSON | ready; CLite itself has no specification |
| CI ratchet | tests; cJSON baseline, then progress after an accepted change | ready; not yet run in a customer's pipeline |
| Your own patch, checked | cFS (`CmdPtr`), cJSON (`buffer_pointer`) | ready |
| Validation: compile, re-check, tests per test, differential run, coverage of changed lines | cFS (112 CTest tests, gcov through its CMake), cJSON (22 CTest tests) | ready |
| Recipes: `local-alias`, `scalar-input`, `output-param` (with leaf-first, optional outputs, conditions, C89) | cFS: 26 of 3,343 eligible; cJSON: 1 of 470 | beta: correct and checked, rarely applicable |
| Change impact, contracts, `weaver check` | tests, cFS `SBBufPtr` study | beta |
| Task model for multi-task programs | pthreads fixture, cFS | beta |
| AI explanations and AI drafts (bring your own key) | tests against a fake Anthropic client and a fake OpenAI-style server | beta: never run against a live model on a real project |
| Web interface (loopback, one user, one project) | tests; cFS with `--scope`; cJSON views under 0.3 s once loaded | ready for guided use |
| Read-only export (`weaver export-ui`) | the playtest page | ready |
| Packaging | the wheel builds in 4 s and installs in a clean virtualenv with one dependency (PyYAML), with its web assets and data files | not published |

## What is not ready

### Before guided playtests

1. ~~**`weaver doctor`.**~~ Done. It checks this machine by running the tools:
   - a JSON AST from Clang;
   - an LTO link with GCC;
   - a coverage build with each compiler.

   It also checks SVF, binutils, build tools, memory, disk and the clock, and the project's
   configuration. Each problem comes with the install command for the platform. On this machine
   it found the missing Clang profile runtime at once.
   It is also in the web interface (**Check setup**).
2. ~~**A container image.**~~ Done ([`container.md`](container.md)):
   - Ubuntu 24.04 with Clang 18 and its coverage runtime, GCC 13 with LTO, binutils, Make, CMake,
     Ninja, Meson and git;
   - SVF as an optional target;
   - a wrapper that mounts the project at the same path and runs as the host user.

   Verified end to end:
   - doctor inside the image reports 0 problems;
   - the whole test suite passes inside the image (Python 3.12);
   - cJSON's CMake build is captured through the wrapper (810 pointers, 57 s without SVF) and
     validates in 19.5 s, with 22 CTest tests, 23 of 23 changed lines covered, and the build
     configuration matching;
   - it runs as a non-root user;
   - the web interface is published on the host's loopback only.
3. ~~**An onboarding guide for a repository.**~~ Done: [`onboarding.md`](onboarding.md). It was
   written from a run on cJSON's CMake build with its tests:
   - 27 units, all `secondary-checked`;
   - 810 pointers;
   - the eligible change validating against 22 CTest tests in 18 s.

   It covers:
   - which build to capture;
   - how the shims work;
   - validation;
   - concurrency;
   - when to declare programs (and when not);
   - effect models;
   - a table of symptoms and fixes.
4. ~~**A warning when the validation build differs from the analysed build.**~~ Done. Every
   validation records its build's compiles and compares them with the analysed commands. It
   reports sources on one side only, and different defines, dialect, includes, target flags or
   compiler. `weaver doctor --build` runs the comparison on demand.
   - On cJSON's Makefile capture it names:
     - the 24 test and fuzzer sources;
     - `ENABLE_LOCALES` and the export defines that only the CMake validation build uses.
   - On the CMake capture, all 27 sources are compiled as analysed.
5. **A better `weaver init`**: ask for the build and test commands (`weaver tests` already detects
   them), and leave out placeholder facts.
6. **Two more first runs by someone other than the author**: bare-metal firmware with a cross
   compiler, and a CMake and Ninja application.

### Before paying customers (Phase 1 of the [deployment plan](deployment-plan.md))

- **Commercial:**
  - sign-in through the portal (no identity provider has been chosen);
  - the per-project licence;
  - Stripe billing;
  - signed releases and an update channel;
  - terms of service, EULA, privacy policy and a data processing agreement;
  - counsel's review of shipping SVF (AGPL-3.0).
- **Operations:**
  - more than one project per server;
  - jobs that can be cancelled, with logs kept on disk;
  - migrations for the versioned `.weaver/` schemas;
  - opt-in crash reports;
  - a documentation site and an issue intake.
- **Platforms and toolchains:**
  - Only Linux x86-64 with GCC 13 and Clang 18 has been exercised.
  - macOS is untested. With Apple Clang the AST is native, but the layout probes read ELF only.
  - Windows is not supported: the capture shims are POSIX shell scripts.
  - Cross and vendor compilers (Diab, Green Hills, IAR, Keil) have no adapter and have not been
    piloted.
  - C only.
- **Scale beyond cFS is unmeasured.** On cFS:
  - inventory takes 51 s;
  - a whole-program recipe evaluation takes about 2–4 minutes in under 1 GB;
  - validating a change takes about 6 minutes, or 12 with coverage.

### Product gaps (value, not polish)

- **Automatic removal stays rare in mature C** (see
  [How much can be automated](deployment-plan.md#how-much-can-be-automated)). The largest measured
  blockers:
  - unit tests and stubs outside the analysed program (cFS: 125 of 146 `output-param` candidates;
    cJSON: its whole test suite);
  - targets inside shared records;
  - in-out parameters.
- **CLite has no specification.** The CLite profile is provisional until an owner writes one.
- **Tests are the only evidence of equivalence.** Checking with CBMC over a bounded set of inputs
  would be stronger evidence, and suits flight-software customers.

## Recommended path

**Stage 0: harden for guided playtests (about two weeks).**
- Work: the six items under "Before guided playtests".
- Exit criteria:
  - On each of three unfamiliar repositories, a person who did not write Weaver gets from a clone
    to a risk report in under an hour, without help.
  - No step returns an empty or all-blocked result without saying why.

**Stage 1: guided playtest (3–5 teams, 4–6 weeks, free).**
- Positioning for the teams: the map, the risk ranking, the ratchet and checked changes.
- What to measure:
  - the time to the first report;
  - which views teams use;
  - how often a blocked reason is judged wrong;
  - how many of their own changes teams run through `weaver patch`;
  - whether the ratchet stays switched on in their CI.
- Hard requirement: no validated change is later found to change behaviour.

**Stage 2: paid beta at $199 per project, run by the customer.**
- Work: everything under "Before paying customers".
- Entry criteria:
  - installs from a signed package on clean Ubuntu and RHEL machines;
  - an upgrade keeps the `.weaver/` state;
  - one team uses it for a month without the author's help.

**Stage 3: general availability.**
- The macOS build, a CI container, and vendor-compiler adapters, in the order customers ask for them.
