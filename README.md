# DBOps Agent

An autonomous, multi-agent DBA for your PostgreSQL fleet. Lean supervisor agents coordinate engine-specific specialists, a PostgreSQL agent covering nine analysis areas, an AWS observability agent, plus research and learning agents, to watch the health of your databases continuously, investigate what is wrong when something degrades, and explain the root cause in plain language, citing every query it ran. It runs read-only against your databases, entirely inside your own AWS account.

> [!IMPORTANT]
> **Sample code, for non-production use.** This repository is provided for learning
> and evaluation. Do not deploy it to a production environment without additional
> security testing of your own. Work with your security and legal teams to meet your
> organization's security, regulatory and compliance requirements before deployment.
>
> It connects to your databases and reads their catalogs and statistics, so treat a
> deployment as having the same sensitivity as the databases it is pointed at. See
> the [security model](docs/ARCHITECTURE.md#security-model) for what the deployment
> does and does not protect, including the findings accepted by design.

## Why this exists

Teams move to open-source PostgreSQL to escape licensing cost. What they do not always plan for is the operational burden that comes with it. They have adopted an engine that still needs deep expertise to run well, and that expertise is scarce and expensive.

A managed database service owns the control plane, and it does that job well: provisioning, backups, patching, failover, high availability. That takes a large operational load off your team, and it is exactly what the service is designed to do.

The data plane is a different job, and by design it stays with you. It is the health of the data and the engine itself: table bloat and dead tuples, autovacuum keeping up with write volume, transaction ID wraparound, lock contention, connection saturation, and query plans that drift as data grows. This work is workload-specific, and only someone with context on your databases can do it well. Traditionally that someone is a DBA, and it is exactly the role most teams no longer staff after moving to a managed, open-source engine.

So the gap is not in the platform, it is in the operating model. Your infrastructure is fully managed, and at the same time no one owns the day-to-day operational health of the data. When something degrades at 2am, the on-call engineer is left reading unfamiliar `pg_stat` views under pressure, reconstructing what a specialist would already know to check. Diagnosis is deep, high-risk, and time-sensitive, and getting it wrong on a production database is costly.

The DBOps Agent closes that gap. It brings the diagnostic reasoning of an experienced PostgreSQL DBA to fleets that do not have one, autonomously and continuously. It starts from a foundation of vetted, deterministic checks built from real-world operational experience, then wraps them in a multi-agent system that can investigate, explain its reasoning, cite what it ran, and verify recommendations against official documentation before surfacing them. It does all of this safely: read-only against your databases, running inside your own AWS account.

The goal is not to replace a DBA. It is to give every team that runs PostgreSQL without one a way to catch problems early, understand them in plain language, and act with confidence.

## What It Does

The agent is always working in the background. A lightweight collector records PostgreSQL-internal metrics on a short cadence, and a deeper batch scan runs daily, together building a continuous picture of your fleet's health and how it is trending. That history is what makes everything else possible: when a metric crosses a threshold or something degrades, there is already a baseline to compare against.

On top of that foundation sits a multi-agent system you can talk to. Ask about a database in plain language, for example *"why did write latency double overnight?"* or *"is anything close to transaction ID wraparound?"*, and a supervisor agent routes the question to the right specialists. A PostgreSQL agent covers engine internals across health, queries, locks, bloat, indexes, and replication; an observability agent reads CloudWatch, Performance Insights, and CloudTrail; and a research agent checks official documentation. The answer comes back from evidence the agents gathered, and it shows its work: the queries it ran, the metrics it read, and the docs it checked to verify the fix.

For a harder problem, an Investigation Agent runs a structured root-cause analysis. It loads the runbook for your engine, streams its reasoning live as specialists work, and ends with severity-ranked findings, a root cause, and recommended actions you can save or download. Every query it issues goes through a single read-only safety gate, so an investigation can never write to your database.

There are two web surfaces:

- **Admin Console** — create Agent Spaces, connect AWS accounts, onboard databases, manage
  users, models and knowledge.
- **Operator App** — chat, investigations, dashboard (with each database's metrics, deep
  scans and reports), trends and topology.

| Capability | What it gives you |
|---|---|
| **Chat** | Ask about a database in plain language; the agent checks metrics, queries, config changes and maintenance state before answering |
| **Investigations** | Structured root-cause analysis driven by engine-specific runbooks, ending in findings, a root cause and verified recommendations |
| **Scheduled scans** | Lightweight metrics every 5 minutes, a deep health scan daily, both adjustable per database |
| **Trends** | Fleet-wide health, findings and metric comparison over 7 / 30 / 90 days |
| **Topology** | Account → region → VPC → database, plus the agent and tool hierarchy, as a drill-down map |
| **Reports** | Every deep scan's report with AI analysis, in the database's Deep scans tab on the Dashboard; pick two to compare them side by side |
| **Agent Spaces** | The access boundary: which accounts, regions and databases the agent may touch |

How the safety claims above are actually enforced:

- **Read-only** — a dedicated `dbops_ro` user holding only `pg_monitor` and `SELECT`,
  onboarded from a Secret ARN you supply explicitly, never guessed from a name. Write statements
  are rejected before they reach the database.
- **Inside your account** — data, logs and credentials never leave your boundary, and database
  traffic stays on the AWS backbone over VPC peering or a VPC Lattice resource endpoint rather than
  the internet.
- **Scoped access** — Super Admin, Space Admin and Operator roles, so space owners run their own
  onboarding and staffing without account-wide permissions.

For how any of it works, including the agent graph, the runbook and skill loading, the metrics
store and the capability cascade, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Architecture

Two React apps talk to one FastAPI backend on ECS Fargate. The backend holds the agents and
the REST API, and it is the only thing that reaches your databases.

![Overview: administrators and operators use two web apps behind CloudFront and Cognito; a REST API on ECS Fargate and Strands agents on Bedrock AgentCore read and write DynamoDB, S3 and CloudWatch metrics; the application reaches your databases read-only over VPC peering or a VPC Lattice resource endpoint, with an assumed role for CloudWatch, Performance Insights and CloudTrail](docs/diagrams/overview.png)

- **Admin Console** for setup: spaces, connected accounts, database onboarding, users, and
  the models and knowledge each layer may use.
- **Operator App** for daily work: chat, investigations, dashboard, trends, topology.
- **Backend** runs the supervisor and domain agents, streams their output through Bedrock
  AgentCore, and triggers the batch scans.
- **Storage** is DynamoDB for the registries and the collected records, CloudWatch custom
  metrics for the long-term series, and S3 for reports and knowledge documents. Cognito handles sign-in, with TOTP required.
- **Connectivity** to your databases goes over VPC peering, falling back to a VPC Lattice resource
  endpoint when
  CIDRs overlap, so traffic stays on the AWS backbone.

The per-audience architecture diagrams (operator app and admin console, every hop named), the
component-by-component version, the orchestration flow and every table are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). All diagrams live in `docs/diagrams/` as SVG and
PNG, built from the AWS Architecture Icons by `docs/diagrams/build_diagrams.py`.

## Supported databases

PostgreSQL only: Amazon RDS for PostgreSQL and Aurora PostgreSQL (provisioned and
Serverless v2), in any account and Region you add to a space.

