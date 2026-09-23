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

Next:

1. The risk view and risk report.
2. The output-parameter recipe.
3. Several projects per server; `weaver doctor` and the container image.
