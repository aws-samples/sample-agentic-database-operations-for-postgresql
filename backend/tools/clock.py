# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The agents' clock: the one tool that tells them what time it is now."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from strands import tool


@tool
def current_time(timezone_name: str = "UTC") -> str:
    """Get the current date and time in ISO 8601 format.

    Args:
        timezone_name: IANA timezone name such as 'UTC', 'Europe/London' or
            'US/Pacific' (default: UTC)

    Returns:
        The current time in that timezone, e.g. 2026-04-15T14:32:16.123456+00:00
    """
    try:
        tz = timezone.utc if timezone_name.upper() == "UTC" else ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"Unknown timezone '{timezone_name}'") from exc
    return datetime.now(tz).isoformat()
