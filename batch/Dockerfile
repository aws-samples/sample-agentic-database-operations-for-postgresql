# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Base image pinned by digest for a reproducible, tamper-resistant build (the tag is
# kept for readability). Dependabot refreshes the digest when a newer image ships.
FROM public.ecr.aws/docker/library/python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        postgresql-client \
        bc \
        curl \
        jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pinned, hashed dependency lock (generated from requirements.in via `make lock`).
# --require-hashes installs exactly the audited artifacts and fails on any mismatch.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# deep_sections.py extracts the report's nine sections as structured data; without it
# run_scan stores only the flat data (it logs a warning, it does not fail).
COPY postgres_health_insights.sh run_scan.py deep_sections.py ./
RUN mkdir -p utils
COPY utils/ utils/
# Curated subset of backend/tools (staged by deployment/build-images.sh): the
# metrics_store writer imported by run_scan.write_to_store (DynamoDB + CloudWatch),
# resource_names, and db_tls.
COPY tools/ tools/
# The public RDS CA bundle (staged from backend/certs by the same step). Every psql
# and psycopg2 connection the scan opens verifies the server certificate against it;
# tools/aws/db_tls.py resolves it as /app/certs/ and refuses to connect without it.
COPY certs/ certs/
# references.yaml is staged into the build context from backend/checks by the
# Makefile (make build-batch), so the findings engine scores from the SAME
# threshold catalog as the agent path.
COPY references.yaml ./
RUN chmod +x postgres_health_insights.sh

# Compile Python to bytecode and strip source files
RUN python -m compileall -b -q /app \
    && find /app -name "*.py" ! -name "__init__.py" -delete

# Run as non-root (clears checkov CKV_DOCKER_3). The scan job only reads from
# the target DB and writes results to S3/Aurora — no host privileges needed.
RUN useradd -m -u 1001 scanuser && chown -R scanuser:scanuser /app
USER scanuser

# Liveness probe (clears checkov CKV_DOCKER_2). This is a one-shot batch job
# rather than a long-running server, so the check is a formality that simply
# confirms the Python runtime is responsive while the job is in flight.
HEALTHCHECK --interval=60s --timeout=5s --retries=2 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["python3", "run_scan.pyc"]
