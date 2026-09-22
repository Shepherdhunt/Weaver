This plan describes a compiler-assisted tool that helps a user remove C pointers in small, reversible steps toward a pointer-free CLite program. NASA cFS is the example codebase. This is a design proposal based on source and documentation review; no repository conversion or build has been performed.

The central rule is: preserve what each pointer does, including shared mutation and lifetime, before changing how the program represents it. The LLM explains and proposes changes. Compiler analysis supplies source facts, and a separate validation process controls acceptance.

1. **Define the preservation contract and CLite capabilities.**

   Record the supported C dialect, compiler options, platforms, build configurations, input domain, and external dependencies. Define the behavior to preserve: outputs, persistent state, side-effect ordering, termination, error behavior, shared state, and relevant concurrent interactions. Explicitly record requirements for binary interfaces, serialized layouts, memory consumption, throughput, and deadlines.

   Establish whether CLite permits value records, indexed collections, dynamic storage, typed object IDs, shared mutable storage, and platform APIs. These are provisional capabilities until a target specification exists. A recipe that requires an unconfirmed capability remains blocked for CLite export, although some C-to-C simplification may proceed independently.

   Typed IDs can remove raw addresses from application code while preserving logical indirection. They are only a valid destination if CLite permits them. If CLite forbids every form of indirection, shared graphs and cyclic structures may require algorithmic redesign or be unsupported. Hardware addresses and existing pointer-based foreign interfaces require equivalent target facilities or a declared compatibility boundary.

   Define completion separately for each scope: application source, compatibility adapters, runtime, and whole program. A remaining C adapter means the whole program is not pointer-free. Integerizing addresses and casting them back does not qualify as pointer elimination. Neither does changing a C parameter from `T *p` to `T p[]`: C adjusts the latter to pointer type. [C11 draft, §§6.3.2.3 and 6.7.6.3](https://www.open-std.org/jtc1/sc22/wg14/www/docs/n1570.pdf)

   The tool must not promise unrestricted equivalence for arbitrary C. Its claims must identify the supported programs, verified preconditions, modeled environment, and validation limits. Existing undefined behavior needs separate investigation; it is not a stable behavior specification to reproduce.

2. **Capture a reproducible baseline.**

   Pin the repository commit, recursive submodule revisions, toolchain, dependency versions, and configuration. Build and run existing tests before editing. Record existing failures and a reference executable or callable reference implementation for later comparisons.

   Collect compilation commands for every supported configuration, including generated headers and code. Treat unparsed files and inactive configurations as unexamined, never as pointer-free. A compilation database supplies the flags and working directory needed to parse each translation unit correctly. [Clang compilation database](https://clang.llvm.org/docs/JSONCompilationDatabase.html)

   For cFS, inventory the populated submodules, not only the top-level repository. The bundle includes cFE, OSAL, PSP, applications, and libraries. Its current default-branch instructions provide a native development/test configuration; actual commands should be taken from the README at the pinned revision. Start there for the pilot, then add the intended target configurations before broader claims. [cFS repository](https://github.com/nasa/cFS)

3. **Build a semantic pointer inventory.**

   Use Clang LibTooling and AST matchers for compiler-resolved types, source locations, queries, and structured edits. Add interprocedural call, alias, escape, and lifetime analysis; AST matching alone does not establish those properties. Preserve an explicit “unknown” result whenever analysis cannot establish a fact. [Clang AST matchers](https://clang.llvm.org/docs/LibASTMatchers.html), [refactoring support](https://clang.llvm.org/docs/RefactoringEngine.html)

   Find pointer variables, parameters, returns, record fields, nested pointer types, typedef-hidden pointers, function pointers, implicit array-to-pointer conversions, address-taking, dereferences, arithmetic, casts, and pointer-bearing library operations. Track macro definitions and expansion sites, generated sources, and external contracts. Address values hidden in integers or byte storage also require investigation when discovered.

   Give each finding a stable ID tied to the source revision and configuration. Record its possible target objects; reads and writes; nullability; ownership; allocation and release; lifetime; aliases; bounds and interior offsets; identity comparisons; escapes; callers and callees; volatile or atomic access; and externally visible layout or interface dependencies. Distinguish compiler-established facts, API contracts, runtime observations, and hypotheses.

   Group the selected pointer with every object, alias, use, caller, and declaration that must change together. This connected group is the migration unit. It may be one local variable or cross several modules. Unknown calls and incomplete callback targets prevent a claim that the group is complete. Runtime observations can reveal targets but cannot establish that no other targets exist.

4. **Choose a transformation recipe with explicit preconditions.**

   Each recipe needs an identifier, applicability checks, target capability requirements, edit rules, preservation argument, targeted validation, and rejection reasons.

   | Original role | Candidate replacement | Essential conditions |
   |---|---|---|
   | Local alias of one known object | Direct access to that object | Target is stable; no relevant address escape or identity observation; access and evaluation behavior preserved |
   | Read-only scalar input | Value parameter | A snapshot has the same behavior as all original reads; `const` alone is insufficient |
   | Isolated output parameter | Return value or result record | All callers updated; aliases, write timing, and success/failure behavior preserved |
   | Array cursor, substring, buffer view | Native collection plus index/range, optionally collection ID | Same backing storage, bounds, overlap, mutation, and lifetime |
   | Owned dynamic allocation | Target object or collection facility | Capacity, allocation failure, initialization, destruction, and required resource behavior preserved |
   | Shared objects, linked structures, cycles | Typed IDs into shared storage | Aliases retain identity and observe the same writes; lifetime and slot reuse handled |
   | Function pointer | Function tag with dispatch | Complete supported target set and equivalent callback behavior; dynamic loading requires a suitable target facility |
   | OS, device, or foreign-library pointer | Equivalent platform API or explicit adapter | External contract preserved; remaining adapter pointers separately reported |

   An object ID identifies storage; it must not be a disguised numeric machine address. If storage slots are reused, a generation field or another lifetime mechanism may be needed. Access through an ID must preserve shared writes rather than return independent mutable copies. Preserve null/absent values explicitly. Do not add fixed capacity to formerly dynamic storage without an established capacity contract.

   Array indexing in an intermediate C implementation still has C pointer semantics. Final pointer-free certification must use CLite's type and operation rules, not a textual search for `*`.

5. **Demonstrate why a recipe preserves behavior.**

   A simple eligible local transformation, assuming the shown code is the complete relevant use, is:

   ```c
   /* Before */
   unsigned total = 3;
   unsigned *p = &total;
   *p += 2;
   return total;
   ```

   ```c
   /* After */
   unsigned total = 3;
   total += 2;
   return total;
   ```

   The replacement accesses the original object directly. Copying the initial value into a separate variable would not preserve the update to `total`.

   An important rejection case for naive output-to-return conversion is:

   ```c
   void update(int *a, int *b) {
       *a = 1;
       *b += 1;
   }

   int x = 0;
   update(&x, &x);  /* x becomes 2 */
   ```

   Both parameters denote the same object. Separate value arguments and independent returned outputs can lose that behavior. The tool must establish that this aliasing cannot occur, preserve it through shared storage, or reject this recipe.

   For broader transformations, define a relation between original and replacement states: corresponding objects contain corresponding values, aliases refer to the same logical object, valid lifetimes match, and observable events correspond. This gives static checks and validation harnesses a concrete specification.

6. **Offer one reviewable transaction at a time.**

   The user flow is: inspect inventory, select a pointer, inspect the connected migration unit, compare eligible replacements, preview the patch and evidence, validate, then accept or skip. An automatic mode may accept recipes under a user-selected policy; semantic design choices remain explicit.

   Every candidate card should state:

   ```text
   Candidate and source/configuration revision:
   Current pointer role and evidence:
   Objects, aliases, files, interfaces, and callers affected:
   Proposed recipe and target capabilities:
   Preconditions established / unresolved:
   Behavior and resource requirements to preserve:
   Patch and validation plan:
   Results, assumptions, bounds, and remaining limitations:
   Decision and rollback checkpoint:
   ```

   Use explicit states such as discovered, analyzed, blocked, proposed, validated, accepted, skipped, and reverted. Validation records must identify whether the evidence is testing, bounded checking, or a proof under stated assumptions. Avoid unsupported percentage-confidence scores.

   Apply changes in an isolated checkpoint. Preserve unrelated user edits. On failure, discard only this transaction. After acceptance, reparse and invalidate affected analysis before selecting the next unit. Bind validation results to the exact patch and source revision so stale results cannot authorize later edits.

7. **Require layered validation before acceptance.**

   Compile all affected supported configurations and check recipe preconditions mechanically where possible. Retain the original tests and add meaningful cases specific to the semantic risk: aliased arguments, empty ranges, overlapping views, boundary offsets, lifetime transitions, allocation failures, and relevant error paths.

   Compare original and transformed behavior under the same controlled inputs. Compare return values, logical state, errors, and external event traces; raw memory bytes may include irrelevant addresses or padding. Control clocks and other environmental inputs, or explicitly model permitted nondeterminism. Do not normalize away ordering or timing that the contract requires.

   Use fuzzing and runtime diagnostics where supported. AddressSanitizer and UndefinedBehaviorSanitizer help detect classes of errors in exercised executions; they do not establish functional equivalence. [AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html), [UndefinedBehaviorSanitizer](https://clang.llvm.org/docs/UndefinedBehaviorSanitizer.html)

   For small components, add paired equivalence or invariant harnesses using a tool such as CBMC. Record input assumptions, loop and recursion bounds, environment models, and whether unwinding assertions establish sufficient coverage. A successful bounded check is not automatically a universal proof. [CBMC](https://www.cprover.org/cbmc/), [unwinding guidance](https://www.cprover.org/cprover-manual/cbmc/unwinding/)

   Measure memory and required timing on representative, uninstrumented target builds. Host tests alone cannot establish target deadlines or concurrent correctness. If a required acceptance condition cannot be evaluated, retain a provisional result rather than silently marking the transaction accepted.

8. **Use cFS to exercise the design incrementally.**

   Begin with a pinned native configuration and one application, such as `sample_app`, together with the dependencies needed to analyze it. First produce an inventory and rejection report. Select the smallest candidate whose recipe preconditions are actually established; do not assume a particular pointer is eligible before analysis.

   For the first editing milestone, remove local aliases while keeping an executable C baseline. Then add isolated scalar input/output transformations within interfaces whose callers are completely known. Changes to public module interfaces need either coordinated migration of all clients or a separately tracked compatibility adapter.

   A concrete later study is `SBBufPtr` in `SAMPLE_APP_Main`. The application passes its address to `CFE_SB_ReceiveBuffer` and dispatches the returned buffer only after `CFE_SUCCESS`. The API contract makes a successful buffer read-only and limits its validity to the next receive on the same pipe. On failure, the output cannot be assumed usable. This is a borrowed service buffer whose lifetime is controlled outside the application. [Pinned sample application](https://github.com/nasa/sample_app/blob/7cbc4c843d892f326df88b1ac0d266ec1eb4d74f/fsw/src/sample_app.c#L48), [pinned receive API contract](https://github.com/nasa/cFE/blob/7d283938c9af5bd6e439f3fc307e61b0c7799640/modules/core_api/fsw/inc/cfe_sb.h#L440)

   Do not replace that pointer with a `CFE_SB_Buffer_t` value and assume the whole message has been copied: the declared buffer union covers the base message and alignment, while a received message can include additional payload. A copying design would need the actual validated message extent as well as a preservation argument for copying costs and lifetime. [Pinned buffer type](https://github.com/nasa/cFE/blob/7d283938c9af5bd6e439f3fc307e61b0c7799640/modules/core_api/fsw/inc/cfe_sb_api_typedefs.h#L138)

   A provisional alternative is a typed message token with read-only access and validity tied to the pipe's receive generation. It must preserve success/failure behavior, backing storage, payload access, and the receive lifetime. If implemented through a C adapter, the adapter's pointers remain on the migration ledger. CLite support and full service analysis are prerequisites; this is a design candidate, not a verified conversion.

   Treat table access, callbacks, and OS/platform interfaces as later studies as well. Their replacements must account for sharing, binary layout, delivery and failure behavior, and any copying or timing costs.

   Compare command results, telemetry content, counters, events, and persistent state where relevant, with controlled treatment of time and scheduling. Track application-local pointers, cross-module pointers, adapter/runtime pointers, and unexamined configurations separately. Report pointer-bearing types and operations in addition to declaration counts.

9. **Build the tool in stages.**

   The proposed components are a build/configuration loader, compiler-backed inventory and dependency graph, recipe catalog, LLM planner/explainer, deterministic source rewriter, independent validation runner, and transaction ledger. The LLM should consume source facts with locations and request focused additional analysis instead of assuming an entire repository fits into its context.

   The first release should deliver a useful inventory, candidate explanations, and a reliable local-alias recipe with preview, validation, rejection, and rollback. Test the recipe on positive examples and deliberate counterexamples involving escape, reassignment, aliasing, macros, and alternate configurations. A successful rejection is part of correctness.

   Later releases can add scalar interface transformations, buffers and ranges, shared-storage IDs, then callback and platform integration. Introduce a recipe only after its preconditions and preservation strategy are explicit. Version the target model and recipes so a resumed migration can detect incompatible assumptions.

   Measure progress by supported behavior preserved, accepted transformations, remaining dependency groups, and actionable blockers. A shrinking count of asterisks is not a sufficient success metric.

10. **Use this reusable instruction for the LLM.**

    ```text
    You are the planner for an incremental C pointer-elimination tool.
    Work toward the supplied CLite target model and preservation contract.
    Do not invent target-language capabilities.

    Inputs: pinned source and dependency revisions; build configurations;
    compiler-derived pointer inventory and dependency graph; API contracts;
    recipe catalog; baseline results; transaction ledger; user selection or
    automatic-selection policy.

    Select one small migration unit. Inspect its objects, aliases, ownership,
    lifetimes, nullability, bounds, reads/writes, escape paths, callers,
    callbacks, casts, and relevant concurrency and external interfaces.
    Distinguish established facts from observations and assumptions.

    Identify an eligible recipe. For every precondition, provide evidence
    or mark it unresolved. Explain the original behavior, replacement
    representation, affected uses, and why the required behavior remains.
    If the recipe is unsupported or evidence is incomplete, report the
    specific blocker and the analysis or design decision that would resolve
    it. Continue with independent eligible candidates when appropriate.

    Produce one minimal transaction and targeted validation plan. Preserve
    shared mutation, identity where observed, lifetime, errors, side-effect
    ordering, capacity, and required interface and resource behavior.
    Do not hide pointers in integer addresses, typedefs, array parameters,
    or undeclared wrappers. Record adapters as remaining dependencies.
    Do not weaken tests or diagnostics to make a change pass.

    Delegate edits and checks to the tool's rewriter and validation runner.
    Never invent validation results or call passing tests a universal proof.
    State all proof assumptions and verification bounds. Bind evidence to
    the exact source revision and patch. Present the candidate card, patch,
    actual results, limitations, and accept/skip/blocked recommendation.
    Accept only under the user's configured acceptance policy. Reanalyze
    affected dependencies after each accepted change.
    ```
