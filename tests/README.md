<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Tests

Three tiers, split by what they need:

| Command | Needs | What it covers |
|---|---|---|
| `make check` | nothing | Static checks: resource naming, template wiring, Python and shell syntax |
| `make test` | nothing | Unit tests, and tests that prove the checkers actually fail |
| `make smoke` | a deployed stack | The deployment serves traffic |

## `make check` — static

Runs `deployment/check-resource-names.py` and `deployment/check-templates.py`, then
`compileall`, `pyflakes` (skipped with a note if not installed) and `bash -n` over every
shell script. No AWS calls, no container runtime, nothing mutated.

Between them the two checkers assert what CloudFormation would otherwise only tell you
part-way through a deploy, after resources exist:

- every DynamoDB table and Lambda function name in the templates matches the suffix
  contract in `backend/tools/resource_names.py`, in both directions;
- every compute resource that reads a table also receives `RESOURCE_PREFIX`;
- no resurrected legacy resource names anywhere in the tree;
- the email-sender record the deploy seeds matches what `governance_store` writes;
- templates parse, every `!GetAtt <Stack>.Outputs.<Name>` resolves, every parameter
  passed to a nested stack exists in that child, and every child parameter without a
  default is supplied;
- no dependency cycles between nested stacks;
- every nested `TemplateURL` has a template on disk that staging can produce.

## `make test` — unit and guard tests

**`test_resource_names.py`** covers name resolution, and — the part worth having —
reconstructs the two stripped-down layouts `resource_names.py` is bundled into (the
Tier 1 Lambda zip and the Batch image) and imports it there in a subprocess with an
empty `PYTHONPATH`. An import the module grows could resolve fine in the repo and then
fail at runtime with `ModuleNotFoundError`; this catches that.

**`test_check_guards.sh`** breaks each invariant the checkers watch and asserts the
checker fails, then restores the file. A checker that cannot fail is worse than none: it
reports `PASS` and buys false confidence. Two of these guards were written, looked
correct, and were only found to be inert when a test broke the thing they watched.

It mutates tracked files on purpose, restoring them from a temp directory via an `EXIT`
trap so an interrupted run still leaves the tree clean. That is why it is not part of
`make check`.

## `make smoke` — against a deployment

`tests/e2e/run.sh` resolves the operator-app URL from the stack outputs and checks the
deployment answers. Add more cases by dropping in either:

- an executable `tests/e2e/*.sh` — receives the URL as `$1`, or
- a `tests/e2e/test_*.py` — pytest, reads `APP_URL` from the environment.

The runner discovers both.

## Known: dev-dependency advisories

`npm audit --omit=dev` is clean for both apps — nothing in a shipped bundle. Two
build-toolchain advisories remain in dev dependencies (`vite`, `esbuild`), both affecting
only the local dev server, neither reachable from the built output.

They are not fixed because the fix is `vite@8`, which replaces the bundler with rolldown
and rejects the object form of `manualChunks` that `vite.config.js` uses. That is a
three-major-version toolchain change deserving its own verification pass, not a drive-by.
