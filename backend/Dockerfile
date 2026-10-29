# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Base image pinned by digest for a reproducible, tamper-resistant build (the tag is
# kept for readability). Dependabot refreshes the digest when a newer image ships.
FROM public.ecr.aws/docker/library/python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

WORKDIR /app

RUN apt-get update && apt-get install -y curl libpq-dev && rm -rf /var/lib/apt/lists/*

# requirements.txt is a fully pinned, hashed lock (generated from requirements.in via
# `make lock`). --require-hashes makes the build install exactly those audited artifacts
# and fail if anything (including a transitive dependency) does not match.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY . .

# Compile Python to bytecode and strip source files
RUN python -m compileall -b -q /app \
    && find /app -name "*.py" ! -name "__init__.py" -delete

RUN useradd -m -u 1001 agentuser && chown -R agentuser:agentuser /app
USER agentuser

ENV PYTHONUNBUFFERED=1 PORT=8080 HOST=0.0.0.0

EXPOSE 8080

# Container liveness probe (clears checkov CKV_DOCKER_2). Hits the FastAPI /ping endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/ping || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
