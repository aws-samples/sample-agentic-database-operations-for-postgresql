// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Follow-up pills for report- and investigation-discussion chat.
//
// A report (or investigation) discussion stays INSIDE its own content: it has no live
// database connection and cannot start an investigation. The model's generic follow-up
// pills do not know that — they have offered "Yes, start a full investigation on
// aurora-pg-database-1" — so in these modes the client ignores the server's suggestions
// and derives its own from the report/investigation it already holds. Every pill here is
// grounded in the context's findings, metrics and recommendations and never proposes a
// live check or starting/launching an investigation.
//
// No React here, so tests/test_context_followups.mjs can exercise it directly.

const MAX = 4

function clean(list, limit = MAX) {
  const out = []
  const seen = new Set()
  for (const s of list) {
    const t = String(s || '').trim()
    if (t.length < 8) continue
    const key = t.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push(t)
    if (out.length >= limit) break
  }
  return out
}

function label(f) {
  if (typeof f === 'string') return f
  return (f && (f.title || f.area || f.summary || f.name)) || ''
}

// Follow-up questions grounded in a saved health report. Reference its findings,
// metrics and recommendations; never a live check, never an investigation.
export function generateReportFollowups(analysis) {
  const a = analysis || {}
  const findings = a.findings || []
  const critical = findings.filter(f => f && f.severity === 'CRITICAL')
  const warnings = findings.filter(f => f && f.severity === 'WARNING')
  const out = []
  for (const f of critical.slice(0, 2)) {
    const l = label(f)
    if (l) out.push(`What does the "${l}" finding mean, and how would I fix it?`)
  }
  for (const f of warnings.slice(0, 2)) {
    const l = label(f)
    if (l) out.push(`Why is "${l}" flagged as a warning in this report?`)
  }
  if (a.metrics && a.metrics.table_cache_hit_ratio) {
    out.push(`Is a ${a.metrics.table_cache_hit_ratio}% cache hit ratio good enough?`)
  }
  if ((a.recommendations || []).length > 0) {
    out.push('Explain the top recommendation in this report')
  }
  out.push('Which finding in this report should I prioritize?')
  out.push('Summarize the key findings in this report')
  return clean(out)
}

// Follow-up questions grounded in an investigation's own findings and conclusion.
// Never a live check; never starting another investigation.
export function generateInvestigationFollowups(inv) {
  const i = inv || {}
  const findings = i.findings || []
  const out = []
  for (const f of findings.slice(0, 3)) {
    const l = label(f)
    if (l) out.push(`What does the finding "${String(l).slice(0, 60)}" tell me?`)
  }
  if (i.summary) out.push('Explain the conclusion here in plain terms')
  out.push('What were the main findings here?')
  out.push('Based on these findings, what should I prioritize?')
  return clean(out)
}
