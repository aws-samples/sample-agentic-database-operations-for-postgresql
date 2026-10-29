#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""configure.py must never block waiting for an answer nobody will give.

The bug this protects against is a hang, not a wrong answer. deployment/configure.py
prompts for missing settings, and the Makefile guards those prompts with `[ -t 0 ]`.
That guard tests for a terminal, not for a person - and AI coding assistants and several
CI runners allocate a pty. Both the shell test and isatty() then report a terminal while
nobody is there to type, input() blocks, and the run dies at the caller's timeout with
the deploy part-way through and nothing on stdout explaining what it wanted.

So every case here drives the script through a **real pty** and fails on a timeout. A
test that fed it a pipe would pass against the broken version, because a pipe raises
EOFError and the code already handled that.

CONFIG_FILE points at a temp file, so nothing here reads or writes a real deploy.config,
and the result is the same on a fresh clone as on a machine with settings already set.

    python3 tests/test_noninteractive.py      (from the repository root)
"""
import os
import pty
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deployment/configure.py"
TIMEOUT = 20

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def run(args, env_extra, config_body=None, timeout=TIMEOUT):
    """Run configure.py with stdin on a pty. Returns (exit_code_or_None, output).

    A None exit code means it was still waiting when the timeout expired - which is the
    failure this file exists to catch.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "deploy.config"
        if config_body is not None:
            cfg.write_text(config_body, encoding="utf-8")
        env = dict(os.environ)
        env["CONFIG_FILE"] = str(cfg)          # absolute, so it wins over REPO / name
        env.pop("NONINTERACTIVE", None)
        env.pop("CI", None)
        env.update(env_extra)

        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, str(SCRIPT)] + args,
                                cwd=str(REPO), env=env, stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.close(slave)
        try:
            out, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            code = None
        finally:
            try:
                os.close(master)
            except OSError:
                pass
    return code, out.decode(errors="replace")


COMPLETE = """
RESOURCE_PREFIX = dbops
AWS_REGION = us-east-1
STACK_NAME = dbops
ARTIFACTS_BUCKET = dbops-artifacts-123456789012
INITIAL_ADMIN_EMAIL = admin@example.com
"""

INCOMPLETE = """
RESOURCE_PREFIX = dbops
AWS_REGION = us-east-1
STACK_NAME = dbops
ARTIFACTS_BUCKET = dbops-artifacts-123456789012
INITIAL_ADMIN_EMAIL =
"""

print("══ a pty must not be mistaken for a person ══")
for label, env in (("NONINTERACTIVE=1", {"NONINTERACTIVE": "1"}),
                   ("CI=true", {"CI": "true"}),
                   ("CI=1", {"CI": "1"})):
    code, out = run([], env, INCOMPLETE)
    check(code is not None, f"{label}: returns instead of blocking on a pty")
    check(code == 1, f"{label}: exits non-zero when a required setting is missing")

code, out = run(["--non-interactive"], {}, INCOMPLETE)
check(code == 1, "--non-interactive works with no environment variable set")

print("")
print("══ the refusal explains itself ══")
code, out = run([], {"NONINTERACTIVE": "1"}, INCOMPLETE)
check("INITIAL_ADMIN_EMAIL" in out, "names the setting that is missing")
check("cannot prompt" in out, "says why it stopped rather than just failing")
# The error is the documentation: someone who hits this should not need the README.
check("deploy.config" in out, "points at deploy.config as one way to supply it")
check("make deploy INITIAL_ADMIN_EMAIL=" in out,
      "prints a runnable command with the missing key in it")

print("")
print("══ a complete config is not a failure ══")
# Skipping an interactive review is not an error. Returning non-zero here would fail a
# deploy whose settings were already complete.
code, out = run([], {"NONINTERACTIVE": "1"}, COMPLETE)
check(code == 0, "exits 0 when nothing was missing")
check("nothing to prompt for" in out, "says there was nothing to ask")

print("")
print("══ CI=false is not a signal ══")
# A shell that exports CI=false must not be read as "this is CI". This one is expected
# to reach the prompt and block, which is also proof that the pty in this harness really
# does look interactive - without that, every check above would pass trivially.
code, out = run([], {"CI": "false"}, INCOMPLETE, timeout=8)
check(code is None, "CI=false still prompts (so the pty genuinely looks interactive)")
check("Resource prefix" in out or "Deployment settings" in out,
      "and it got as far as asking a question")

print("")
print("══ --confirm declines instead of blocking ══")
# Every caller of --confirm is an optional "would you like to...", so no answer must
# read as no, leaving existing settings alone.
code, out = run(["--confirm", "Change any of the settings above?"],
                {"NONINTERACTIVE": "1"}, COMPLETE)
check(code is not None, "--confirm returns instead of blocking")
check(code == 1, "--confirm reports 'no' when nobody can answer")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
