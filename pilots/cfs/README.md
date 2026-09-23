# Pilot: NASA core Flight System (cFS) v7.0.1

The pilot is the third milestone of the planning documents: run Weaver on a real flight-software
code base and record what the evidence supports and what it rejects, and why. The targets are:

- pin cFS and capture the native configuration;
- produce the inventory, flow evidence and a rejection report for `sample_app`;
- write reviewed OSAL and cFE effect models;
- study `SBBufPtr` as a borrowed-buffer contract.

Everything below was produced by the commands in [Reproduce](#reproduce). The numbers come from
one run on a 4-core, 16 GB Linux container with GCC 13.3 and Clang 18.1.

## Reproduce

```sh
pilots/cfs/setup.sh cfs                 # clone cFS v7.0.1, check the pinned commits, add pilot_defs/ and weaver.yaml
weaver -C cfs refresh --capture         # rebuild through shims, collect, fidelity, inventory, SVF and GCC points-to
weaver -C cfs report --scope apps/sample_app -o cfs/REPORT.md
weaver -C cfs contract pin P-6af1f4646a --expect borrowed --reason "cfe_sb.h: valid until the next receive"
weaver -C cfs candidates --recipe local-alias --eligible      # whole program
weaver -C cfs serve                     # or explore in the browser
```

`setup.sh` refuses to continue unless `HEAD` is `088b2fa8` (tag `v7.0.1`) and every submodule is at
the commit in [`submodules.txt`](submodules.txt). It changes nothing in the cFS tree except adding:

- `pilot_defs/`: the stock `sample_defs` mission, trimmed to one native CPU with `sample_app` and
  `sample_lib`. The stock mission also builds lab applications, whose tables reference `hs`.
  The pilot's [`targets.cmake`](pilot_targets.cmake), [`install_custom.cmake`](pilot_install_custom.cmake)
  and [`generate_startup.cmake`](pilot_generate_startup.cmake) replace the stock files.
- [`weaver.yaml`](weaver.yaml): one profile (`native`, GCC production compiler, Clang as secondary
  frontend), the `cpu1` program, the `builtin:cfs` effect models, and CTest validation.

## What Weaver sees

| Stage | Result |
|---|---|
| Capture | 287 compile units and 10 links from `make mission-all`; 37 CMake probe compilations (compiler identification, try-compile) recognised and excluded. |
| Fidelity | All 287 units `secondary-checked`: Clang's view of each unit matches GCC's for every macro project code can observe, the header set and the active conditional segments. Getting there needed macros compared as token sequences with parameter names normalised (`offsetof(TYPE, MEMBER)` vs `offsetof(t, d)`), and macros used only in `#if` handled by the active-segment check. |
| Inventory | 3,343 pointer findings: 1,930 parameters, 1,039 locals, 168 fields, 119 returns, 41 typedefs, 46 globals and declarations. 435 lines are compiled by no analysed configuration (reported as unexamined). |
| Link model | 10 images. `cpu1` = `core-cpu1` + `cfe_assert.so` + `sample_lib.so` + `sample_app.so`, closed over the four entry points the startup script names (181 units). Five host tools are separate closed programs. `cfe_testcase.so`, which the pilot does not load, is an open program. |
| SVF | One `wpa` job per program. `cpu1`: 86,868 pointer nodes, 8,825 objects, 55 indirect call sites (6 unresolved), 10.6 s. All programs complete. |
| GCC points-to | 227 units recompiled with `-flto -fipa-pta`, 10 image links replayed, all complete, 8.5 s with cached objects. |

## `sample_app`

`sample_app` has 15 pointer findings: 10 parameters and 5 locals. By what each does to its
target: 8 escape, 4 are unused, 2 are read-only and 1 is reassigned. No refactor is eligible.
[`REPORT.md`](REPORT.md) lists every blocker with its first piece of evidence:

- **`scalar-input`** applies to the 10 parameters, and each fails several preconditions:
  - `SI.parameter-type` (9 of 10). The parameters point to messages and buffers
    (`const CFE_SB_Buffer_t *`, the command structs, `const CFE_MSG_Message_t *`) or to `void`
    (the table validation callback). Passing a message by value would copy it, so the recipe
    declines, as it should. The tenth, `TableName` in `SAMPLE_APP_GetCrc`, is a `const char *`
    that is passed on to `CFE_TBL_GetInfo` as a string, so it is not a read-only scalar either.
  - `SI.unconditional-read` (10). Most command handlers never read their message at all.
  - `SI.no-modification-during-call` (10). A message pointer may point into a software-bus
    buffer, and the cFE calls the handlers make (`CFE_EVS_SendEvent`, `CFE_SB_TransmitMsg`,
    `CFE_TBL_GetAddress`) may write framework-owned state. For the validation callback, GCC
    reports that its argument may point outside `sample_app.so` while the call writes memory.
  - `SI.complete-callers` (10). `sample_app_eds_dispatch.c`, compiled only with
    `CFE_EDS_ENABLED=ON`, and the coverage tests under `unit-test/` name these functions. Neither
    is in the analysed configuration, so those references are not explained by analysed calls.
  - `SI.no-concurrent-writers` (10, unresolved). cFS applications run in their own OSAL tasks and
    share data through the software bus and tables, so the pilot declares no concurrency model.
- **`local-alias`** applies to the 5 locals, and `LA.target-stable` is the only precondition each
  fails. `SBBufPtr`, `TblAddr` and `TblPtr` have no initializer: they are set by
  `CFE_SB_ReceiveBuffer(&SBBufPtr, …)`, by `CFE_TBL_GetAddress(&TblAddr, …)`, and by
  `TblPtr = TblAddr`. `TableName` points to a string literal. `TblDataPtr` is a cast of the
  callback's argument. None is an alias of one named object, which is what the recipe removes.

## Whole program

Across `cpu1`, 20 `local-alias` candidates are eligible, all in cFE Executive Services: locals
such as `CDS = &CFE_ES_Global.CDSVars` and `PerfDumpState`. For 987 other `local-alias` candidates
the only failing precondition is `LA.target-stable`: the local is assigned more than once, or
through an out-parameter.

One candidate was taken through the full transaction: `CDS` in `CFE_ES_ClearCDS`
(`cfe/modules/es/fsw/src/cfe_es_cds.c`). Validation:

1. recompiled the patched unit with the production command (no new diagnostics);
2. re-checked the patched AST;
3. rebuilt both workspaces with unit tests enabled;
4. ran all 117 CTest tests in the unpatched and patched trees.

112 tests pass on both trees. Five OSAL functional tests (`network-api-test`, `osal-core-test`,
`queue-test`, `timer-add-api-test`, `timer-test`) fail on both, because they need sockets, queues
and timers the container does not allow. They are recorded as pre-existing and not attributed to
the patch, and the transaction is `validated` with strength `behavioural`. Before per-test
comparison, CTest's exit status alone rejected every cFS transaction.

### Two flow backends on 1,871 scalar-input candidates

The may-modify precondition (`SI.no-modification-during-call`) over all 1,871 scalar-input
candidates in the pilot, by flow backend:

| Backends | Established | Violated | Unresolved | Time | Peak memory |
|---|---|---|---|---|---|
| SVF only | 331 | 683 | 857 | 206 s | 6.8 GB |

The GCC-only and combined rows are being measured and will be added here.

## `SBBufPtr` as a borrowed buffer

`CFE_SB_ReceiveBuffer` hands the application a pointer into a software-bus buffer. `cfe_sb.h` says
it must be treated as read-only and is valid only until the next receive on the same pipe. That is
a contract on how the pointer is used, not a refactor, so the pilot added a `borrowed` expectation.
It holds when nothing writes through the pointer or anything derived from it, and nothing stores
it where it would outlive the call (a global, the heap, a struct field, a return value).

`weaver contract pin … --expect borrowed` checks this before pinning. For `SBBufPtr` in
`SAMPLE_APP_Main` it followed the pointer through nine pointers:

| Function | Pointer | Where |
|---|---|---|
| `SAMPLE_APP_Main` | `SBBufPtr` (local) | `sample_app.c:48` |
| `SAMPLE_APP_TaskPipe` | `SBBufPtr` | `sample_app_dispatch.c:132` |
| `SAMPLE_APP_ProcessGroundCommand` | `SBBufPtr` | `sample_app_dispatch.c:77` |
| `SAMPLE_APP_VerifyCmdLength` | `MsgPtr` | `sample_app_dispatch.c:39` |
| `SAMPLE_APP_NoopCmd`, `ResetCountersCmd`, `ProcessCmd`, `DisplayParamCmd`, `SendHkCmd` | `Msg` | `sample_app_cmds.c` |

It went through the casts from `CFE_SB_Buffer_t *` to each command type and through
`&SBBufPtr->Msg`. It also reached `CFE_MSG_GetMsgId`, `CFE_MSG_GetFcnCode` and `CFE_MSG_GetSize`,
which the cFS pack models as reading the message and writing only their output argument. It found
no write and no retention, so the contract was pinned as `C-11150a15`. `weaver check` now reports
a violation if a later edit stores the buffer pointer or writes through it, for example a handler
that caches `Msg` in a global to use after the next receive.

## Effect models

[`data/models/cfs.yaml`](../../src/weaver/data/models/cfs.yaml) holds 44 reviewed models: 25 cFE
and 19 OSAL APIs that `sample_app`, `sample_lib` and their callees use. Each one was checked
against the v7.0.1 source. They are **boundary** models: for an application, a cFE call is judged by its
contract, not by re-analysing cFE's implementation. The pack declares cFE, OSAL and PSP paths as
framework-owned (`owns`), so a model can say "writes only framework state" (`writes_owned`), which
matters only for a pointer that may point there. Entries that needed care:

- `CFE_TBL_Load`, `CFE_TBL_Manage` and `CFE_TBL_Validate` can call the application's table
  validation function (`calls_back: true`).
- `OS_TimerCreate` writes arguments 1 and 3 (the timer ID and the clock accuracy). An early draft
  had 1 and 2.
- `OS_TaskCreate` keeps its stack pointer argument (`retains`).
- `CFE_SB_TransmitMsg` copies the message and writes nothing the caller passed.
- `CFE_SB_ReceiveBuffer` writes only argument 1, the buffer pointer (the borrow above).

POSIX and glibc calls (about 190 functions) come from a separate pack that is always loaded.

## Things to know

- **SVF is field-insensitive in this configuration** (`-field-limit=0`). A write to any field of
  `CFE_ES_Global` is a write to all of it. That is sound, but it is why many cFE may-modify answers
  are `yes`.
- **Integer address arithmetic reaches SVF's black-hole object.** cFE's memory pools and
  software-bus buffer descriptors compute addresses from integers. SVF then models the result as
  pointing to unknown memory, and may-modify answers `unknown`, never `no`.
- **No concurrency model is declared.** Interface refactors stay blocked on
  `SI.no-concurrent-writers` until someone states which task owns which data. A per-task ownership
  model is the next milestone.
- **Other configurations name the same functions.** The capture profile builds without unit tests
  and without EDS, which matches the flight build. The coverage tests and the EDS dispatch table
  still name application functions, so complete-caller checks fail on references no analysed
  configuration explains. A second profile with `ENABLE_UNIT_TESTS=TRUE` and one with EDS would
  turn them into analysed callers. Validation already builds with unit tests.
- **Sandbox-dependent tests fail on both trees.** Per-test comparison keeps them from blocking
  every transaction, and the card lists them.
- **Cost.** Fidelity takes about 2 minutes. Evaluating one recipe over the whole program takes
  about 3.5 minutes, with about 7 GB peak resident memory. Validating one transaction takes about
  70 s (two full builds with unit tests, and CTest twice).
