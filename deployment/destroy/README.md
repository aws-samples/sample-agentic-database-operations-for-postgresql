<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# deployment/destroy

**Everything in here deletes things.** Nothing else in `deployment/` does.

That separation is the point. These used to sit alongside the build and check scripts,
one of them named `check-orphans.sh`, and the only confirmation lived in the `Makefile`
target that usually called it. So invoking a file directly destroyed an installation
with no prompt:

```bash
DELETE=1 ./deployment/check-orphans.sh dbops dbops us-east-1   # no confirmation, ever
```

A guard that protects one caller is not a guard. Each script here now carries its own,
via `confirm.sh`, so it behaves the same whether it is reached through `make` or run by
hand.

| File | What it deletes |
|---|---|
| `confirm.sh` | nothing — the shared confirmation, sourced by the others |
| `purge-orphans.sh` | retained resources a failed deploy left behind: registries, metrics store, buckets, secrets, user pool, agent memory |
| `retry-stack-delete.sh` | finishes a stack delete blocked by AgentCore's leaked network interfaces |

## The confirmation

Each asks you to type `<name>@<account>` — for example `dbops@123456789012`.

The account is in there deliberately. Every account uses the same stack name, so typing
`dbops` confirms *what* but not *where*, and the mistake this guards against was a
teardown aimed at the right stack name in the wrong account. Nothing is deleted before
the prompt, and the exact list is printed first.

`make destroy` confirms once and passes `DESTROY_CONFIRMED` so a mid-teardown retry does
not stop to ask again. That value must match the specific target, so it cannot be set
once and reused against something else.

## Normal use

Reach these through `make`, which supplies the stack name, prefix and region:

```bash
make destroy         # delete the deployment (calls retry-stack-delete.sh if needed)
make purge-orphans   # remove what a failed deploy or a teardown left behind
```

To see what would be removed without removing anything, use the read-only checker
outside this directory:

```bash
./deployment/check-orphans.sh <stack> <prefix> <region>
```
