# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Managed web search never breaks a deploy in a Region that cannot have it, and WAF
works in every Region.

Web search is genuinely Region-limited: the agent stack creates an AgentCore Web Search
gateway target whose managed connector exists in us-east-1, eu-west-1 and ap-northeast-1
only, so accepting the on-by-default anywhere else must be skipped, not fail.

WAF is NOT Region-limited. A CLOUDFRONT-scope WebACL can only be CREATED through the
us-east-1 control plane, so a us-east-1 deploy creates it inline (frontend.yaml /
admin-console.yaml) while any other Region gets one from a small us-east-1 edge stack
(edge-waf.yaml) whose ARN the main stack attaches. Either way the distribution is
protected, so WAF defaults on everywhere and is never "skipped in this Region".

Pinned here:
  * the inline WebACL is created only in us-east-1 (and only with the flag on), and the
    distribution otherwise attaches an externally-built ARN;
  * the edge stack always defines the two CLOUDFRONT WebACLs and outputs their ARNs;
  * the Security page reads what was deployed (ENABLE_WAF follows WafActive, now Region
    independent);
  * the web-search gateway is created only in supported Regions; configure.py's web-search
    Region list matches the template, its default is off elsewhere, and --verify says it is
    skipped (a note, not a failure) while WAF is never skipped.

