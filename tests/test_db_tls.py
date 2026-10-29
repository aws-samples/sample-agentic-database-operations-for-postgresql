#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every database connection VERIFIES the server certificate, through one module.

libpq defaults to ``sslmode=prefer``: attempt TLS, fall back to plaintext if the server
offers none, and never verify the certificate. ``require`` is only one step better: the
traffic is encrypted, but nothing checks WHO is on the other end, so a connection that
was routed to the wrong host (a /32 route clash, a poisoned resolver) hands the Secrets
Manager password to whoever answers. Certificate verification against the RDS CA bundle
is what pins the host.

The rule, in ``backend/tools/aws/db_tls.py`` and nowhere else:

* ``verify-full`` by default (chain + hostname);
* ``verify-ca`` when the connection goes through a PrivateLink endpoint, whose DNS name
  is not on the RDS certificate (the chain still proves a genuine RDS server);
* no encrypt-only mode at all: every database this product can reach is RDS or Aurora
  (discovery is the only way a record is created, and it stores the RDS ARN), so every
  server has an RDS-issued certificate and nothing on a record can weaken the check;
* no bundle means NO CONNECTION, not a silent downgrade.

This test pins the module's behaviour, then walks the tree with ``ast`` and asserts that
every connection takes its TLS settings from the module (``**ssl_kwargs``) and from
nowhere else, however the call is spelled: ``psycopg2.connect``, a module alias, a name
imported from ``psycopg2`` (with or without ``as``), a ``**dict`` spread, or a positional
DSN string that could carry ``sslmode=`` where no keyword would show it. The finder is
exercised on fixtures of each shape first, so the test proves it catches them. It also
checks the shell path (psql honours ``PGSSLMODE``/``PGSSLROOTCERT``) and that the bundle
is packaged everywhere the module runs.
"""
import ast
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# Generated copies: batch/tools and batch/certs are staged subsets of backend/
# (gitignored), so the source is checked and the copy would only duplicate every finding.
SKIP_PARTS = {
    ".git", "node_modules", "__pycache__", "dist", "build",
    "global-s3-assets", "regional-s3-assets", ".layer-build", ".tier1-build",
}
SKIP_RELATIVE = {pathlib.Path("batch/tools"), pathlib.Path("batch/certs")}

passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# ── 1. the module ─────────────────────────────────────────────────────────────
print("══ db_tls: the one place the TLS mode is decided ══")
from tools.aws import db_tls  # noqa: E402

bundle = ROOT / "backend/certs/rds-global-bundle.pem"
check(bundle.is_file(), "the RDS CA bundle is vendored at backend/certs/rds-global-bundle.pem")
pem = bundle.read_text(encoding="utf-8") if bundle.is_file() else ""
n_certs = pem.count("-----BEGIN CERTIFICATE-----")
check(n_certs >= 100, f"it is the GLOBAL bundle, every region's CAs ({n_certs} certificates)")

os.environ.pop("RDS_CA_BUNDLE", None)
check(db_tls.bundle_path() == str(bundle), "bundle_path() resolves certs/ relative to the code root")
os.environ["RDS_CA_BUNDLE"] = "/elsewhere/bundle.pem"
check(db_tls.bundle_path() == "/elsewhere/bundle.pem", "RDS_CA_BUNDLE overrides the location")
os.environ.pop("RDS_CA_BUNDLE", None)

check(db_tls.SSL_MODE == "verify-full", "one mode: verify-full, chain and hostname")
check(not hasattr(db_tls, "VALID_MODES") and not hasattr(db_tls, "tls_mode_for"),
      "there is no mode selection to get wrong: no VALID_MODES, no tls_mode_for")
import inspect  # noqa: E402
check(not inspect.signature(db_tls.ssl_kwargs).parameters,
      "ssl_kwargs() takes nothing: no record, host or mode can steer it")
for weak in ("require", "prefer", "allow", "disable", "verify-ca"):
    check(weak not in (ROOT / "backend/tools/aws/db_tls.py").read_text(encoding="utf-8").replace(
        "fails CLOSED", "").split("SSL_MODE =")[1],
          f"no {weak!r} literal after the mode constant: nothing else is ever chosen")

kw = db_tls.ssl_kwargs()
check(kw == {"sslmode": "verify-full", "sslrootcert": str(bundle)},
      "ssl_kwargs: verify-full carries the bundle as sslrootcert")

os.environ["RDS_CA_BUNDLE"] = os.path.join(tempfile.gettempdir(), "no-such-bundle.pem")
try:
    db_tls.ssl_kwargs()
    check(False, "ssl_kwargs: a missing bundle is an error, not a downgrade to require")
except RuntimeError as e:
    check("refusing to connect" in str(e), "ssl_kwargs: a missing bundle is an error, not a downgrade to require")
os.environ.pop("RDS_CA_BUNDLE", None)

print("")
print("══ nothing about TLS is stored on a record or accepted from a client ══")
app_src = (ROOT / "backend/app.py").read_text(encoding="utf-8")
check("tls_mode" not in app_src, "app.py has no tls_mode field, parameter or write")
check("tls_mode" not in (ROOT / "admin-console/src/components/SpaceDetail.jsx").read_text(encoding="utf-8")
      and "encryptOnly" not in (ROOT / "admin-console/src/components/SpaceDetail.jsx").read_text(encoding="utf-8"),
      "the onboarding wizard offers no encrypt-only choice")
check('"require"' not in (ROOT / "backend/tools/aws/db_tls.py").read_text(encoding="utf-8"),
      "db_tls has no require literal")


# ── 2. every connect() site goes through the module ───────────────────────────
def python_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if SKIP_PARTS & set(rel.parts):
            continue
        if any(str(rel).startswith(str(s)) for s in SKIP_RELATIVE):
            continue
        yield path


class _ConnectFinder(ast.NodeVisitor):
    """Every call that can open a libpq connection in a module, however it is spelled.

    Tracks the names a module binds to psycopg2 and to its connect function, so all of
    these are found: ``psycopg2.connect(...)``, ``import psycopg2 as pg; pg.connect(...)``,
    ``from psycopg2 import connect; connect(...)``, ``from psycopg2 import connect as c``.
    For each call it reports the keyword names, the sources of any ``**spread`` argument,
    and whether a positional argument (a DSN string or a variable) was passed, since a
    DSN can carry ``sslmode=`` where no keyword would show it."""

    def __init__(self):
        self.module_names = set()      # names bound to the psycopg2 module
        self.connect_names = set()     # names bound to psycopg2.connect itself
        self.calls = []                # (lineno, kwargs, starred, positional)

    def visit_Import(self, node):
        for a in node.names:
            if a.name == "psycopg2":
                self.module_names.add(a.asname or "psycopg2")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module == "psycopg2":
            for a in node.names:
                if a.name == "connect":
                    self.connect_names.add(a.asname or "connect")
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        is_connect = (
            (isinstance(f, ast.Attribute) and f.attr == "connect"
             and isinstance(f.value, ast.Name) and f.value.id in self.module_names)
            or (isinstance(f, ast.Name) and f.id in self.connect_names)
        )
        if is_connect:
            kwargs = {k.arg for k in node.keywords if k.arg}
            starred = {ast.unparse(k.value) for k in node.keywords if k.arg is None}
            self.calls.append((node.lineno, kwargs, starred, bool(node.args)))
        self.generic_visit(node)


def connect_calls_in(source: str):
    """Yield (lineno, kwargs, starred, positional) for every connection call in `source`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    finder = _ConnectFinder()
    # Two passes so a `from psycopg2 import connect` below a call site still counts.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            finder.visit(node)
    finder.visit(tree)
    return finder.calls


