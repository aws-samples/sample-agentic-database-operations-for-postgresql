# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""One checkout, many environments, deployed at the same time from different terminals.

Pinned here:
  * an environment is a free-form name (letters, digits, '-', '_'), its settings in
    deploy.<name>.config and its build state in deployment/.state/<name>/;
  * the Makefile resolves the environment from ENV on the command line, an exported
    DEPLOY_ENV, or by asking - never from a shell's own ENV variable - and refuses a
    name that is not safe as a file name;
  * with no environment named, a goal that needs one asks (or, with no terminal, fails
    with how to name one); a bare deploy.config or a CONFIG_FILE on the command line
    keeps the single-environment behaviour;
  * a new environment proposes a VPC range no other saved config uses;
  * two environments that resolve to one stack (same account, region, stack/prefix)
    are refused; an environment refuses credentials for another account;
  * each build script writes its state under DEPLOY_STATE_DIR, and the web-app build,
    which shares node_modules, runs one environment at a time.

Run:  python3 tests/test_deploy_envs.py
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

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


spec = importlib.util.spec_from_file_location("configure", ROOT / "deployment/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


def write_cfg(root, name, **kv):
    p = pathlib.Path(root) / (f"deploy.{name}.config" if name else "deploy.config")
    p.write_text("".join(f"{k} = {v}\n" for k, v in kv.items()), encoding="utf-8")
    return p


print("== environment names ==")
for good in ("dev", "uat", "env1", "SIT", "dbopsdev-euw1", "a_b", "x" * 40):
    check(configure.valid_env_name(good) == "", f"{good[:20]!r} is accepted")
for bad in ("", "-dev", "_x", "a b", "../x", "a/b", "a.b", "x" * 41, "dév"):
    check(configure.valid_env_name(bad) != "", f"{bad[:20]!r} is refused")

print("")
print("== saved environments are discovered by file name ==")
with tempfile.TemporaryDirectory() as tmp:
    write_cfg(tmp, "dev", AWS_REGION="eu-west-1")
    write_cfg(tmp, "uat", AWS_REGION="us-east-1")
    write_cfg(tmp, "", AWS_REGION="us-east-1")                      # bare deploy.config
    (pathlib.Path(tmp) / "deploy.config.example").write_text("x = 1\n")
    (pathlib.Path(tmp) / "deploy.bad name.config").write_text("x = 1\n")
    envs = configure.env_configs(pathlib.Path(tmp))
    check(sorted(envs) == ["dev", "uat"], f"deploy.<name>.config only ({sorted(envs)})")
    check(configure.env_of(pathlib.Path(tmp) / "deploy.dev.config") == "dev", "env_of reads the name")
    check(configure.env_of(pathlib.Path(tmp) / "deploy.config") == "", "a bare deploy.config has no name")
    others = configure.other_configs(envs["dev"], pathlib.Path(tmp))
    check(sorted(label for label, _ in others) == ["deploy.config", "uat"],
          "the other configs exclude the file itself and include a bare deploy.config")

print("")
print("== a new environment proposes a free network range ==")
check(configure.pick_network([])["VPC_CIDR"] == "172.20.0.0/16", "nothing used: the default 172.20")
n = configure.pick_network(["172.20.0.0/16"])
check(n["VPC_CIDR"] == "172.21.0.0/16" and n["PUBLIC_SUBNET1_CIDR"] == "172.21.1.0/24"
      and n["PRIVATE_SUBNET2_CIDR"] == "172.21.20.0/24", f"172.20 used: 172.21 with matching subnets ({n})")
check(configure.pick_network(["172.20.0.0/16", "172.22.0.0/16"])["VPC_CIDR"] == "172.21.0.0/16",
      "the first gap is taken")
check(configure.pick_network(["172.16.0.0/12"]) == {}, "a /12 covering all twelve: nothing proposed")
check(configure.pick_network(["10.0.0.0/16", "not a cidr"])["VPC_CIDR"] == "172.20.0.0/16",
      "unrelated and malformed ranges are ignored")
for cand in configure.NETWORK_CANDIDATES:
    vals = {"VPC_CIDR": cand, **configure.subnets_for(cand)}
    if configure.subnets_inside_vpc(vals):
        check(False, f"{cand}: derived subnets sit inside the VPC")
        break
else:
    check(True, "every candidate's derived subnets sit inside its VPC")

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    shutil.copyfile(ROOT / "deploy.config.example", root / "deploy.config.example")
    write_cfg(tmp, "dev", VPC_CIDR="172.20.0.0/16")
    write_cfg(tmp, "uat", VPC_CIDR="172.21.0.0/16")
    saved = (configure.REPO, configure.CONFIG, configure.EXAMPLE)
    configure.REPO, configure.EXAMPLE = root, root / "deploy.config.example"
    configure.CONFIG = root / "deploy.sit.config"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            vals = configure.parse(configure.read_lines())
        check(vals.get("VPC_CIDR") == "172.22.0.0/16" and vals.get("PRIVATE_SUBNET1_CIDR") == "172.22.10.0/24",
              f"a newly created deploy.sit.config is seeded with 172.22 ({vals.get('VPC_CIDR')})")
        check(vals.get("ACCOUNT_ID", "x") == "", "it starts with no account pinned")
        # An existing file is never re-seeded, whatever its range.
        configure.CONFIG = root / "deploy.dev.config"
        with contextlib.redirect_stdout(io.StringIO()):
            vals = configure.parse(configure.read_lines())
        check(vals.get("VPC_CIDR") == "172.20.0.0/16", "an existing config keeps the range it has")
    finally:
        configure.REPO, configure.CONFIG, configure.EXAMPLE = saved

print("")
print("== two environments that resolve to one stack are refused ==")
base = {"ACCOUNT_ID": "111111111111", "AWS_REGION": "eu-west-1", "RESOURCE_PREFIX": "dbops", "STACK_NAME": "dbops"}
check(configure.stack_clashes(base, [("uat", dict(base))]) != [], "same account, region, stack: a clash")
check(configure.stack_clashes(base, [("uat", {**base, "ACCOUNT_ID": "222222222222"})]) == [],
      "another account: no clash")
check(configure.stack_clashes(base, [("uat", {**base, "AWS_REGION": "us-east-1"})]) == [],
      "another region: no clash")
check(configure.stack_clashes(base, [("uat", {**base, "RESOURCE_PREFIX": "acme", "STACK_NAME": "acme"})]) == [],
      "another prefix and stack: no clash")
check(configure.stack_clashes(base, [("uat", {**base, "STACK_NAME": "other"})]) != [],
      "same prefix, other stack name: still a clash (every resource name collides)")
check(configure.stack_clashes({**base, "ACCOUNT_ID": ""}, [("uat", dict(base))]) == [],
      "no account recorded yet: nothing to compare")

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    common = dict(INITIAL_ADMIN_EMAIL="a@example.com", ARTIFACTS_BUCKET="b", **base)
    write_cfg(tmp, "dev", **common)
    write_cfg(tmp, "uat", **common)
    saved = (configure.REPO, configure.CONFIG)
    configure.REPO, configure.CONFIG = root, root / "deploy.uat.config"
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = configure.verify(configure.parse(configure.CONFIG.read_text().splitlines()))
        check(rc == 1 and "same stack as another saved environment" in err.getvalue() and "dev" in err.getvalue(),
              "--verify refuses it and names the other environment")
    finally:
        configure.REPO, configure.CONFIG = saved

print("")
print("== an environment refuses credentials for another account ==")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    saved = (configure.CONFIG, configure.account_for)
    try:
        configure.CONFIG = write_cfg(tmp, "prod", ACCOUNT_ID="", AWS_REGION="eu-west-1")
        configure.account_for = lambda profile: "111111111111"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = configure.check_account()
        pinned = configure.parse(configure.CONFIG.read_text().splitlines()).get("ACCOUNT_ID")
        check(rc == 0 and pinned == "111111111111", "first run: the account is recorded")
        with contextlib.redirect_stdout(io.StringIO()):
            check(configure.check_account() == 0, "same account: allowed")
        configure.account_for = lambda profile: "222222222222"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = configure.check_account()
        check(rc == 1 and "111111111111" in err.getvalue() and "222222222222" in err.getvalue(),
              "another account: refused, naming both")
        configure.account_for = lambda profile: ""
        check(configure.check_account() == 0, "no credentials: left to the steps that report it")
        configure.CONFIG = root / "deploy.none.config"
        check(configure.check_account() == 0, "no config yet: nothing to compare")
    finally:
        configure.CONFIG, configure.account_for = saved

print("")
print("== asking for the environment ==")
env = dict(os.environ, NONINTERACTIVE="1")
out = subprocess.run([sys.executable, str(ROOT / "deployment/configure.py"), "--choose-env"],
                     capture_output=True, text=True, env=env)
check(out.returncode == 1 and out.stdout == "" and "ENV=<name>" in out.stderr and "DEPLOY_ENV" in out.stderr,
      "no terminal: fails, prints nothing on stdout, and says how to name one")

print("")
print("== the Makefile resolves the environment (hermetic copy) ==")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    (root / "deployment").mkdir()
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    shutil.copyfile(ROOT / "deploy.config.example", root / "deploy.config.example")
    shutil.copyfile(ROOT / "deployment/configure.py", root / "deployment/configure.py")
    os.chmod(root / "deployment/configure.py", 0o755)
    write_cfg(tmp, "uat", RESOURCE_PREFIX="uatp", AWS_REGION="eu-west-1", STACK_NAME="uatp")
    base_env = {k: v for k, v in os.environ.items() if k not in ("DEPLOY_ENV", "ENV", "CONFIG_FILE", "MAKEFLAGS")}

    def make(*args, extra=None):
        e = dict(base_env, **(extra or {}))
        return subprocess.run(["make", "-s", "--no-print-directory", *args], cwd=root,
                              capture_output=True, text=True, env=e)

    def info(r):
        return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)

    r = make("env-info", "ENV=uat")
    i = info(r)
    check(i.get("DEPLOY_ENV") == "uat" and i.get("CONFIG_FILE") == "deploy.uat.config"
          and i.get("DEPLOY_STATE_DIR", "").endswith("/deployment/.state/uat"),
          f"ENV=uat: deploy.uat.config and deployment/.state/uat ({i})")
    i = info(make("env-info", extra={"DEPLOY_ENV": "sit"}))
    check(i.get("CONFIG_FILE") == "deploy.sit.config", "an exported DEPLOY_ENV pins the terminal")
    i = info(make("env-info", "ENV=uat", extra={"DEPLOY_ENV": "sit"}))
    check(i.get("DEPLOY_ENV") == "uat", "ENV on the command line wins over the exported DEPLOY_ENV")
    i = info(make("env-info", extra={"ENV": "uat"}))
    check(i.get("DEPLOY_ENV", "x") == "" and i.get("CONFIG_FILE") == "deploy.config",
          "a shell's own ENV variable is ignored")
    r = make("help", "ENV=uat")
    check("uatp" in r.stdout and "eu-west-1" in r.stdout, "the environment's file is the one included")
    for bad in ("../x", "a/b", "a.b", "a b"):
        r = make("env-info", f"ENV={bad}")
        said = r.stderr + r.stdout
        check(r.returncode != 0 and ("not allowed" in said or "spaces" in said)
              and not (root / f"deploy.{bad}.config").exists(),
              f"ENV={bad!r} is refused before any file is touched")
    r = make("-n", "outputs", extra={"NONINTERACTIVE": "1"})
    check(r.returncode != 0 and "No deployment environment" in r.stderr,
          "no environment, no terminal: a goal that needs one stops at parse time")
    r = make("-n", "check-settings", "CONFIG_FILE=deploy.uat.config", extra={"NONINTERACTIVE": "1"})
    check(r.returncode == 0, "a CONFIG_FILE on the command line needs no environment")
    r = make("help", extra={"NONINTERACTIVE": "1"})
    check(r.returncode == 0 and "No deployment environment" not in r.stderr, "help never asks")
    write_cfg(tmp, "", RESOURCE_PREFIX="solo")
    r = make("-n", "outputs", extra={"NONINTERACTIVE": "1"})
    i = info(make("env-info"))
    check(r.returncode == 0 and i.get("CONFIG_FILE") == "deploy.config"
          and i.get("DEPLOY_STATE_DIR", "").endswith("/deployment"),
          "a bare deploy.config: the single-environment behaviour, state where it always was")
    r = make("envs")
    check("uat" in r.stdout and "eu-west-1" in r.stdout, "make envs lists the saved environments")

    # The question itself, on a real terminal: make asks at parse time through /dev/tty
    # (stdout is captured by $(shell)), takes Enter as the only saved environment, and
    # re-asks on a name that is not safe as a file name.
    import pty
    import select

    def on_tty(answers, *args):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(root)
            e = {k: v for k, v in base_env.items() if k not in ("CI", "NONINTERACTIVE")}
            os.execvpe("make", ["make", "-s", "--no-print-directory", *args], e)
        out, queue, t0 = b"", list(answers), time.time()
        while time.time() - t0 < 20:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if queue and out.count(b"Environment name") > len(answers) - len(queue):
                os.write(fd, queue.pop(0))
        os.waitpid(pid, 0)
        return out.decode(errors="replace")

    (root / "deploy.config").unlink()
    t = on_tty([b"\n"], "-n", "outputs")
    check("Environment name [uat]" in t and "--stack-name uatp" in t and "--region eu-west-1" in t,
          "on a terminal: asked first, Enter takes the only saved environment")
    t = on_tty([b"../x\n", b"qa1\n"], "-n", "outputs")
    check("use 1-40 letters" in t and "New environment" in t and "deploy.qa1.config" in t,
          "an unsafe name is re-asked; a new one is announced")