Run:  python3 tests/test_region_gated_features.py
"""
import contextlib
import importlib.util
import io
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


class Tag:
    def __init__(self, name, value):
        self.name, self.value = name, value


class _Loader(yaml.SafeLoader):
    pass


def _construct(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return Tag(suffix, value)


_Loader.add_multi_constructor("!", _construct)


def load(rel):
    return yaml.load((ROOT / rel).read_text(encoding="utf-8"), Loader=_Loader)  # nosec B506 - SafeLoader subclass


def evaluate(expr, params, conditions):
    """Evaluate a CloudFormation condition expression (!And/!Or/!Not/!Equals/!Ref/!Condition)."""
    if isinstance(expr, Tag):
        n, v = expr.name, expr.value
        if n == "Ref":
            return params[v]
        if n == "Equals":
            return evaluate(v[0], params, conditions) == evaluate(v[1], params, conditions)
        if n == "And":
            return all(evaluate(x, params, conditions) for x in v)
        if n == "Or":
            return any(evaluate(x, params, conditions) for x in v)
        if n == "Not":
            return not evaluate(v[0], params, conditions)
        if n == "Condition":
            return evaluate(conditions[v], params, conditions)
        raise AssertionError(f"unhandled tag {n}")
    return expr


def created(doc, resource, params):
    cond_name = doc["Resources"][resource].get("Condition")
    if not cond_name:
        return True
    return bool(evaluate(doc["Conditions"][cond_name], params, doc["Conditions"]))


spec = importlib.util.spec_from_file_location("configure", ROOT / "deployment/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)
SEARCH_REGIONS = configure.REGION_LIMITED["ENABLE_WEB_SEARCH"][0]
UNSUPPORTED = ["eu-west-2", "ap-southeast-2", "us-west-2", "sa-east-1"]

print("== the inline WebACL is created only in us-east-1; elsewhere an ARN is attached ==")
for rel, acl in (("infrastructure/frontend.yaml", "FrontendWebACL"),
                 ("infrastructure/admin-console.yaml", "AdminWebACL")):
    doc = load(rel)
    check(created(doc, acl, {"EnableWAF": "true", "AWS::Region": "us-east-1", "WebAclArn": ""}),
          f"{rel}: inline WebACL created in us-east-1 with EnableWAF=true")
    for region in UNSUPPORTED + ["eu-west-1"]:
        check(not created(doc, acl, {"EnableWAF": "true", "AWS::Region": region, "WebAclArn": "arn:x"}),
              f"{rel}: inline WebACL NOT created in {region} (the edge stack supplies it)")
    check(not created(doc, acl, {"EnableWAF": "false", "AWS::Region": "us-east-1", "WebAclArn": ""}),
          f"{rel}: inline WebACL skipped when EnableWAF=false")
    # The distribution attaches a passed-in ARN wherever one is supplied (non-us-east-1).
    check(evaluate(doc["Conditions"]["ExternalWaf"], {"WebAclArn": "arn:aws:wafv2:...:x"}, doc["Conditions"]),
          f"{rel}: ExternalWaf true when an ARN is passed")
    check(not evaluate(doc["Conditions"]["ExternalWaf"], {"WebAclArn": ""}, doc["Conditions"]),
          f"{rel}: ExternalWaf false when no ARN is passed")

print("")
print("== the us-east-1 edge stack always defines both WebACLs and outputs their ARNs ==")
edge = load("infrastructure/edge-waf.yaml")
for res in ("FrontendWebACL", "AdminWebACL"):
    r = edge["Resources"][res]
    check(r.get("Condition") is None and r["Properties"]["Scope"] == "CLOUDFRONT",
          f"edge-waf.yaml: {res} is an unconditional CLOUDFRONT WebACL")
for out in ("FrontendWebAclArn", "AdminWebAclArn"):
    check(out in edge.get("Outputs", {}), f"edge-waf.yaml outputs {out}")

print("")
print("== the agent stack creates web search only where the connector exists ==")
agent = load("backend/template.yaml")
for res in ("WebSearchGateway", "WebSearchGatewayTarget", "WebSearchGatewayRole"):
    for region in sorted(SEARCH_REGIONS):
        check(created(agent, res, {"EnableWebSearch": "true", "AWS::Region": region}),
              f"{res} created in {region}")
    for region in UNSUPPORTED:
        check(not created(agent, res, {"EnableWebSearch": "true", "AWS::Region": region}),
              f"{res} skipped in {region}")
template_regions = {r for r in ["us-east-1", "eu-west-1", "ap-northeast-1", *UNSUPPORTED]
                    if evaluate(agent["Conditions"]["EnableWebSearchCond"],
                                {"EnableWebSearch": "true", "AWS::Region": r}, agent["Conditions"])}
check(template_regions == SEARCH_REGIONS,
      f"configure.py's web-search Regions match the template ({sorted(template_regions)})")

print("")
print("== the Security page reads the deployed WAF state ==")
api = load("infrastructure/api.yaml")
env = None
for res in api["Resources"].values():
    for c in ((res.get("Properties") or {}).get("ContainerDefinitions") or []):
        for e in c.get("Environment") or []:
            if e.get("Name") == "ENABLE_WAF":
                env = e["Value"]
check(isinstance(env, Tag) and env.name == "If" and env.value[0] == "WafActive",
      "ENABLE_WAF on the API task follows the WafActive condition")
# WAF is now attached in every Region when enabled (inline in us-east-1, edge stack
# elsewhere), so WafActive follows EnableWAF alone — no Region clause.
for region in ("us-east-1", "eu-west-2"):
    check(evaluate(api["Conditions"]["WafActive"], {"EnableWAF": "true", "AWS::Region": region},
                   api["Conditions"]) is True, f"WafActive is True in {region} with EnableWAF=true")
check(evaluate(api["Conditions"]["WafActive"], {"EnableWAF": "false", "AWS::Region": "eu-west-2"},
               api["Conditions"]) is False, "WafActive is False when EnableWAF=false")
check("ENABLE_WAF" not in configure.REGION_LIMITED, "configure.py no longer Region-limits WAF")

print("")
print("== configure.py: off by default where unsupported, and --verify says why ==")
waf, search = configure.OPTIONAL_SETTINGS["ENABLE_WAF"], configure.OPTIONAL_SETTINGS["ENABLE_WEB_SEARCH"]
check(configure.region_default("ENABLE_WAF", waf, "us-east-1") == "true", "WAF defaults on in us-east-1")
check(configure.region_default("ENABLE_WAF", waf, "eu-west-2") == "true",
      "WAF defaults on in eu-west-2 too (no longer Region-limited)")
check(configure.region_default("ENABLE_WEB_SEARCH", search, "eu-west-1") == "true",
      "web search defaults on in eu-west-1")
check(configure.region_default("ENABLE_WEB_SEARCH", search, "eu-west-2") == "false",
      "web search defaults off in eu-west-2")
check(waf["default"] == "true" and search["default"] == "true",
      "the template defaults are unchanged (check-config-defaults keeps them in step)")

values = {k: "x" for k in configure.MANDATORY}
values.update({"AWS_REGION": "eu-west-2", "ENABLE_WAF": "true"})
orig_validators = {k: configure.SETTINGS[k] for k in configure.MANDATORY}
for k in configure.MANDATORY:  # completeness only: the values here are placeholders
    configure.SETTINGS[k] = (orig_validators[k][0], orig_validators[k][1], None)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = configure.verify(values)
text = out.getvalue()
check(rc == 0, "--verify passes: an unsupported feature is skipped, not a failure")
check("ENABLE_WAF" not in text,
      f"--verify never notes WAF as skipped (it works in every Region) ({text.strip()[:120]})")
check("ENABLE_WEB_SEARCH=true is skipped in eu-west-2" in text,
      "--verify names the skipped web search (template default on)")
values.update({"AWS_REGION": "us-east-1"})
out = io.StringIO()
with contextlib.redirect_stdout(out):
    configure.verify(values)
check("skipped" not in out.getvalue(), "no note in us-east-1")
values.update({"AWS_REGION": "eu-west-2", "ENABLE_WAF": "false", "ENABLE_WEB_SEARCH": "false"})
out = io.StringIO()
with contextlib.redirect_stdout(out):
    configure.verify(values)
check("skipped" not in out.getvalue(), "no note once both are set false")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
