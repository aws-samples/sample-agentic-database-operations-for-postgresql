---
name: Bug report
about: Report something that does not work as described
title: ''
labels: bug
assignees: ''
---

**Describe the bug**
What happened, and what you expected instead.

**To reproduce**
The steps you took. If it happened during a deploy, the exact command is the most useful thing
you can give us:

```
make deploy
```

**Where it happened**

- AWS region:
- Deployment stage: preflight / stage / images / deploy-stack / deploy-spas / onboarding / running
- Stack status, from `make outputs` or the CloudFormation console:

**Logs or error output**
Paste the relevant part. `make logs` tails the backend. Please redact account IDs, ARNs and
endpoints if you would rather not share them; the shape of the error is usually enough.

**Environment**

- OS:
- Container runtime and version (`docker --version`, `finch --version`, `podman --version`):
- `aws --version`:
- Commit or release you are on:

**Anything else**
Whether you had modified the templates or configuration, and whether the account already held an
installation. Both change the answer.