print("")
print("== build state is per environment ==")
s3 = (ROOT / "deployment/build-s3-dist.sh").read_text(encoding="utf-8")
img = (ROOT / "deployment/build-images.sh").read_text(encoding="utf-8")
spa = (ROOT / "deployment/deploy-spas.sh").read_text(encoding="utf-8")
mk = (ROOT / "Makefile").read_text(encoding="utf-8")
check('STATE_DIR="${DEPLOY_STATE_DIR:-$DEPLOYMENT_DIR}"' in s3
      and 'GLOBAL_DIR="$STATE_DIR/global-s3-assets"' in s3 and 'cat > "$STATE_DIR/.artifacts-env"' in s3
      and 'LAYER_BUILD_DIR="$STATE_DIR/.layer-build"' in s3,
      "build-s3-dist.sh stages templates, archives, the layer and .artifacts-env per environment")
check('cat > "$STATE_DIR/.images-env"' in img and "DEPLOY_STATE_DIR" in img,
      "build-images.sh writes .images-env per environment")
check("stage_copy " in img and 'mv -f "$tmp" "$2"' in img and '\ncp "$REPO_ROOT/backend' not in img,
      "the shared batch build context is written by atomic rename")
check('dist="$DEPLOY_STATE_DIR/spa/$app"' in spa and '--outDir "$dist"' in spa
      and 'build_lock_acquire "$REPO_ROOT/$app/.build.lock"' in spa,
      "deploy-spas.sh builds into the environment's directory, under a per-app lock")
