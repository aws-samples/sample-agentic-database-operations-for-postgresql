#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Regenerate THIRD-PARTY-LICENSES from the dependency locks.

Reads licence metadata from Python environments installed from backend/requirements.txt
and batch/requirements.txt, and from license-checker output over the production
dependencies of frontend/ and admin-console/. Writes the attribution file at the repo
root, alphabetical within each section, so the output is stable between runs.

Usage:
  deployment/third-party-licenses.py [--python PY]

The script creates temporary virtual environments under $TMPDIR and removes them. It
needs network access to PyPI and npm, and `npx`. It is not part of the build.
"""
import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY_COMPONENTS = ["backend", "batch"]
NPM_APPS = ["frontend", "admin-console"]
LICENSE_CHECKER = "license-checker-rseidelsohn@4.4.2"

META_SCRIPT = r'''
import json, sys
from importlib.metadata import distributions
SKIP = {"pip", "setuptools", "wheel"}
out = {}
for d in distributions(path=[sys.argv[1]]):
    m = d.metadata
    name = m["Name"]
    if not name or name.lower() in SKIP:
        continue
    classifiers = [c.split(" :: ")[-1] for c in (m.get_all("Classifier") or []) if c.startswith("License ::")]
    lic = m.get("License")
    if lic and len(lic) > 80:
        lic = lic.strip().splitlines()[0][:80] + " ..."
    urls = {}
    for u in m.get_all("Project-URL") or []:
        label, _, url = u.partition(", ")
        urls[label.strip().lower()] = url.strip()
    home = m.get("Home-page") or ""
    if not home:
        for key in ("source", "source code", "repository", "homepage", "home", "github", "code"):
            if key in urls:
                home = urls[key]
                break
    if not home:
        rest = [u for k, u in urls.items() if "issue" not in k and "changelog" not in k]
        home = rest[0] if rest else ""
    out[name.lower()] = {
        "name": name, "version": d.version,
        "expression": m.get("License-Expression"), "license_field": lic,
        "classifiers": classifiers, "home": home,
    }
json.dump(out, sys.stdout)
'''


def resolve(v):
    if v["expression"]:
        return v["expression"]
    if v["classifiers"]:
        return "; ".join(v["classifiers"])
    if v["license_field"] and v["license_field"].upper() != "UNKNOWN":
        return v["license_field"]
    return None


def python_packages(python):
    pk = {}
    for comp in PY_COMPONENTS:
        venv = pathlib.Path(tempfile.mkdtemp(prefix=f"tpl-{comp}-"))
        try:
            subprocess.run([python, "-m", "venv", str(venv)], check=True)
            subprocess.run([str(venv / "bin/pip"), "install", "-q", "--require-hashes", "--no-deps",
                            "-r", str(ROOT / comp / "requirements.txt")], check=True)
            site = subprocess.run([str(venv / "bin/python"), "-c",
                                   "import site;print(site.getsitepackages()[0])"],
                                  check=True, capture_output=True, text=True).stdout.strip()
            meta = subprocess.run([str(venv / "bin/python"), "-c", META_SCRIPT, site],
                                  check=True, capture_output=True, text=True).stdout
        finally:
            shutil.rmtree(venv, ignore_errors=True)
        for k, v in json.loads(meta).items():
            entry = pk.setdefault(k, {**v, "used_by": []})
            entry["used_by"].append(comp)
    return pk


def npm_packages():
    own = {json.loads((ROOT / app / "package.json").read_text())["name"] for app in NPM_APPS}
    npm = {}
    for app in NPM_APPS:
        out = subprocess.run(["npx", "--yes", LICENSE_CHECKER, "--production", "--excludePrivatePackages",
                              "--json"], cwd=ROOT / app, check=True, capture_output=True, text=True).stdout
        for key, v in json.loads(out).items():
            name, version = key.rsplit("@", 1)
            if name in own:
                continue
            entry = npm.setdefault(name, {"versions": set(), "licence": str(v.get("licenses")),
                                          "url": v.get("repository") or "", "used_by": set()})
            entry["versions"].add(version)
            entry["used_by"].add(app)
    return npm


def clean_url(u):
    u = (u or "").strip()
    u = re.sub(r"^git\+", "", u)
    return re.sub(r"\.git$", "", u)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="interpreter used to build the temp venvs")
    args = ap.parse_args()

    py = python_packages(args.python)
    unresolved = [v["name"] for v in py.values() if not resolve(v)]
    if unresolved:
        sys.exit(f"licence not declared by: {', '.join(unresolved)}; resolve by hand before writing")
    npm = npm_packages()

    lines = [
        "Third-party software",
        "====================",
        "",
        "This project's own code is licensed as stated in LICENSE. It depends on the",
        "third-party packages below, which are installed from PyPI and npm when the",
        "containers and web apps are built. None of them is modified or copied into this",
        "repository. Each package is listed with its version and licence; the full licence",
        "text ships with each package and is available at its source URL.",
        "",
        "Two packages use licences other than the permissive ones that cover the rest:",
        "  - psycopg2-binary (LGPL-3.0 with exceptions), backend and batch containers",
        "  - certifi (MPL-2.0), backend container",
        "Source for both is available from their PyPI pages listed below.",
        "",
        "",
        "Python packages (backend and batch containers)",
        "----------------------------------------------",
        "",
    ]
    for _, v in sorted(py.items()):
        url = clean_url(v["home"])
        if v["name"].lower() in ("psycopg2-binary", "certifi"):
            url = f"https://pypi.org/project/{v['name']}/{v['version']}/"
        lines += [f"{v['name']} {v['version']}", f"  Licence: {resolve(v)}"]
        if url:
            lines.append(f"  Source:  {url}")
        lines += [f"  Used by: {', '.join(sorted(v['used_by']))}", ""]

    lines += [
        "",
        "JavaScript packages bundled into the web apps (production dependencies)",
        "-----------------------------------------------------------------------",
        "",
    ]
    for name in sorted(npm):
        v = npm[name]
        lines += [f"{name} {', '.join(sorted(v['versions']))}", f"  Licence: {v['licence']}"]
        if v["url"]:
            lines.append(f"  Source:  {clean_url(v['url'])}")
        lines += [f"  Used by: {', '.join(sorted(v['used_by']))}", ""]

    (ROOT / "THIRD-PARTY-LICENSES").write_text("\n".join(lines).rstrip() + "\n")
    print(f"wrote THIRD-PARTY-LICENSES: {len(py)} Python, {len(npm)} npm packages")


if __name__ == "__main__":
    main()
