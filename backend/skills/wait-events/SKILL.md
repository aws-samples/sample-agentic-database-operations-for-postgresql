---
name: wait-events
description: Read current wait events to see what sessions are blocked on right now — lock contention vs I/O vs normal client waits. Activate on "what is the database waiting on", live slowdowns, wait event analysis, or real-time bottleneck triage.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-023]
---

# Wait Event Investigation

See what active sessions are waiting on right now to pinpoint the live bottleneck. The shared
diagnostic guardrails apply; the verdict is already computed by the tool.

## When to use
Symptoms involving: a live slowdown, "what is the database waiting on", or real-time bottleneck
triage.

## Steps (which tool to call)
1. **Primary — `get_wait_event_analysis`** (via `postgres_agent`). Returns each `wait_event_type` /
   `wait_event` with a session count and a `status`. **Trust `status`.** Interpretation:
   - `Client:*` / `Activity:*` / idle waits are **normal** (OK) — `Client:ClientRead` just means idle
     sessions waiting for the client. Never report these as a problem.
   - `Lock:*` escalates with concurrency (>=2 sessions WARNING, >10 CRITICAL) — real contention.
   - `IO:*` escalates with concurrency (>5 WARNING, >20 CRITICAL) — a storage bottleneck.
2. **For Lock waits, find who blocks whom — `get_lock_analysis`** (via `postgres_agent`). Wait events
   tell you *that* sessions are lock-waiting; the lock analysis tells you the blocking chain.
3. **For IO waits, corroborate — `observability_agent`.** High `IO:DataFileRead` with many sessions
   should line up with CloudWatch ReadIOPS/ReadLatency; if those are flat, reconsider.

## Output
Report the dominant wait events with session counts and verdict, grouped by type. Remediation is a
proposal that depends on the wait class: Lock waits → resolve the blocker / the contended access
pattern; IO waits → the cache-hit / storage / memory path; and explicitly call out that
Client/idle waits are normal so they are not mistaken for a problem.

Refer to this in user-facing output by name — "wait events" — never by an internal catalog code.
Catalog ids live only in this skill's frontmatter metadata.