check('. "$(DEPLOY_STATE_DIR)/.artifacts-env"' in mk and 'deployment/.artifacts-env ]' not in mk,
      "the stack deploy reads the environment's .artifacts-env and .images-env")
for target in ("stage:", "images:", "deploy-stack-run:", "deploy-spas:", "deploy:", "purge-orphans:"):
    body = mk.split(f"\n{target}", 1)[1].split("\n\n", 1)[0]
    check("configure.py --check-account" in body.split("\n")[1],
          f"{target} checks the account before anything else")
destroy = mk.split("\ndestroy:", 1)[1].split("\n\n", 1)[0]
check("--check-account" in destroy.split("@ACCT=")[0], "destroy checks the account before describing anything")

print("")
print("== the web-app build runs one environment at a time ==")
lock_sh = ROOT / "deployment/build-lock.sh"
with tempfile.TemporaryDirectory() as tmp:
    log = pathlib.Path(tmp) / "log"
    lock = pathlib.Path(tmp) / "app.build.lock"
    worker = (f'. "{lock_sh}"; build_lock_acquire "{lock}" "test build"; '
              f'echo "start $1" >> "{log}"; sleep 0.6; echo "end $1" >> "{log}"; build_lock_release')
    procs = [subprocess.Popen(["bash", "-c", worker, "w", name], stdout=subprocess.DEVNULL)
             for name in ("a", "b", "c")]
    rcs = [p.wait(timeout=60) for p in procs]
    lines = log.read_text().split()
    pairs = [lines[i:i + 4] for i in range(0, len(lines), 4)]
    serial = all(p[0] == "start" and p[2] == "end" and p[1] == p[3] for p in pairs) and len(pairs) == 3
    check(rcs == [0, 0, 0] and serial, f"three concurrent builds never overlap ({' '.join(lines)})")
    check(not lock.exists(), "the lock is released after the last one")
    # A lock left by a process that died is taken over, not waited on.
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock.mkdir()
    (lock / "pid").write_text(str(dead.pid))
    t0 = time.time()
    r = subprocess.run(["bash", "-c", f'. "{lock_sh}"; build_lock_acquire "{lock}" x && echo got'],
                       capture_output=True, text=True, timeout=30)
    check("got" in r.stdout and time.time() - t0 < 5, "a stale lock (holder gone) is taken over at once")
    check(not lock.exists(), "and released on exit")
    r = subprocess.run(["bash", "-c", f'. "{lock_sh}"; build_lock_acquire "{lock}" x; exit 3'],
                       capture_output=True, text=True, timeout=30)
    check(r.returncode == 3 and not lock.exists(), "a failing step still releases the lock")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
