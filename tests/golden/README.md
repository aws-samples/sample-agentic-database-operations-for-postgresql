# Golden harness

A regression harness that walks the product's real flows and checks the rules in
[docs/PRODUCT-RULES.md](../../docs/PRODUCT-RULES.md) at every step.

`make golden` (part of `make test`) runs the BUILT operator app and admin console, served
with `vite preview`, driven in Chrome against a stateful fake backend. No AWS, no network
beyond localhost. All seven scenarios run in about 50 seconds, so `make test` runs all of
them.

## Running the harness

Needs Node 18+, Chrome, Playwright and `npm ci` in `frontend/` and `admin-console/`.
Playwright is not a dependency of either app. The harness looks for it in `GOLDEN_PLAYWRIGHT`
(a path to playwright's `index.mjs` or its directory), then by module name, then in the npx
cache. `npx -y playwright@1 --version` puts it in the cache.

The harness builds each app into its own directory under the OS temp dir
(`GOLDEN_BUILD_DIR` overrides it), never into `dist/`, and rebuilds only when a source file is
newer than that build (`GOLDEN_REBUILD=1` forces a build). After a deploy, `dist/config.js`
holds the deployed runtime config; the harness never reads, changes or deletes it, and fails
fast if the `config.js` it serves is not the app's `public/config.js` placeholder.

```bash
make golden                    # all scenarios
make golden SCENARIOS="S1 S4"  # some of them
make golden-update             # rewrite the snapshots, then review the diff and commit it
GOLDEN_HEADED=1 make golden    # watch it
```

Output: one line per step, the rule checks that failed (each names its rule number), a
readable diff for any snapshot that changed, and a summary. Screenshots of every step go to
`.agents/golden-out/<scenario>/` for people only. They are never compared.

## How it works

- `fake_api.mjs` answers every `/api/**` call from an in-memory model of spaces, databases,
  operators, users, secrets, audit records, network state and the access roles in member
  accounts (`putMemberRole`: whose ExternalId each trusts and its policy-version tag, which
  the account check reads). It moves state on the way the
  backend's background work does (onboard: configuring, then configured; delete: deleting,
  then gone or Delete failed; space delete; failover; route repair). It refuses what the
  backend refuses, with the backend's messages. **A route it does not implement fails the
  step**, naming the route, so the fake cannot drift silently from the API.
- `rules.mjs` checks the page after every step: no raw AWS error text (18), no success right
  after a refusal (17), an operator gets no admin console (2), a non-owner has no enabled
  manage control and sends no change (3), nobody became an operator without being added (5).
  Rule 15 (no manual refresh) is checked by `h.eventually`. The harness never reloads or
  presses a refresh control while waiting, and a timeout is reported as rule 15.
- Snapshots: at a named checkpoint, the visible text of a region (default `main` in the
  console, `#root` in the operator app), with timestamps, dates, relative times, UUIDs and
  AWS resource ids masked. They are stored in `snapshots/<scenario>/<checkpoint>.txt`.
- `known_bugs.mjs`: product bugs the harness found that are not fixed yet. The assertion that
  shows one is wrapped in `h.xfail('<id>', ...)`. It is reported as XFAIL, and as XPASS once the
  product is fixed: then delete the entry and the marker. `GOLDEN_STRICT_XFAIL=1` makes XPASS
  fail.

## Scenarios

| | Flow | Rules |
|---|---|---|
| S1 | create a space (creator not an operator), validate access, discover (Global Database only under Not supported), onboard an RDS instance and an Aurora cluster with a reader (Configuring… then Ready, no refresh), Next through the stages (operators empty until added), Topology draws each database box inside the visible map without Refresh metadata or a reload, and fits itself when its canvas is laid out late; audit log | 5, 14, 15, 16, 23 |
| S2 | old-named secret flagged in Step 2 and refused by Test; wrong host refused naming the endpoint; replica accepts its primary's secret | 8, 9, 17, 18 |
| S3 | admin B in admin A's space is read-only; super admin manages it and the audit flags it; operator gets no admin console; operator app lists only the spaces the user was added to | 2, 3, 4, 5, 19 |
| S4 | remove a database and delete a space: Deleting… then gone, a double click sends one request; failed teardown shows Delete failed and Retry | 15, 16, 17 |
| S5 | cluster members with lag; unreachable reader disabled in the picker with its reason; failover swaps roles on the open page (the table re-reads the members on its own); failover audited once | 15, 20, 21 |
| S6 | audit vault off needs the typed phrase, is recorded first, and the Security tab shows the reduced state | 24 |
| S7 | add-account wizard: the account check's four role states (ready: nothing to run; outdated: only the update command; missing: the create command; denied: the reason and the trust-policy command), the shared role name read-only until "Use a different name", a name outside the prefix refused with the fix, a ready account registered pending validation with the owner admin as External ID | 7, 17, 18 |

## Adding a scenario

1. Add `scenarios/sN_name.mjs` exporting `{ id, name, run(h) }`. Seed the fake (`standardUsers`,
   `seedSpace`, `seedMember`, `discoverFor`, `putSecret`), open people with
   `h.open('admin' | 'operator', userKey)`, and write each user step as
   `h.step(name, actor, fn, { checkpoint, space, canManage })`.
2. Find things the way a person does (role and visible name; `lib/ui.mjs` has helpers).
3. If the flow calls a route the fake lacks, the run says which one. Add it to `fake_api.mjs`
   by mirroring the handler in `backend/app.py`, including its refusals.
4. Run `make golden-update SCENARIOS=SN`, read the new snapshots, and commit them.
