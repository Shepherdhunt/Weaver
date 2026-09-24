# Deployment plan

Goal: a subscription product at $199 per month. Users sign in through the company portal, bring their
own repositories, trace every pointer, see what each one risks, and simplify the code. CLite, a C
subset without pointers and many other constructs, is one possible target. Often the target is
simply smaller, simpler software that can then be redesigned into modules. Weaver does not certify
CLite: it helps people work through the translation problems by hand, with evidence. Until release,
Weaver runs on local machines for testing.

## Decisions made

| Question | Decision |
|---|---|
| Where code is analysed | On the customer's machines. Weaver is licensed software they run themselves. |
| Licence | One subscription per project, for the project's named users. Use on other projects is discouraged, not blocked. A licence file or signed token; not needed yet. |
| Identity | The portal has no identity provider yet; one is chosen in Phase 1. |
| Priority | Functional capability first; licensing, billing and sign-in come later. |
| CLite | One target profile among several. The goal is simplification and modular redesign, not certification. |
| Points-to analysis | SVF and GCC both first-class, shown side by side. |
| AI explanations | Off by default, switchable per project. Bring your own key. One explanation guide makes every provider explain in the same way. |

## Where Weaver is today

- A local tool: a CLI and a loopback web interface (port 61847 or the next free one, entered through
  a sign-in link that `weaver serve` prints). One user, one project per server, all state in the
  project's `.weaver/` directory.
- Proven on the demo program and on NASA cFS: about 14,000 pointers, and whole-program evaluation in
  about 4 minutes per recipe with under 1 GB of memory (see [`pilots/cfs`](../pilots/cfs/README.md)).
- Two pointer-removing recipes (`local-alias`, `scalar-input`), validated transactions with revert,
  change impact, pinned contracts, and a checked task model for multi-task programs.
- Not yet a product: there are no accounts or licences, the only installation is from source, users
  install their own compilers and analysis tools, and CLite itself has no specification yet.

## Why customer-run

Weaver builds the customer's code. It runs their build commands, compilers and tests. This can
happen in two places, and the choice shapes everything after Phase 0.

- **Customer-run.** Weaver runs on the customer's workstation or CI. The portal handles sign-in,
  subscription, licence and updates. Source code never leaves the customer.
- **Hosted.** Customers connect repositories, and Weaver's cloud runs their builds.

**Decided: customer-run. A hosted option stays in Phase 3 for customers who want it.**

- **Export control.** The natural market (flight, defence, medical and automotive C) often cannot
  upload source: ITAR/EAR, controlled unclassified information, and contract terms rule it out.
  cFS users are in this group.
- **Toolchains.** Builds need the customer's toolchains: cross compilers, vendor compilers with
  node-locked licences (Wind River Diab, Green Hills) and board support packages. A cloud service
  cannot host those in general.
- **Security.** A hosted service executes arbitrary customer builds, so it needs isolated
  sandboxes, egress control and a security programme before the first customer.
- **Compute cost.**
  - Analysis at cFS scale takes minutes of CPU per recipe evaluation.
  - Each validation needs two full builds and two test runs (about 70 s on cFS).
  - Hosted compute for a large repository could exceed $199 per month without usage limits.
  - Customer-run puts the compute on the customer's machines.
- **Fit.** The current architecture is already local-first.

## Phase 0: finish local testing (now)

Aim: Weaver installs on other machines without help and holds up on repositories other than cFS.

1. **Packaging.**
   - A versioned wheel.
   - A container image with pinned Clang 18, GCC 13 (with the LTO plugin), binutils, and SVF as
     an optional layer. Publish the port only on the host's loopback:
     `-p 127.0.0.1:61847:61847`.
   - Native installers later.
2. **`weaver doctor`.** Checks compilers, the GCC LTO plugin, SVF, disk and memory, and says what
   is missing and how to install it.
3. **Several projects per server.**
   - The web interface opens and switches between projects, and remembers recent ones across
     restarts. Today it keeps them in memory.
   - `--scope` already handles large trees.
4. **Robustness.**
   - Cancelling jobs, and job logs kept on disk.
   - Opt-in crash reports.
   - Migrations for the versioned `.weaver/` schemas.
