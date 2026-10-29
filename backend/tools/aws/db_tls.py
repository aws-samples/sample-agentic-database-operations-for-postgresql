# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""TLS settings for connections to the databases this product monitors.

Every connection to a monitored database verifies the server's certificate chain AND
hostname (``verify-full``) against the public RDS CA bundle, which ships inside the
images and the Tier 1 Lambda zip (``certs/rds-global-bundle.pem``, downloaded from AWS
at build time; it covers every region, RDS and Aurora, and RDS rotates server
certificates under the same CAs). With verification on, a connection that was routed to
the wrong host, through a route-table clash or anything else, fails the handshake before
the password is sent.

There is one mode. Every database this product can reach is an RDS or Aurora instance
(the registry is populated by discovery alone, and discovery records the RDS ARN), so
every server presents an RDS-issued certificate for its own hostname, and every
connection, whatever the network path, is made to that hostname: over VPC peering it
resolves to the database; over a Lattice resource endpoint, private DNS resolves it to
the endpoint inside the agent VPC. Nothing on a record or in a request can weaken this.

The connection fails CLOSED when the bundle is missing: it is never silently downgraded
because a file is absent. Shared by the agent (``pg_client``), the Tier 2 scan
(``run_scan``), the Tier 1 collector, the onboarding checks in the API and the metrics
store, so they cannot drift from one another.
"""
import os
from pathlib import Path

SSL_MODE = "verify-full"
_BUNDLE_NAME = "rds-global-bundle.pem"


def bundle_path() -> str:
    """Where the RDS CA bundle lives: ``RDS_CA_BUNDLE`` if set, else ``certs/`` beside
    the code root. The module sits at ``<root>/tools/aws/db_tls.py`` in the backend
    image, the batch image and the Lambda zip alike, so the relative location is the
    same in all three."""
    env = (os.getenv("RDS_CA_BUNDLE") or "").strip()
    if env:
        return env
    return str(Path(__file__).resolve().parents[2] / "certs" / _BUNDLE_NAME)


def ssl_kwargs() -> dict:
    """psycopg2 connection kwargs for TLS: ``sslmode=verify-full`` and ``sslrootcert``.
    Raises ``RuntimeError`` when the bundle is not present, instead of silently
    downgrading."""
    path = bundle_path()
    if not os.path.isfile(path):
        raise RuntimeError(
            f"RDS CA bundle not found at {path}; refusing to connect without certificate "
            f"verification. The bundle ships in the image; set RDS_CA_BUNDLE if it lives elsewhere."
        )
    return {"sslmode": SSL_MODE, "sslrootcert": path}
