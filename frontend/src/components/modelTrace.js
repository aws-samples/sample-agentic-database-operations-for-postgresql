// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Agent-trace label for the server's per-turn `model` event: the resolved model id and the
// reasoning controls the turn runs with (effort level and thinking mode), when the server
// sends them. Shared by the chat and investigation traces so both read the same way.
export function modelTraceLabel(event) {
  const parts = [`Model: ${event?.model_id || 'unknown'}`]
  if (event?.effort) parts.push(`effort ${event.effort}`)
  if (event?.thinking) parts.push(`thinking ${event.thinking}`)
  return parts.join(' · ')
}

// The trace entry's detail object: everything the server reported about the model.
export function modelTraceDetail(event) {
  const { type, ...rest } = event || {}
  return rest
}
