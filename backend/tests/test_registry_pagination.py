# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every "list all" read follows the next page, and Batch job lookups find their job.

DynamoDB returns at most 1 MB per scan or query and a truncated page looks complete. The
registry readers stopped at page one, so past roughly a hundred databases rows vanished
from the console, an operator was told they were not in a space they owned, and discovery,
which de-duplicates against the list, created a second record for a database it already had.
The Batch job lookups listed the whole queue per status, one page each, then filtered by a
name built from the first eight characters of the id: a busy queue hid a database's jobs,
and the duplicate guard turned its own 409 into "Failed to submit scan job".

Pinned with fakes that return two pages, the match on page 2:
  * ddb_pages.read_all, and the registries built on it: instances (scan and account query),
    spaces, investigations, recommendations (by instance and by space), network by VPC;
  * discovery confirms on the ARN index before creating a record, so a row the list did
    not return is not created twice;
  * scan jobs: one JOB_NAME-filtered ListJobs followed through nextToken; the guard finds a
    RUNNING job on page 2 and answers 409 with its own message; history lists page-2 jobs;
    two ids sharing eight leading characters never mix; jobs are named by the full id;
  * the Tier 1 zip ships ddb_pages.py, which spaces_client.py now imports.
(The compound (scan_time, id) cursor is pinned in test_scan_records.py.)

Run:  python3 backend/tests/test_registry_pagination.py
"""
import asyncio
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_SRC = (ROOT / "backend/app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


class TwoPageTable:
    """scan/query return the rows in two pages; records every call's kwargs."""
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _page(self, kw):
        self.calls.append(dict(kw))
        half = len(self.rows) // 2
        if "ExclusiveStartKey" not in kw:
            return {"Items": self.rows[:half], "LastEvaluatedKey": {"k": "page-2"}}
        assert kw["ExclusiveStartKey"] == {"k": "page-2"}
        return {"Items": self.rows[half:]}

    def scan(self, **kw):
        return self._page(kw)

    def query(self, **kw):
        return self._page(kw)


_boto3 = types.ModuleType("boto3")
_boto3.resource = lambda *a, **k: None
_boto3.client = lambda *a, **k: None
sys.modules.setdefault("boto3", _boto3)
_conditions = types.ModuleType("boto3.dynamodb.conditions")


class _Cond:
    def __init__(self, name):
        self.name = name

    def eq(self, v):
        return (self.name, v)


_conditions.Key = _Cond
_conditions.Attr = _Cond
sys.modules.setdefault("boto3.dynamodb", types.ModuleType("boto3.dynamodb"))
sys.modules.setdefault("boto3.dynamodb.conditions", _conditions)
_botocore_exc = types.ModuleType("botocore.exceptions")
_botocore_exc.ClientError = type("ClientError", (Exception,), {})
sys.modules.setdefault("botocore", types.ModuleType("botocore"))
sys.modules.setdefault("botocore.exceptions", _botocore_exc)

from tools.aws.ddb_pages import read_all  # noqa: E402
from tools.aws.dynamodb_client import DbInstanceRegistry  # noqa: E402
from tools.aws.spaces_client import SpacesRegistry  # noqa: E402
from tools.aws.investigations_client import InvestigationsRegistry  # noqa: E402
from tools.aws.recommendations_client import RecommendationsRegistry  # noqa: E402
from tools.aws.network_registry import NetworkRegistry  # noqa: E402

ROWS = [{"instance_id": f"db-{i}", "space_id": "sp", "started_at": f"2026-10-0{i}",
         "created_at": f"2026-10-0{i}", "status": "open", "priority": "high"} for i in range(1, 5)]

print("== read_all follows LastEvaluatedKey ==")
t = TwoPageTable(ROWS)
got = read_all(t.scan, FilterExpression="x")
check([r["instance_id"] for r in got] == ["db-1", "db-2", "db-3", "db-4"], "both pages are returned")
check(len(t.calls) == 2 and t.calls[1]["FilterExpression"] == "x", "the second call keeps the filter")
empty_first = TwoPageTable([])
empty_first.rows = []
check(read_all(lambda **kw: {"Items": [], "LastEvaluatedKey": {"k": 1}} if "ExclusiveStartKey" not in kw
               else {"Items": [{"id": "late"}]}) == [{"id": "late"}],
      "an empty filtered page that is not the last does not stop the read")


def with_table(registry, table):
    registry._table = table
    return registry


print("")
print("== the registries read every page ==")
reg = with_table(DbInstanceRegistry(), TwoPageTable(ROWS))
check(len(reg.list_instances()) == 4, "DbInstanceRegistry.list_instances (scan)")
reg = with_table(DbInstanceRegistry(), TwoPageTable(ROWS))
check(len(reg.list_instances(account_id="111111111111")) == 4 and reg.table.calls[0]["IndexName"] == "account-index",
      "DbInstanceRegistry.list_instances (account query)")
