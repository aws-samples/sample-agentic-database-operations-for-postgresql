# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from strands import Agent


@dataclass
class SubAgentResult:
    """Wrapper to identify and pass sub-agent events to orchestrator"""
    agent: Agent
    event: dict
