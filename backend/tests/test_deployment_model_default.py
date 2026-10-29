#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The deployment's model is the one a fresh install shows and uses.

Two halves:
  1. `make config` proposes the Region's geographic inference profile (eu.* in Europe,
     us.* in the US) when Bedrock lists it, else the global profile.
  2. With no catalog stored yet, the catalog reads with the deployment's model enabled,
     so the admin console shows it enabled and as the default, and requests resolve to
     it. A stored catalog, or a failed read, is returned unchanged.
No AWS: the aws CLI and the config table are stubbed.
"""
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "deployment"))
os.environ.setdefault("AWS_REGION", "eu-west-1")

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# ── 1. make config proposes the geographic profile ───────────────────────────
import configure  # noqa: E402

DEFAULT = configure.OPTIONAL_SETTINGS["AGENT_MODEL_ID"]["default"]
check(DEFAULT == "global.anthropic.claude-haiku-5-5", f"the template default is global Haiku 5.5 ({DEFAULT})")

calls = []


def fake_aws(listed):
    def _aws(*args):
        calls.append(args)
        return listed
    return _aws


EU = "eu.anthropic.claude-haiku-5-5"
US = "us.anthropic.claude-haiku-5-5"
configure.aws = fake_aws(f"{DEFAULT}\t{EU}\teu.anthropic.claude-opus-5-5")
check(configure.model_default("eu-west-1") == EU, "eu-west-1 proposes the EU profile Bedrock lists")
check("--region" in calls[-1] and "eu-west-1" in calls[-1], "and asks Bedrock in that Region")
configure.model_default("eu-central-1", "my-profile")
check(calls[-1][-2:] == ("--profile", "my-profile"), "the shell's profile is passed through")

configure.aws = fake_aws(f"{US}\t{DEFAULT}")
check(configure.model_default("us-east-1") == US, "us-east-1 proposes the US profile")

configure.aws = fake_aws(DEFAULT)
check(configure.model_default("eu-west-1") == DEFAULT, "an EU profile Bedrock does not list falls back to global")

configure.aws = fake_aws("")
check(configure.model_default("eu-west-1") == DEFAULT, "Bedrock unreachable (no credentials) falls back to global")

calls.clear()
configure.aws = fake_aws(EU)
check(configure.model_default("ap-southeast-2") == DEFAULT and not calls,
      "a Region without a geography here proposes global without asking Bedrock")


# ── 2. a fresh catalog starts with the deployment's model enabled ────────────
from tools.aws.governance_store import GovernanceRegistry  # noqa: E402


class Table:
    def __init__(self, item=None, boom=False):
        self.item, self.boom = item, boom

    def get_item(self, Key):
        if self.boom:
            raise RuntimeError("throttled")
        return {"Item": self.item} if self.item else {}


class Registry(GovernanceRegistry):
    def __init__(self, table):
        self._t = table

    @property
    def config_table(self):
        return self._t


os.environ["AGENT_MODEL_ID"] = EU
fresh = Registry(Table()).get_model_catalog()
check(fresh["models"] == {EU: {"enabled": True}}, "no stored catalog: the deployment's model reads as enabled")
check(fresh["account_default_model"] == "", "and no account default is stored on its behalf")

stored = {"config_key": "model_catalog", "account_default_model": "",
          "models": {"eu.anthropic.claude-opus-5-5": {"enabled": True}}}
check(Registry(Table(stored)).get_model_catalog() is stored, "a stored catalog is returned unchanged")
check(Registry(Table(boom=True)).get_model_catalog()["models"] == {}, "a failed read enables nothing")

os.environ["AGENT_MODEL_ID"] = ""
check(Registry(Table()).get_model_catalog()["models"] == {}, "no deployment model set: nothing is enabled")
os.environ["AGENT_MODEL_ID"] = EU

import model_resolver  # noqa: E402

check(model_resolver.account_default_model(fresh) == (EU, "deployment"),
      "the console shows it as the default, sourced from the deployment")

import types  # noqa: E402

gov = types.ModuleType("tools.aws.governance_store")
gov.governance_registry = types.SimpleNamespace(get_model_catalog=lambda: fresh)
real = sys.modules["tools.aws.governance_store"]
sys.modules["tools.aws.governance_store"] = gov
try:
    r = model_resolver.resolve_model()
finally:
    sys.modules["tools.aws.governance_store"] = real
check(r == {"model_id": EU, "source": "deployment"}, f"a request resolves to that same model ({r})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
