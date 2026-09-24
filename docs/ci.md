# Weaver in CI: the pointer ratchet

A team that is removing pointers wants new code to stop adding them. `weaver ratchet` compares
the current analysis with a committed baseline and fails when, in any file it compares, one of
these went up:

- the number of pointers;
- the number of high-risk pointers;
- the number of sites that violate a rule of the chosen simplification profile.

It names each new pointer. Numbers that went down are reported as progress.

## Set up once

```sh
weaver refresh --capture --no-flow      # the same analysis CI will run (see "Same evidence" below)
weaver ratchet --update                 # writes weaver-ratchet.json next to weaver.yaml
git add weaver-ratchet.json && git commit -m "Weaver ratchet baseline"
```

Optional settings in `weaver.yaml`:

```yaml
ratchet:
  baseline: weaver-ratchet.json   # where the baseline lives (relative to weaver.yaml)
  profile: pointer-free           # which profile's violations to count (default: simplify.profile)
  enforce: [pointers, high, violations]   # drop a dimension to stop enforcing it
```

## In a merge request

```sh
weaver refresh --capture --no-flow
weaver ratchet --base origin/main       # compare only the files this branch changed
```

The exit status is 1 when something went up. The output names the file, the count before and
after, and each new pointer with its function, line and risk level. Any of these fixes the build:

- **Remove the new pointer.** This is usually the answer.
- **Accept the increase deliberately.** Run `weaver ratchet --update` and commit the baseline, so the
  increase is reviewed as a change to `weaver-ratchet.json`.

When numbers went down, run `weaver ratchet --update` and commit the file to lock the progress in.
`weaver ratchet --strict` fails until that is done, which keeps the committed baseline tight.

Counts, not pointer IDs, decide. Renaming a pointer or moving a function changes IDs but not the
work left, so it does not fail the ratchet.

## GitHub Actions

`--format github` turns each new pointer into an error annotation on the pull request's diff.

```yaml
name: pointer ratchet
on: pull_request
jobs:
  ratchet:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0              # --base needs the target branch's history
      - run: sudo apt-get update && sudo apt-get install -y clang gcc make
      - run: pip install ./weaver     # your licensed Weaver build
      - run: weaver refresh --capture --no-flow
      - run: weaver ratchet --base "origin/${{ github.base_ref }}" --format github
```

## GitLab CI

```yaml
pointer-ratchet:
  image: ubuntu:24.04
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    GIT_DEPTH: 0
  script:
    - apt-get update && apt-get install -y clang gcc make python3-pip git
    - pip install --break-system-packages ./weaver
    - weaver refresh --capture --no-flow
    - git fetch origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - weaver ratchet --base "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
```

## Same evidence, same numbers

A pointer's risk level depends on the evidence Weaver has. For example, points-to results can
show that a pointer may reach unknown memory. Build the baseline and run CI with the same
`weaver refresh`: both with `--no-flow` (faster), or both with points-to analysis. The baseline
records whether points-to evidence was present. When it differs, the ratchet skips the high-risk
comparison and prints a warning instead of failing falsely.

The same applies to a new Weaver version. If counts move in files nobody changed, refresh the
baseline with `weaver ratchet --update`.

## What else to run in CI

- `weaver check --since <snapshot>` fails on high-severity changes in pointer behaviour since a
  saved snapshot: a read-only pointer that is now written, a new escape, or a pinned or implied
  contract that no longer holds.
- `weaver report` writes the inventory and rejection report, useful as a build artifact.