5. **More pilots.** Three to five codebases of different shapes:
   - bare-metal firmware with a cross compiler;
   - a Linux daemon;
   - a library;
   - a CMake + Ninja project;
   - a Makefile project with generated code.

   For each, measure time, memory, unexamined code, candidates found, and blocks later judged
   wrong.
6. **Playtesting.** Collect feedback from the read-only page (`weaver export-ui`) and from local
   installs.

## Phase 1: paid beta, customer-run (the $199 product)

1. **Sign-in through the portal.**
   - The portal has no identity provider yet. Candidates: Auth0, AWS Cognito, Okta or self-hosted
     Keycloak, all of which speak OpenID Connect.
   - `weaver login` uses the OAuth 2.0 device-authorization flow (like `gh auth login`): it shows a
     code, the user approves it in the portal, and Weaver keeps a refresh token in the operating
     system's keychain.
   - This identity is separate from the local server's sign-in link, which stays.
2. **Subscriptions.**
   - Stripe Billing: Checkout and the customer portal for the $199 plan, trials, invoices, and tax
     (Stripe Tax). Card data never reaches Weaver's servers.
   - Stripe webhooks feed an entitlement record per account.
3. **Licence check, per project.**
   - A subscription covers one project and lists its named users. The licence is a file or a token
     signed by the company, naming the project and users, valid for about a week and cached locally
     with an offline grace period.
   - Use on other projects is discouraged (the licence names its project), not prevented.
   - A Python tool can be patched. The value customers pay for is updates, reviewed models,
     recipes and support, not copy protection.
4. **Distribution.** Signed releases, an update channel and release notes, from a package index
   that requires sign-in.
5. **Interface.** Show the signed-in account and plan, add sign-out and licence status, and link
   to the portal for billing.
6. **Telemetry.** Opt-in, counts and durations only, never code, file names or identifiers.
7. **Legal.**
   - Terms of service, EULA, privacy policy, and a data processing agreement for the portal.
   - SVF (AGPL-3.0) is first-class alongside GCC's points-to. It runs as a separate process and is
     installed with `weaver[flow]`. Have counsel confirm the distribution terms.
   - AI explanations send code excerpts to the provider the customer chooses, with the customer's
     own key. They are off by default and switched on per project. A local model server keeps
     everything on the customer's machine.
8. **Support.** A documentation site, a guide to onboarding a repository (build command with
   `{cc}`, tests, programs, tasks), and an issue intake.

## Phase 2: teams and results in the portal

- **Result upload.** Optional, never source: pointer counts, risk trends, CLite readiness and
  transactions, for dashboards across projects. The customer chooses what leaves the machine.
- **CI.** `weaver check` as a GitHub or GitLab check, and pull requests opened from accepted
  transactions.
- **Review.** A second person approves a transaction before it is accepted, with roles in the
  organisation.
- **Enterprise.** Organisation billing with seats, an admin console, and SAML single sign-on.

## Phase 3: hosted option (only for customers whose code may leave their network)

- **Control plane.**
  - The web server becomes a multi-user service. Sessions come from OpenID Connect, and every
    route checks the organisation's access to the project.
  - Postgres holds accounts, projects and a ledger index.
  - Object storage holds evidence, encrypted with a key per tenant.
- **Repository access.** A GitHub App or GitLab OAuth, read-only, with short-lived tokens.
- **Build runners.**
  - Every job runs in a fresh microVM (Firecracker, Kata or gVisor), destroyed afterwards.
  - Quotas on CPU, memory, time and disk.
  - No network beyond an allow-listed package mirror.
  - Customers supply a build image with their toolchain.
- **Operations.** A job queue and scheduler, compute quotas per plan, and a cost model per
  analysed line of code.
- **Security programme.**
  - Threat model, penetration test, SOC 2 Type II, incident response, retention and deletion, and
    audit logs.
  - FedRAMP- or ITAR-compliant hosting is a separate, expensive track.

## Product track, in parallel: from pointer risk to simpler software

