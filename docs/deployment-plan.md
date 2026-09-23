# Deployment plan (draft)

Goal: a subscription product at $199 per month. Users sign in through the company portal, bring their
own repositories, trace every pointer, see what each one risks, and refactor the code toward CLite,
a C subset without pointers and other constructs. Until then Weaver runs on local machines for
testing.

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

## Decision 1: where customer code is analysed

Weaver builds the customer's code. It runs their build commands, compilers and tests. This can
happen in two places, and the choice shapes everything after Phase 0.

- **Customer-run.** Weaver runs on the customer's workstation or CI. The portal handles sign-in,
  subscription, licence and updates. Source code never leaves the customer.
- **Hosted.** Customers connect repositories, and Weaver's cloud runs their builds.

**Recommendation: customer-run first, hosted later as an option.**

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
   - The portal acts as an OpenID Connect provider. If it has none: Auth0, AWS Cognito, Okta or
     self-hosted Keycloak.
   - `weaver login` uses the OAuth 2.0 device-authorization flow (like `gh auth login`): it shows a
     code, the user approves it in the portal, and Weaver keeps a refresh token in the operating
     system's keychain.
   - This identity is separate from the local server's sign-in link, which stays.
2. **Subscriptions.**
   - Stripe Billing: Checkout and the customer portal for the $199 plan, trials, invoices, and tax
     (Stripe Tax). Card data never reaches Weaver's servers.
   - Stripe webhooks feed an entitlement record per account.
3. **Licence check.**
   - A short-lived entitlement (a token signed by the company, valid for about a week) is cached
     locally, with an offline grace period. Features are gated per plan in one module.
   - A Python tool can be patched. The value customers pay for is updates, reviewed models,
     recipes and support, not copy protection.
4. **Distribution.** Signed releases, an update channel and release notes, from a package index
   that requires sign-in.
5. **Interface.** Show the signed-in account and plan, add sign-out and licence status, and link
   to the portal for billing.
6. **Telemetry.** Opt-in, counts and durations only, never code, file names or identifiers.
7. **Legal.**
   - Terms of service, EULA, privacy policy, and a data processing agreement for the portal.
   - SVF is AGPL-3.0. Keep it an optional component that the user installs (as now), or default to
     GCC's points-to. Either way, have counsel review it.
   - `weaver explain` sends code excerpts to an LLM API. Keep it off by default, enable it per
     organisation, and use the customer's own API key or a zero-data-retention agreement.
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

## Product track, in parallel: from pointer risk to CLite

1. **CLite specification (blocker).** Weaver needs the language definition:
   - which constructs CLite forbids besides pointers (unions, `goto`, variadic functions,
     function pointers, dynamic allocation, recursion?);
   - what it offers instead (value records, indexed collections, typed object IDs).

   Until then the `clite` capabilities in `weaver.yaml` stay `provisional` or `unknown`, and no
   recipe can claim CLite export.
2. **Conformance checker.** From the AST, list every construct in each function and file that
   CLite does not allow. A function is CLite-ready when it has none and no pointer. Readiness
   becomes a metric with the same progress bar as removed pointers.
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
5. **Translation.** Once a function conforms, emit it as CLite (if CLite has its own syntax) or
   certify it in place. Adapters at the C/CLite boundary stay on the ledger until removed.
6. **Precision at scale.** Field-sensitive ownership and lock-aware concurrency (see
   [`architecture.md`](architecture.md), next milestones).

## Open decisions

1. Where code is analysed: customer-run first (recommended), or hosted.
2. What $199 per month covers: one user or one organisation, and limits on projects or code size
   (essential if hosted).
3. The portal's identity provider: an existing one, or which to adopt.
4. The CLite specification: who owns it and where it lives.
5. SVF in the paid product: an optional add-on, or GCC's points-to only.
6. LLM features: included, bring-your-own key, or off.

## Next work items (need none of the decisions above)

1. `weaver doctor` and the container image.
2. Several projects per server, with recent projects remembered.
3. The risk view and risk report.
4. A conformance-checker skeleton against a provisional rule list, replaced once the CLite
   specification exists.
5. The output-parameter recipe.

Phase 1 starts once decisions 1-3 are made.
