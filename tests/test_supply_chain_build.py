#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build/deploy supply-chain guards.

The four attack chains B1-B4 all begin at a mutable image tag: a principal with
ecr:PutImage could re-point a build-timestamp tag and the next Fargate task would pull
swapped code with no stack event. The fixable-here half is:

  1. ECR repositories are created IMMUTABLE (and hardened idempotently if they predate it),
     so a pushed tag cannot be re-pointed.
  2. The two images are deployed to the consumers by their @sha256 digest, not the tag.
  3. The SPA build fails closed on a lock-file mismatch instead of silently resolving fresh.

The CI security gate (scan/sign/SBOM) stays a documented accept because the sample ships no
pipeline; that is asserted to remain *documented* here, not implemented.

These are shell scripts, so the guards read their source rather than executing them.

Run:  python3 tests/test_supply_chain_build.py   (from the repository root)
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD_IMAGES = (ROOT / "deployment/build-images.sh").read_text(encoding="utf-8")
DEPLOY_SPAS = (ROOT / "deployment/deploy-spas.sh").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

problems = 0


def check(cond, msg):
    global problems
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  ❌ {msg}")
        problems += 1


print("── ECR repositories are immutable-tagged ──")
check("--image-tag-mutability IMMUTABLE" in BUILD_IMAGES,
      "create-repository sets IMMUTABLE tag mutability")
check("put-image-tag-mutability" in BUILD_IMAGES and
      re.search(r"put-image-tag-mutability.*\n?.*IMMUTABLE", BUILD_IMAGES),
      "an existing repository is hardened to IMMUTABLE idempotently")

print("")
print("── images are deployed by digest, not by a resolvable tag ──")
check("digest_uri()" in BUILD_IMAGES, "a digest_uri helper exists")
check("describe-images" in BUILD_IMAGES and "imageDigest" in BUILD_IMAGES,
      "the pushed image's digest is read from ECR")
check('${registry}/${repo}@${digest}' in BUILD_IMAGES,
      "the deploy URI is the @sha256 digest form")
# both image URIs are reassigned to the digest form after push
check(BUILD_IMAGES.count('digest_uri "$') >= 2 or
      (re.search(r'BACKEND_IMAGE_URI="\$\(digest_uri', BUILD_IMAGES) is not None and
       re.search(r'BATCH_IMAGE_URI="\$\(digest_uri', BUILD_IMAGES) is not None),
      "both BACKEND and BATCH image URIs are resolved to digest form")

print("")
print("── the SPA build fails closed on a dependency mismatch ──")
# Only executable lines count — a comment may reference the removed fallback for context.
_spas_code = "\n".join(ln for ln in DEPLOY_SPAS.splitlines() if not ln.lstrip().startswith("#"))
check("npm install" not in _spas_code,
      "no executable `npm install` fallback remains (the lock file is never discarded)")
check(re.search(r"npm ci\b(?!.*2>/dev/null)", _spas_code) is not None,
      "npm ci runs without stderr suppression (the failure reason stays visible)")
check("2>/dev/null" not in _spas_code or "npm ci --silent 2>/dev/null" not in _spas_code,
      "the silent-and-suppressed npm ci form is gone")

print("")
print("── the CI security gate remains a documented accept ──")
low = README.lower()
check("no build pipeline of its own" in low or "has no build pipeline" in low,
      "README states the repo ships no build pipeline")
check(("immutable" in low) and ("digest" in low),
      "README documents the immutable-tag + digest deployment as current state")

print("")
if problems:
    print(f"FAIL — {problems} problem(s)")
    sys.exit(1)
print("PASS")
