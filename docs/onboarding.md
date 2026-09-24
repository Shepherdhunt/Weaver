# Onboarding a repository

This guide takes a C project from a fresh clone to a pointer inventory, a risk report and a first
checked change. On a mid-size project it takes about an hour, most of it spent deciding which build
to capture and which tests to run.

The worked example is [cJSON](https://github.com/DaveGamble/cJSON) at `6d9f244`: a C89 library with a
Makefile and a CMake build that compiles its tests. Every command and number below comes from
running this guide on it. The complete configuration is at the end.

The steps:

0. Check the machine: `weaver doctor`
1. Choose the build to capture
2. Write `weaver.yaml`
3. Run the first analysis, and read what it says
4. Tell Weaver how to check a change
5. Declare concurrency
6. Programs and entry points
7. Effect models for code outside the project
8. First results
9. First change
10. Keep the numbers from going back up

## 0. Check the machine

```sh
weaver doctor            # this machine, and the project in this directory if it has a weaver.yaml
weaver doctor --machine  # the machine only
```

In the web interface, use **Check setup** (or the link on the setup form).

Doctor checks what Weaver needs. Where a check can be made by running the tool, doctor does that:
it compiles a few lines instead of only looking for executables. It checks:

- Python;
- Clang, and that it can dump a JSON AST;
- GCC, and that it links with LTO;
- binutils;
- a coverage build with each compiler, and the tool that reads the counts;
- SVF;
- your build tools and git;
- memory, disk and the clock.

Each check is `ok`, `info`, `warn` or `fail`, with a fix for your platform:

```
Coverage
  warn  clang --coverage       cannot link a coverage build: the profile runtime (libclang_rt.profile) is
                               missing. Validation then says the changed lines' coverage was not measured
                               fix: sudo apt-get install libclang-rt-18-dev
```

Fix every `fail` before going on. A `warn` means something works less well. For example, without
the Clang profile runtime, validation of a Clang project cannot say which changed lines the tests
ran. Run doctor again after each step below: once a `weaver.yaml` exists, it also checks the
project's configuration.

**Tested platforms and tools.** Linux x86-64, Clang 18, GCC 13, Make and CMake. On macOS and with
cross compilers Weaver should work, but they have not been piloted; tell us what you find. Windows
needs WSL 2.

**Or use the container.** It has every tool doctor checks for, already installed:
Clang 18 with its coverage runtime, GCC 13 with LTO, binutils, Make, CMake, Ninja, Meson and git.
With `container/weaver-docker`, every command in this guide works unchanged: write
`weaver-docker refresh --capture` where the guide says `weaver refresh --capture`. See
[`container.md`](container.md).

## 1. Choose the build to capture

Weaver analyses what the compiler really builds. It records every compile and link of one real
build through shims placed in front of the compiler. Which build you capture decides what Weaver
can see.

- **Capture the build that also compiles the tests.**
  - A function called only from test code that is not in the build looks as if it had unknown
    callers, so every recipe leaves its signature alone.
  - On cFS, callers outside the analysed build block 125 of the 146 output-parameter candidates.
  - For cJSON, capture the CMake build with `-DENABLE_CJSON_TEST=On` (27 units), not the plain
    Makefile (4 units).
- **Capture the configuration you will validate with.**
  - Validation runs your build and tests on the changed tree. If validation compiles with other
    defines, a different `-std`, or sources the analysis never saw, it tests code Weaver did not
    analyse.
  - cJSON's Makefile leaves `ENABLE_LOCALES` off, and its CMake build turns it on.
  - `weaver doctor --build` runs your validation build once and compares what it compiles with what
    was analysed. Every validation compares it too (step 4).

    On cJSON, the Makefile capture checked against the CMake validation build gives:

    ```
    warn  build configuration    the validation build differs from the analysed build: it compiles 24 source(s)
                                 no analysed configuration compiled (fuzzing/cjson_read_fuzzer.c, ...), so the
                                 tests run code Weaver never analysed; it compiles 3 analysed source(s)
                                 differently: -DCJSON_API_VISIBILITY (3), -DCJSON_EXPORT_SYMBOLS (3),
                                 -DENABLE_LOCALES (3), ... only in the validation build
    ```

    Captured from the CMake build instead, it gives:

    ```
    ok    build configuration    the validation build compiles the 27 analysed source(s) as analysed
    ```
- **Capture a full rebuild.** Clean first, so every unit is compiled while Weaver watches.
- **Use one profile per configuration you ship.** Examples: a native build and a cross build, or
  two boards. Each is analysed separately, and a change must hold in all of them.

## 2. Write `weaver.yaml`

Let `weaver init` read it from the project:

```sh
weaver init            # in a terminal: shows what it found and asks about each choice
weaver init --yes      # takes what it found; --print shows the file without writing it
```

It detects:
- **the build system:** CMake, Meson, Autotools or Make.
  - When a project has both CMake and a Makefile, it takes CMake if CMake builds the tests.
- **the compiler, named as the build calls it:** `--cc`, else `$CC`, else the one a Makefile sets
  in `CC`, else `cc`. The capture shim takes that name, so the build's own flags are recorded.
- **CMake options:**
  - the ones about tests are turned on;
  - the others that are off are listed in the file, and `weaver init` asks about them.
- **the tests the project registers:** CTest, `meson test`, `make check`/`test`, or test scripts.

It writes one profile:
- the capture is a clean build in `.weaver/build/capture`;
- the validation build is the same configuration in each workspace;
- the tests run against that validation build.

It records the compiler's target triple; there are no placeholders to fill in. For cJSON it
picks the CMake build with its tests. It lists `ENABLE_CJSON_UTILS` as off, which
`weaver init --enable ENABLE_CJSON_UTILS` turns on.

The flags, for scripts and CI:
- `--system`, `--build` (any other build), `--clean`, `--cc`;
- `--enable OPTION`;
- `--test CMD`, `--no-tests`, `--compare CMD`;
- `--concurrency single-threaded`, `--secondary CLANG`;
- `--force`.

The web interface's setup form does the same detection (**Detect**, or when you enter the project
directory). You can also write the file by hand, starting from the examples below.