def tls_literals_in(source: str):
    """String constants anywhere in `source` that carry a TLS setting: a DSN or a
    connection string with sslmode=/sslrootcert=, or a PGSSLMODE assignment."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(r"\bsslmode\s*=", node.value) or re.search(r"\bsslrootcert\s*=", node.value):
                out.append((node.lineno, node.value))
    return out


def site_is_clean(rel, kwargs, starred, positional, text):
    """A connection site is acceptable only if it takes TLS from db_tls and nowhere else:
    it spreads ssl_kwargs(...) directly, or spreads a dict this file built from
    **ssl_kwargs(...); it names no sslmode/sslrootcert keyword; and it passes no
    positional argument (a DSN would be the one place a weaker mode could hide)."""
    uses_module = any(x.startswith("ssl_kwargs(") for x in starred) or (
        bool(starred) and "**ssl_kwargs(" in text)
    return uses_module and not ({"sslmode", "sslrootcert"} & kwargs) and not positional


print("")
print("══ the finder itself recognises every way of writing a connection ══")
FIXTURES = {
    "module attribute": "import psycopg2\npsycopg2.connect(host='h', sslmode='require')\n",
    "module alias": "import psycopg2 as pg\npg.connect(host='h', sslmode='require')\n",
    "imported name": "from psycopg2 import connect\nconnect(host='h', sslmode='require')\n",
    "imported alias": "from psycopg2 import connect as c\nc(host='h', sslmode='require')\n",
    "starred dict": "import psycopg2\nd = {'host': 'h', 'sslmode': 'require'}\npsycopg2.connect(**d)\n",
    "DSN string": "import psycopg2\npsycopg2.connect('host=h sslmode=require')\n",
    "DSN in a variable": "import psycopg2\ndsn = 'host=h sslmode=require'\npsycopg2.connect(dsn)\n",
}
for label, src in FIXTURES.items():
    calls = connect_calls_in(src)
    check(len(calls) == 1, f"fixture [{label}]: the connection call is found")
    if calls:
        _, kw, st, pos = calls[0]
        check(not site_is_clean("fixture", kw, st, pos, src), f"fixture [{label}]: and it is flagged as not clean")
check(tls_literals_in(FIXTURES["DSN string"]) and tls_literals_in(FIXTURES["DSN in a variable"]),
      "fixtures [DSN]: the sslmode inside the string literal is found")
GOOD = "import psycopg2\nfrom tools.aws.db_tls import ssl_kwargs\npsycopg2.connect(host='h', **ssl_kwargs(r, 'h'))\n"
_, kw, st, pos = connect_calls_in(GOOD)[0]
check(site_is_clean("fixture", kw, st, pos, GOOD), "fixture [db_tls]: the correct shape passes")
check(not connect_calls_in("import psycopg2\nx = psycopg2.extras.RealDictCursor\n"), "fixture: unrelated psycopg2 attribute access is not a call")

print("")
print("══ every connection in the tree takes its TLS settings from db_tls ══")
sites = []
for path in python_files():
    text = path.read_text(encoding="utf-8", errors="ignore")
    for lineno, kwargs, starred, positional in connect_calls_in(text):
        sites.append((path.relative_to(ROOT), lineno, kwargs, starred, positional, text))

check(len(sites) >= 6, f"found {len(sites)} connection site(s) to check")
for rel, lineno, kwargs, starred, positional, text in sites:
    if rel == pathlib.Path("backend/tools/aws/db_tls.py"):
        continue
    check(site_is_clean(rel, kwargs, starred, positional, text),
          f"{rel}:{lineno} spreads **ssl_kwargs(...), names no sslmode, passes no DSN")

print("")
print("══ no sslmode anywhere else: not as a keyword, not inside a string ══")
for path in python_files():
    rel = path.relative_to(ROOT)
    if rel in (pathlib.Path("backend/tools/aws/db_tls.py"), pathlib.Path(__file__).relative_to(ROOT)):
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    literals = tls_literals_in(text)
    check(not literals, f"{rel} has no string carrying sslmode=/sslrootcert= (found: {literals or 'none'})")
    check("sslmode=" not in text.replace("sslmode=$PGSSLMODE", ""), f"{rel} has no sslmode= outside db_tls")

# ── 3. the shell path ─────────────────────────────────────────────────────────
print("")
print("══ the batch shell path sets PGSSLMODE and PGSSLROOTCERT, covering every psql call ══")
run_scan = (ROOT / "batch/run_scan.py").read_text(encoding="utf-8")
check('env["PGSSLMODE"] = tls["sslmode"]' in run_scan,
      "run_scan.py takes PGSSLMODE from db_tls")
check('env["PGSSLROOTCERT"] = tls["sslrootcert"]' in run_scan,
      "run_scan.py passes the bundle as PGSSLROOTCERT")
check(run_scan.index('env["PGPASSWORD"]') < run_scan.index('env["PGSSLMODE"]'),
      "it sits with PGPASSWORD, where the environment is built")
check('"require"' not in run_scan.split("def run_shell_script")[1].split("def ")[0],
      "run_shell_script has no require literal left")

script = (ROOT / "batch/postgres_health_insights.sh").read_text(encoding="utf-8")
check('export PGSSLMODE="verify-full" PGSSLROOTCERT="$_bundle"' in script,
      "the scan script defaults a standalone run to verify-full with the bundle")
guard_block = script.split('if [ -z "${PGSSLMODE:-}" ]', 1)[1].split("\nfi", 1)[0] \
    if 'if [ -z "${PGSSLMODE:-}" ]' in script else ""
check("exit 1" in guard_block and "PGSSLMODE is unset" in guard_block,
      "no bundle and no PGSSLMODE is an error, not a downgrade")
check('PGSSLMODE:-require' not in script, "the script does not default to require")
psql_lines = [n for n, line in enumerate(script.splitlines(), 1) if "psql -h" in line]
export_line = next(n for n, line in enumerate(script.splitlines(), 1) if "export PGSSLMODE=" in line)
check(psql_lines and export_line < min(psql_lines),
      f"the export precedes all {len(psql_lines)} psql invocations")

# ── 4. the metrics store opens no database connection at all ──────────────────
print("")
print("══ the internal metrics store has no database connection to secure ══")
# The internal metrics store is DynamoDB + CloudWatch, reached over the AWS SDK (SigV4
# over TLS), so the TLS-to-database rule applies only to the MONITORED databases. Pinned
# so a psycopg2 path cannot creep into the store module without the db_tls rule coming
# with it.
ms = (ROOT / "backend/tools/aws/metrics_store.py").read_text(encoding="utf-8")
check("import psycopg2" not in ms, "metrics_store does not import psycopg2")
check("sslmode" not in ms and '"require"' not in ms, "metrics_store has no TLS mode of its own")
check(not (ROOT / "infrastructure/aurora-store.yaml").exists(),
      "the Aurora store template (and its retention sweeper) is gone")
# ── 5. packaging: the bundle travels with the module ──────────────────────────
print("")
print("══ db_tls.py and the bundle ship everywhere the module runs ══")
tier1 = (ROOT / "backend/scripts/build-tier1.sh").read_text(encoding="utf-8")
check("tools/aws/db_tls.py" in tier1 and "certs/rds-global-bundle.pem" in tier1,
      "build-tier1.sh stages db_tls.py and the bundle into the tier1 zip")
check("zip -qr tier1_collector.zip tier1_collector.py alarm_notifier.py tools certs" in tier1,
      "and zips certs/ alongside tools/")
check('cp certs/rds-global-bundle.pem "$LAYER_BUILD_DIR/certs/' in tier1,
      "build-tier1.sh puts the bundle in the psycopg2 layer for the sweeper")

dist = (ROOT / "deployment/build-s3-dist.sh").read_text(encoding="utf-8")
check('"backend/tools/aws/db_tls.py"' in dist and '"backend/certs/rds-global-bundle.pem"' in dist,
      "build-s3-dist.sh lists both in TIER1_FILES (so they are hashed and staged)")
check('*"certs/rds-global-bundle.pem"*' in dist, "build-s3-dist.sh fails the build if the zip lacks the bundle")
check('cp "$REPO_ROOT/backend/certs/rds-global-bundle.pem" "$LAYER_BUILD_DIR/certs/' in dist,
      "build-s3-dist.sh puts the bundle in the psycopg2 layer")

images = (ROOT / "deployment/build-images.sh").read_text(encoding="utf-8")
check("backend/tools/aws/db_tls.py" in images and "batch/certs/rds-global-bundle.pem" in images,
      "build-images.sh stages db_tls.py and the bundle into the batch build context")
dockerfile = (ROOT / "batch/Dockerfile").read_text(encoding="utf-8")
check("COPY certs/ certs/" in dockerfile, "batch/Dockerfile copies certs/ into the image")
backend_dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
check("COPY . ." in backend_dockerfile, "backend/Dockerfile copies the whole tree, bundle included")
backend_ignore = ROOT / "backend/.dockerignore"
if backend_ignore.exists():
    check("certs" not in backend_ignore.read_text(encoding="utf-8"),
          "backend/.dockerignore does not exclude certs/")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
