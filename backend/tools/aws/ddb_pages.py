# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read every page of a DynamoDB scan or query.

A scan or query returns at most 1 MB per call, and a truncated page looks exactly like a
complete one unless the caller follows ``LastEvaluatedKey``. Registry rows are several KB
each, so a single call stops returning rows somewhere past a hundred databases, silently:
rows vanish from the console, and discovery, which de-duplicates against the list, creates
a second record for a database it already has. Every "list all" read goes through here.
"""


def read_all(operation, **kwargs) -> list:
    """All items of ``operation(**kwargs)`` (a boto3 ``Table.scan`` or ``Table.query``),
    following ``LastEvaluatedKey`` until the last page. A filter applies per page, so a
    filtered read can return an empty page that is not the last one; this keeps going."""
    items, kw = [], dict(kwargs)
    while True:
        page = operation(**kw) or {}
        items.extend(page.get("Items", []) or [])
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kw["ExclusiveStartKey"] = last