### How capture works

A profile's `capture` names the build command and the compilers it runs.

```yaml
profiles:
  - id: native
    compile_commands: .weaver/compdb/native/compile_commands.json   # written by the capture
    capture:
      command: "make -B"            # a full rebuild
      clean: "make clean"           # optional, run first
      tools: {gcc: /usr/bin/gcc}    # the name the build calls -> the real compiler
```

For each tool, Weaver writes a shim with the same name (`gcc` here). The shims go first on `PATH`
while the build runs. A build that calls `gcc` then runs the shim, which records the command and
runs the real compiler. Your build's own flags are recorded unchanged, including those a Makefile
puts in `CC` (`CC = gcc -std=c89`).

- **Name the tool as the build calls it:** `gcc`, `cc`, `arm-none-eabi-gcc`, or several.
- **A build that runs the compiler by absolute path** (`/usr/bin/gcc` in a Makefile, or a CMake
  toolchain file with a full path) never looks at `PATH`. Put `{gcc}` where the compiler goes.
  Weaver replaces it with the shim's path.

  ```yaml
  command: "make -B CC={gcc}"   # careful: this replaces any flags the Makefile keeps in CC
  ```

**CMake.** Configure a separate build directory and name the compiler by name, so CMake finds the
shim on `PATH`:

```yaml
capture:
  command: "cmake -S . -B wbuild -DENABLE_CJSON_TEST=On -DCMAKE_C_COMPILER=gcc && cmake --build wbuild -j8"
  clean: "rm -rf wbuild"
  tools: {gcc: /usr/bin/gcc}
```

CMake's own compiler checks run through the shim too. Weaver recognises them and sets them aside.

**Other build systems** work in the same way if they find the compiler on `PATH` or take it in a
variable.

### GCC and other non-Clang compilers