| Works | With limits | Not supported |
|---|---|---|
| RDS Single-AZ and Multi-AZ instances | Over a VPC Lattice resource endpoint only the cluster endpoint is reachable, so readers are not collected; they show as skipped in the members table | Aurora Global Database: its member clusters are not offered for onboarding; discovery lists them in a separate, collapsed "Not supported" section. When discovery cannot check a Region for Global Database membership, the Aurora clusters it finds there are listed there too until a later discovery can |
| RDS Multi-AZ DB clusters, labelled as RDS PostgreSQL; chat can focus on their readers | Cross-Region address clashes: every member's address is routed as a host route. Tier 1 detects a moved or departed member and the API task repairs the route, so after an Aurora failover, Multi-AZ failover or blue/green switchover the path is repaired within about 5 to 10 minutes; with Tier 1 paused the drift is not detected | Aurora custom endpoints, RDS Proxy |
| Aurora clusters: chat can target the cluster, the writer, the reader endpoint or one instance | Aurora Serverless v2 with a 0 ACU minimum will not auto-pause while monitored: Tier 1 connects every interval | IAM-authentication-only logins (`dbops_ro` uses a password) |
| RDS read replicas: each is its own database, listed under its primary, and may use the primary's secret | | RDS-managed master secrets (`rds!…`), and secrets under a customer KMS key whose key policy does not name the space owner's role |
| Databases in other accounts and Regions | Databases on a non-default port: chat and the 5-minute metrics work; the daily deep scan reaches only the ports in `DB_PORT_RANGE` (default `5432-5432`) | A database VPC whose CIDR overlaps the deployment VPC |
| Same-Region address clashes, over a VPC Lattice resource endpoint | | Publicly accessible databases on the Lattice path |
| Replica lag: `AuroraReplicaLag` for Aurora readers, `ReplicaLag` for RDS read replicas and Multi-AZ DB cluster readers | | |
| Scheduled collection covers every member: Tier 1 on the writer and each reader; the daily scan runs a deep scan on the writer and a reader scan on each reader. The dashboard shows the members, and charts, records, the agent's Trends and Findings tools and Performance Insights follow the chosen member | | |
| Failover is detected on the next Tier 1 run (not while Tier 1 is paused): roles update, the event is audited and a notice goes to the alert topic | | |

The secret's `host`, if present, must be the database's own endpoint exactly as RDS
reports it (for Aurora, the cluster endpoint `…cluster-…`, not the reader or an instance
endpoint), or you can leave `host` out. A secret naming another host is refused, with the
endpoint to use. The one exception is an RDS read replica, which shares its primary's
read-only user: the primary's secret, whose `host` is the primary's endpoint, is accepted
for the replica, and the agent still connects to the replica.

## Quick Start

### Supported regions

Deploy to **us-east-1** or **eu-west-1** for now. Amazon Bedrock AgentCore Runtime
provides VPC connectivity only in a specific subset of Availability Zones per Region, and
there is no API to discover that subset — so the deployment pins the supported AZs for
these two Regions from the [AgentCore VPC docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html).
A deploy to any other Region fails fast with a clear message rather than rolling back. To
add a Region, extend `SUPPORTED_AZ_IDS` in `infrastructure/vpc.yaml` with that Region's
supported AZ IDs from that doc (and update this note).

### Prerequisites

On your machine:

| Need | Notes |
|---|---|
| AWS CLI v2 | with credentials for the target account |
| A container runtime | see below; builds the backend and batch images |
| Python 3 | used by the build and check scripts |
| Node.js | builds the two web apps |
| `zip` / `unzip` | packages the Lambda archives |

**Container runtime.** Any of Docker, Finch, Podman or nerdctl. On macOS all of them run a
small Linux VM that has to be created once and started after each reboot, which is the most
common reason a first deploy fails. `make preflight` distinguishes "not installed" from "not
started" and prints the exact command for whichever you have.

```bash
# macOS — Finch is what this project is tested with
brew install --cask finch && finch vm init

# macOS — Colima, if you prefer a drop-in docker CLI
brew install colima docker && colima start --arch aarch64

# Linux — no VM needed
#   Docker Engine: https://docs.docker.com/engine/install/
#   Podman:        sudo apt install podman   /   sudo dnf install podman
```

The images are built for `linux/arm64`. On Apple silicon or arm64 Linux that is native; on an
x86 host it works under emulation but the image build is noticeably slower, and `preflight`
says so rather than leaving you to wonder.

In the AWS account:

- **Bedrock model access** for Anthropic Claude Haiku 5.5, enabled in the region you deploy
  to. Request it under *Bedrock → Model access*. Without it the agent deploys but cannot answer.
- Permission to create IAM roles, VPCs, CloudFront distributions, DynamoDB tables, Cognito and
  AgentCore resources.
- **`us-east-1` is the recommended region.** The optional WAF on CloudFront is only valid
  there, and AgentCore is not available in every region.

Check everything at once:

```bash
make preflight
```

`preflight` reports both halves of readiness: the tools above, and the deployment
settings it is about to use:

```
🔍 Checking deployment settings...
   config file.......... ✅ deploy.dev.config
   resource prefix...... ✅ dbops
   region............... ✅ us-east-1
   admin email.......... ✅ you@example.com
                            ↳ the temporary password goes here — check it is you
   target account....... ✅ 123456789012 (profile myprofile)
```

