# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Server-side pieces of the operator experience that every way of reaching the agent
shares — the operator web app today, other clients later.

What lives here is deliberately *not* client-specific: a client names things (a space, a
database) and the server does the rest against the registry, after its own membership
checks. That is what keeps the operator experience identical for every operator, whatever
their other roles and whatever they connect with.
"""