Weaver reads source through Clang's AST. For a GCC build, Weaver runs a Clang next to GCC as the
*secondary frontend*. It takes the target, dialect, include paths and predefined macros from GCC,
and then checks that Clang sees what GCC compiles (fidelity). By default this is the `clang` on
`PATH`. To name another, or turn it off:

```yaml
    secondary_frontend: {compiler: clang-18, extra_args: ["-DMY_BOARD=1"]}   # or: secondary_frontend: false
```

With it turned off, a GCC build has no pointer facts. `weaver refresh` then prints a warning, and
doctor reports a `fail`.

## 3. First analysis

```sh
weaver refresh --capture
```

`refresh --capture` does the following:
1. Rebuilds through the shims.
2. Collects compiler evidence for every unit.
3. Checks the secondary frontend's fidelity.
4. Builds the pointer inventory.
5. Runs points-to analysis: SVF, and GCC's own.

On cJSON, the Makefile build takes 11 s. The CMake build with its tests takes 82 s: 27 units and 26
programs. Later runs of `weaver refresh` redo only what changed.

Read the output; each line says something:

```
[cmake] collect: 27 collected, 0 cached, 0 failed
[cmake] fidelity: {'secondary-checked': 27}
inventory: 810 finding(s) in 27 unit(s); 205 unexamined line(s)
[cmake] flow cJSON_test: complete
...
[cmake] gcc points-to: incomplete
```

- **`failed` units** are listed with the compiler's error. A unit that fails to collect has no
  pointer facts.
- **Fidelity.**
  - `secondary-checked` means Clang saw what GCC compiled.
  - `secondary-partial` means a difference that your code can observe. `weaver fidelity` names it.
    Usually it is a define or include path the translation missed; add it to
    `secondary_frontend.extra_args`.
  - Candidates in partial units are blocked by default (`acceptance.min_evidence`).
- **`WARNING: … have no AST evidence`** means those units have no pointers in the inventory. See
  the secondary frontend above.
- **Unexamined lines** are code that no captured configuration compiled: other platforms' `#if`
  branches, or files that no unit includes. `weaver coverage` lists them. They are unanalysed,
  not pointer-free.
- **Points-to per program**: `complete`, `incomplete` with a reason, or `failed`. Only complete,
  current evidence is used.
  - GCC's analysis works one linked image at a time. An executable linked against a shared library
    built in the same project is therefore `incomplete` ("not recompiled with LTO: libcjson.so").
  - SVF analyses those programs whole. With `flow.backend: auto` (the default), the recipes use the
    evidence that is complete.

## 4. Tell Weaver how to check a change

Validation copies the project into two isolated workspaces: the tree as it is, and the tree with the
change. It builds and tests both.

```yaml
    validation:
      build: {run: "cmake -S . -B vb -DENABLE_CJSON_TEST=On -DCMAKE_C_COMPILER=gcc >/dev/null && cmake --build vb -j8 >/dev/null", cwd: "{workspace}"}
      tests: [{name: ctest, run: "ctest --test-dir vb --output-on-failure -j8", cwd: "{workspace}"}]
      compare: [{name: examples, run: ["./vb/cJSON_test"], cwd: "{workspace}"}]
acceptance:
  require: [compile, mechanical-recheck, testing]
```

- **`build`.** Your build, with the real compiler, in the same configuration as the capture (step
  1). Use a build directory that `workspace_exclude` leaves out of the copy.
- **`tests`.**
  - A test that passes on the unchanged tree and fails on the changed one rejects the change.
  - CTest and Meson results are compared test by test, so a test that already fails is not blamed
    on the change.
  - `weaver tests` detects `make test`/`check`, CTest, Meson and test scripts. Check that the
    command it suggests really runs your tests.
- **`compare`.** A program whose exit status and output must be identical on both trees. It must
  be deterministic: no timestamps and no addresses in the output.
- **Coverage of the changed lines** is on by default. A second, instrumented build runs the same
  tests and reports which changed lines they executed. A change the tests never ran is marked
  `unexercised`, not `behavioural`. `validation.coverage: false` turns this off. Doctor says whether
  your compiler can build with coverage.