Read the admin email and account lines before deploying. See
[Deployment settings](#deployment-settings) for why.

If you use Finch and it reports the runtime is installed but not running, start its VM first
This takes a minute or so and is only needed once per machine:

```bash
finch vm init      # first time
finch vm start     # subsequent boots
```

### Deploy

```bash
make deploy
```

The first run asks which environment this is (a name you choose, such as `dev` or `uat`;
see [Environments](#environments)), then for the following. It saves them to
`deploy.<name>.config` (gitignored, so it never lands in version control), then deploys.
Press Enter to accept a default.

| Prompt | Default | Notes |
|---|---|---|
| Resource prefix | `dbops` | prefixes every resource name |
| AWS region | `us-east-1` | |
| CloudFormation stack name | same as the prefix | |
| Artifacts S3 bucket | `<prefix>-artifacts-<account>-<region>` | created if absent; holds templates and Lambda archives |
| **Initial admin email** | *required* | Cognito emails a temporary password here |
| Initial admin display name | `Administrator` | |
| Alert notification email | optional | for critical findings and [email alarms](#email-alarms) |
| Invitation sender email | optional | must be a verified SES identity |

Expect **20–35 minutes**. Most of it is CloudFront and the agent runtime. The deploy
runs four steps in order, and you can repeat any one of them on its own. See
[Deployment Commands](#deployment-commands).

### Environments

One checkout can deploy any number of environments, for example `dev`, `uat` and `prod` in
three accounts, and update each later. An environment is a name you choose. Its settings are
saved in `deploy.<name>.config` and its build state in `deployment/.state/<name>/`, so two
terminals can deploy two environments at the same time.

```bash
make deploy                  # asks for the environment name first, listing the saved ones
make deploy ENV=uat          # names it for this run
export DEPLOY_ENV=uat        # or pins this terminal to it, like AWS_PROFILE
make envs                    # every saved environment: account, region, stack, VPC range
```

Three guards keep environments apart:

- **Account.** Each environment records the account it was created in. A deploy, stage or
  destroy refuses when the shell's credentials resolve to another account, so `ENV=prod`
  run from a terminal holding dev credentials stops before anything is touched. The shell
  still chooses the account (`AWS_PROFILE`); the environment only checks it.
- **Network range.** A new environment proposes a VPC range none of your other saved
  environments uses (`172.20.0.0/16`, then `172.21.0.0/16`, ...). Environments that reach
  the same database need distinct ranges: a database VPC can route one agent range to only
  one peering.
- **Stack.** Two environments with the same account, region and stack name or resource
  prefix would update one stack. The deploy refuses the second.

A checkout that only ever deploys one environment can keep a plain `deploy.config`; it is
used whenever no environment is named.

### Deployment settings

Your answers are saved to **`deploy.<name>.config`** in the repository root, one file per
environment.

That file is **not** committed. It holds an email address and an account-specific bucket
name, values belonging to whoever is deploying rather than to the project. Committing it
would ship one person's address to every clone and mail them the temporary password for every
new installation. **`deploy.config.example`** is committed in its place and documents every
setting, so you can see what exists without inheriting anyone's values. Pre-seed it if you
prefer not to answer prompts:

```bash
cp deploy.config.example deploy.dev.config    # then edit; deploy with ENV=dev
```

You do not have to. `make preflight` and `make deploy` both ask for anything missing and
write the file for you. Neither uses a default for the admin email: Cognito mails the first
temporary password there, so a wrong value produces a deployment that succeeds and that
nobody can sign in to.

**Which account you deploy into is never chosen from a file.** There is no profile setting
in the config. The target comes from your shell, or from the default credential chain, the
same way the `aws` CLI resolves it. So a second terminal pointed at a different account just
works, with no file to edit and nothing to go stale:

```bash
export AWS_PROFILE=my-other-account
make preflight            # prints: target account ✅ 111122223333 (profile my-other-account)
```

`preflight` and every destructive command print the account they resolved, so you can confirm
before committing 30 minutes to a deploy, or before a `destroy`. For a one-off without
touching the shell: `make deploy AWS_PROFILE=<name>`.

Prompting happens when a setting is **missing or unusable**, not merely when the file is
absent, so an inherited config with an empty or malformed email is caught rather than used.
A prefix the templates would reject is caught here too, before anything is built.

To change settings later:

```bash
make config          # re-prompts with the current values as defaults; Enter keeps them
make check-settings  # print the effective settings without changing anything
```

### Deploying without prompts (CI, scripts, AI assistants)

Set `NONINTERACTIVE=1` whenever nothing can answer a question:

```bash
export NONINTERACTIVE=1
cp deploy.config.example deploy.ci.config     # then set INITIAL_ADMIN_EMAIL
make deploy ENV=ci
```

Or supply the values on the command line and skip the file:

```bash
NONINTERACTIVE=1 make deploy ENV=ci INITIAL_ADMIN_EMAIL=you@example.com
```

`CI=1` is honoured identically, so most CI systems need nothing set. With the flag on, a
missing setting is reported and the run exits non-zero, naming each one and printing a
command that supplies it — rather than waiting at a prompt.

Set it even when a terminal appears to be attached. The check that decides whether to
prompt can only see whether a *terminal* exists, not whether a *person* does, and AI
assistants and some CI runners allocate one. Without the flag, such a run stops at the
first prompt and stays there until its own timeout kills it, with the deploy partly
done. `NONINTERACTIVE=1` is what makes the difference visible.

The other setting worth knowing for an unattended run is the prefix. Every resource name
derives from `RESOURCE_PREFIX`, and two installations can share an account as long as
their prefixes differ:

```bash
NONINTERACTIVE=1 make deploy ENV=acme RESOURCE_PREFIX=acme STACK_NAME=acme \
                             INITIAL_ADMIN_EMAIL=you@example.com
```

### Security, guardrails and capacity

`master.yaml` exposes twenty more settings that most deployments never need to touch, so
`make config` asks about them as one optional group rather than twenty questions. They are
written to the environment's config file either way, so they stay visible and editable, and
`make preflight` prints the values a deploy would actually use.

Defaults are the recommended posture rather than the cheapest one. The two that cost money
are on, and you turn them off deliberately. Prompts show the accepted values, and answering `yes`/`no`/`on`/`off` is
mapped rather than rejected.

| Setting | Default | Effect |
|---|---|---|
| `ENABLE_WAF` | `true` | AWS WAF on both CloudFront distributions, which are the only public entry points. **Adds a per-WebACL and per-request charge**: one WebACL per distribution, four rules each (see [Indicative costs](#indicative-costs)). Set `false` if your own org-level WAF already fronts this. Works in any Region: a CloudFront WebACL can only be *created* through the us-east-1 control plane, so for a deployment outside us-east-1 `make deploy` first creates the WebACLs in a small us-east-1 edge stack (`<stack>-edge-<region>`) and attaches them — enforcement still happens at the CloudFront edge nearest each viewer, with no added latency. `make destroy` removes that edge stack too. |
| `ENABLE_CLOUDTRAIL` | `true` | A trail scoped to this deployment, so an audit record exists by default. Set `false` if an organisation trail already covers the account. AWS delivers the first copy of management events per region free, so a second trail is **charged per event** for events you already have. Its bucket is retained on teardown. |
| `ENABLE_COGNITO_THREAT_PROTECTION` | `false` | Cognito advanced security — compromised-credential detection and adaptive authentication. Off by default because it requires the **Cognito Plus tier, billed per monthly active user**. Mandatory MFA and the per-IP sign-in rate limit apply regardless. |
| `JWT_SIGNATURE_VERIFICATION` | `enforce` | In-app JWT signature verification. On top of the API Gateway / AgentCore edge authorizers, the backend re-verifies every bearer against the Cognito JWKS (signature, issuer, expiry, app-client binding). `enforce` rejects a genuine failure; `log` verifies and logs only; `off` disables it. Enforce is safe because the edge already verifies every sanctioned request, so this only rejects a token that reached the backend without it, and it **fails open on a JWKS/dependency outage** so a key-endpoint blip cannot become an auth outage. Leave at `enforce` unless debugging. |
| `DB_PORT_RANGE` | `5432-5432` | PostgreSQL ports (`from-to`) the daily deep scan may reach. The scan task's security group allows egress to these ports only (5432 is always allowed), so a database on another port is never deep-scanned until you widen it, e.g. `5432-5440`. Configure writes a warning to the database's configuration log when its port is outside the range. The agent and the Tier 1 collector are not limited by it. |
| `SAME_REGION_CONNECTIVITY` | `peering-first` | How the agent reaches a database in its own region. `peering-first` peers the database VPC and uses a VPC Lattice resource endpoint only when that VPC's address range is already routed to another peered VPC (two accounts both on `10.0.0.0/16`). `lattice` uses a resource endpoint for every same-region database, so no route is ever added to a database VPC; it adds a small hourly charge per database (see [Indicative costs](#indicative-costs)). Databases in other regions always peer. |
| `AUDIT_VAULT` | `on` | The admin audit vault. While it is on, every privileged admin-console action is written once to an S3 bucket with Object Lock (COMPLIANCE), so nobody, not even the account root user, can change or delete the record before its retention ends; the `<prefix>-audit` table is its search index. A super admin can switch it in the admin console (Settings → Audit; turning it off needs a typed confirmation and notifies the alert topic), and a redeploy reads the console value and keeps it. **Trials and test stacks: set `off`**, so the bucket stays empty and `make destroy` deletes it. The bucket adds a small S3 storage charge; see [Indicative costs](#indicative-costs). |
| `AUDIT_RETENTION_DAYS` | `365` | The floor, in days (1-3653), each admin audit record stays locked. A super admin can raise retention for new records in the console, never below this; IAM refuses anything shorter. Changing it affects new records only: an existing record's lock is never shortened. 365 matches a common 12-month audit evidence window; some organisations keep 2555 (7 years). Not read back from the console, because it is the floor. |
| `ENABLE_GUARDRAIL` | `true` | Bedrock guardrail on every model call. `false` runs without one rather than failing. |
| `GUARDRAIL_CONTENT_STRENGTH` | `LOW` | Content filters. `NONE`/`LOW`/`MEDIUM`/`HIGH`. |
| `GUARDRAIL_PROMPT_ATTACK_STRENGTH` | `MEDIUM` | Jailbreak and injection detection on input. |
| `GUARDRAIL_PII_ACTION` | `ANONYMIZE` | PII in model output: redact, or `BLOCK`. |
| `GUARDRAIL_THROTTLE_ALARM_THRESHOLD` | `5` | **Alert-only.** Notifies when `ApplyGuardrail` throttles more than this many times in 5 minutes (approaching the account quota); never blocks a call. A super admin can retune it later in the admin console (Settings → Security), and a redeploy reads the live alarm and keeps that value. |
| `GUARDRAIL_INTERVENTION_ALARM_THRESHOLD` | `25` | **Alert-only.** Notifies when the input guardrail intervenes (blocks a turn) more than this many times in 5 minutes (a possible abuse spike); never blocks a call. Console-retunable like the throttle threshold; reconciled on redeploy. |
| `ENABLE_WEB_SEARCH` | `true` | Managed AgentCore web search for the research agent; queries stay inside AWS. Available in us-east-1, eu-west-1 and ap-northeast-1; in any other Region the stack skips it and `make config` defaults it to `false`. |
| `AGENT_MODEL_ID` | Claude Haiku 5.5 | Must be enabled for your account in this region, or the agent deploys but cannot answer. `make config` proposes the Region's geographic profile where Bedrock offers one (`eu.*` in Europe, `us.*` in the US), else the global profile. A fresh install starts with this model enabled and as the default in the admin console. |
| `TIER1_RETENTION_DAYS` | `30` | How long the raw Tier 1 samples (5-minute rows with their table-bloat and wait-event detail) are kept before DynamoDB's TTL removes them. A database's own retention setting overrides it. The numeric series outlives them in CloudWatch (5-minute points for 63 days, hourly for 15 months), and deep-scan records have no expiry. |
| `LOG_RETENTION_DAYS` | `90` | Flow logs, API access logs, log-archive bucket. |
| `REFRESH_TOKEN_VALIDITY_DAYS` | `7` | How long a refresh token stays valid before re-login, in days (1-3650). Shorter bounds how long a stolen refresh token can be used; logout and a privilege change (added to or removed from `admins`/`space-admins`) revoke it immediately regardless. Access and id tokens are fixed at 60 minutes; a role change, removal from a space, offboarding or deletion makes the API refuse the user's earlier tokens within 30 seconds. |
| `BUDGET_LIMIT_AMOUNT` | `100` | Monthly cost budget in USD. **Alert-only** — emails `NOTIFICATION_EMAIL` (when set) at 80% and 100%; it never blocks anything. A super admin can change it later in the admin console (Settings → Security), and a redeploy reads the live budget and keeps that value rather than resetting it. |

Blanking any of them is safe. The Makefile then omits the parameter and CloudFormation
applies the template default.

The REST API also carries a gateway rate throttle — a requests/second ceiling with burst
headroom (template parameters `ApiThrottleRateLimit` / `ApiThrottleBurstLimit`, default
`100`/`200`), set well above normal use so it only sheds a flood or a runaway loop. The chat
and investigation calls stream to the AI runtime directly (not through API Gateway), and are
bounded by authentication, a per-caller rate limit on the model-invoking paths
(`RATE_LIMIT_PER_MINUTE`), and AWS service quotas instead; see the
[security model](docs/ARCHITECTURE.md#security-model) for that residual and how to harden it.

Or edit `deploy.<name>.config` directly, since it is plain `KEY = value` make syntax. To start over,
delete it. Any setting can also be overridden per-run without touching the file:

```bash
make deploy RESOURCE_PREFIX=acme STACK_NAME=acme
```

Every resource name derives from the prefix, so a second deployment under a different prefix
runs fully independently in the same account:

```bash
make deploy RESOURCE_PREFIX=acme STACK_NAME=acme
```

When it finishes it prints the two URLs. `make outputs` reprints them at any time.

### First sign-in

1. Open the **Admin Console** URL.
2. Sign in with the admin email you supplied and the temporary password Cognito emailed.
   Check spam if it has not arrived.
3. You are asked to set a new password, then to **set up multi-factor authentication** with
   an authenticator app (TOTP). This is required. Scan the QR code and enter the six-digit
   code. Keep the authenticator, as it is needed on every later sign-in.

Add more users with `make cognito-user`, which asks for an email and which group to put them
in (`admins`, `space-admins` or `operators`).

### Onboard your first database

Everything below happens in the **Admin Console**. Budget **30–45 minutes** the first time.
The two parts that depend on your own environment are connecting the account your databases
live in, and creating the read-only database user.

You need an AWS account where PostgreSQL databases run. It can be the account you just
deployed into or a different one, with permission to create IAM roles and Secrets Manager
secrets in it.

#### 1. Create an Agent Space

An Agent Space is the boundary that decides which accounts, regions and databases the agent
can see. Nothing outside a space is reachable.

1. On the **Agent Spaces** screen click **+ Create Agent Space**. A new installation starts
   with none.
2. Give it a name (for example `my-test-space`) and an optional description.
3. Leave **AWS resource access** as it is. A dedicated least-privilege IAM role is created
   for the space in the account you deployed into. Expand *View the permissions this role
   will have* to read it first.
4. Tags are optional.
5. Click **Create**.

You land inside the space, on the first of its five stages: **Accounts & regions → Databases →
Agent capabilities → Operators → Topology**. The stepper across the top is the space's
navigation; each stage unlocks when the one before it is done, and every stage ends with a
**Next** button. The banner reads **Setup required** until access is configured.

#### 2. Connect the account your databases run in

The space is already connected to the account you deployed into, as its **Primary** source.
If your databases live elsewhere, add that account as a **Secondary** source: on the
**Accounts & regions** stage, under *Cloud sources → Secondary sources*, click **Add**.

Enter the 12-digit **Account ID** of the account that holds the databases. You have one access
role per account, `<prefix>-agent-<owner key>`, shared by all your spaces, and the wizard
checks that account for it as soon as the id is typed:

- **Already there**: the role is ready (the wizard names your other spaces that use it).
  Nothing to run; click **Next**.
- **Older permissions**: the role is there, but its permission policy is older than this
  version. Run the one update command shown (it re-applies the policy and tags the role
  `DBOpsPolicyVersion=<n>`).
- **Not found**: copy the command and run it with credentials for that account (for example
  in CloudShell). It creates the role with the trust policy (the deployment's roles, with your
  admin id as `sts:ExternalId`), attaches the read-only permission policy and records its
  version. The same steps by hand in the IAM console are folded underneath.
- **Refused**: the reason and the fix, for example re-applying the trust policy when a role
  your other space validated no longer accepts the deployment.

The role name is shown read-only; **Use a different name** makes it editable for strict
naming policies (keep the `<prefix>-agent-` start). Click **Next**, then **Add account**. The
account appears under Secondary sources as **Pending validation**.

> The policy is read-oriented for diagnostics across RDS, CloudWatch, Performance Insights,
> CloudTrail and logs, plus the networking permissions needed to reach your databases
> (VPC peering / VPC Lattice) and Resource Explorer for discovery. No write permission on
> your data.

#### 3. Select regions and validate access

Still on **Accounts & regions**, under *Regions & access*:

1. Click the regions your databases run in. Selected regions highlight.
2. **Validate access** (enabled once a region is selected) — this enables AWS Resource
   Explorer in those regions and checks that the agent can assume the role in every
   connected account.

On success each account flips to **Valid** and you see *All accounts validated. Resource
Explorer ready.* On failure the error names the cause, nearly always a role name or trust
policy that does not match. Fix it in IAM, then click **Validate access again**.

#### 4. Discover databases

Click **Next** to reach the **Databases** stage and click **Run discovery**. The agent scans the selected regions
in every connected account for PostgreSQL databases. What it finds is listed inside this step
as *proposed* databases: discovery only proposes, it never adds a database to the space.
**Dismiss** hides one you do not want here (it reappears on the next run). Clicking a name
lets you rename it for display only; nothing in AWS is renamed.

Nothing found? Check the database is PostgreSQL, sits in a region you selected, and that
*Validate access* succeeded. Then run discovery again.

#### 5. Onboard each database

Click **Onboard** on a proposed database to open a two-step wizard. This is what connects
the agent, using a dedicated read-only user, and what actually puts the database into this
space. Run it once per database and space.

**Step 1 — create the read-only user and store its password**

1. Copy the SQL from the wizard and run it as the database's master user, in `psql` or any
   SQL client. It creates a `dbops_ro` role with `pg_monitor` and read-only `SELECT` access
   and returns its password as the last result row.
2. Copy the AWS CLI block from the wizard and paste it into CloudShell (the wizard links to
   it) signed in to **the same account as the database**, or any shell with credentials for
   it. It asks for the Step A password, stores the credentials in Secrets Manager and prints
   a **secret ARN**.

**Step 2 — connect**

1. Paste the **Secret ARN**.
2. Set the **Database name** (defaults to `postgres`).
3. Optionally click **Test secret** for a read-only connection pre-flight.
4. Click **Onboard**.

The database moves to **Configuring** while network connectivity is set up (VPC peering or a
VPC Lattice resource endpoint) and the connection is verified, then becomes **Ready** and appears in the
space's database table. Expect a couple of minutes; the status refreshes itself.

The agent is only ever given this read-only user. The full access model is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security-model).

> A database is tracked once, by its ARN, and credentials belong to you, the admin, not
> to a space. All the spaces you create share one access role per account and one secret
> namespace, so if another of **your** spaces already onboarded this database, **Add to
> this space** opens the wizard with that space's secret already filled in: click Test,
> then Add. Two clicks, no new user, no new secret (replace the ARN if you want a
> different read-only user for this space). If the database was onboarded by **another
> admin's** space, their secret is not offered and could not be read by your role if
> pasted; you onboard with your own read-only user and secret, created with the wizard's
> command. Either way the agent in each space runs as the credential that space holds,
> while monitoring keeps one active credential, one schedule and one scan history,
> visible to every space that includes the database. Removing the database from one space
> never removes it from the others — see
> [DB-level tenancy](docs/ARCHITECTURE.md#db-level-tenancy-physical-db-is-the-unit-spaces-are-a-view).

#### 6. Grant operator access, then open the Operator App

1. Click **Next** past **Agent capabilities** (the inherited defaults already work; change them
   only if this space needs its own knowledge or model) to reach **Operators**.
2. Under *User access* click **Add user**.
3. To try it yourself, grant your own admin email operator access. To invite someone else,
   enter their address under *Invite a new operator by email* and click **Invite & grant
   access**.
4. Click **Open operator app**, also available as *Operator access* in the space header once
   at least one database is Ready.

Sign in with the same credentials as the Admin Console. The app opens straight into your
space; if you have several, you pick one first. In the Operator App everyone is an operator:
admin roles grant nothing there, so a super admin or space admin also has to be added to a
space (step 3) to open it, and then sees exactly what every other operator of that space sees.

In the Admin Console a space is managed by its creator or by a super admin, nobody else.
Being added to a space grants use of it, never its management, so a space admin added to
another admin's space sees it read-only. Super admins run the instance (users, settings,
models, monitoring cadence), can see every space, and can act in any space as an audited
break-glass manager; those actions still run under the space owner's role, so IAM limits
them to that owner's secrets. Super admins create spaces only for themselves.

### Using the Operator App

- **Left rail** — New chat, the views (Dashboard, Investigations, Trends, Topology), your
  recent chats (a dot marks a reply you have not seen yet), the space switcher, and sign-out.
- **Chat pane** — always present on the left. Views open as panels to its right; drag the
  divider to resize.
- **Scope selector** — at the top of the chat pane, and it matters: it decides which
  databases the agent works with.
- **Agent Trace** — a bar along the bottom recording every tool call, sub-agent and skill the
  agent used, for both chat and investigations. It stays put whichever view is open.

**Chat.** Pick your scope, then type or use a suggested prompt. Pills under each answer offer
follow-up questions, or one-click replies when the agent asked you something. Once a
conversation exists, **Download chat** exports the transcript as Markdown. Several chats can
run at once; a reply that lands while you are in another chat marks that chat with a dot.

**Dashboard.** One row per database with its health, newest Tier 1 sample and last deep scan.
The row actions open a drawer under the table for that database: **Metrics (Tier 1)** lists
every lightweight collection in a window with every collected value (rows expand to table
bloat and wait events), **Deep scans (Tier 2)** lists every scan with its headline numbers,
findings and report — **Compare…** picks two and shows them side by side — **Trends** plots
the key Tier 1 series with the deep scans marked on the same axis, and **CloudWatch metrics**
charts every series the deployment publishes for the database, Tier 1 and deep-scan daily,
over windows up to 15 months. **Chat** on a row scopes the chat to that database.

**Trends** (left rail) is the fleet view: the newest sample per database, health per scan day,
and a metric comparison across databases where the picker reaches every published series
and **Worst first** ranks the fleet on the chosen one.

**Investigations** — a structured, multi-agent root-cause analysis. Two ways in, both landing
in the same place:

- From **Investigations**: describe the issue or click a quick-start chip (High CPU,
  Replication lag, Lock contention, Connection spike, Storage growth, Slow queries), choose
  the database and a time range, then **Start investigation**.
- From a **chat**: when the agent judges something worth a deeper look it offers a *Start
  investigation* action, which carries your current scope over. That chat pauses while the
  investigation runs (other chats stay usable) and, when it finishes, gets the outcome back
  with pills such as *Summarize the investigation here*. *Chat about this* on a finished
  investigation reopens a conversation with the findings preloaded.

Either way it streams live, then shows a Timeline, Findings, Root Cause Analysis, Recommended
Actions and a Summary. **Download** in the *Full investigation report* bar saves it as
Markdown; the Agent Trace exports as JSON, one interaction at a time or all at once.

### Email alarms

Every onboarded database gets CloudWatch alarms on its Tier 1 metrics, and a state change
emails the **alert notification email** you set at deploy time (`NOTIFICATION_EMAIL`). With
no notification email set, alarms are still created but nobody is told.

| Alarm | Fires when | Default |
|---|---|---|
| Tier 1 collection failing | three Tier 1 runs in a row could not collect the database | on |
| Transaction ID wraparound risk | the writer's XID age passes the warning point in the findings (1 billion XIDs) | on |
| Connections near max_connections | connections reach 80% of `max_connections` in 2 of 3 runs | on |
| Long-running transaction | a client transaction has been open 30 minutes | on |
| Sustained lock contention | queries are blocked in 3 runs in a row | on |
| Deadlocks | any deadlock since the previous run | on |
| Replica lag | the worst reader lags 5 s or more in 3 runs in a row | on |
| Low buffer cache hit ratio | the cache hit ratio stays under 95% for 3 runs | on |
| High rollback ratio | 10% or more of transactions roll back for 3 runs | off |

The thresholds are the bands the deep-scan findings use (`backend/checks/references.yaml`),
so an alarm and a finding agree. Each alarm evaluates once per Tier 1 interval of its
database; a paused or new database never alarms on missing data.

The email names the database, its region and engine, the value against the threshold, what
it means and what to check first, with a link to the Operator App. A second email says when
it has cleared. An alarm that clears and fires again within 30 minutes is not emailed again.
Email goes out as HTML through SES when the invitation sender (Settings › Email) is a verified
identity and SES can reach the address (production access, or the address itself verified
while SES is in the sandbox); otherwise as plain text through the notification topic.

A super admin manages them in the admin console under **Settings › Alarms**: one switch for
all alarms and one per alarm, which take effect within about a minute. The panel shows which
channel delivers, how many alarms exist, which are firing now, and **Send a test email**.
Alarms only notify; nothing they do changes a database.

## Deployment Commands

Everything deploys as one nested CloudFormation stack (`infrastructure/master.yaml`).

```bash
make preflight       # Check prerequisites
make deploy          # Interactive deploy (asks which environment, prompts once, saves deploy.<name>.config)
make deploy-all      # Same, without prompts (reads the environment's config)
make envs            # List the saved environments
                     # Any of these with ENV=<name> (or an exported DEPLOY_ENV) skips the question
make cognito-user    # Create a console user (interactive)
make outputs         # Show stack outputs (URLs, pool ID, ...)
make check           # Static checks on templates and resource naming
make test            # Run the guard tests
make purge-orphans   # Remove resources a failed deploy left behind
make destroy         # Delete the stack (asks for confirmation)
```

The four steps `deploy-all` runs, in order, if you need to repeat just one:

```bash
make stage           # Build + upload templates and Lambda artifacts to S3
make images          # Build + push the backend and batch images (arm64) to ECR
make deploy-stack    # Deploy infrastructure/master.yaml
make deploy-spas     # Build + publish the operator app and admin console
```

Order matters: the stack references the staged templates and images, and the web apps read
their configuration from stack outputs, so they are published last.

```bash
make logs            # Tail the agent runtime logs
make test-scan       # Trigger a scan
make dev             # Run the backend locally on port 8084
make smoke           # Check the deployed operator app responds
make clean           # Remove local build artifacts only
```

## Teardown

```bash
make destroy
```

It asks you to type the stack name, empties the web-app, reports and knowledge buckets, then
deletes the stack. Afterwards it deletes the admin audit vault bucket if it is empty, and
otherwise says how long its newest record stays locked.

**Expect the first pass to take a while, and often to need a second.** Deleting the AgentCore
runtime usually exceeds CloudFormation's stabilization window, so it is reported as
`NotStabilized` even though the runtime is gone. The network interfaces it placed in the
private subnets (interface type `agentic_ai`, with a service-managed `ela-attach-*`
attachment) stay attached afterwards, and while they exist the subnets and their security
group cannot be deleted, so the VPC stack fails with:

```
The subnet 'subnet-...' has dependencies and cannot be deleted
```

`make destroy` recognises this, waits up to 30 minutes for the service to release them, then
retries the delete. Those interfaces cannot be detached or deleted by hand, because the API
rejects it with `currently in use`, and we have seen them persist for over an hour, so waiting
longer does not always help.

This is safe to leave sitting. Everything else is deleted in the first pass, including the NAT
gateway, so what remains is a VPC, two private subnets, a security group, an IAM role, a log
group and a small Lambda. None of those accrue hourly charges.

If you would rather not leave a stack in `DELETE_FAILED`, delete it and keep the VPC behind:

```bash
aws cloudformation delete-stack --stack-name <stack> --retain-resources VPCStack
```

The stack then disappears cleanly and the VPC is left orphaned for you to remove once the
interfaces are released. Check whether they have gone with:

```bash
aws ec2 describe-network-interfaces --filters Name=interface-type,Values=agentic_ai
```

### What teardown keeps

`make destroy` does **not** leave the account empty. These carry `DeletionPolicy: Retain` so a
teardown cannot destroy your onboarding or audit trail, and a later `make deploy` picks them
back up:

- the nine DynamoDB registries: spaces, databases, investigations, recommendations,
  sessions, audit, network, config, user knowledge
- the deep-scan history table (`<prefix>-tier2-scans`)
- the log buckets (`<prefix>-logs`, `<prefix>-frontend-logs`, `<prefix>-admin-logs`) and the
  reports bucket, whose contents are emptied but the bucket kept
- the ECR repositories (`<prefix>-backend`, `<prefix>-batch`) and the artifacts bucket
- the CloudTrail bucket, if you enabled it
- the admin audit vault bucket (`<prefix>-audit-<account>-<suffix>`), while it holds any
  record. Its records are under Object Lock (COMPLIANCE): nothing, not even the account
  root user, can delete one before its retention ends, so neither `make destroy` nor
  `make purge-orphans` empties it. Each deletes the bucket only once it is already empty;
  S3 lifecycle removes every record when its lock ends. Each deploy creates its own bucket
  (the suffix comes from the stack id), so a retained one never blocks a redeploy, and
  `check-orphans.sh` only notes it. For a trial, deploy with `AUDIT_VAULT = off`: the bucket
  then stays empty and teardown deletes it.

Deleted outright: the Tier 1 metrics table, both CloudFront distributions and their buckets,
the Cognito user pool, the API, the agent runtime and memory, and the VPC.

For a genuinely clean account, follow up with:

```bash
make purge-orphans
```

It lists exactly what it found, then asks you to type `<prefix>@<account>` before deleting
anything.

Both `make destroy` and `make purge-orphans` want the account in that token on purpose.
Every account uses the same stack name, so typing `dbops` confirms *what* but not *where*,
and a teardown aimed at the right stack name in the wrong account is the mistake worth
designing against.

Everything that can delete lives in one place, [`deployment/destroy/`](deployment/destroy/),
and each script carries its own confirmation rather than relying on the `make` target that
usually calls it. Nothing else under `deployment/` deletes anything, so
`./deployment/check-orphans.sh <stack> <prefix> <region>` is always safe to run to see what
*would* be removed.

## Indicative costs

Prices are indicative list prices for us-east-1 as of 2 October 2026. AWS prices change and vary by Region; check the linked AWS pricing pages before you plan a budget.

| Component | Indicative monthly cost | Pricing page |
|---|---|---|
| VPC endpoints (13 interface endpoints) | the largest fixed cost; charged per interface endpoint per month | [AWS PrivateLink pricing](https://aws.amazon.com/privatelink/pricing/) |
| NAT Gateway (one) | about $32, plus data transfer | [Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/) |
| CloudWatch custom metrics (the Tier 1 and deep-scan series) | per metric per month, plus GetMetricData per metric read. Each collected member publishes its Tier 1 numerics under a Member and a Role dimension, and each deep scan its daily numerics the same way, so the count grows with members, not with samples | [Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| CloudWatch alarms ([email alarms](#email-alarms)) | per alarm per month: one per enabled alarm per monitored database (replica lag only where the database has a reader). Switching an alarm off in Settings › Alarms deletes it | [Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| Alarm emails (SES, or the notification topic) | per email sent | [Amazon SES pricing](https://aws.amazon.com/ses/pricing/), [Amazon SNS pricing](https://aws.amazon.com/sns/pricing/) |
| AgentCore Runtime | pay per use | [Amazon Bedrock AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) |
| AgentCore web search (`EnableWebSearch`, on by default) | $7 per 1,000 queries; us-east-1, eu-west-1 and ap-northeast-1 only | [Amazon Bedrock AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) |
| ECS Fargate (REST API) | about $15 (0.25 vCPU, 0.5 GB) | [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/) |
| AWS WAF (`ENABLE_WAF`, two web ACLs, four rules each) | about $18 before per-request charges | [AWS WAF pricing](https://aws.amazon.com/waf/pricing/) |
| Batch on Fargate (Tier 2 scans) | per scan, about $0.02 a scan | [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/) |
| Tier 1 collector (Lambda) | per invocation, one every cadence interval per database | [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Bedrock models | per token | [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) |
| VPC Lattice resource endpoint (each database on the Lattice path) | $0.02 per hour per resource endpoint (about $14.60 a month), plus $0.01 per GB processed in the deployment account and $0.006 per GB billed to the database's account. Resource gateways and resource configurations have no hourly charge | [AWS PrivateLink pricing](https://aws.amazon.com/privatelink/pricing/), [Amazon VPC Lattice pricing](https://aws.amazon.com/vpc/lattice/pricing/) |
| DynamoDB (12 tables, on-demand; the Tier 1 metrics table grows with databases × cadence) | about $2 before Tier 1 volume | [Amazon DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/) |
| S3 (reports) | about $1 | [Amazon S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 admin audit vault (Object Lock) | S3 storage only; tiny: one small JSON object per admin action, kept for the retention period | [Amazon S3 pricing](https://aws.amazon.com/s3/pricing/) |
| CloudFront (two distributions) | about $2 (operator app and admin console) | [Amazon CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/) |

## Troubleshooting

**A deploy failed, and now every retry fails too.** Most stateful resources carry
`DeletionPolicy: Retain` so that a teardown cannot destroy your data, but CloudFormation
applies that when it rolls back a *failed create* as well. So the tables, buckets, secret and
agent memory survive, and the next attempt cannot recreate them.
CloudFormation reports this unhelpfully, naming no resource:

```
Validation failed with 9 error(s). Call DescribeEvents to retrieve the full list
```

`make deploy` now checks for these before starting and lists them. To remove them and retry:

```bash
make purge-orphans   # lists what it found, asks you to type <prefix>@<account>, then deletes
make deploy
```

If the account holds an installation you want to keep, deploy alongside it instead:

```bash
make deploy RESOURCE_PREFIX=myprefix STACK_NAME=myprefix
```

**`Memory with name ... already exists`** or a similar `AlreadyExists`. An installation is
already in the account under that prefix. Deploy under a different `RESOURCE_PREFIX`, or
remove the old one.

**The agent deploys but cannot answer.** Confirm Bedrock model access for Claude Haiku 5.5 is
enabled in your region, then check `make logs`.

**Credentials expired mid-deploy.** Refresh them and re-run `make deploy`; it is safe to
repeat and will continue from the existing stack.

### Onboarding

**"Access Denied" in the Admin Console.** The account is not in the `space-admins` group. Add
it with `make cognito-user`, or change the group from the Users screen as an admin.

**A secondary account is stuck on "Pending validation".** The role name does not match what
you entered, or the trust policy is wrong. Compare the role in IAM against the values the
wizard showed, then click **Validate access again**. Validation only runs when you ask it to.

**Discovery finds no databases.** Confirm the database is PostgreSQL, is in a region you
selected, and that *Validate access* succeeded first, since discovery relies on the Resource
Explorer which that step enables. Then run it again.

**A discovered database is not in the space's table.** Discovery only proposes. Open the
**Databases** stage and click **Onboard** on it; a database joins a space only once an
admin has onboarded it there with a verified read-only secret.

**Onboard fails to connect.** The secret's name must start with your secret folder,
`<prefix>/<your admin id>/`, exactly as the wizard's Step 1 generates it: the space's access
role reads that folder and nothing else, so a secret named any other way (including an older
secret stored under a different name) is refused, and the wizard says so before it calls AWS. To
reuse an old secret, copy its value to a new secret with the Step 1 name. The secret must also
be in the same account and region as the
database, must hold the `dbops_ro` credentials rather than the master user's, and the
database must be reachable from the deployment's VPC. Its `host`, if present, must be
exactly the endpoint the wizard shows (for Aurora, the cluster endpoint, not the reader or an
instance endpoint), or left out; an RDS read replica may use its primary's secret. **Test
secret** in the wizard reports which of those failed and names the endpoint to put in
`host`. See [Supported databases](#supported-databases) for the topologies that are not
supported yet.

**Two databases in different accounts share a VPC CIDR (both on `10.0.0.0/16`).** In the same
region the second one is connected over a VPC Lattice resource endpoint automatically: no route
is added anywhere, the database's own hostname resolves to the endpoint inside the deployment's
VPC, and the endpoint adds a small hourly charge (see [Indicative costs](#indicative-costs)). (Set the `SameRegionConnectivity`
stack parameter to `lattice` to use a resource endpoint for every same-region database, if you
would rather never add a route to a database VPC.) Across regions the agent
routes that database's own address through its own peering; the configuration log says
"routing only this database's address". Every member of a cluster gets its own route. If
the database later fails over, a member is replaced, or members are added or removed, the
next Tier 1 run detects it and flags the database ("Route repair needed" in the admin
console), and the API task moves the routes within about 5 to 10 minutes (each change is a
"Host route repair" line in the configuration log). **Repair** on that database runs the
repair at once. If the repair is refused (for example, the new address belongs to another
registered database) the console shows "Route repair refused" with the reason and a
notification is sent. While Tier 1 is paused the drift is not detected; **Reconfigure
network** on that database re-asserts the routes.

**"Not an operator of this space" in the Operator App.** Operator access is per space: add the
user under the space's **Operators** stage. This applies to admins too — creating a space, or
holding the super-admin or space-admin role, does not open it in the Operator App (a space
admin who creates a space is added as its operator automatically; a super admin is not). The
screen names the account you are signed in as and lists the spaces it can open: the Admin
Console and the Operator App share one browser, so clicking **Open operator app** while the
Operator App is still signed in as a different account is the usual cause. Use **Sign out and
switch account**. At least one database must also be **Ready**. The app refreshes your session
once on its own when a newly granted space is missing; if it still says denied, sign out and
back in.

**No invite email.** Check spam first. Cognito sends from its default address unless you
configured a verified SES sender, and those are easily filtered. Resend from the Users screen.

## Project Structure

```
backend/           FastAPI app, the agents, and the diagnostic tools
  app.py             REST API and the two supervisor agents
  sub_agents/        PostgreSQL, observability, research and learning agents
  tools/             Diagnostic tools, plus the AWS clients (the metrics store among them)
  runbooks/          SOPs the investigation agent loads before it starts
  skills/            One SKILL.md per diagnostic skill
  checks/            Check catalog and severity thresholds
batch/             The health-scan container that AWS Batch runs
admin-console/     React app for setup
frontend/          React app for operators
infrastructure/    CloudFormation, one nested stack per concern
deployment/        Build, deploy and preflight scripts
  destroy/           Everything that can delete, kept separate on purpose
tests/             Guard tests for the deploy tooling
docs/              ARCHITECTURE.md, the orchestration-flow diagram, and diagrams/ (architecture
                   diagrams as SVG + PNG, with the script that builds them)
```

The templates under `infrastructure/` are composed by `master.yaml`: VPC, Cognito, storage,
the API, the agent runtime, the two web apps, and an optional
guardrail. `make deploy` stages them to S3 and deploys that one parent stack.

## Learn more

The README covers deploying and using the agent. Everything about how it works internally
lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), which goes considerably deeper than
this file:

| Topic | What it covers |
|---|---|
| [Multi-Agent Architecture](docs/ARCHITECTURE.md#multi-agent-architecture) | Why supervisors stay small, and how work is split between them and the domain agents |
| [End-to-End Orchestration Flow](docs/ARCHITECTURE.md#end-to-end-orchestration-flow) | A question's full path, from the browser to a database and back |
| [Investigations](docs/ARCHITECTURE.md#investigations) | The structured protocol, how findings get their severity, and what is persisted |
| [Runbook/SOP System](docs/ARCHITECTURE.md#runbooksop-system) | Which SOPs load on which surface, and why the guardrails are always on |
| [Diagnostic Checks](docs/ARCHITECTURE.md#diagnostic-checks) | The check catalog, its thresholds, and how skills map to symptoms |
| [Agent Tools](docs/ARCHITECTURE.md#agent-tools-complete-reference) | Every tool, what it returns, and which agent holds it |
| [Network Architecture](docs/ARCHITECTURE.md#network-architecture) | VPC peering, the VPC Lattice fallback, and the connection registry |
| [Metrics Store](docs/ARCHITECTURE.md#metrics-store) | The two metric tiers, the DynamoDB records and CloudWatch series, and retention |
| [Alarms](docs/ARCHITECTURE.md#alarms) | The alarm catalogue, how alarms are kept in sync, and the email notifier |
| [Security Model](docs/ARCHITECTURE.md#security-model) | Roles, the read-only path to your data, and the built-in controls |
| [Topology](docs/ARCHITECTURE.md#topology) | How the drill-down map is assembled without extra AWS calls |

## Contributing

Bug reports and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers how to run the
checks locally, both of which work without AWS credentials:

```bash
make check    # templates, resource naming, deploy settings, container runtime
make test     # unit tests plus the guard tests for the deploy tooling
```

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).

## Security

If you find a potential security issue, please report it through the
[AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) rather
than opening a public issue. See [SECURITY.md](SECURITY.md).

For how the deployment protects your databases, including the read-only path and the built-in
controls, see the [security model](docs/ARCHITECTURE.md#security-model). That page also lists the
static-analysis findings that are accepted by design, and why.

### Encrypting the API Gateway to load balancer hop

The internal load balancer listens on HTTP. Requests reach it only through API Gateway over a VPC
Link, so the hop never leaves your VPC — but it is plaintext, which some regimes (PCI DSS, FedRAMP)
do not permit even inside a VPC.

Closing it needs a TLS certificate for an internal hostname, which the deployment cannot create for
you: a public ACM certificate requires a domain you control, and a private one requires AWS Private
CA. Both are decisions only you can make. To do it yourself after deploying:

1. **Get a certificate in ACM**, in the same region as the stack. Either request a public
   certificate for a subdomain you own (for example `dbops.internal.example.com`) and validate it
   via DNS, or issue one from AWS Private CA if you already run a private CA.
2. **Point the hostname at the load balancer** with a Route 53 private-hosted-zone alias record, so
   the name resolves inside the VPC.
3. **Switch the listener to HTTPS** on the `APIStack` load balancer: port 443, protocol HTTPS, your
   certificate ARN, and an `SslPolicy` of `ELBSecurityPolicy-TLS13-1-2-2021-06` or later. Keep the
   target group on HTTP port 8080.
4. **Tell API Gateway to verify the name** by setting `TlsConfig.ServerNameToVerify` on the ALB
   integration to the hostname from step 1. Without this, API Gateway keeps speaking HTTP.

This encrypts API Gateway to the load balancer. The final hop from the load balancer to the ECS task
stays HTTP on port 8080, because the container serves plain HTTP; that segment is confined to one
security group inside a private subnet. Terminating TLS in the container as well means building a
certificate into the image and managing its rotation.

### Where to harden further

Things this deployment does not do, listed because you should decide about them rather
than discover them. None is a defect in the sense of a control that was meant to be
here and is missing — each is a trade-off with reasoning behind it, recorded in the
[security model](docs/ARCHITECTURE.md#security-model). What follows is the short
version, and what you would change if your environment needs more.

**The agent trusts what it reads.** It runs diagnostic queries and reads knowledge
documents, then feeds the results to a model that decides which tool to call next. A
Bedrock guardrail screens the operator's prompt, and knowledge documents are wrapped in
delimiters that mark them untrusted — but neither makes injected instructions inside
query results or document text impossible to act on. Someone who can write to a table
the diagnostics read, or upload a knowledge document, has an indirect channel to the
agent. Treat write access to monitored databases and to a space's knowledge documents
as trusted, and keep the operator's read-only credential read-only.

**Sign-in tokens live in browser storage.** Both web apps keep the Cognito JWT and
refresh token in `localStorage`, so any script running in the page origin can read them
— which is what makes a cross-site scripting flaw in the app or a dependency more
serious than it looks. A Content Security Policy is in place and both SPAs are served
from CloudFront with Origin Access Control. Moving to cookie-based sessions with
`HttpOnly` would close the class rather than mitigate it, and needs a same-site API
domain to be worth doing.

**Sign-in uses password authentication over TLS.** The web client uses Cognito's
`USER_PASSWORD_AUTH` flow: the password travels over TLS and Cognito verifies it, and
every account carries mandatory TOTP MFA, so a password alone never signs anyone in — a
per-IP WAF rate limit bounds guessing on top of that. SRP, which proves the password
without sending it, is a stronger posture; adopting it means rewriting both apps'
hand-rolled login and TOTP-MFA flows against an SRP library, so it is left as a
deliberate choice rather than done here.

**Cognito advanced security is off by default.** Compromised-credential detection and
adaptive authentication need the Cognito Plus tier, which is billed per monthly active
user. Enable it with `EnableCognitoThreatProtection=true` (the
`ENABLE_COGNITO_THREAT_PROTECTION` deploy setting); mandatory MFA and the sign-in rate
limit apply either way.

**Your own identity provider is not wired up.** Cognito can federate SAML or OIDC into
the pool this deployment creates, so you do not need to own the pool — but the
application addresses users by email address, and a federated user arrives with a
provider-prefixed username instead. They would sign in successfully and hold no group,
so the console could not grant them a role. Supporting it means keying identity on the
email claim across role resolution, space membership and the audit trail, which is more
than a configuration change.

**Database connections verify the server certificate, always.** Every connection to a
monitored database, from the agent, the scans and the onboarding checks alike, verifies
the server against the public RDS certificate authority bundle that ships inside the
images, so nothing is needed from you for RDS or Aurora in any region. The check covers the
certificate chain and the hostname on every path: over a VPC Lattice resource endpoint the
database's own hostname is what resolves to the endpoint, so nothing is skipped. There is
no encrypt-only option: every database
this deployment can monitor is an RDS or Aurora instance and therefore has an RDS-issued
certificate. If the bundle were ever missing from a build, connections refuse to open
rather than fall back to an unverified connection.

**The CloudFront distributions accept older TLS versions.** They use the default
`*.cloudfront.net` certificate, and with that certificate the minimum protocol version
is fixed and cannot be raised. Bringing a custom domain and an ACM certificate lets you
set `MinimumProtocolVersion` to a modern policy — the same prerequisite as the ALB
change described above.

**The AI runtime is reached directly, and rate limiting is layered.** The chat and
investigation calls stream to the Bedrock AgentCore runtime **directly, not through API
Gateway**, so no AWS WAF and no gateway throttle evaluate that data plane — the optional WAF
covers the two CloudFront distributions (the static web apps) only. What bounds the AI
runtime instead is authentication (a Cognito JWT), a **per-caller rate limit** on the
model-invoking paths (a generous per-minute budget per operator, default `RATE_LIMIT_PER_MINUTE`
and retunable by a super admin from the Security console without a redeploy — the value is
persisted in the config store and survives stack updates — that sheds a runaway client or a
stolen token while never getting in a real operator's way, and fails open if its counter is
unavailable), and the account's Bedrock service quota as the
final backstop. The one REST route that invokes a model per call, `POST
/api/reports/compare-summary`, additionally carries a tighter per-route API Gateway throttle.
The per-caller limit keys off the authenticated identity, so it bounds an authenticated
caller rather than an anonymous distributed flood; a space running expensive investigations
is contained to its own budget rather than draining everyone else's.

**Outbound access from the private workloads is scoped by port, and the scan task is
isolated.** The Tier 2 scan container is the sensitive one — it holds the monitored
database's password and the cross-account role — so it runs on its **own** security group
(not the shared private-workload group) with **no public IP** and egress limited to `443`
(AWS APIs; cross-region Secrets Manager via NAT), the PostgreSQL ports in `DB_PORT_RANGE`
(default `5432`, over the per-database peering or Lattice path), and `53` (DNS). The remaining private workloads (the API task
and the agent runtime) still share a group with broad egress, because they connect to
database endpoints whose addresses are only known once you register them, over peering or a
Lattice resource endpoint. Inbound is restricted to the load balancer and the VPC. Narrowing those last
components to a per-endpoint allowlist would mean rebuilding the security group whenever a
database is added; the scan task, which is the credential-bearing one, is the piece that is
isolated here. Dropping the NAT route entirely is deliberately not done: the scan
legitimately fetches secrets in other regions, which a regional VPC endpoint cannot reach.

**Cross-account role assumption is bound to the admin whose space requested it.** The
deployment assumes roles in member accounts by role name, because the set of accounts is
not known when the stack is created. Each assumption also presents an `sts:ExternalId` set
to the id of the admin who owns the space, and the trust policy a member account installs
requires that id, so a role installed for one admin's spaces cannot be used by another
admin's. One role per admin per account serves every space that admin creates. The binding
stays inert until someone with IAM rights in the target account creates that role, so
consent is expressed on the far side, by whoever controls the account.

The role a member account installs also does not trust every identity in the deployment
account: the trust policy the console hands out adds an `aws:PrincipalArn` condition that
narrows the trust to IAM roles in the deployment account, so a user or federated session
there cannot assume it even with the id. Narrow it further to the exact ARNs published as
the `AssumeRolePrincipalArns` stack output if you want to. The same policy already pins the
role to its admin through `sts:ExternalId`, and its secret read to that admin's namespace,
so a role installed for one admin is never usable for another, including across
organisations that do not trust each other.

**The build supply chain is pinned, but not signed or scanned in a pipeline.** The
container base images are pinned by digest; the two images this project builds are pushed
to **immutable-tag** ECR repositories and deployed to the Batch job, the ECS task and the
AgentCore runtime by their `@sha256` **digest**, so a pushed tag cannot be re-pointed to
swap the code a running task pulls. The Python dependencies are installed from a fully
pinned, hashed lock (`pip install --require-hashes`, generated with `make lock`), the web
apps build with `npm ci` (which fails closed on a lock-file mismatch rather than silently
resolving fresh versions), and Dependabot watches both so a digest or dependency cannot
silently fall behind on patches — it opens an update pull request, including for known
CVEs. What this repository
does not add, because it has no build pipeline of its own (images are built locally via
`make images`), is a step that blocks a build on a vulnerability scan, cryptographic image
signing, or a generated software bill of materials. Those belong in the pipeline you build
and publish from: scan the image and fail the build on a threshold, sign it (for example
with cosign) and verify the signature at deploy, and emit an SBOM alongside it. If you run
this in production next to real databases, wiring those into your release pipeline is the
recommended next step.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text,
[NOTICE](NOTICE) for attributions, and [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) for the
licence of every third-party dependency.

Although this repository is released under the Apache-2.0 license, its backend and batch
containers use the third-party psycopg2 project, whose licensing includes the LGPL-3.0 license.
It is used unmodified and installed from PyPI when you build the containers.