1. **Simplification checker with target profiles.**
   - From the AST, list every construct in each function and file that the chosen target does not
     allow: pointers, `goto`, unions, variadic functions, function pointers, dynamic allocation,
     recursion, casts and so on.
   - CLite is one profile. Others describe a simplification goal (for example "no pointers in
     application code" or "no dynamic allocation"), and projects can adjust rules.
   - A function that meets the profile counts as ready. Readiness becomes a metric with the same
     progress bar as removed pointers. It is a guide for manual work, not a certification.
2. **CLite definition.** When a CLite specification exists, its rules replace the provisional CLite
   profile. Until then the profile is marked provisional.
3. **Risk view.**
   - Score each pointer from facts Weaver already has: escapes, writes through, unknown targets,
     concurrent writers, pointer arithmetic, casts to integers, borrowed buffers.
   - Aggregate per function, file and module.
   - Show a heat map on the Map view and a sortable risk list, included in `weaver report`.
4. **More recipes, in the order cFS needs them:**
   - output parameter → return value;
   - buffer and length → bounded array or range;
   - handles → typed IDs;
   - callbacks → enumerated dispatch.

   Each comes with preconditions, evidence and validation as today.
5. **Translation support.** For a function that meets its profile, help the manual translation
   into CLite or into a module of the redesign. Adapters at the boundary stay on the ledger until
   removed.
6. **Precision at scale.** Field-sensitive ownership and lock-aware concurrency (see
   [`architecture.md`](architecture.md), next milestones).

## Still open

- The portal's identity provider (Phase 1).
- Who owns the CLite definition, and where it lives.

## Next work items, functional first

Done:

- AI explanations: on/off per project, bring-your-own key, one shared guide for every provider.
- SVF and GCC evidence side by side in the interface.
- The simplification checker with target profiles (`weaver simplify`, the Simplify tab).
- The risk view and risk report (`weaver risk`, the Risk tab, colour by risk on the Map).
- The output-parameter recipe (`output-param`): void and status forms, optional outputs
  (`has_value`) and leaf-first conversion through forwarded pointer parameters. On cFS, 6 of 146
  candidates are eligible; one of each form passes validation on the real build:
  - `CFE_TBL_TxnOpenTableLoadFile` (status);
  - `UT_ObjIdDecompose` (void);
  - `CFE_TBL_SearchCmdHandlerTbl` (optional output).

  Each time, every affected unit compiles, the mechanical re-check and full build pass, and the same
  112 ctest tests pass before and after the change. The first validation of `UT_ObjIdDecompose`
  failed, and that failure is how the discarded-output case was found.
- Your own change (`weaver patch`, **Check my change…**): a unified diff becomes a transaction,
  validated like a recipe's patch. Its re-check compares every affected unit's pointer facts before
  and after the patch, fails when a named pointer survives or a contract breaks, and lists every other
  change for review. On cFS, a hand-written patch removing the local alias `CmdPtr` in
  `CFE_ES_StartPerfDataCmd` validates. `local-alias` refuses that pointer, because it is reached
  through `data->`. The re-check finds the pointer removed and nothing else changed, the full build
  passes, and the same 112 ctest tests pass before and after the change.
- AI drafts (`weaver draft`, **Draft a change with AI**, behind `ai.drafts`): the model drafts a patch
  under its own guide. Weaver applies it by content and asks once more if it does not apply, then
  proposes it as a patch transaction under the same checks. Nothing is applied until the draft
  validates and a person accepts it.

Next:

1. Several projects per server; `weaver doctor` and the container image.
2. Widen `output-param` along its measured cFS blockers (146 candidates, 6 eligible). One candidate
   usually fails several preconditions:
   - Callers outside the analysed build (unit tests, other applications, other OS ports) or a
     function whose address is taken: 125 candidates, 41 of them blocked by nothing else. In cFS,
     every public function also has a generated unit-test stub. Analysing the unit-test build as a
     profile, and updating stubs together with the function, would lift most of these.
   - Targets that are not private: 59 candidates. Most pass a pointer into a shared record
     (`&Rec->Field`) or a pointer variable. Allowing them needs the points-to evidence to show that
     the call touches the target through nothing else, and the task model to show that no other
     task does.
   - In-out parameters, read as well as written: 22 candidates. They need their own recipe.
   - Callers that pass `NULL` because they do not want the output: 10 call sites. These are
     convertible when the null test in the callee guards only the write.

## How much can be automated

This assessment is measured on the cFS pilot: cFE, OSAL, PSP and the sample app, with 3,343 pointer
findings in the analysed build.

| What | Result on cFS |
|---|---|
| Analysis: inventory, SVF and GCC points-to, task model, risk, simplification | the whole build in minutes, under 1 GB |
| `output-param` | 146 candidates, 6 eligible, 3 validated on the real build |
| `scalar-input` | 1,871 candidates, 0 eligible |
| `local-alias` | 1,039 candidates, 20 eligible |
| All recipes together | about 26 of 3,343 pointers removable automatically today (under 1%) |

**The analysis half is realistic today.** It shows a team:
- where the pointers are;
- which pointers are risky, and why;
- what each pointer may point to, by two independent engines;
- which task may write its target;
- which functions already meet a target profile;
- what exactly blocks each automatic change.

That is the map a de-pointering effort needs, and it scales to a flight-software code base.

**Automatic removal will stay a minority of the pointers in mature C.** 987 of the 1,039 local
pointers fail only on being bound once to a fixed object: they receive a buffer or record from a
call, or are reassigned. Most cFS pointers are structural:
- buffers with lengths;
- handles to shared records reached through pointers;
- message buffers;
- callbacks;
- OS interfaces;
- public functions whose stubs, tests and users elsewhere depend on their signatures.

Removing one of these is a design decision, such as a new type, an ownership rule or an interface
change. A behaviour-preserving recipe cannot make that decision on its own. Each recipe extension
adds candidates (optional outputs and leaf-first took `output-param` from 3 to 6 eligible on cFS),
but none changes that picture.

**So the realistic product is a guided, checked migration.** Weaver finds and ranks the work, and
makes the provably safe mechanical changes itself. Every other change, made by a person or an AI
assistant, goes through the same checks. The recipes remain valuable as much for their blockers,
which tell an engineer exactly what to change by hand, as for their patches.

### Options that fit the tool

In rough order of value:

1. **Validate your own patch** (done: `weaver patch`). Take a diff written by hand and run it
   through the same pipeline:
   - compile in every configuration;
   - re-check that the pointer is gone and that nothing else changed;
   - run the tests and differential runs;
   - show the change impact on facts and contracts;
   - record a ledger entry.

   This makes the manual majority of the work as checkable as the recipes, and fits the existing
   transaction model.
2. **AI-drafted patches through the same checks** (done: `weaver draft`). For a blocked pointer,
   the configured model drafts a change from the evidence slice under its own guide, and Weaver
   validates it like a manual patch. The model proposes; nothing is accepted without the checks. This reuses the
   bring-your-own-key setup.
3. **Coverage of the changed lines.** During validation, measure (gcov or llvm-cov) whether the
   tests executed the edited lines, and say so on the card. A passing suite that never runs the
   changed function is weak evidence today, and the card should not call it behavioural.
4. **A ratchet in CI.** `weaver check` fails a merge request that adds pointers, high-risk pointers
   or profile violations in the files it touches. This keeps progress from eroding while teams
   migrate, and builds on snapshots and change impact.
5. **Unit tests and stubs inside the program.** Analyse the unit-test build as a profile, and let a
   recipe update generated stubs and test call sites together with the function. On cFS this is
   the largest single blocker (see Next, item 2).
6. **Shared targets from evidence.** Allow outputs into shared records when SVF and GCC agree that
   the call reaches the record only through the parameter, and the task model shows no other task
   touches it during the call.
7. **Idiom recipes in order of frequency.** Count the idioms first, using the simplification
   checker's rule counts. Then build recipes in the order the code base needs them:
   - buffer and length → bounded array or span record;
   - handle → typed ID;
   - callback → enumerated dispatch;
   - small read-only record → by value;
   - in-out scalar → value in, value out.
8. **Bounded equivalence per function.** For a recipe's output, generate a CBMC harness that runs
   the old and new function on the same unconstrained inputs and compares their results, up to a
   loop bound. This is stronger than tests, and honest about its bound. It suits the certification
   culture of flight software.
9. **Module-at-a-time redesign with boundary adapters.** Convert a module's interior to the target
   profile and keep a pointer-based adapter at its boundary. The ledger lists the adapters until
   the callers move. This is how a C-to-CLite translation can proceed incrementally.
10. **Team workflow for named users.** Assign modules or pointers to people, record reviews on
    ledger entries, and show a burn-down per module across snapshots. This matches the per-project
    licence for named users.

**Limits to state to customers:**
- The analysis covers the configurations that were built.
- Macro-heavy code blocks edits more often than analysis.
- C only, no C++.
- A working build must be captured.
- Every linked object must be analysed or modelled.