- **`acceptance.require`** lists which checks must pass. Without tests or a compare run, a change
  is validated when it compiles and re-checks, and every card says that nothing ran it.

Set `project.workspace_exclude` to your build directories and large generated files, so the
workspaces copy only the sources.

**The build configuration.**
- Validation records every compile of the unchanged tree's build and compares it with the analysed
  commands:
  - sources that no analysed configuration compiled;
  - analysed sources the validation build never compiles;
  - different defines, dialect, include paths, target flags, or compiler.
- A difference does not fail the change. The `configuration` record says what differs, the card and
  `weaver accept` repeat it, and the web interface shows it as a banner.
- `acceptance.require: [..., configuration]` makes such a change provisional instead.
- Doctor shows the last comparison.

## 5. Declare concurrency

A recipe that turns a pointer parameter into a value must know that nothing else writes the target
during the call.

```yaml
preservation:
  concurrency: single-threaded     # checked: no call in the program starts a thread
```

For a multi-task program, declare its tasks, interrupt handlers and dispatchers instead. Weaver
checks the declaration against every thread start. For an example, see
[`pilots/cfs/weaver.yaml`](../pilots/cfs/weaver.yaml) and `weaver tasks`. Undeclared, the
`no-concurrent-writers` precondition stays unresolved, and so does every `scalar-input` candidate.

## 6. Programs and entry points

"Who else can call this function?" decides whether a recipe may change its signature. By default,
each linked image is a program of its own:
- An **executable** is closed: only its `main` is called from outside.
- A **shared object** is open: everything it exports may be called by code Weaver never saw.

Declare a program when shared objects are loaded only by your own executable, as with plugins or
applications:

```yaml
programs:
  - {name: cpu1, images: [core-cpu1, sample_app.so], entry_points: [main, SAMPLE_APP_Main]}
```

Only the declared entry points then count as called from outside.

**Do not declare one for a library that others use.** cJSON's exported functions are called by
programs Weaver will never see, so they stay open. The recipes then work on the library's internal
(`static`) functions, which is correct. Doctor reports shared objects that have no program as
`info`, so that the choice is deliberate.

## 7. Effect models for code outside the project

A call into code that Weaver did not analyse might write anything, so a precondition that depends
on it stays `unknown`. There are two built-in sets of reviewed models:
- POSIX and glibc, always loaded;
- `builtin:cfs`, for cFE and OSAL.

Add your own for the libraries you call:

```yaml
flow:
  models: [builtin:cfs]
  externals:
    rand: {writes: [], calls_back: false, assumptions: ["only updates its own hidden state"]}
```

`weaver candidates --all` lists every blocked candidate with its reasons, including the unmodelled calls.

## 8. First results

In this order:

```sh
weaver risk --top 20            # the pointers that need attention first, with the evidence for each factor
weaver simplify                 # which functions already meet a target profile, and what stands in the way
weaver candidates --all         # what can change automatically, and exactly what blocks the rest
weaver report -o REPORT.md      # all of it, for the team
weaver serve                    # the map, the risk heat map, the source with inline marks
```

On cJSON, with the CMake build:
- 810 pointers;
- 1 eligible change, and 697 blocked, each with its reasons.

Expect few automatic changes in mature C. The blockers are the useful part: they say exactly what
would have to change.

## 9. First change

Either apply an eligible candidate:

```sh
weaver propose P-284446480c     # prints the card and the patch; nothing is changed yet
weaver validate T-…             # both workspaces: compile, re-check, build, tests, differential run, coverage
weaver accept T-…               # applies it; weaver revert T-… undoes it
```

Or check a change you wrote:

```sh
weaver patch my-change.diff --removes P-… --title "fold the alias into its only use"
weaver validate T-…
```

On cJSON, both kinds validate in 18 s:
- the build passes;
- all 22 CTest tests pass on both trees;
- the tests execute every changed line.

## 10. Keep the numbers from going back up

```sh
weaver ratchet --update     # commit weaver-ratchet.json
```

