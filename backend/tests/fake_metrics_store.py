# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""In-memory DynamoDB tables and a CloudWatch client for the metrics-store tests.

``install()`` points ``tools.aws.metrics_store`` at a fresh fake (both tables and the
metrics namespace) and returns it, so a test drives the real store code end to end:
``put_tier1_rows`` / ``publish_tier1_metrics`` write, and the reads see exactly what a
Query or GetMetricData would return — key order, ``between`` ranges, filter
expressions, ``Limit`` paging and per-period statistics. No AWS, no moto.
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TIER1 = "test-tier1-metrics"
TIER2 = "test-tier2-scans"
NAMESPACE = "test/Metrics"


def _eval(cond, item) -> bool:
    """Evaluate a boto3 condition (Key/Attr: eq, between, &) against an item."""
    e = cond.get_expression()
    op, vals = e["operator"], e["values"]
    if op == "AND":
        return _eval(vals[0], item) and _eval(vals[1], item)
    if op == "OR":
        return _eval(vals[0], item) or _eval(vals[1], item)
    name = vals[0].name
    if name not in item:
        return False
    v = item[name]
    if op == "=":
        return v == vals[1]
    if op == "BETWEEN":
        return vals[1] <= v <= vals[2]
    if op == "begins_with":
        return str(v).startswith(vals[1])
    raise NotImplementedError(op)


def _partition(cond):
    """The value a key condition pins the partition (hash) key to."""
    e = cond.get_expression()
    if e["operator"] == "AND":
        return _partition(e["values"][0]) or _partition(e["values"][1])
    if e["operator"] == "=" and e["values"][0].name in ("instance_id", "report_s3_key"):
        return e["values"][1]
    return None


class FakeTable:
    def __init__(self, name, gsi_keys=None):
        self.name = name
        self.items: dict[tuple, dict] = {}
        self.gsi_keys = gsi_keys or {}      # index name -> hash attribute
        self.queries: list[dict] = []
        self.page_size = 1000               # items per Query page before Limit

    def put_item(self, Item):
        self.items[(Item["instance_id"], Item["sk"])] = copy.deepcopy(Item)
        return {}

    def get_item(self, Key):
        it = self.items.get((Key["instance_id"], Key["sk"]))
        return {"Item": copy.deepcopy(it)} if it else {}

    def batch_writer(self, overwrite_by_pkeys=None):
        table = self

        class _W:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def put_item(self, Item):
                table.put_item(Item)
        return _W()

    def query(self, KeyConditionExpression, ScanIndexForward=True, FilterExpression=None,
              Limit=None, Select=None, ExclusiveStartKey=None, IndexName=None):
        self.queries.append({"index": IndexName, "limit": Limit, "filter": FilterExpression is not None,
                             "pk": _partition(KeyConditionExpression)})
        rows = [it for it in self.items.values() if _eval(KeyConditionExpression, it)]
        sort_attr = "sk" if IndexName is None else "report_s3_key"
        rows.sort(key=lambda it: (it.get("instance_id", ""), it.get(sort_attr, "")),
                  reverse=not ScanIndexForward)
        start = 0
        if ExclusiveStartKey:
            k = (ExclusiveStartKey["instance_id"], ExclusiveStartKey["sk"])
            keys = [(r["instance_id"], r["sk"]) for r in rows]
            start = keys.index(k) + 1
        page_cap = min(Limit or self.page_size, self.page_size)
        page = rows[start:start + page_cap]
        out = {}
        evaluated = page
        kept = [r for r in evaluated if FilterExpression is None or _eval(FilterExpression, r)]
        if Select == "COUNT":
            out["Count"] = len(kept)
        else:
            out["Items"] = copy.deepcopy(kept)
            out["Count"] = len(kept)
        if start + page_cap < len(rows):
            last = page[-1]
            out["LastEvaluatedKey"] = {"instance_id": last["instance_id"], "sk": last["sk"]}
        return out


class FakeClient:
    def __init__(self, resource):
        self.resource = resource
        self.batch_calls = 0

    def batch_get_item(self, RequestItems):
        self.batch_calls += 1
        out = {"Responses": {}, "UnprocessedKeys": {}}
        for name, spec in RequestItems.items():
            t = self.resource.Table(name)
            found = []
            for k in spec["Keys"]:
                it = t.items.get((k["instance_id"], k["sk"]))
                if it:
                    found.append(copy.deepcopy(it))
            out["Responses"][name] = found
        return out


class FakeResource:
    def __init__(self):
        self.tables = {TIER1: FakeTable(TIER1), TIER2: FakeTable(TIER2, {"by-report-key": "report_s3_key"})}

        class _Meta:
            pass
        self.meta = _Meta()
        self.meta.client = FakeClient(self)

    def Table(self, name):
        return self.tables.setdefault(name, FakeTable(name))


class FakeCloudWatch:
    def __init__(self):
        self.datums: list[dict] = []
        self.put_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def put_metric_data(self, Namespace, MetricData):
        assert len(MetricData) <= 1000, "PutMetricData takes at most 1000 datums"
        self.put_calls.append({"Namespace": Namespace, "count": len(MetricData)})
        for d in MetricData:
            self.datums.append(dict(d, Namespace=Namespace))
        return {}

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime, ScanBy=None, NextToken=None):
        assert len(MetricDataQueries) <= 500, "GetMetricData takes at most 500 queries"
        self.get_calls.append({"queries": MetricDataQueries, "start": StartTime, "end": EndTime})
        results = []
        for q in MetricDataQueries:
            ms = q["MetricStat"]
            metric, period, stat = ms["Metric"], ms["Period"], ms["Stat"]
            dims = sorted((d["Name"], d["Value"]) for d in metric["Dimensions"])
            buckets: dict[int, list[float]] = {}
            for d in self.datums:
                if d["Namespace"] != metric["Namespace"] or d["MetricName"] != metric["MetricName"]:
                    continue
                if sorted((x["Name"], x["Value"]) for x in d["Dimensions"]) != dims:
                    continue
                ts = d["Timestamp"]
                if not (StartTime <= ts < EndTime):
                    continue
                b = int(ts.timestamp()) // period * period
                buckets.setdefault(b, []).append(float(d["Value"]))
            ts_out, vals = [], []
            for b in sorted(buckets):
                v = buckets[b]
                agg = {"Average": sum(v) / len(v), "Minimum": min(v), "Maximum": max(v),
                       "SampleCount": float(len(v)), "Sum": sum(v)}[stat]
                ts_out.append(datetime.fromtimestamp(b, tz=timezone.utc))
                vals.append(agg)
            results.append({"Id": q["Id"], "Label": q["Label"], "Timestamps": ts_out, "Values": vals})
        return {"MetricDataResults": results}


class Fake:
    def __init__(self, ddb, cw):
        self.ddb, self.cw = ddb, cw

    @property
    def t1(self) -> FakeTable:
        return self.ddb.Table(TIER1)

    @property
    def t2(self) -> FakeTable:
        return self.ddb.Table(TIER2)


def install():
    """A fresh fake behind tools.aws.metrics_store; returns it."""
    os.environ["TIER1_METRICS_TABLE"] = TIER1
    os.environ["TIER2_SCANS_TABLE"] = TIER2
    os.environ["CW_METRICS_NAMESPACE"] = NAMESPACE
    from tools.aws import metrics_store
    ddb, cw = FakeResource(), FakeCloudWatch()
    metrics_store._ddb = ddb
    metrics_store._cw = cw
    return Fake(ddb, cw)