check(len(with_table(SpacesRegistry(), TwoPageTable(ROWS)).list_spaces()) == 4, "SpacesRegistry.list_spaces")
check(len(with_table(InvestigationsRegistry(), TwoPageTable(ROWS)).list_for_space("sp")) == 4,
      "InvestigationsRegistry.list_for_space")
check(len(with_table(RecommendationsRegistry(), TwoPageTable(ROWS)).list_for_instance("db-1")) == 4,
      "RecommendationsRegistry.list_for_instance")
check(len(with_table(RecommendationsRegistry(), TwoPageTable(ROWS)).list_for_space("sp")) == 4,
      "RecommendationsRegistry.list_for_space")
check(len(with_table(NetworkRegistry(), TwoPageTable(ROWS)).find_by_vpc("vpc-1")) == 4,
      "NetworkRegistry.find_by_vpc")

print("")
print("== discovery checks the ARN index before creating a record ==")
# The discovery body moved to _run_discovery (the route only schedules it).
disc = APP_SRC[APP_SRC.index("def _run_discovery("):]
disc = disc[:disc.index("\nUNSUPPORTED_PER_REGION_MAX")]
create_at = disc.index("db_registry.create_instance(")
guard_at = disc.find("db_registry.find_by_arn(arn)")
check(0 < guard_at < create_at, "find_by_arn runs before create_instance on the candidate path")
# Behaviour, through the same fakes test_global_cluster_block uses: a row the list missed.
sys.path.insert(0, str(ROOT / "backend/tests"))
gcb_src = (ROOT / "backend/tests/test_global_cluster_block.py").read_text(encoding="utf-8")
gcb = types.ModuleType("gcb")  # __name__ is not __main__, so its own tests do not run
gcb.__file__ = str(ROOT / "backend/tests/test_global_cluster_block.py")
exec(compile(gcb_src, gcb.__file__, "exec"), gcb.__dict__)  # noqa: S102


class MissingFromList(gcb._Reg):
    def list_instances(self):
        return []  # the list read missed this row (the bug this guards against)


known = {"instance_id": "db-known", "display_name": "plain", "account_id": gcb.ACCT, "region": "us-east-1",
         "arn": gcb._arn("plain"), "tags": {"arn": gcb._arn("plain")}, "member_space_ids": {"sp-other"}}
orig = gcb._Reg
gcb._Reg = lambda rows=(): MissingFromList(rows)
try:
    out, reg2, _ = gcb._run_discovery([gcb._found("plain")], rows=[known])
finally:
    gcb._Reg = orig
check(reg2.created == [], f"no second record is created ({[r.get('display_name') for r in reg2.created]})")
check(reg2.candidates_added == ["db-known"], "the existing record is proposed to the space instead")


# ── Batch job lookups ────────────────────────────────────────────────────────
def _extract(name, is_async=False):
    kw = "async def" if is_async else "def"
    m = re.search(rf"^{kw} {re.escape(name)}\(.*?(?=^(?:@|def |async def |class |[A-Z_]+ = )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, name
    return m.group(0)


ID = "abcdef12-3333-4444-8555-666666666666"
TWIN = "abcdef12-9999-4999-8999-999999999999"   # same first eight characters


class FakeBatch:
    def __init__(self, jobs):
        self.jobs = jobs  # newest first
        self.calls = []
        self.submitted = []

    def list_jobs(self, **kw):
        self.calls.append(kw)
        name = kw["filters"][0]["values"][0]
        assert "jobStatus" not in kw, "the filtered call needs no status"
        match = [j for j in self.jobs if j["jobName"].lower() == name.lower()]
        if "nextToken" not in kw:
            return {"jobSummaryList": match[:1], "nextToken": "t2"} if len(match) > 1 else {"jobSummaryList": match}
        return {"jobSummaryList": match[1:]}

    def submit_job(self, **kw):
        self.submitted.append(kw)
        return {"jobId": "j-new", "jobName": kw["jobName"]}


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def build(batch):
    ns = {"os": os, "HTTPException": HTTPException, "Request": object,
          "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
          "db_registry": types.SimpleNamespace(get_instance=lambda iid: {"instance_id": iid, "secret_arn": "arn:s"}),
          "_can_view_instance": lambda auth, iid: True, "_get_batch_client": lambda: batch,
          "_get_cross_account_role_arn": lambda inst: ""}
    exec(re.search(r"^_SCAN_ACTIVE_STATUSES = .*$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    for fn in ("_scan_job_name", "_scan_jobs_for", "_scan_job_view", "list_scan_jobs",
               "_submit_scan_job"):
        exec(_extract(fn), ns)  # noqa: S102
    exec(_extract("trigger_scan", is_async=True), ns)  # noqa: S102
    return ns


os.environ.update({"BATCH_JOB_QUEUE": "q", "BATCH_JOB_DEFINITION": "d"})
REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})

