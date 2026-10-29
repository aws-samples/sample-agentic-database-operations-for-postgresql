// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Product bugs the harness has found and that are not fixed yet. A scenario wraps the
// assertion that exposes one in h.xfail('<id>', ...): the run reports it as an expected
// failure (XFAIL) with this text instead of failing, and as XPASS once the product is
// fixed, which is the signal to delete the entry and the marker. Product code is never
// changed to make the harness pass; fixes go to the product, then the marker goes.
//
// Fixed and removed: GH-1 (rule 21, an open members table did not show a failover),
// fixed in 753ae86; S5 now asserts it directly.
export const KNOWN_BUGS = {}