In CI, `weaver ratchet --base origin/main` fails a merge request that adds pointers, high-risk
pointers or profile violations in the files it changes. See [`ci.md`](ci.md).

## When something looks wrong

| You see | Why | What to do |
|---|---|---|
| `0 finding(s)`, or `WARNING: … have no AST evidence` | A non-Clang build with no secondary frontend | Install clang, or set `secondary_frontend` (step 2). Doctor reports it as `fail`. |
| The recorded commands lack flags the real build uses (`-std=c89`, defines) | The capture replaced `CC` (`make CC={cc}`) and dropped flags the Makefile keeps in it | Use the `PATH` shims: name the tool as the build calls it, and use a plain `make -B` |
| Units are `secondary-partial` | Clang sees something different from your compiler | `weaver fidelity` names the difference. Add missing defines or includes to `secondary_frontend.extra_args`. |
| `weaver probe` shows capabilities as `unverified` | The probe could not produce the artifact | Read the detail. `weaver probe --json` has the exact command. |
| Every candidate is blocked by `no-concurrent-writers` | No concurrency declaration | Step 5 |
| Many are blocked by `complete-callers` | Callers outside the build (tests, other programs), exported symbols of shared objects, or an address that is taken | Capture the build with the tests (step 1). Declare programs where that is true (step 6). |
| Calls are `unknown` in may-modify | Calls into unmodelled external code | Add effect models (step 7) |
| GCC points-to `incomplete: … not recompiled with LTO: libX.so` | An executable linked against a shared library from the same project | Nothing: SVF covers the program, and `flow.backend: auto` uses it |
| Validation says `compile-only` | No tests or compare run are configured | Step 4 |
| Validation says `unexercised` or `partly-exercised` | The tests do not run the changed lines | Add a test that runs them, or say so when accepting |
| Coverage is `not measured` | The compiler cannot link a coverage build | See doctor's Coverage section (for Clang, the profile runtime package) |
| The validation build fails where the unit compiles passed | Validation builds another configuration than the one captured | Make `validation.build` and the capture use the same configuration (step 1) |
| `build configuration` warns, or a card says "validated with a different build" | The validation build compiles other sources or flags than the analysed build | Capture the build you validate with (step 1). Check with `weaver doctor --build`. |
| `build configuration: not observed` | The validation build runs its compiler by absolute path, or found its objects already built | Name the compiler as the build calls it (`CC=gcc`, `-DCMAKE_C_COMPILER=gcc`), and exclude build outputs with `project.workspace_exclude` |
| The web interface says a job failed | See the job log in the interface | Run the same step from the command line for the full output |

## The complete cJSON configuration

```yaml
schema: weaver.project/1
project:
  name: cJSON
  workspace_exclude: [".git", "wbuild", "vb"]      # build directories stay out of the workspaces
preservation:
  behaviors: [outputs, errors]
  concurrency: single-threaded                     # cJSON is not thread-safe; callers serialise access
acceptance:
  require: [compile, mechanical-recheck, testing]
profiles:
  - id: cmake
    compile_commands: .weaver/compdb/cmake/compile_commands.json
    capture:                                        # the build that also compiles the tests
      command: "cmake -S . -B wbuild -DENABLE_CJSON_TEST=On -DENABLE_CJSON_UTILS=On -DCMAKE_C_COMPILER=gcc && cmake --build wbuild -j8"
      clean: "rm -rf wbuild"
      tools: {gcc: /usr/bin/gcc}
    # secondary_frontend: the clang on PATH (default for GCC builds)
    validation:                                     # the same configuration, with the real compiler
      build: {run: "cmake -S . -B vb -DENABLE_CJSON_TEST=On -DENABLE_CJSON_UTILS=On -DCMAKE_C_COMPILER=gcc >/dev/null && cmake --build vb -j8 >/dev/null", cwd: "{workspace}"}
      tests: [{name: ctest, run: "ctest --test-dir vb --output-on-failure -j8", cwd: "{workspace}"}]
# no 'programs:': cJSON is a library whose exported functions others call (step 6)
```