print("")
print("== scan jobs: one filtered lookup, every page ==")
jobs = [{"jobId": "j3", "jobName": f"scan-{TWIN}", "status": "RUNNING", "createdAt": 30},
        {"jobId": "j2", "jobName": f"scan-{ID}", "status": "SUCCEEDED", "createdAt": 20},
        {"jobId": "j1", "jobName": f"scan-{ID}", "status": "RUNNING", "createdAt": 10}]
batch = FakeBatch(jobs)
ns = build(batch)
check(ns["_scan_job_name"](ID) == f"scan-{ID}", "the job name carries the full registry id")
try:
    asyncio.run(ns["trigger_scan"](ID, REQ))
    check(False, "a RUNNING job on page 2 blocks a second scan")
except HTTPException as e:
    check(e.status_code == 409 and "already running" in e.detail,
          f"a RUNNING job on page 2 blocks a second scan with its own 409 ({e.status_code}: {e.detail})")
check(not batch.submitted, "nothing is submitted")
check(len(batch.calls) == 2 and batch.calls[1].get("nextToken") == "t2", "the lookup followed nextToken")
check(batch.calls[0]["filters"] == [{"name": "JOB_NAME", "values": [f"scan-{ID}"]}],
      "one JOB_NAME-filtered ListJobs, not a queue listing per status")

hist = ns["list_scan_jobs"](ID, REQ, limit=10)["jobs"]
check([j["job_id"] for j in hist] == ["j2", "j1"], f"history lists both of this database's jobs ({hist})")

print("")
print("== a FAILED job shows the reason the scan recorded, not Batch's 'container exited' ==")
failed_jobs = [{"jobId": "j9", "jobName": f"scan-{ID}", "status": "FAILED", "createdAt": 40,
                "statusReason": "Essential container in task exited"}]
batch = FakeBatch(failed_jobs)
ns = build(batch)
# The record carries last_scan_error keyed by this job id (run_scan.record_scan_failure).
ns["db_registry"] = types.SimpleNamespace(get_instance=lambda iid: {
    "instance_id": iid, "secret_arn": "arn:s",
    "last_scan_error": {"job_id": "j9", "reason": "The deep scan ran, but its result could not be saved.",
                        "at": "2030-01-01T00:00:00Z"}})
exec(_extract("list_scan_jobs"), ns)  # noqa: S102 - rebind with the new registry
view = ns["list_scan_jobs"](ID, REQ)["jobs"][0]
check(view["status_reason"] == "The deep scan ran, but its result could not be saved.",
      f"the recorded reason replaces Batch's own ({view['status_reason']!r})")
# A stale reason from a different job id must not attach to this one.
ns["db_registry"] = types.SimpleNamespace(get_instance=lambda iid: {
    "instance_id": iid, "last_scan_error": {"job_id": "old", "reason": "stale", "at": "x"}})
exec(_extract("list_scan_jobs"), ns)  # noqa: S102
view = ns["list_scan_jobs"](ID, REQ)["jobs"][0]
check(view["status_reason"] == "Essential container in task exited",
      "a reason from a different job id does not attach; Batch's own stands")

print("")
print("== a twin id sharing eight characters never mixes ==")
batch = FakeBatch([{"jobId": "j3", "jobName": f"scan-{TWIN}", "status": "RUNNING", "createdAt": 30}])
ns = build(batch)
res = asyncio.run(ns["trigger_scan"](ID, REQ))
check(res.get("status") == "submitted" and batch.submitted[0]["jobName"] == f"scan-{ID}",
      "the twin's running scan does not block this database, and the job is named by the full id")
check(ns["list_scan_jobs"](ID, REQ)["jobs"] == [], "the twin's job is not in this database's history")

print("")
print("== the scheduled trigger names jobs the same way ==")
tmpl = (ROOT / "backend/template.yaml").read_text(encoding="utf-8")
check('jobName=f"scan-{instance_id}",' in tmpl and "instance_id[:8]" not in tmpl,
      "Scan Trigger Lambda uses scan-<full id>")

print("")
print("== the Tier 1 zip ships what spaces_client imports ==")
build_sh = (ROOT / "backend/scripts/build-tier1.sh").read_text(encoding="utf-8")
dist_sh = (ROOT / "deployment/build-s3-dist.sh").read_text(encoding="utf-8")
staged = set(re.findall(r"tools/aws/(\w+)\.py", build_sh))
for mod in sorted(staged):
    src = (ROOT / "backend/tools/aws" / f"{mod}.py").read_text(encoding="utf-8")
    for dep in re.findall(r"^from tools\.aws\.(\w+) import|^from tools\.aws import (\w+)", src, re.M):
        name = dep[0] or dep[1]
        check(name in staged, f"{mod}.py imports tools.aws.{name}, which build-tier1.sh stages")
check('"backend/tools/aws/ddb_pages.py"' in dist_sh, "build-s3-dist.sh stages ddb_pages.py for Tier 1")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
