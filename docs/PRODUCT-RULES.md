# Product rules

The rules this product must always keep, stated as behaviour a user can observe. They are the
checklist for reviews and the invariants the golden harness (`tests/golden/`) asserts at every
step of every scenario. A change that breaks one of them is a bug, however reasonable the code
looks on its own. Changing a rule is a product decision: change it here first, in the same commit.

## Roles and access

1. **Three roles.** A super admin runs the instance. A space admin creates and runs their own
   spaces. An operator uses the spaces they were added to, in the operator app only.
2. **Operators never use the admin console.** Only super admins and space admins sign in to it.
3. **A space is managed by its creator or a super admin, nobody else.** Being added to a space
   (whatever role you hold elsewhere) lets you use it, never change it.
4. **A super admin can view every space and manage any space as break-glass.** Every action a
   super admin takes on a space they did not create is audited as such.
5. **Creating a space never makes anyone an operator of it.** The operator app lists a space only
   for people added to it on purpose; an admin who wants it for their own space adds themselves.
6. **Ownership does not change.** The creator owns the space for its whole life (handover is a
   later feature).

## Credentials and accounts

7. **Credentials belong to the admin who owns the space.** One access role per admin per account,
   shared by all their spaces. Different admins' spaces share nothing.
8. **Secrets live in the owner's folder.** A secret is usable only if its name starts with
   `<prefix>/<owner admin id>/`. Any other name is refused before any AWS call, with a message
   that names the secret and the folder. The wizard generates the right name.
9. **A secret's `host`, if present, is exactly the database's endpoint** (for Aurora, the cluster
   endpoint). A read replica may use its primary's secret. Anything else is refused, naming the
   endpoint to use.
10. **No application role can read another admin's secrets**, in any account. IAM enforces it.

## Connecting to databases

11. **Every database connection verifies the server certificate and hostname** (`verify-full`).
    There is no weaker mode anywhere.
12. **Connections are read-only.** The agent and the collectors never write to a monitored
    database.
13. **Same-region databases connect over VPC peering, or a VPC Lattice resource endpoint** when
    the address range clashes or the deployment is set to Lattice. Cross-region databases peer.
14. **Unsupported databases are never onboarded.** They are shown separately as "Not supported"
    with the reason (for example an Aurora Global Database), never as a normal candidate.

## What users see

15. **Every page shows current data without a manual refresh.** After an action finishes (onboard,
    delete, failover, repair), the pages that show it update on their own.
16. **Long actions answer at once and show progress.** Deletes and onboarding return immediately,
    show "Configuring…" or "Deleting…", and end in a final state or a visible failure with Retry.
17. **Nothing reports success when it failed.** A failed or partial action is shown as failed,
    with the reason.
18. **Errors say what to do.** A refusal names the cause and the fix (which endpoint, which
    folder, which policy), never a raw AWS error alone.
19. **People see only their own tenant's data.** A space admin never sees another admin's
    databases, secrets, network details or names; operators see no network internals beyond the
    reason a member is skipped. In what the agent streams, the line is the deployment's own
    infrastructure against the space's databases: it masks the deployment's identifiers (secret,
    IAM role and STS ARNs, the account id in any other ARN, the metrics store's table names and
    addresses inside the deployment's VPC) and keeps the space's databases' own diagnostic data
    (their endpoints, their own addresses and their clients' addresses).

## Monitoring

20. **A cluster is one database with members.** The writer and every reader are collected; the
    members view shows each one's role, health, lag and last collection.
21. **A failover is detected within one Tier 1 interval** and reflected in roles, the audit log
    and a notification, exactly once.
22. **Metrics and findings are attributed to the member they came from.**

## Audit

23. **Every admin-console action is audited** with who, what, where and the before/after values.
24. **With the vault on, audit records cannot be changed or deleted** by anyone until their
    retention ends. Turning the vault off needs a typed confirmation, is itself recorded first,
    and notifies.

## Deployment

25. **No prices in code or templates.** Indicative costs live in one dated README section.
26. **Pre-release changes may break old spaces; there is no migration code for spaces.** The
    stack's own stores upgrade in place, except where a release says otherwise: the move of
    the metrics store to DynamoDB and CloudWatch starts the collected history afresh.
## Metrics store
27. **Every metrics read is built by the server for a database the caller may see.** Routes and
    agent tools gate on the database first, then read its own records or series with a key or
    metric query the server builds. No browser, caller or model supplies a key, expression or
    metric query, and agent results name databases by alias.
28. **A removed database's series cannot be deleted.** CloudWatch keeps them for up to 15 months
    and then drops them; they carry only opaque ids (the registry id, plus the member's instance
    identifier on per-member series; the collection-status series carries the registry id
    alone), never a name, endpoint or account. Nothing in the product shows them once the
    database is gone, and its alarms are deleted with its schedules.

## Alarms

29. **Alarms only notify, and the server builds every one.** Each alarm is made from a registry
    record and the fixed catalogue, carries no action, and emails only the deployment's
    notification address; no caller supplies a metric, dimension, threshold, recipient or
    action. Alarms are on by default, and a super admin can switch all of them, or each one,
    off in Settings › Alarms.
