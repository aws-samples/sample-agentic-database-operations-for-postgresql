// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// "Send to live agent" from report discussion.
//
// A report discussion answers only from the saved report and has no live connection. When
// the user asks something that needs live data, the agent says so and ends its answer with
// a fixed marker (the report-mode prompt instructs it to). The client recognises the
// marker, strips it from the shown text, and offers ONE inline pill under that answer:
// "Ask the live agent about <database>". Clicking it switches the chat toggle to Live
// Agent and re-sends the same question scoped to the report's OWN database only.
//
// A fixed marker is simpler and more reliable than parsing the prose, and keeps the
// decision with the model that wrote the answer.
//
// No React here, so tests/test_live_handoff.mjs can exercise it directly.

export const LIVE_MARKER = '[[LIVE_DATA_NEEDED]]'

// Strip the live-data marker from streamed text. Returns {text, needed}: a complete
// marker is removed and flagged; a trailing PARTIAL marker (its first half arrived in one
// delta, the rest not yet) is held back so it never flashes on screen mid-stream.
export function splitLiveMarker(buffer) {
  let text = String(buffer || '')
  let needed = false
  if (text.includes(LIVE_MARKER)) {
    needed = true
    text = text.split(LIVE_MARKER).join('')
  }
  for (let n = Math.min(LIVE_MARKER.length - 1, text.length); n > 0; n--) {
    if (text.endsWith(LIVE_MARKER.slice(0, n))) {
      text = text.slice(0, text.length - n)
      break
    }
  }
  return { text, needed }
}

// The report's OWN database as a scope entry {instance_id, display_name}, or null when the
// report carries no instance id. Prefers the live record from `databases` (for its real
// display name); otherwise builds a minimal entry from the report. NEVER the picker or the
// whole space — a report hand-off is scoped to the report's one database.
export function reportDatabaseScope(reportContext, databases = []) {
  const meta = reportContext?.meta || {}
  const analysis = reportContext?.analysis || {}
  const instanceId = meta.instance_id || analysis.instance_id || ''
  if (!instanceId) return null
  const match = (databases || []).find(d => d && d.instance_id === instanceId)
  if (match) return match
  return { instance_id: instanceId, display_name: analysis.display_name || meta.instance_id || instanceId }
}

// The investigation's OWN database(s) as scope entries {instance_id, display_name}. An
// investigation can span one or more databases (its database_ids); a hand-off is scoped to
// exactly those, never the picker or the whole space. Prefers the live record from
// `databases` (for its real display name); otherwise builds a minimal entry per id. Returns
// [] when the investigation carries no database ids.
export function investigationDatabaseScope(investigationContext, databases = []) {
  const ids = investigationContext?.database_ids || []
  if (!ids.length) return []
  return ids.map(id => {
    const match = (databases || []).find(d => d && d.instance_id === id)
    return match || { instance_id: id, display_name: id }
  })
}
