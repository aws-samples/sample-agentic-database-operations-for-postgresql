// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'

const SEV_COLORS = {
  CRITICAL: 'bg-err-bg text-err-text border-err-border',
  WARNING: 'bg-warn-bg text-warn-text border-warn-border',
  INFO: 'bg-info-bg text-info-text border-info-border',
  OK: 'bg-ok-bg text-ok-text border-ok-border',
}

const SCORE_COLORS = {
  critical: { bg: 'bg-err', text: 'text-white' },
  warning: { bg: 'bg-warn', text: 'text-warn-inverse' },
  ok: { bg: 'bg-ok', text: 'text-white' },
}

// When and what: from the scan record the API compared (its scan_time), so the labels
// name the exact scan — not a date parsed out of the S3 key, which is UTC and shared
// by every scan of that day. The key is the fallback for S3-only documents.
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
function scanMeta(side, key) {
  const t = side?.generated_at ? new Date(side.generated_at) : null
  if (t && !Number.isNaN(t.getTime())) {
    return {
      date: `${t.getDate()} ${MONTHS[t.getMonth()]} ${t.getFullYear()}`,
      time: t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    }
  }
  // Key format: accountId/instanceId/YYYY-MM-DD/displayname-dbname-engine-HHMMSS/report.html
  const parts = (key || '').split('/')
  const date = parts[2] || ''
  const timeMatch = (parts[3] || '').match(/(\d{6})$/)
  const time = timeMatch ? `${timeMatch[1].slice(0,2)}:${timeMatch[1].slice(2,4)} UTC` : ''
  const [y, m, d] = date.split('-')
  return { date: date ? `${parseInt(d)} ${MONTHS[parseInt(m)-1]} ${y}` : '—', time }
}

