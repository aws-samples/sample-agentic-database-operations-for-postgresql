#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Walk the deployment settings one at a time, then write them back.

Seeds deploy.config from deploy.config.example on first run, so the file a user ends
up editing carries the same explanatory comments as the committed reference, and every
setting is visible rather than only the ones that got answered.

Values are written back in place: only the value side of a `KEY = value` line changes,
and comments, blank lines and ordering survive. A generated file that discards the
comments would leave the reader with a list of bare keys and no explanation of what
they do, which is the thing the example exists to avoid.

Mandatory settings must hold a value before the deploy is allowed to run. The admin
email is the one with no safe default: Cognito mails the first temporary password
there, so a wrong or empty value produces a deployment that succeeds and that nobody
can sign in to.
"""

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / os.environ.get("CONFIG_FILE", "deploy.config")
EXAMPLE = REPO / "deploy.config.example"

PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# ── environments ─────────────────────────────────────────────────────────────
# One checkout can hold several environments, each a deploy.<name>.config at the repo
# root (dev, uat, env1, ...). The name is free-form within what is safe in a file name;
# the Makefile picks one per run (ENV=<name>, an exported DEPLOY_ENV, or by asking), and
# each environment keeps its own build state under deployment/.state/<name>/, so two
# terminals can deploy two environments at once. A bare deploy.config is still read
# when no environment is chosen, for a checkout that only ever deploys one.
ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
ENV_FILE_RE = re.compile(r"^deploy\.([A-Za-z0-9][A-Za-z0-9_-]{0,39})\.config$")

# The VPC range a new environment proposes: the first /16 in 172.20-172.31 that no
# other saved environment uses, so environments that peer the same database get
# distinct agent CIDRs (a database VPC can route one CIDR to only one peering).
NETWORK_CANDIDATES = [f"172.{n}.0.0/16" for n in range(20, 32)]


def valid_env_name(v: str) -> str:
    if not ENV_NAME_RE.match(v or ""):
        return ("use 1-40 letters, digits, '-' or '_', starting with a letter or digit "
                "(e.g. dev, uat, env1)")
    return ""


def env_configs(root: Path = None) -> dict:
    """``{name: path}`` for every deploy.<name>.config in ``root`` (the repo root)."""
    root = root or REPO
    out = {}
    for p in sorted(root.glob("deploy.*.config")):
        m = ENV_FILE_RE.match(p.name)
        if m:
            out[m.group(1)] = p
    return out


def env_of(path: Path) -> str:
    """The environment a config file belongs to, or '' for a bare deploy.config."""
    m = ENV_FILE_RE.match(path.name)
    return m.group(1) if m else ""


def other_configs(this: Path, root: Path = None) -> list:
    """Every OTHER saved config in the checkout (environments and a bare deploy.config),
    as ``[(label, values)]``."""
    root = root or REPO
    paths = list(env_configs(root).values())
    bare = root / "deploy.config"
    if bare.exists():
        paths.append(bare)
    out = []
    for p in paths:
        if p.resolve() == this.resolve():
            continue
        try:
            values = parse(p.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
        out.append((env_of(p) or p.name, values))
    return out


def subnets_for(vpc_cidr: str) -> dict:
    """The four subnet ranges for a 172.N.0.0/16, laid out like the default."""
    net = ipaddress.IPv4Network(vpc_cidr, strict=False)
    a, b = str(net.network_address).split(".")[:2]
    return {"PUBLIC_SUBNET1_CIDR": f"{a}.{b}.1.0/24", "PUBLIC_SUBNET2_CIDR": f"{a}.{b}.2.0/24",
            "PRIVATE_SUBNET1_CIDR": f"{a}.{b}.10.0/24", "PRIVATE_SUBNET2_CIDR": f"{a}.{b}.20.0/24"}


def pick_network(used: list) -> dict:
    """The VPC and subnet ranges for a new environment: the first candidate /16 that
    overlaps none of ``used`` (the other environments' VPC_CIDR values). ``{}`` when all
    twelve are taken, so the caller keeps the default and warns."""
    taken = []
    for c in used:
        try:
            taken.append(ipaddress.IPv4Network(c, strict=False))
        except ValueError:
            continue
    for cand in NETWORK_CANDIDATES:
        net = ipaddress.IPv4Network(cand)
        if not any(net.overlaps(t) for t in taken):
            return {"VPC_CIDR": cand, **subnets_for(cand)}
    return {}


def stack_clashes(values: dict, others: list) -> list:
    """Other environments that would deploy the SAME stack: same account, region and
    stack name (or resource prefix). Both would update one CloudFormation stack, so the
    second deploy silently rewrites the first environment. Environments with no account
    recorded yet are skipped - there is nothing to compare."""
    acct = (values.get("ACCOUNT_ID") or "").strip()
    if not acct:
        return []
    region = region_of(values)
    prefix = (values.get("RESOURCE_PREFIX") or "dbops").strip()
    stack = (values.get("STACK_NAME") or prefix).strip()
    out = []
    for label, v in others:
        if (v.get("ACCOUNT_ID") or "").strip() != acct:
            continue
        if (v.get("AWS_REGION") or "us-east-1").strip() != region:
            continue
        o_prefix = (v.get("RESOURCE_PREFIX") or "dbops").strip()
        o_stack = (v.get("STACK_NAME") or o_prefix).strip()
        if o_stack == stack or o_prefix == prefix:
            out.append(f"{label} (stack {o_stack}, prefix {o_prefix})")
    return out


class _Tty:
    """The controlling terminal, for a prompt made from inside make's $(shell), whose
    stdout is captured: questions go to the terminal, only the answer to stdout.

    Two handles, not one: a terminal is not seekable, and Python refuses to open it
    read+write in text mode ("File or stream is not seekable")."""

    def __init__(self):
        self._in = open("/dev/tty", "r", encoding="utf-8")
        try:
            self._out = open("/dev/tty", "w", encoding="utf-8")
        except OSError:
            self._in.close()
            raise

    def write(self, s):
        self._out.write(s)

    def flush(self):
        self._out.flush()

    def readline(self):
        self._out.flush()
        return self._in.readline()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._out.flush()
        self._out.close()
        self._in.close()


def _tty():
    try:
        return _Tty()
    except OSError:
        return None


def choose_env() -> int:
    """Ask which environment this run is for; print the chosen name on stdout.

    Called by the Makefile at parse time when no environment was given, so every
    question and message goes to the terminal, never to stdout."""
    envs = env_configs()
    # Ask only when the run is attended: not suppressed, and make's own stdin (inherited
    # by $(shell)) is a terminal. A script piping into make, or running it with stdin
    # from /dev/null, must fail with how to name one rather than block on /dev/tty.
    tty = _tty() if interactive() else None
    if tty is None:
        names = ", ".join(envs) or "none yet"
        print("❌ No deployment environment was given, and this run cannot ask for one.",
              file=sys.stderr)
        print(f"   Saved environments: {names}", file=sys.stderr)
        print("   Name one per run:      make deploy ENV=<name>", file=sys.stderr)
        print("   or pin this terminal:  export DEPLOY_ENV=<name>", file=sys.stderr)
        return 1
    with tty:
        tty.write("\n🌐 Deployment environment\n")
        tty.write("   A name for this set of settings and its deployment, e.g. dev, uat,\n")
        tty.write("   env1. Each one is saved as deploy.<name>.config and remembered.\n")
        if envs:
            tty.write("\n   Saved:\n")
            for name, path in envs.items():
                v = parse(path.read_text(encoding="utf-8").splitlines())
                where = " · ".join(x for x in (v.get("ACCOUNT_ID", ""), v.get("AWS_REGION", ""),
                                               v.get("STACK_NAME", "")) if x)
                tty.write(f"     {name:<16} {where}\n")
        default = next(iter(envs)) if len(envs) == 1 else ""
        while True:
            shown = f" [{default}]" if default else ""
            tty.write(f"\n   Environment name{shown}: ")
            tty.flush()
            line = tty.readline()
            if not line:
                tty.write("\n")
                return 1
            name = line.strip() or default
            problem = valid_env_name(name)
            if not problem:
                break
            tty.write(f"      {problem}\n")
        if name not in envs:
            tty.write(f"   New environment - its settings will be saved to deploy.{name}.config\n")
        tty.write("\n")
    print(name)
    return 0


def list_envs() -> int:
    """`make envs`: every saved environment, for choosing one or patching several."""
    envs = env_configs()
    current = env_of(CONFIG)
    if not envs:
        print("No saved environments yet. Create one with: make deploy ENV=<name>")
        return 0
    print(f"   {'ENVIRONMENT':<18}{'ACCOUNT':<15}{'REGION':<15}{'STACK':<14}VPC CIDR")
    for name, path in envs.items():
        v = parse(path.read_text(encoding="utf-8").splitlines())
        mark = "*" if name == current else " "
        print(f" {mark} {name:<18}{v.get('ACCOUNT_ID', '') or '-':<15}{v.get('AWS_REGION', '') or '-':<15}"
              f"{v.get('STACK_NAME', '') or '-':<14}{v.get('VPC_CIDR', '') or '-'}")
    print("")
    print("   Deploy or update one:   make deploy ENV=<name>")
    print("   Pin a terminal to one:  export DEPLOY_ENV=<name>")
    return 0


def check_account() -> int:
    """Refuse when the shell's credentials are for a different account than this
    environment was created in. Records the account on first use.

    The shell still decides the account (AWS_PROFILE); this only catches a mismatch -
    ENV=prod run from a terminal holding dev credentials would otherwise stage into,
    push images to, or deploy over the wrong account."""
    if not CONFIG.exists():
        return 0
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    values = parse(lines)
    profile = os.environ.get("AWS_PROFILE", "").strip()
    actual = account_for(profile)
    if not actual:
        return 0   # no usable credentials: the steps that follow report that themselves
    pinned = (values.get("ACCOUNT_ID") or "").strip()
    if not pinned:
        values["ACCOUNT_ID"] = actual
        write_back(lines, values)
        print(f"   {CONFIG.name}: this environment is now pinned to account {actual}.")
        return 0
    if pinned == actual:
        return 0
    label = env_of(CONFIG) or CONFIG.name
    src = f"profile {profile}" if profile else "the default credential chain"
    print("", file=sys.stderr)
    print(f"❌ Environment '{label}' belongs to account {pinned}, but {src}", file=sys.stderr)
    print(f"   resolves to account {actual}. Nothing was run.", file=sys.stderr)
    print("", file=sys.stderr)
    print("   Point this terminal at the right account (export AWS_PROFILE=<name>), or", file=sys.stderr)
    print(f"   pick the environment for {actual}: make envs", file=sys.stderr)
    return 1


def aws(*args: str) -> str:
    """Run an aws CLI command, returning stdout stripped, or '' on any failure."""
    try:
        out = subprocess.run(
            ["aws", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


# CloudFormation parameter -> deploy.config key, for reading settings back off a
# deployed stack. Only the ones an operator supplied are listed: the rest are either
# derived or left at template defaults.
CFN_TO_CONFIG = {
    "ResourcePrefix": "RESOURCE_PREFIX",
    "InitialAdminEmail": "INITIAL_ADMIN_EMAIL",
    "InitialAdminName": "INITIAL_ADMIN_NAME",
    "NotificationEmail": "NOTIFICATION_EMAIL",
    "EmailSenderAddress": "EMAIL_SENDER_ADDRESS",
    "EmailSenderName": "EMAIL_SENDER_NAME",
}


def account_for(profile: str) -> str:
    env_arg = ["--profile", profile] if profile else []
    acct = aws("sts", "get-caller-identity", "--query", "Account", "--output", "text", *env_arg)
    return "" if acct in ("", "None") else acct


def deployed_parameters(stack: str, region: str, profile: str) -> dict:
    """Parameter values of an existing stack, keyed by config key. {} if no stack.

    This is what makes updating a deployment work without re-supplying anything: the
    values were already given once, and CloudFormation kept them. Without it, a lost
    or fresh deploy.config turns an update into an interrogation - and worse, a blank
    answer would be passed as an empty override and overwrite what is deployed.
    """
    args = ["cloudformation", "describe-stacks", "--stack-name", stack,
            "--query", "Stacks[0].Parameters", "--output", "json"]
    if region:
        args += ["--region", region]
    if profile:
        args += ["--profile", profile]
    raw = aws(*args)
    if not raw:
        return {}
    try:
        params = json.loads(raw) or []
    except (json.JSONDecodeError, TypeError):
        return {}

    mapping = dict(CFN_TO_CONFIG)
    mapping.update({spec["cfn"]: key for key, spec in TEMPLATE_SETTINGS.items()})

    found = {}
    for p in params:
        key = mapping.get(p.get("ParameterKey", ""))
        value = (p.get("ParameterValue") or "").strip()
        if key and value:
            found[key] = value
    return found


def adopt(values: dict, lines: list, quiet: bool = False) -> list:
    """Fill blank settings from the deployed stack. Returns the keys adopted.

    Only fills what is missing, so anything already in deploy.config - or overridden on
    the command line - still wins. Existing values are never overwritten from the stack,
    because a local edit is the operator saying what they want to change.
    """
    stack = values.get("STACK_NAME") or values.get("RESOURCE_PREFIX") or "dbops"
    region = values.get("AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")
    # The shell decides the account - deploy.config holds no profile. See the Makefile.
    profile = os.environ.get("AWS_PROFILE", "")

    deployed = deployed_parameters(stack, region, profile)
    if not deployed:
        return []

    adopted = [k for k, v in deployed.items() if not values.get(k, "").strip()]
    for key in adopted:
        values[key] = deployed[key]

    if adopted:
        write_back(lines, values)
        if not quiet:
            print(f"   Found the deployed stack '{stack}' and took {len(adopted)} setting(s)")
            print("   from it, so this is an update rather than a new install:")
            for key in adopted:
                print(f"     {key} = {values[key]}")
            print("")
    return adopted


def reconcile_live_defaults(values: dict, lines: list, quiet: bool = False) -> list:
    """Refresh settings whose source of truth is a live AWS resource from that value.

    Unlike adopt (which only fills blanks off the deployed stack), this deliberately
    overwrites the stored value for settings carrying a `live_default` — the cost budget
    a super admin edits in the console. Without it, a non-interactive redeploy would pass
    the stale deploy.config amount and reset the budget the admin had changed.
    """
    changed = []
    for key, spec in TEMPLATE_SETTINGS.items():
        fn = spec.get("live_default")
        if not fn:
            continue
        live = fn(values)
        if live and live != (values.get(key) or "").strip():
            values[key] = live
            changed.append(key)
    if changed:
        write_back(lines, values)
        if not quiet:
            for key in changed:
                print(f"   Reconciled {key} from the live account: {values[key]}")
    return changed


# ── validators ───────────────────────────────────────────────────────────────
def valid_email(v: str) -> str:
    if not EMAIL_RE.match(v):
        return "that does not look like an email address"
    return ""


def valid_prefix(v: str) -> str:
    if not PREFIX_RE.match(v):
        return ("must be 2-21 characters, lowercase letters, digits and hyphens, "
                "starting with a letter")
    return ""


def valid_region(v: str) -> str:
    if not re.match(r"^[a-z]{2}-[a-z]+-\d$", v):
        return "does not look like an AWS region, e.g. us-east-1"
    return ""


def valid_optional_email(v: str) -> str:
    if v and not EMAIL_RE.match(v):
        return "that does not look like an email address (leave empty to skip)"
    return ""


def valid_cidr(min_prefix: int = 16, max_prefix: int = 24):
    """Build a validator for an RFC 1918 CIDR block within a prefix-length range.

    A VPC accepts /16 to /28, but this deployment needs room for four subnets plus the
    interface endpoints, so the VPC itself is held to /24 or wider. Subnets go to /28.
    """
    def check(v: str) -> str:
        try:
            net = ipaddress.IPv4Network(v, strict=True)
        except ValueError as exc:
            # strict=True rejects a host bit set, which is the common typo: 172.20.0.1/16.
            return f"not a valid IPv4 CIDR block ({exc})"
        if not (min_prefix <= net.prefixlen <= max_prefix):
            return f"prefix must be between /{min_prefix} and /{max_prefix}"
        if not net.is_private:
            return "must be a private range (RFC 1918): 10/8, 172.16/12 or 192.168/16"
        return ""
    return check


def subnets_inside_vpc(values: dict) -> list:
    """Return a message per subnet that does not sit inside the VPC CIDR.

    This cannot be a field validator because it compares five settings. It matters more
    than it looks: CloudFormation rejects a subnet outside its VPC range, so getting this
    wrong turns a deploy into a rollback twenty minutes in, and the error names the
    subnet resource rather than the setting that caused it.
    """
    vpc_raw = (values.get("VPC_CIDR") or "").strip()
    if not vpc_raw:
        return []
    try:
        vpc = ipaddress.IPv4Network(vpc_raw, strict=True)
    except ValueError:
        return []  # the field validator already reported this

    problems = []
    for key in ("PUBLIC_SUBNET1_CIDR", "PUBLIC_SUBNET2_CIDR",
                "PRIVATE_SUBNET1_CIDR", "PRIVATE_SUBNET2_CIDR"):
        raw = (values.get(key) or "").strip()
        if not raw:
            continue
        try:
            net = ipaddress.IPv4Network(raw, strict=True)
        except ValueError:
            continue
        if not net.subnet_of(vpc):
            problems.append(f"{key} ({net}) is not inside VPC_CIDR ({vpc})")
    return problems


def overlapping_vpcs(cidr: str, region: str, profile: str) -> list:
    """VPCs already in the account whose range overlaps `cidr`.

    Overlap is legal as long as the two VPCs never peer, so this warns rather than
    blocks. It is worth checking because avoiding a clash is the only reason to change
    the range, and CloudFormation will not say a word: peering fails later, separately.
    """
    try:
        want = ipaddress.IPv4Network(cidr, strict=True)
    except ValueError:
        return []
    raw = aws("ec2", "describe-vpcs", "--query",
              "Vpcs[].{id:VpcId,cidr:CidrBlock,name:Tags[?Key=='Name']|[0].Value}",
              "--output", "json", "--region", region,
              *(["--profile", profile] if profile else []))
    try:
        vpcs = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []

    clashes = []
    for v in vpcs or []:
        try:
            other = ipaddress.IPv4Network(v.get("cidr") or "", strict=False)
        except ValueError:
            continue
        if want.overlaps(other):
            label = v.get("name") or v.get("id") or "?"
            clashes.append(f"{other} ({label})")
    return clashes


# key -> (prompt, mandatory, validator)
#
# No AWS_PROFILE here on purpose. Which account to deploy into is ambient session
# context taken from the shell, not a setting stored in the repo - see the Makefile.
SETTINGS = {
    "RESOURCE_PREFIX": ("Resource prefix for every resource name", True, valid_prefix),
    "AWS_REGION": ("AWS region", True, valid_region),
    "STACK_NAME": ("CloudFormation stack name", True, None),
    "ARTIFACTS_BUCKET": ("S3 bucket for staged templates and Lambda archives", True, None),
    "INITIAL_ADMIN_EMAIL": ("Initial admin email - the temporary password is sent here",
                            True, valid_email),
    "INITIAL_ADMIN_NAME": ("Initial admin display name", False, None),
    "NOTIFICATION_EMAIL": ("Alert email for critical findings (optional)",
                           False, valid_optional_email),
    "EMAIL_SENDER_ADDRESS": ("Invitation sender email, must be a verified SES identity "
                             "(optional)", False, valid_optional_email),
    "EMAIL_SENDER_NAME": ("Invitation sender display name", False, None),
}

# Settings the deploy cannot proceed without. Checked by --verify.
MANDATORY = [k for k, (_, req, _) in SETTINGS.items() if req]


def one_of(*allowed):
    def check(v: str) -> str:
        if v not in allowed:
            return f"must be one of: {', '.join(allowed)}"
        return ""
    return check


def number_between(low: float, high: float):
    def check(v: str) -> str:
        try:
            n = float(v)
        except ValueError:
            return "must be a number"
        if not low <= n <= high:
            return f"must be between {low} and {high}"
        return ""
    return check


def whole_number_between(low: int, high: int):
    """number_between that also refuses a fraction. S3 DefaultRetention.Days and
    lifecycle days reject one mid-update, which would roll the stack update back."""
    in_range = number_between(low, high)

    def check(v: str) -> str:
        problem = in_range(v)
        if problem:
            return problem
        if not float(v).is_integer():
            return "must be a whole number"
        return ""
    return check


def valid_port_range(v: str) -> str:
    """`from-to`, both TCP ports (1-65535), from <= to. 5432-5432 is a single port."""
    m = re.fullmatch(r"(\d{1,5})-(\d{1,5})", (v or "").strip())
    if not m:
        return "must be from-to, e.g. 5432-5432 or 5432-5440"
    low, high = int(m.group(1)), int(m.group(2))
    if not (1 <= low <= 65535 and 1 <= high <= 65535):
        return "ports must be between 1 and 65535"
    if low > high:
        return "the first port must not be greater than the second"
    return ""


def valid_model_id(v: str) -> str:
    if not v or " " in v:
        return "must be a Bedrock model ID, e.g. global.anthropic.claude-haiku-5-5"
    return ""


def live_budget_amount(values: dict) -> str:
    """The current limit of this deployment's cost budget, read live from the account,
    or '' if it does not exist yet or cannot be read.

    The budget's amount is authoritative in AWS Budgets — a super admin changes it from
    the admin console after deploy. Reading it here lets the deploy seed its default from
    what the console last set, so a redeploy converges to that value instead of resetting
    it to a stale stored one. First install (no budget yet) falls back to the default.
    """
    profile = os.environ.get("AWS_PROFILE", "").strip()
    account = account_for(profile)
    if not account:
        return ""
    prefix = (values.get("RESOURCE_PREFIX") or "dbops").strip()
    region = region_of(values)
    amount = aws("budgets", "describe-budget",
                 "--account-id", account,
                 "--budget-name", f"{prefix}-monthly-cost-{region}",
                 "--query", "Budget.BudgetLimit.Amount", "--output", "text",
                 *(["--profile", profile] if profile else []))
    if not amount or amount == "None":
        return ""
    try:
        f = float(amount)
    except ValueError:
        return ""
    return str(int(f)) if f.is_integer() else str(f)


def live_alarm_threshold(alarm_suffix: str):
    """Build a live_default reader for a guardrail alarm's current Threshold.

    Like live_budget_amount, this lets a redeploy seed the parameter from what the console
    last set (a super admin retunes the alarm after deploy) rather than resetting it to a
    stale stored value. Returns '' when unauthenticated, when the alarm does not exist yet
    (first install), or when it cannot be read.
    """
    def _read(values: dict) -> str:
        profile = os.environ.get("AWS_PROFILE", "").strip()
        if not account_for(profile):          # cheap "are we authenticated" gate
            return ""
        prefix = (values.get("RESOURCE_PREFIX") or "dbops").strip()
        thr = aws("cloudwatch", "describe-alarms",
                  "--alarm-names", f"{prefix}-{alarm_suffix}",
                  "--query", "MetricAlarms[0].Threshold", "--output", "text",
                  *(["--profile", profile] if profile else []))
        if not thr or thr == "None":
            return ""
        try:
            f = float(thr)
        except ValueError:
            return ""
        return str(int(f)) if f.is_integer() else str(f)
    return _read


def live_audit_vault(values: dict) -> str:
    """The admin audit vault switch a super admin last set in the console, as 'on' /
    'off', or '' when it was never set, cannot be read, or we are unauthenticated.

    Like the budget, the console value is the source of truth after deploy, so a
    redeploy keeps it instead of resetting it to a stale deploy.config value.
    """
    profile = os.environ.get("AWS_PROFILE", "").strip()
    if not account_for(profile):              # cheap "are we authenticated" gate
        return ""
    prefix = (values.get("RESOURCE_PREFIX") or "dbops").strip()
    region = (values.get("AWS_REGION") or "").strip()
    raw = aws("dynamodb", "get-item", "--table-name", f"{prefix}-config",
              "--key", '{"config_key":{"S":"audit_settings"}}',
              "--query", "Item.vault_enabled.BOOL", "--output", "text",
              *(["--region", region] if region else []),
              *(["--profile", profile] if profile else []))
    if raw == "True":
        return "on"
    if raw == "False":
        return "off"
    return ""


# Template behaviour that master.yaml exposes as parameters and the deploy was silently
# leaving at CloudFormation's defaults - WAF, CloudTrail, the guardrail settings, the
# Tier 1 retention, the model. They are surfaced here so they are visible and changeable without
# reading a template, and passed through by deploy-stack.
#
# config key -> (CloudFormation parameter, prompt, default, validator)
#
# The defaults MUST match master.yaml's, or a deploy through the Makefile would behave
# differently from the same template deployed through the console. That is a real drift
# risk, since the values now live in two files - deployment/check-config-defaults.py
# fails the build if they diverge.
BOOL = ["true", "false"]
STRENGTH = ["NONE", "LOW", "MEDIUM", "HIGH"]

OPTIONAL_SETTINGS = {
    "ENABLE_WAF": {
        "cfn": "EnableWAF",
        "prompt": "Attach AWS WAF to both CloudFront distributions",
        "default": "true",
        "choices": BOOL,
        "note": "recommended; two WebACLs add a per-WebACL and per-request charge (see "
                "Indicative costs in the README), set false to skip",
        "validator": one_of(*BOOL),
    },
    "ENABLE_CLOUDTRAIL": {
        "cfn": "EnableCloudTrail",
        "prompt": "Create a CloudTrail scoped to this deployment",
        "default": "true",
        "choices": BOOL,
        "note": "recommended; costs extra if the account already has an org trail",
        "validator": one_of(*BOOL),
    },
    "JWT_SIGNATURE_VERIFICATION": {
        "cfn": "JwtSignatureVerification",
        "prompt": "In-app JWT signature verification (off / log / enforce)",
        "default": "enforce",
        "choices": ["off", "log", "enforce"],
        "note": "enforce recommended; re-verifies each bearer against Cognito JWKS on top of "
                "the edge authorizer, fails open on a JWKS outage",
        "validator": one_of("off", "log", "enforce"),
    },
    "DB_PORT_RANGE": {
        "cfn": "DbPortRange",
        "prompt": "PostgreSQL port range the daily deep scan may reach (from-to)",
        "default": "5432-5432",
        "note": "the scan task's security group allows egress to these ports only; widen it "
                "(e.g. 5432-5440) when a monitored database listens on another port. Configure "
                "warns when a database's port is outside the range",
        "validator": valid_port_range,
    },
    "SAME_REGION_CONNECTIVITY": {
        "cfn": "SameRegionConnectivity",
        "prompt": "Same-region database connectivity (peering-first / lattice)",
        "default": "peering-first",
        "choices": ["peering-first", "lattice"],
        "note": "peering-first peers each database VPC and uses a VPC Lattice resource endpoint "
                "only when that VPC's address range is already routed to another peered VPC; "
                "lattice uses a resource endpoint for every same-region database so no route is "
                "ever added to a database VPC; it adds a small hourly charge per database; see Indicative costs in the README",
        "validator": one_of("peering-first", "lattice"),
    },
    "AUDIT_VAULT": {
        "cfn": "AuditVault",
        "prompt": "Admin audit vault (on / off)",
        "default": "on",
        "choices": ["on", "off"],
        "note": "on writes every admin action to a locked S3 bucket that nobody can alter or "
                "delete before its retention ends; a super admin can switch it in Settings > "
                "Audit, and a redeploy keeps that value. Trials and test stacks set off",
        "validator": one_of("on", "off"),
        # Source of truth is the console switch, read back like the budget.
        "live_default": live_audit_vault,
    },
    "AUDIT_RETENTION_DAYS": {
        "cfn": "AuditRetentionDays",
        "prompt": "Minimum days each admin audit record is locked (S3 Object Lock)",
        "default": "365",
        "note": "the floor: a super admin can raise retention in the console, never below this. "
                "Applies to new records; an existing record's lock is never shortened. 365 suits "
                "most audits; regulated estates often keep 2555 (7 years). Adds a small S3 "
                "storage charge; see Indicative costs in the README",
        "validator": whole_number_between(1, 3653),
        # Deliberately no live_default: this is the floor. Reading a console-raised value
        # back would ratchet the floor (and the bucket default) up on every redeploy.
    },
    "ENABLE_COGNITO_THREAT_PROTECTION": {
        "cfn": "EnableCognitoThreatProtection",
        "prompt": "Cognito advanced security (compromised-credential / adaptive auth)",
        "default": "false",
        "choices": BOOL,
        "note": "off by default; requires the Cognito Plus tier, billed per monthly active user",
        "validator": one_of(*BOOL),
    },
    "ENABLE_GUARDRAIL": {
        "cfn": "EnableGuardrail",
        "prompt": "Bedrock guardrail on every model call",
        "default": "true",
        "choices": BOOL,
        "note": "recommended",
        "validator": one_of(*BOOL),
    },
    "GUARDRAIL_CONTENT_STRENGTH": {
        "cfn": "GuardrailContentStrength",
        "prompt": "Guardrail content-filter strength",
        "default": "LOW",
        "choices": STRENGTH,
        "validator": one_of(*STRENGTH),
    },
    "GUARDRAIL_PROMPT_ATTACK_STRENGTH": {
        "cfn": "GuardrailPromptAttackStrength",
        "prompt": "Guardrail prompt-attack detection strength",
        "default": "MEDIUM",
        "choices": STRENGTH,
        "validator": one_of(*STRENGTH),
    },
    "GUARDRAIL_PII_ACTION": {
        "cfn": "GuardrailPiiAction",
        "prompt": "Action for PII found in model output",
        "default": "ANONYMIZE",
        "choices": ["ANONYMIZE", "BLOCK"],
        "validator": one_of("ANONYMIZE", "BLOCK"),
    },
    "ENABLE_WEB_SEARCH": {
        "cfn": "EnableWebSearch",
        "prompt": "Managed AgentCore web search for the research agent",
        "default": "true",
        "choices": BOOL,
        "validator": one_of(*BOOL),
    },
    "AGENT_MODEL_ID": {
        "cfn": "AgentModelId",
        "prompt": "Bedrock model ID for the agent",
        "default": "global.anthropic.claude-haiku-5-5",
        "note": "must be enabled for your account in this region",
        "validator": valid_model_id,
    },
    "TIER1_RETENTION_DAYS": {
        "cfn": "Tier1RetentionDays",
        "prompt": "Days to keep the Tier 1 raw 5-minute samples",
        "default": "30",
        "note": "the detail table only; the series stays 15 months in CloudWatch",
        "validator": number_between(1, 365),
    },
    "LOG_RETENTION_DAYS": {
        "cfn": "LogRetentionDays",
        "prompt": "Retention in days for flow logs, API logs and the log archive",
        "default": "90",
        "validator": number_between(1, 3653),
    },
    "REFRESH_TOKEN_VALIDITY_DAYS": {
        "cfn": "RefreshTokenValidityDays",
        "prompt": "Refresh token lifetime in days (how long before a user must re-login)",
        "default": "7",
        "note": "shorter is safer; logout and a privilege change revoke immediately regardless",
        "validator": number_between(1, 3650),
    },
    "BUDGET_LIMIT_AMOUNT": {
        "cfn": "BudgetLimitAmount",
        "prompt": "Monthly cost budget in USD (alert-only; emails at 80% and 100%)",
        "default": "100",
        "note": "a super admin can change it later in the admin console; a redeploy keeps that value",
        "validator": number_between(1, 1000000),
        # Source of truth is the live budget (super admin edits it in the console), so the
        # default is reconciled from the account rather than fixed — see live_budget_amount.
        "live_default": live_budget_amount,
    },
    "GUARDRAIL_THROTTLE_ALARM_THRESHOLD": {
        "cfn": "GuardrailThrottleAlarmThreshold",
        "prompt": "Guardrail throttle alarm threshold (alert-only; notifies, never blocks)",
        "default": "5",
        "note": "a super admin can retune it later in the admin console; a redeploy keeps that value",
        "validator": number_between(1, 100000),
        # Source of truth is the live alarm (super admin edits it in the console).
        "live_default": live_alarm_threshold("guardrail-applyguardrail-throttled"),
    },
    "GUARDRAIL_INTERVENTION_ALARM_THRESHOLD": {
        "cfn": "GuardrailInterventionAlarmThreshold",
        "prompt": "Guardrail intervention alarm threshold (alert-only; notifies, never blocks)",
        "default": "25",
        "note": "a super admin can retune it later in the admin console; a redeploy keeps that value",
        "validator": number_between(1, 100000),
        "live_default": live_alarm_threshold("guardrail-interventions-high"),
    },
}

# Network addressing, kept separate from OPTIONAL_SETTINGS so it can be its own section
# with its own gate. Almost nobody needs to change these, and the one reason to do so -
# a clash with a VPC you intend to peer with - has nothing to do with security posture or
# capacity, so grouping them together would bury it.
#
# All five are declared in master.yaml and passed down to vpc.yaml, and api.yaml's
# security-group rules reference the same VPC range by parameter. One definition matters
# here: a security group left trusting a stale range produces a deployment that comes up
# clean and then cannot reach itself.
NETWORK_SETTINGS = {
    "VPC_CIDR": {
        "cfn": "VpcCidr",
        "prompt": "CIDR block for the VPC this deployment creates",
        "default": "172.20.0.0/16",
        "note": "change only if it clashes with a VPC you will peer with",
        "validator": valid_cidr(16, 24),
    },
    "PUBLIC_SUBNET1_CIDR": {
        "cfn": "PublicSubnet1Cidr",
        "prompt": "Public subnet, first availability zone",
        "default": "172.20.1.0/24",
        "note": "must be inside the VPC CIDR",
        "validator": valid_cidr(16, 28),
    },
    "PUBLIC_SUBNET2_CIDR": {
        "cfn": "PublicSubnet2Cidr",
        "prompt": "Public subnet, second availability zone",
        "default": "172.20.2.0/24",
        "note": "must be inside the VPC CIDR",
        "validator": valid_cidr(16, 28),
    },
    "PRIVATE_SUBNET1_CIDR": {
        "cfn": "PrivateSubnet1Cidr",
        "prompt": "Private subnet, first availability zone",
        "default": "172.20.10.0/24",
        "note": "the API and agent runtime live here",
        "validator": valid_cidr(16, 28),
    },
    "PRIVATE_SUBNET2_CIDR": {
        "cfn": "PrivateSubnet2Cidr",
        "prompt": "Private subnet, second availability zone",
        "default": "172.20.20.0/24",
        "note": "the API and agent runtime live here",
        "validator": valid_cidr(16, 28),
    },
}

# Features that exist only in some Regions: key -> (Regions, why). The templates skip them
# anywhere else, so a deploy there still succeeds (frontend.yaml / admin-console.yaml
# WafEnabled, backend/template.yaml EnableWebSearchCond); here their default is off in
# those Regions and the walk, --verify and --report say why. tests/test_region_gated_features.py
# keeps the Region lists and the templates in step.
# WAF is NOT listed here: it works in every Region. A CloudFront WebACL can only be
# created through the us-east-1 control plane, but `make deploy` handles that by building
# it in a small us-east-1 edge stack for non-us-east-1 deployments (see edge-waf.yaml),
# so WAF defaults on everywhere and is never "skipped in this Region".
REGION_LIMITED = {
    "ENABLE_WEB_SEARCH": ({"us-east-1", "eu-west-1", "ap-northeast-1"},
                          "AgentCore Web Search is available in us-east-1, eu-west-1 "
                          "and ap-northeast-1 only"),
}
# Every setting that maps to a master.yaml parameter. Used wherever the distinction
# between the two groups does not matter: reporting, adoption, and the check that no
# parameter is left silently at its default.
TEMPLATE_SETTINGS = {**OPTIONAL_SETTINGS, **NETWORK_SETTINGS}


def read_lines() -> list:
    """Current config lines, seeding from the example on first run."""
    if not CONFIG.exists():
        if EXAMPLE.exists():
            shutil.copyfile(EXAMPLE, CONFIG)
            print(f"   Created {CONFIG.name} from {EXAMPLE.name}")
        else:
            CONFIG.write_text(
                "# Deployment settings. Plain make syntax: KEY = value.\n", encoding="utf-8"
            )
        seed_network()
    return CONFIG.read_text(encoding="utf-8").splitlines()


def seed_network() -> None:
    """Give a NEW config a VPC range no other saved config in this checkout uses.

    Environments that peer the same database need distinct agent CIDRs: the database
    VPC's route table holds one route per destination, so a second deployment on the
    same range cannot reach it. Only an untouched, newly seeded file is changed; a range
    someone chose is never rewritten."""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    values = parse(lines)
    used = [v.get("VPC_CIDR", "") for _, v in other_configs(CONFIG) if v.get("VPC_CIDR")]
    if not used:
        return
    net = pick_network(used)
    if not net:
        print("   ⚠️  Every 172.20-172.31 /16 is already used by another saved config;")
        print("      keeping the default range. Pick one in 'make config' if this")
        print("      deployment will peer a database another one already reaches.")
        return
    if net["VPC_CIDR"] == values.get("VPC_CIDR"):
        return
    values.update(net)
    write_back(lines, values)
    print(f"   Network range {net['VPC_CIDR']}: the next one free across your saved configs,")
    print("   so this deployment can reach a database another one already peers with.")


def parse(lines: list) -> dict:
    values = {}
    for line in lines:
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


# Keys that are not settings. Dropped whenever the file is rewritten, because an inert
# line is worse than no line: it still reads as configuration. AWS_PROFILE is listed
# because the shell owns the profile (see the Makefile), so a copy saved here would look
# authoritative while being ignored — and could point a command at the wrong account.
RETIRED_KEYS = {"AWS_PROFILE"}


def write_back(lines: list, values: dict) -> None:
    """Replace value sides in place; append any key the file does not already carry."""
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^(\s*)([A-Z_][A-Z0-9_]*)(\s*)=\s*(.*?)\s*$", line)
        if m and m.group(2) in RETIRED_KEYS:
            continue
        if m and m.group(2) in values:
            key = m.group(2)
            seen.add(key)
            out.append(f"{m.group(1)}{key} = {values[key]}".rstrip())
        else:
            out.append(line)
    missing = [k for k in values if k not in seen]
    if missing:
        out.append("")
        for k in missing:
            out.append(f"{k} = {values[k]}".rstrip())
    CONFIG.write_text("\n".join(out) + "\n", encoding="utf-8")


# Typing "no" where a template wants "false" is an easy mistake, and CloudFormation
# rejects it against AllowedValues rather than interpreting it. The intent is not
# ambiguous, so map the obvious synonyms instead of failing on them.
BOOL_SYNONYMS = {
    "yes": "true", "y": "true", "on": "true", "enable": "true", "enabled": "true", "1": "true",
    "no": "false", "n": "false", "off": "false", "disable": "false", "disabled": "false",
    "0": "false",
}

# Everything that counts as "yes" at a [y/N] prompt. Derived from the same table as the
# true/false settings, so the two vocabularies cannot drift apart: a user who has just
# been taught to answer 'true' must not have it silently read as "no" one prompt later.
AFFIRMATIVE = {k for k, v in BOOL_SYNONYMS.items() if v == "true"} | {"true"}

# Answers that mean "no" when they appear in NONINTERACTIVE or CI, so that CI=false
# does not read as "yes, this is CI".
NEGATIVE_ENV = {"0", "false", "no", "off"}


def non_interactive_requested() -> bool:
    """True when the caller has explicitly said no one will answer a prompt.

    This is the signal that matters, and the reason it has to exist. A terminal check
    cannot tell a person from a program: AI coding assistants and several CI runners
    allocate a pty, so both `[ -t 0 ]` in the Makefile and isatty() here report a
    terminal while nobody is there to type. The prompt then blocks until the caller's
    own timeout kills it, with the deploy part-way through and nothing printed to say
    what it was waiting for.

    Deliberately does not consider isatty(), because piped input is a real answer:
    `printf 'y\\n' | configure.py --confirm "…"` must read the y rather than assume
    silence. An exhausted pipe is already handled where the reads happen, by EOFError.
    """
    if "--non-interactive" in sys.argv[1:]:
        return True
    for var in ("NONINTERACTIVE", "CI"):
        value = os.environ.get(var, "").strip().lower()
        if value and value not in NEGATIVE_ENV:
            return True
    return False


def interactive() -> bool:
    """True when a prompt could be answered: not suppressed, and attached to a terminal.

    Used to decide whether to start the settings walk at all. Stricter than
    non_interactive_requested() because a walk reached with a plain redirect and no
    answers would ask its first question, hit EOF, and stop with a message about one
    field - where refusing up front can list every missing setting and how to supply it.
    """
    return not non_interactive_requested() and sys.stdin.isatty()


def refuse_to_prompt(values: dict) -> int:
    """Explain how to supply the settings without a prompt, then fail.

    What this replaces is a hang, so it names the settings that are missing and the two
    ways to provide them. The error is the documentation: a caller that hits this learns
    the non-interactive path from the message, without having to find it in the README.
    """
    missing = [key for key in MANDATORY if not values.get(key, "").strip()]

    # Nothing is missing, so there was nothing to ask. Walking the settings is a review,
    # and skipping a review is not a failure - returning non-zero here would fail a
    # deploy whose settings were already complete.
    if not missing:
        print("")
        print("⚙️  Every required setting is already set; nothing to prompt for.")
        print("   Run 'make config' from a terminal to review them one at a time.")
        return 0

    print("", file=sys.stderr)
    print("❌ Deployment settings are incomplete, and this run cannot prompt for them.",
          file=sys.stderr)
    print("   (no terminal, or --non-interactive / CI / NONINTERACTIVE is set)",
          file=sys.stderr)
    print("", file=sys.stderr)
    print("   Missing or invalid:", file=sys.stderr)
    for key in missing:
        print(f"     {key}  — {SETTINGS[key][0]}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"   Set them in {CONFIG.name}:", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"     cp deploy.config.example {CONFIG.name}", file=sys.stderr)
    for key in missing:
        print(f"     # then set {key}", file=sys.stderr)
    print("", file=sys.stderr)
    print("   or pass them per-run, which needs no file at all:", file=sys.stderr)
    print("", file=sys.stderr)
    args = " ".join(f"{key}=<value>" for key in missing)
    print(f"     make deploy {args}", file=sys.stderr)
    print("", file=sys.stderr)
    return 1


def confirm(question: str) -> bool:
    """Ask a yes/no question. Used by every [y/N] prompt, here and in the Makefile.

    One implementation on purpose. The Makefile had its own `case "$ans" in [Yy]*`,
    which accepted a different set of answers from this file - so the same words meant
    different things depending on which prompt you were looking at.

    Destructive actions deliberately do NOT use this: `make destroy` and
    `make purge-orphans` make you type the stack name or prefix, because a single
    keystroke should not be able to delete a deployment.
    """
    # Suppressed, so take the default rather than blocking on input(). Every caller of
    # this is an optional "would you like to...", so declining is the safe reading - it
    # leaves the existing settings alone. Note this asks whether prompting was turned
    # off, not whether a terminal exists: a piped answer still has to be read.
    if non_interactive_requested():
        return False
    try:
        answer = input(f"   {question} [y/N]: ").strip().lower()
    except EOFError:
        # No input to read: treat as the default, which is no.
        print("")
        return False
    return answer in AFFIRMATIVE


def normalise(value: str, choices) -> str:
    """Map a near-miss answer onto one of the accepted values."""
    if not choices:
        return value
    for choice in choices:
        if value.lower() == choice.lower():
            return choice  # fixes case, e.g. 'high' -> 'HIGH'
    if set(choices) == set(BOOL) and value.lower() in BOOL_SYNONYMS:
        return BOOL_SYNONYMS[value.lower()]
    return value


def ask(prompt: str, current: str, mandatory: bool, validator, choices=None, note="") -> str:
    """Prompt once per setting, offering the current value. Re-asks until acceptable."""
    if choices:
        prompt = f"{prompt} ({'/'.join(choices)})"
    if note:
        prompt = f"{prompt} — {note}"
    # Backstop. main() refuses before reaching the walk, so this only fires if some
    # other caller reaches a prompt while prompting is suppressed. Failing here beats
    # blocking on input() until a timeout kills the run. Piped answers are still read,
    # so this checks the explicit signal rather than whether a terminal is attached.
    if non_interactive_requested():
        if mandatory and not current:
            print(f"❌ {prompt} is required, and this run cannot prompt for it.",
                  file=sys.stderr)
            sys.exit(1)
        return current
    while True:
        shown = f" [{current}]" if current else ""
        suffix = "" if current or not mandatory else " (required)"
        try:
            answer = input(f"   {prompt}{shown}{suffix}: ").strip()
        except EOFError:
            # Input is exhausted - piped in, or the terminal closed. Treating that as
            # "empty" would re-ask a required field forever, so stop instead of looping.
            print("")
            if mandatory and not current:
                print(f"❌ {prompt} is required, and there is no more input to read.",
                      file=sys.stderr)
                sys.exit(1)
            return current
        value = normalise(answer or current, choices)
        if mandatory and not value:
            print("      This one is required.")
            continue
        if validator and value:
            problem = validator(value)
            if problem:
                print(f"      {problem}")
                continue
        return value


def region_of(values: dict) -> str:
    return (values.get("AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")).strip()


def region_note(key: str, region: str) -> str:
    """Why `key` cannot take effect in `region` ("" when it can)."""
    limit = REGION_LIMITED.get(key)
    if not limit or region in limit[0]:
        return ""
    return f"skipped in {region}: {limit[1]}"


def region_default(key: str, spec: dict, region: str) -> str:
    """The setting's default in `region`: off where the feature cannot be deployed."""
    return "false" if region_note(key, region) else spec["default"]


# Geographic inference profiles the agent model proposes, by the Region's leading part:
# eu.* in a European Region and us.* in a US one keep model calls inside that geography.
# Every other Region proposes the global profile.
MODEL_GEOS = {"eu": "eu", "us": "us"}


def model_default(region: str, profile: str = "") -> str:
    """The agent model to propose in `region`: the default model on this Region's
    geographic profile when Bedrock lists that profile here, else the global default.
    Also the global default when Bedrock cannot be asked (no credentials, no CLI)."""
    base = OPTIONAL_SETTINGS["AGENT_MODEL_ID"]["default"]
    geo = MODEL_GEOS.get(region.split("-", 1)[0])
    if not geo or not base.startswith("global."):
        return base
    candidate = f"{geo}.{base[len('global.'):]}"
    listed = aws("bedrock", "list-inference-profiles", "--region", region,
                 "--query", "inferenceProfileSummaries[].inferenceProfileId",
                 "--output", "text", *(["--profile", profile] if profile else []))
    return candidate if candidate in listed.split() else base


def region_notes(values: dict) -> list:
    """`(key, note)` for each Region-limited feature that is on (set, or by the template
    default) in a Region where the templates skip it."""
    region = region_of(values)
    out = []
    for key in REGION_LIMITED:
        effective = (values.get(key) or "").strip() or OPTIONAL_SETTINGS[key]["default"]
        note = region_note(key, region)
        if effective == "true" and note:
            out.append((key, note))
    return out


def verify(values: dict) -> int:
    """Non-interactive completeness check, for `make deploy` to gate on."""
    # Not a failure: the templates skip these features in this Region, so the deploy
    # succeeds without them. Said here so nobody believes they are on.
    for key, note in region_notes(values):
        print(f"ℹ️  {key}=true is {note}. The deploy goes ahead without it; set {key}=false "
              f"in deploy.config to stop this note.")
    missing = []
    for key in MANDATORY:
        value = values.get(key, "").strip()
        if not value:
            missing.append(key)
            continue
        validator = SETTINGS[key][2]
        if validator and validator(value):
            missing.append(key)
    if missing:
        print("", file=sys.stderr)
        print("❌ These required deployment settings are missing or invalid:", file=sys.stderr)
        for key in missing:
            print(f"     {key}", file=sys.stderr)
        print("", file=sys.stderr)
        print("   Run 'make config' to set them.", file=sys.stderr)
        return 1
    # Two saved environments that resolve to one stack (same account, region and stack
    # name or prefix) would both update it: the second deploy rewrites the first.
    clashes = stack_clashes(values, other_configs(CONFIG))
    if clashes:
        print("", file=sys.stderr)
        print(f"❌ {CONFIG.name} would deploy the same stack as another saved environment:",
              file=sys.stderr)
        for c in clashes:
            print(f"     {c}", file=sys.stderr)
        print("", file=sys.stderr)
        print("   Same account and region, same stack name or resource prefix. Give this one",
              file=sys.stderr)
        print("   its own RESOURCE_PREFIX and STACK_NAME (make config), or deploy it to",
              file=sys.stderr)
        print("   another account or region.", file=sys.stderr)
        return 1
    return 0


def report_optional(values: dict) -> int:
    """Print the effective template settings, marking which are not in the file.

    Read from the file rather than the environment: these are twelve values and
    threading them all through the Makefile only to print them would mean a second
    place that has to list them. A setting absent from deploy.config is not an error -
    CloudFormation applies the template default - but it is worth saying so, because
    otherwise the only way to know what a deploy will use is to read a template.
    """
    # Compare against what is actually deployed. Adoption only fills blanks, so a
    # setting already present in deploy.config silently wins over the running stack -
    # and for WAF, CloudTrail or the guardrail that means an update can change the
    # security posture without anyone asking for it. Say so before it happens.
    stack = values.get("STACK_NAME") or values.get("RESOURCE_PREFIX") or "dbops"
    deployed = deployed_parameters(
        stack,
        values.get("AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1"),
        os.environ.get("AWS_PROFILE", ""),
    )

    missing = 0
    changes = []
    for key, spec in TEMPLATE_SETTINGS.items():
        set_value = values.get(key, "").strip()
        effective = set_value or spec["default"]
        suffix = "" if set_value else "  (template default)"
        if not set_value:
            missing += 1

        live = deployed.get(key, "")
        if live and live != effective:
            suffix += f"   ← deployed: {live}"
            changes.append((key, live, effective))
        rnote = region_note(key, region_of(values))
        if rnote and effective == "true":
            suffix += f"   ({rnote})"
        print(f"   {key:<34} {effective}{suffix}")

    if missing:
        print("")
        print(f"   {missing} of {len(TEMPLATE_SETTINGS)} are not in {CONFIG.name}, so the template's")
        print("   default applies. Run 'make config' to write them in and change them.")

    if changes:
        print("")
        print(f"⚠️  {len(changes)} setting(s) differ from the deployed stack '{stack}'.")
        print("   Deploying would change them:")
        for key, live, wanted in changes:
            print(f"     {key}: {live} -> {wanted}")
        print("")
        print("   If that is not what you want, edit deploy.config (or run 'make config')")
        print("   to match the deployed value before deploying.")
    return 0


def main() -> int:
    args = sys.argv[1:]

    # Environments (see "environments" at the top).
    if "--choose-env" in args:
        return choose_env()
    if "--envs" in args:
        return list_envs()
    if "--check-account" in args:
        return check_account()

    # Exposed so the Makefile asks yes/no questions through the same code path rather
    # than reimplementing the answer matching in shell.
    if "--confirm" in args:
        question = args[args.index("--confirm") + 1] if len(args) > args.index("--confirm") + 1 else "Continue?"
        return 0 if confirm(question) else 1

    if "--report" in args:
        return report_optional(parse(read_lines() if CONFIG.exists() else []))

    # Adopt the deployed stack's settings into any blanks, then carry on. Run before
    # both the settings check and the deploy, so updating an existing stack needs no
    # input at all - the answers were given once and CloudFormation still has them.
    if "--adopt" in args:
        lines = read_lines()
        values = parse(lines)
        adopt(values, lines, quiet="--quiet" in args)
        # Non-interactive deploys skip the walk, so converge live_default settings here
        # too — otherwise a redeploy would reset a console-edited budget to a stale value.
        reconcile_live_defaults(values, lines, quiet="--quiet" in args)
        return 0

    if "--verify" in args:
        # Read the file directly rather than trusting the environment: this runs as a
        # gate right before a deploy, and make parses deploy.config once at startup, so
        # an in-flight edit would not be reflected in its variables.
        return verify(parse(read_lines() if CONFIG.exists() else []))

    lines = read_lines()
    values = parse(lines)

    # Refuse up front rather than part-way through the walk. Reaching the first prompt
    # and stopping there would already have written nothing and explained nothing.
    if not interactive():
        return refuse_to_prompt(values)

    print("")
    print("⚙️  Deployment settings — press Enter to keep the value in brackets.")
    print("")

    # Which account: detected and reported, never asked for and never saved.
    #
    # The profile comes from the shell (`export AWS_PROFILE=<name>`) or the default
    # credential chain, exactly as it does for the aws CLI. Nothing is written here, so
    # there is no stored value that could contradict the shell on a later command. The
    # account is still resolved up front because the default bucket name embeds it.
    profile = os.environ.get("AWS_PROFILE", "").strip()
    source = f"profile {profile}" if profile else "default credential chain"
    account = account_for(profile)
    label = env_of(CONFIG)
    if label:
        print(f"   Environment '{label}' ({CONFIG.name}).")
    pinned = (values.get("ACCOUNT_ID") or "").strip()
    if account and pinned and pinned != account:
        print("", file=sys.stderr)
        print(f"❌ {CONFIG.name} belongs to account {pinned}, but the {source} resolves",
              file=sys.stderr)
        print(f"   to {account}. Point this terminal at {pinned} (export AWS_PROFILE=<name>),",
              file=sys.stderr)
        print("   or choose the environment for this account (make envs). Nothing was saved.",
              file=sys.stderr)
        return 1
    if account:
        # Recorded, not used to select: the shell still decides the account. It lets a
        # later run refuse a terminal holding another account's credentials.
        values["ACCOUNT_ID"] = account
        print(f"   Deploying into account {account}, via the {source}.")
        print("   Export a different AWS_PROFILE to switch accounts; this environment remembers")
        print("   its account and refuses a deploy from any other.")
    else:
        print(f"   ⚠️  The {source} does not resolve to an account.")
        print("      Point the shell at one (export AWS_PROFILE=<name>, or aws configure) and")
        print("      re-run. The questions below still work, but the deploy will fail.")
    print("")

    # Now that the profile is known, offer the deployed stack's values as defaults, so
    # an update starts from what is running rather than from an empty file.
    adopt(values, lines)

    for key, (prompt, mandatory, validator) in SETTINGS.items():
        current = values.get(key, "")
        # Defaults that depend on earlier answers, applied only when nothing is set.
        if not current:
            if key == "STACK_NAME":
                current = values.get("RESOURCE_PREFIX", "")
            elif key == "ARTIFACTS_BUCKET" and account:
                # Must match the Makefile's derived default,
                # <prefix>-artifacts-<account>-<region>: an S3 bucket lives in one region
                # and a deploy reads its templates from the bucket in its own region, so
                # the name carries the region to stay unique across regions of one account.
                current = f"{values.get('RESOURCE_PREFIX', 'dbops')}-artifacts-{account}-{region_of(values)}"
            elif key == "INITIAL_ADMIN_NAME":
                current = "Administrator"
        # Skip the sender display name unless a sender address was actually given.
        if key == "EMAIL_SENDER_NAME" and not values.get("EMAIL_SENDER_ADDRESS"):
            values[key] = ""
            continue
        values[key] = ask(prompt, current, mandatory, validator)

    # Template behaviour: security, guardrails, capacity, model. Defaults are sensible,
    # so this is one question rather than twelve - but the values are always written to
    # the file, so they stay visible and editable even when the walk is skipped.
    print("")
    print("   Security, guardrails and capacity default to the recommended posture:")
    print("   WAF and a scoped CloudTrail on, Bedrock guardrail on, Tier 1 samples kept 30 days,")
    print("   logs kept 90 days. WAF and CloudTrail carry a monthly cost - turn them")
    print("   off here if your account already covers them.")
    review = confirm("Review those too?")

    for key, spec in OPTIONAL_SETTINGS.items():
        # A live_default setting takes its default from the account (e.g. the cost budget
        # a super admin edited), so a redeploy converges to that rather than a stale value.
        live = spec["live_default"](values) if spec.get("live_default") else ""
        # A feature this Region cannot deploy defaults to off here (the template skips it).
        current = live or values.get(key) or region_default(key, spec, region_of(values))
        # A model still on the global default (as a new config copies it from the
        # example) moves to this Region's geographic profile where Bedrock offers one.
        if key == "AGENT_MODEL_ID" and current == spec["default"]:
            current = model_default(region_of(values), os.environ.get("AWS_PROFILE", "").strip())
        rnote = region_note(key, region_of(values))
        if review:
            note = "; ".join(n for n in (spec.get("note", ""), rnote) if n)
            values[key] = ask(spec["prompt"], current, True, spec["validator"],
                              spec.get("choices"), note)
        else:
            values[key] = current
        if rnote and values[key] == "true":
            print(f"   ℹ️  {key}=true is {rnote}; the deploy goes ahead without it.")

    # Network addressing, its own section because the reason to change it has nothing to do
    # with the settings above. Shown before the question, so the answer is informed: an
    # operator can only know whether the default clashes if they can see what it is.
    print("")
    print("   Network addressing for the VPC this deployment creates:")
    print("")
    for key, spec in NETWORK_SETTINGS.items():
        print(f"     {values.get(key) or spec['default']:<18} {spec['prompt']}")
    print("")
    print("   The default suits almost every account. The one reason to change it is an")
    print("   overlap with a VPC you intend to peer with, because peering rejects")
    print("   overlapping ranges.")

    region = values.get("AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")
    clashes = overlapping_vpcs(values.get("VPC_CIDR") or NETWORK_SETTINGS["VPC_CIDR"]["default"],
                               region, profile)
    if clashes:
        print("")
        print(f"   ⚠️  Already in this account ({region}), overlapping that range:")
        for c in clashes:
            print(f"        {c}")
        print("      Fine if these will never peer with the new VPC. If they might,")
        print("      change the range now.")

    if confirm("Change the network addressing?"):
        while True:
            for key, spec in NETWORK_SETTINGS.items():
                current = values.get(key) or spec["default"]
                values[key] = ask(spec["prompt"], current, True, spec["validator"],
                                  spec.get("choices"), spec.get("note", ""))
            # Cross-field: a subnet outside the VPC range is rejected by CloudFormation
            # twenty minutes into a deploy, naming the subnet rather than the setting.
            problems = subnets_inside_vpc(values)
            if not problems:
                break
            print("")
            for msg in problems:
                print(f"   ❌ {msg}")
            print("      A subnet must sit inside the VPC range. Let's go round again.")
            print("")
    else:
        for key, spec in NETWORK_SETTINGS.items():
            values[key] = values.get(key) or spec["default"]

    others = other_configs(CONFIG)
    same_range = [label for label, v in others
                  if v.get("VPC_CIDR") and _overlaps(v["VPC_CIDR"], values.get("VPC_CIDR", ""))]
    if same_range:
        print("")
        print(f"   ⚠️  {values.get('VPC_CIDR')} overlaps another saved environment: {', '.join(same_range)}.")
        print("      Fine unless both will reach the same database; a database VPC can route")
        print("      one agent range to only one peering.")

    write_back(lines, values)
    print("")
    print(f"✅ Saved to {CONFIG.name}")
    clashes = stack_clashes(values, others)
    if clashes:
        print("")
        print("⚠️  Same account, region and stack as another saved environment:")
        for c in clashes:
            print(f"     {c}")
        print("   The deploy refuses this. Re-run 'make config' with its own RESOURCE_PREFIX")
        print("   and STACK_NAME.")
    return 0


def _overlaps(a: str, b: str) -> bool:
    try:
        return ipaddress.IPv4Network(a, strict=False).overlaps(ipaddress.IPv4Network(b, strict=False))
    except ValueError:
        return False


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C at a prompt is a normal way to back out, not a crash. A traceback here
        # buries the one useful fact: nothing was written.
        print("")
        print("   Cancelled - no settings were saved.")
        sys.exit(130)
