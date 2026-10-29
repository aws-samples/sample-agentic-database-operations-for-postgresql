# Engineering conventions

## Code and docs describe the current state, never the change

A comment, docstring, or document explains what the code **does now** and why that is the
right behaviour. It never narrates a fix, a bug, or what the code used to do.

- Do not write comments like "previously this was 8s and timed out", "this used to be
  dropped silently", "fixes the regression where…", or "no longer does X".
- Do not leave history in docstrings or READMEs: "for weeks the log group held only…".
- The *why-it-changed* story belongs in the **commit message**, which is the record of
  change. The code is the record of state.

A good comment answers "why is it this way?" in the present tense:

```python
# The runtime forwards stdout to CloudWatch, not stderr, so application logs go to stdout.
logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
```

Not this:

```python
# Log to stdout, not the default stderr: previously every app line was silently dropped
# and the pills vanished with no trace.
```

The same applies to tests. A test's comment states the behaviour it pins, not the incident
that motivated it.

When editing an existing comment that already narrates history, rewrite it to current
state rather than appending to it.

## Documents stay current, not cumulative

READMEs, design notes, and `docs/` describe how the system works today. Do not add
"Changelog" or "Previously…" sections to them; the git log and `CHANGELOG.md` carry history.
If a document describes behaviour that no longer exists, update the description, don't
annotate it.

## Commit messages carry the change

Follow Conventional Commits. Put the problem, the cause, and what changed in the commit
body. That is the one place the before/after story is wanted.