function MetricRow({ label, val1, val2, unit = '', higherIsBetter = true }) {
  if (val1 == null && val2 == null) return null
  const v1 = val1 != null ? (typeof val1 === 'number' ? val1.toLocaleString() : val1) : '—'
  const v2 = val2 != null ? (typeof val2 === 'number' ? val2.toLocaleString() : val2) : '—'
  let indicator = null
  if (val1 != null && val2 != null && val1 !== val2) {
    const improved = higherIsBetter ? val2 > val1 : val2 < val1
    const arrow = val2 > val1 ? '↑' : '↓'
    indicator = (
      <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${improved ? 'bg-ok-bg-2 text-ok-text' : 'bg-err-bg-2 text-err-text'}`}>
        {arrow} {Math.abs(val2 - val1).toLocaleString(undefined, { maximumFractionDigits: 2 })}{unit}
      </span>
    )
  } else {
    indicator = <span className="text-[10px] text-fg-muted">—</span>
  }
  return (
    <div className="grid grid-cols-4 items-center py-2.5 border-b border-line-2 last:border-0">
      <span className="text-xs text-fg-2">{label}</span>
      <span className="text-xs font-mono text-fg text-right">{v1}{unit}</span>
      <span className="text-xs font-mono text-fg text-right">{v2}{unit}</span>
      <div className="text-right">{indicator}</div>
    </div>
  )
}

function Section({ title, count, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-xl border border-line overflow-hidden">
      <button onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-5 py-3 bg-surface-2 hover:bg-surface-3 transition-colors">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold text-fg">{title}</h3>
          {count != null && <span className="text-[10px] text-fg-muted bg-surface-active px-1.5 py-0.5 rounded">{count}</span>}
        </div>
        <svg className={`w-4 h-4 text-fg-muted transition-transform ${open ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
      </button>
      {open && <div className="px-5 py-4 bg-surface">{children}</div>}
    </div>
  )
}

function FindingPill({ finding }) {
  const sev = (finding.severity || 'INFO').toUpperCase()
  return (
    <div className={`flex items-start gap-2.5 px-3 py-2 rounded-lg border ${SEV_COLORS[sev] || SEV_COLORS.INFO}`}>
      <span className="text-[10px] font-bold uppercase mt-0.5 flex-shrink-0">{sev}</span>
      <div className="min-w-0">
        <span className="text-xs font-medium">{finding.title}</span>
        {finding.detail && <p className="text-[10px] opacity-75 mt-0.5 line-clamp-2">{finding.detail}</p>}
      </div>
    </div>
  )
}

// Two scans of one database, side by side. `scan1`/`scan2` are `{ id?, key }` — the
// store row id identifies the exact scan; the key is what S3-only documents have. The
// API orders the pair oldest-first itself, so the sides are read from its response.
export default function ReportComparison({ api, scan1, scan2, onClose, onViewReport, instanceName }) {
  const [comparison, setComparison] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [aiSummary, setAiSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)

  const params = (scan1?.id != null && scan2?.id != null)
    ? new URLSearchParams({ id1: String(scan1.id), id2: String(scan2.id) })
    : new URLSearchParams({ key1: scan1?.key || '', key2: scan2?.key || '' })
  const paramKey = params.toString()

  useEffect(() => {
    if (!scan1 || !scan2) return
    setLoading(true)
    api.get(`/api/reports/compare?${paramKey}`)
      .then(data => { setComparison(data); setError(null) })
      .catch(err => setError(err.detail || err.message || 'Failed to compare'))
      .finally(() => setLoading(false))
  }, [paramKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const generateSummary = async () => {
    setSummaryLoading(true)
    try {
      const data = await api.post(`/api/reports/compare-summary?${paramKey}`)
      setAiSummary(data.summary)
    } catch (e) { setAiSummary('Failed to generate summary: ' + (e.detail || e.message)) }
    setSummaryLoading(false)
  }

  if (loading) return (
    <div className="flex items-center justify-center py-20">
      <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
    </div>
  )
  if (error) return (
    <div className="text-center py-12">
      <p className="text-sm text-err mb-2">{error}</p>
      <button onClick={onClose} className="text-sm text-primary hover:underline">Back</button>
    </div>
  )
  if (!comparison) return null

  const s = comparison.sections || {}
  const diff = comparison.findings_diff || {}
  const recs = comparison.recommendations || {}
  const reportKey1 = comparison.report1?.key
  const reportKey2 = comparison.report2?.key
  const meta1 = scanMeta(comparison.report1, reportKey1)
  const meta2 = scanMeta(comparison.report2, reportKey2)
  const dbName = instanceName || comparison.report1?.display_name || comparison.report1?.instance_id || ''
  const reportMeta = (side) => ({ key: side?.key, instance_id: side?.instance_id, date: (side?.generated_at || '').slice(0, 10), report_name: side?.display_name ? `${side.display_name} — ${scanMeta(side, side.key).date} ${scanMeta(side, side.key).time}` : undefined })
  const score1 = s.health_summary?.report1_score
  const score2 = s.health_summary?.report2_score
  const sc1 = SCORE_COLORS[score1] || { bg: 'bg-fg-muted', text: 'text-white' }
  const sc2 = SCORE_COLORS[score2] || { bg: 'bg-fg-muted', text: 'text-white' }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-fg">Compare deep scans{dbName ? ` — ${dbName}` : ''}</h2>
          <p className="text-xs text-fg-3">Two Tier 2 scans of the same database, oldest on the left</p>
        </div>
        <button onClick={onClose}
          className="flex items-center gap-1.5 text-xs text-fg-3 hover:text-primary transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          Close
        </button>
      </div>

      {/* Report cards — side by side */}
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-xl border border-line bg-surface p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] font-semibold text-fg-muted uppercase tracking-wider">Older scan</span>
            {score1 && <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${sc1.bg} ${sc1.text}`}>{score1.toUpperCase()}</span>}
          </div>
          <div className="text-sm font-semibold text-fg">{meta1.date}</div>
          <div className="text-[10px] text-fg-3 mt-0.5">{meta1.time}</div>
          {comparison.report1?.summary && <p className="text-[10px] text-fg-3 mt-2 line-clamp-2">{comparison.report1.summary}</p>}
          <button onClick={() => onViewReport?.(reportMeta(comparison.report1))}
            className="text-[10px] text-primary hover:underline mt-2 inline-block">View full report →</button>
        </div>
        <div className="rounded-xl border border-line bg-surface p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] font-semibold text-fg-muted uppercase tracking-wider">Newer scan</span>
            {score2 && <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${sc2.bg} ${sc2.text}`}>{score2.toUpperCase()}</span>}
          </div>
          <div className="text-sm font-semibold text-fg">{meta2.date}</div>
          <div className="text-[10px] text-fg-3 mt-0.5">{meta2.time}</div>
          {comparison.report2?.summary && <p className="text-[10px] text-fg-3 mt-2 line-clamp-2">{comparison.report2.summary}</p>}
          <button onClick={() => onViewReport?.(reportMeta(comparison.report2))}
            className="text-[10px] text-primary hover:underline mt-2 inline-block">View full report →</button>
        </div>
      </div>

      {/* AI Summary — on-demand */}
      <div className="rounded-xl border border-line bg-surface p-4">
        {aiSummary ? (
          <div>
            <div className="flex items-center gap-2 mb-2">
              <div className="w-6 h-6 rounded-lg bg-brand-gradient flex items-center justify-center">
                <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>
              </div>
              <span className="text-xs font-semibold text-fg">AI Comparison Summary</span>
            </div>
            <p className="text-[13px] text-fg-2 leading-relaxed">{aiSummary}</p>
          </div>
        ) : (
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <div className="w-6 h-6 rounded-lg bg-surface-3 flex items-center justify-center">
                <svg className="w-3.5 h-3.5 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>
              </div>
              <span className="text-xs text-fg-3">Generate an AI-powered summary of what changed</span>
            </div>
            <button onClick={generateSummary} disabled={summaryLoading}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-primary-fg bg-primary rounded-lg hover:bg-primary-hover disabled:opacity-50 transition-colors">
              {summaryLoading ? (
                <><div className="w-3 h-3 border-2 border-line border-t-transparent rounded-full animate-spin" /> Generating...</>
              ) : (
                <><svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg> Generate Summary</>
              )}
            </button>
          </div>
        )}
      </div>

      {/* Findings Diff */}
      {(diff.new?.length > 0 || diff.resolved?.length > 0 || diff.persistent?.length > 0) && (
        <Section title="Findings Changes" count={`${diff.new?.length || 0} new · ${diff.resolved?.length || 0} resolved · ${diff.persistent?.length || 0} persistent`} defaultOpen={false}>
          {diff.new?.length > 0 && (
            <div className="mb-4">
              <div className="flex items-center gap-2 mb-2">
                <div className="w-1.5 h-1.5 rounded-full bg-err" />
                <span className="text-[10px] font-semibold text-err uppercase tracking-wider">New Findings</span>
              </div>
              <div className="space-y-1.5">{diff.new.map((f, i) => <FindingPill key={i} finding={f} />)}</div>
            </div>
          )}
          {diff.resolved?.length > 0 && (
            <div className="mb-4">
              <div className="flex items-center gap-2 mb-2">
                <div className="w-1.5 h-1.5 rounded-full bg-ok" />
                <span className="text-[10px] font-semibold text-ok uppercase tracking-wider">Resolved</span>
              </div>
              <div className="space-y-1.5">{diff.resolved.map((f, i) => <FindingPill key={i} finding={f} />)}</div>
            </div>
          )}
          {diff.persistent?.length > 0 && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <div className="w-1.5 h-1.5 rounded-full bg-fg-muted" />
                <span className="text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Persistent</span>
              </div>
              <div className="space-y-1.5">{diff.persistent.map((f, i) => <FindingPill key={i} finding={f} />)}</div>
            </div>
          )}
        </Section>
      )}

      {/* Metrics — column headers */}
      <div className="rounded-xl border border-line overflow-hidden">
        <div className="grid grid-cols-4 px-5 py-2.5 bg-surface-2 border-b border-line">
          <span className="text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Metric</span>
          <span className="text-[10px] font-semibold text-fg-3 uppercase tracking-wider text-right">{meta1.date.split(' ').slice(0,2).join(' ')}</span>
          <span className="text-[10px] font-semibold text-fg-3 uppercase tracking-wider text-right">{meta2.date.split(' ').slice(0,2).join(' ')}</span>
          <span className="text-[10px] font-semibold text-fg-3 uppercase tracking-wider text-right">Change</span>
        </div>
        <div className="bg-surface px-5">
          {s.health_summary && <>
            <MetricRow label="Findings Count" val1={s.health_summary.report1_findings} val2={s.health_summary.report2_findings} higherIsBetter={false} />
          </>}
          {s.performance && <>
            <MetricRow label="Table Cache Hit" val1={s.performance.report1_table_cache} val2={s.performance.report2_table_cache} unit="%" />
            <MetricRow label="Index Cache Hit" val1={s.performance.report1_index_cache} val2={s.performance.report2_index_cache} unit="%" />
            <MetricRow label="Deadlocks" val1={s.performance.report1_deadlocks} val2={s.performance.report2_deadlocks} higherIsBetter={false} />
          </>}
          {s.maintenance && <>
            <MetricRow label="Dead Tuples" val1={s.maintenance.report1_dead_tuples} val2={s.maintenance.report2_dead_tuples} unit="%" higherIsBetter={false} />
            <MetricRow label="XID Age" val1={s.maintenance.report1_xid_age} val2={s.maintenance.report2_xid_age} unit="%" higherIsBetter={false} />
          </>}
          {s.optimization && <>
            <MetricRow label="Unused Indexes" val1={s.optimization.report1_unused_indexes} val2={s.optimization.report2_unused_indexes} higherIsBetter={false} />
            <MetricRow label="Connections" val1={s.optimization.report1_connections} val2={s.optimization.report2_connections} higherIsBetter={false} />
          </>}
          {s.capacity && <>
            <MetricRow label="DB Size" val1={s.capacity.report1_db_size} val2={s.capacity.report2_db_size} unit=" MB" higherIsBetter={false} />
          </>}
        </div>
      </div>

      {/* Detailed Diff — config changes, bloat changes, index changes */}
      {comparison.detailed_diff?.config_changes?.length > 0 && (
        <Section title="Configuration Changes" count={comparison.detailed_diff.config_changes.length} defaultOpen={false}>
          <div className="space-y-0">
            <div className="grid grid-cols-4 py-2 border-b border-line">
              <span className="text-[10px] font-semibold text-fg-3 uppercase">Parameter</span>
              <span className="text-[10px] font-semibold text-fg-3 uppercase text-right">Before</span>
              <span className="text-[10px] font-semibold text-fg-3 uppercase text-right">After</span>
              <span className="text-[10px] font-semibold text-fg-3 uppercase text-right">Unit</span>
            </div>
            {comparison.detailed_diff.config_changes.map((c, i) => (
              <div key={i} className="grid grid-cols-4 py-2 border-b border-line-2 last:border-0">
                <span className="text-xs text-fg-2 font-mono">{c.parameter}</span>
                <span className="text-xs text-fg-3 text-right font-mono">{c.old ?? '—'}</span>
                <span className="text-xs text-fg text-right font-mono font-medium">{c.new ?? '—'}</span>
                <span className="text-[10px] text-fg-muted text-right">{c.unit}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {comparison.detailed_diff?.bloat_changes?.length > 0 && (
        <Section title="Table Bloat Changes" count={comparison.detailed_diff.bloat_changes.length} defaultOpen={false}>
          <div className="space-y-0">
            <div className="grid grid-cols-4 py-2 border-b border-line">
              <span className="text-[10px] font-semibold text-fg-3 uppercase">Table</span>
              <span className="text-[10px] font-semibold text-fg-3 uppercase text-right">Before</span>
              <span className="text-[10px] font-semibold text-fg-3 uppercase text-right">After</span>
              <span className="text-[10px] font-semibold text-fg-3 uppercase text-right">Change</span>
            </div>
            {comparison.detailed_diff.bloat_changes.map((b, i) => {
              const delta = (b.new_dead_pct || 0) - (b.old_dead_pct || 0)
              const worse = delta > 0
              return (
                <div key={i} className="grid grid-cols-4 py-2 border-b border-line-2 last:border-0">
                  <span className="text-xs text-fg-2 font-mono truncate">{b.table}</span>
                  <span className="text-xs text-fg-3 text-right">{b.old_dead_pct != null ? `${b.old_dead_pct}%` : '—'}</span>
                  <span className="text-xs text-fg text-right font-medium">{b.new_dead_pct != null ? `${b.new_dead_pct}%` : '—'}</span>
                  <div className="text-right">
                    {delta !== 0 && <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${worse ? 'bg-err-bg-2 text-err-text' : 'bg-ok-bg-2 text-ok-text'}`}>{worse ? '↑' : '↓'} {Math.abs(delta).toFixed(2)}%</span>}
                  </div>
                </div>
              )
            })}
          </div>
        </Section>
      )}

      {comparison.detailed_diff?.index_changes && (comparison.detailed_diff.index_changes.new_unused?.length > 0 || comparison.detailed_diff.index_changes.removed_unused?.length > 0) && (
        <Section title="Index Changes" defaultOpen={false}>
          {comparison.detailed_diff.index_changes.new_unused?.length > 0 && (
            <div className="mb-3">
              <div className="text-[10px] font-semibold text-err uppercase tracking-wider mb-1.5">New Unused Indexes</div>
              {comparison.detailed_diff.index_changes.new_unused.map((idx, i) => (
                <div key={i} className="text-xs text-fg-2 py-1 font-mono">{idx.indexrelname} ({idx.size})</div>
              ))}
            </div>
          )}
          {comparison.detailed_diff.index_changes.removed_unused?.length > 0 && (
            <div>
              <div className="text-[10px] font-semibold text-ok uppercase tracking-wider mb-1.5">Indexes Now Being Used</div>
              {comparison.detailed_diff.index_changes.removed_unused.map((idx, i) => (
                <div key={i} className="text-xs text-fg-2 py-1 font-mono">{idx.indexrelname} ({idx.size})</div>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* Recommendations side-by-side */}
      {(recs.report1?.length > 0 || recs.report2?.length > 0) && (
        <Section title="Recommendations" count={`${recs.report1?.length || 0} → ${recs.report2?.length || 0}`} defaultOpen={false}>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="text-[10px] font-semibold text-fg-muted uppercase tracking-wider mb-2">{meta1.date.split(' ').slice(0,2).join(' ')}</div>
              <div className="space-y-2">
                {(recs.report1 || []).map((r, i) => (
                  <div key={i} className="text-[11px] text-fg-2 flex items-start gap-1.5 bg-surface-2 rounded-lg px-2.5 py-2">
                    <span className="text-primary font-bold flex-shrink-0">{i+1}.</span>
                    <span className="line-clamp-3">{r}</span>
                  </div>
                ))}
                {!recs.report1?.length && <p className="text-[10px] text-fg-muted">No recommendations</p>}
              </div>
            </div>
            <div>
              <div className="text-[10px] font-semibold text-fg-muted uppercase tracking-wider mb-2">{meta2.date.split(' ').slice(0,2).join(' ')}</div>
              <div className="space-y-2">
                {(recs.report2 || []).map((r, i) => (
                  <div key={i} className="text-[11px] text-fg-2 flex items-start gap-1.5 bg-surface-2 rounded-lg px-2.5 py-2">
                    <span className="text-primary font-bold flex-shrink-0">{i+1}.</span>
                    <span className="line-clamp-3">{r}</span>
                  </div>
                ))}
                {!recs.report2?.length && <p className="text-[10px] text-fg-muted">No recommendations</p>}
              </div>
            </div>
          </div>
        </Section>
      )}
    </div>
  )
}
