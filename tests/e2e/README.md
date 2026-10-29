# UAT end-to-end tests

These run automatically in the release pipeline's **Tests** stage, against the
**live UAT deployment** (after the candidate build is deployed to the UAT
account, before the security pentest and before human approval). If they fail,
the release is stopped and the UAT stack is torn down.

## How it runs

The pipeline's `*-uat-tests` CodeBuild project clones this repo and runs:

```
tests/e2e/run.sh "$UAT_URL"
```

`$UAT_URL` is the live URL of the deployed UAT product (its dashboard/CloudFront URL).

## Adding tests (no pipeline change needed)

`run.sh` auto-discovers and runs:

- **Shell tests** — any executable `tests/e2e/*.sh` (receives `$UAT_URL` as `$1`).
  Exit non-zero to fail the release.
- **Python tests** — any `tests/e2e/test_*.py` (run with `pytest`; read the
  `UAT_URL` environment variable). `pytest` + `requests` are installed on the fly.

Example `tests/e2e/test_login.py`:

```python
import os, requests
def test_dashboard_reachable():
    r = requests.get(os.environ["UAT_URL"], timeout=20)
    assert r.status_code < 500
```

Built in, `run.sh` already does a reachability smoke test on `$UAT_URL`.
