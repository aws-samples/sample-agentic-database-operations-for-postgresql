#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The API Gateway stage is created after every route it names.

``AWS::ApiGatewayV2::Stage.RouteSettings`` is keyed by route key, a plain string that
CloudFormation does not read as a dependency, while API Gateway validates each key
against the routes that exist at the moment the stage is created. A stage and a route
that share nothing but the API id may therefore be created in parallel on a fresh stack,
and when the stage lands first the create fails with "Unable to find Route by key" and
the whole stack rolls back. It passes most of the time, which is why it hides until a
customer's first deploy.

Asserted here: ``ApiStage`` carries a ``DependsOn`` naming EVERY ``AWS::ApiGatewayV2::Route``
in the template (not only the ones in ``RouteSettings`` today, so a future per-route
throttle cannot reintroduce the race), every ``RouteSettings`` key is the ``RouteKey`` of
one of those routes, and no route is left out of the list.

Run:  python3 tests/test_api_stage_ordering.py   (from the repository root)
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form tags (!Ref, !Sub, ...)."""


for _tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!If", "!Equals", "!Not", "!And", "!Or",
             "!Select", "!Split", "!FindInMap", "!Base64", "!Cidr", "!ImportValue", "!Condition"):
    _Loader.add_constructor(_tag, lambda loader, node: None)

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


doc = yaml.load((ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8"),  # nosec B506 - SafeLoader subclass
                Loader=_Loader)
resources = doc["Resources"]

routes = {name: res for name, res in resources.items() if res.get("Type") == "AWS::ApiGatewayV2::Route"}
stages = {name: res for name, res in resources.items() if res.get("Type") == "AWS::ApiGatewayV2::Stage"}

print("══ every stage is created after every route ══")
check(len(routes) >= 7, f"{len(routes)} routes found in api.yaml")
check(len(stages) == 1, f"{len(stages)} stage(s) found in api.yaml")
for sname, stage in stages.items():
    depends = stage.get("DependsOn") or []
    depends = [depends] if isinstance(depends, str) else list(depends)
    missing = sorted(set(routes) - set(depends))
    check(not missing, f"{sname} DependsOn lists every route (missing: {missing or 'none'})")
    unknown = sorted(set(depends) - set(resources))
    check(not unknown, f"{sname} DependsOn names only resources that exist (unknown: {unknown or 'none'})")
    check(stage["Properties"].get("AutoDeploy") is True,
          f"{sname} auto-deploys, which is why the ordering matters at create time")

    print("")
    print("══ every RouteSettings key is a declared route ══")
    keys = list((stage["Properties"].get("RouteSettings") or {}).keys())
    route_keys = {r["Properties"].get("RouteKey") for r in routes.values()}
    check(keys, f"{sname} has {len(keys)} per-route setting(s)")
    for k in keys:
        check(k in route_keys, f"RouteSettings key {k!r} is the RouteKey of a declared route")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
