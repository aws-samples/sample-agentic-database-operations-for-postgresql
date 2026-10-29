// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'

// The scan's HTML report ships one inline <script> for its section navigator and
// its "Expand All" button. The frame below inherits this app's CSP (script-src
// 'self'), so that script never runs — and it never should, since the report is
// built from database-derived text. The report's own accordions are CSS-only
// (checkbox + label) and need nothing; the two script-driven pieces do, and the
// nav cards need one thing more: an `href="#section1"` in a srcdoc document
// resolves against the PARENT page's URL, so without a base of its own a click
// would load the app inside its own frame. Two script-free rewrites, applied to
// the HTML before it goes into the frame:
//   • A `<base href="about:srcdoc">` makes fragment links resolve against the
//     report's own URL, so a nav card scrolls within the report, and :target
//     rules open the section it landed on. (A blob: URL would give the report
//     its own base too, but Chrome refuses a sandboxed frame's navigation to a
//     blob URL — even a fragment of its own — so srcdoc it stays.) The section
//     header keeps working as a toggle afterwards: a targeted section is open
//     while its checkbox is unchecked and closed while it is checked.
//   • "Expand All" becomes a checkbox + label; sibling-combinator rules open
//     every accordion while it is checked.
// The same script also filled in the header's end time and duration (the header
// is written before the scan runs); those two values are lifted out of the
// script text and placed statically.
const REPORT_PATCH_HEAD = `
<base href="about:srcdoc">
<style>
  .section-header { scroll-margin-top: 12px; }
  /* A nav card landed here: open, with the open-state margin and glyph... */
  .section-header:target ~ .accordion-content { max-height: 10000px; }
  .accordion-checkbox:not(:checked) + .accordion-label.section-header:target { margin: 30px 0 20px 0; }
  .accordion-checkbox:not(:checked) + .accordion-label.section-header:target::after { content: '\\2212'; background: rgba(52, 152, 219, 0.2); transform: rotate(180deg); }
  /* ...until the header is clicked, which checks the box and closes it again. */
  .accordion-checkbox:checked ~ .section-header:target ~ .accordion-content { max-height: 0; }
  .accordion-checkbox:checked + .accordion-label.section-header:target { margin: 0 0 -14px 0; }
  .accordion-checkbox:checked + .accordion-label.section-header:target::after { content: '+'; background: rgba(52, 152, 219, 0.1); transform: none; }
  /* Expand All: every accordion open, whatever its own state. */
  .master-toggle-cb { display: none; }
  .master-toggle-cb:checked ~ .master-toggle::before { content: '\\25BC' ' Collapse All'; }
  .master-toggle-cb:checked ~ .section .accordion-content,
  .master-toggle-cb:checked ~ .section .accordion-checkbox:checked ~ .section-header:target ~ .accordion-content { max-height: 10000px; }
  .master-toggle-cb:checked ~ .section > .accordion-checkbox + .accordion-label.section-header { margin: 30px 0 20px 0; }
  .master-toggle-cb:checked ~ .section .accordion-checkbox + .accordion-label::after { content: '\\2212'; background: rgba(52, 152, 219, 0.2); transform: rotate(180deg); }
</style>
`
const escapeHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

export function prepareReportHtml(html) {
  if (typeof html !== 'string' || !html) return html
  let out = html.replace(
    /<button id="masterToggle" class="master-toggle"[^>]*><\/button>/,
    '<input type="checkbox" id="masterToggleCb" class="master-toggle-cb"><label for="masterToggleCb" id="masterToggle" class="master-toggle"></label>',
  )
  for (const [variable, spanId] of [['endTimeElement', 'end-time'], ['durationElement', 'duration']]) {
    const value = out.match(new RegExp(`${variable}\\.textContent = '([^']*)'`))?.[1]
    if (value) out = out.replace(new RegExp(`(<span id=['"]${spanId}['"]>)[^<]*(</span>)`), (_, open, close) => open + escapeHtml(value) + close)
  }
  // Nothing in it is needed (see above), and the sandbox would only log that it refused to run it.
  out = out.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
  const at = out.lastIndexOf('</head>')
  out = at >= 0 ? out.slice(0, at) + REPORT_PATCH_HEAD + out.slice(at) : REPORT_PATCH_HEAD + out
  return out
}

const severityConfig = {
  CRITICAL: { bg: 'bg-err-bg', border: 'border-err-border', badge: 'bg-err-bg-2 text-err-text', icon: '🔴' },
  WARNING: { bg: 'bg-warn-bg', border: 'border-warn-border', badge: 'bg-warn-bg-2 text-warn-text', icon: '🟡' },
  INFO: { bg: 'bg-info-bg', border: 'border-info-border', badge: 'bg-info-bg-2 text-info-text', icon: '🔵' },
  OK: { bg: 'bg-ok-bg', border: 'border-ok-border', badge: 'bg-ok-bg-2 text-ok-text', icon: '🟢' },
}

const scoreConfig = {
  critical: { bg: 'bg-err', text: 'text-white', label: 'CRITICAL' },
  warning: { bg: 'bg-warn', text: 'text-warn-inverse', label: 'WARNING' },
  ok: { bg: 'bg-ok', text: 'text-white', label: 'HEALTHY' },
  unknown: { bg: 'bg-fg-muted', text: 'text-white', label: 'UNKNOWN' },
}

function MetricPill({ label, value, unit }) {
  if (value === null || value === undefined) return null
  return (
    <div className="bg-surface-2 rounded-lg border border-line px-2.5 py-1.5 text-center min-w-[80px]">
      <div className="text-sm font-bold text-fg">{typeof value === 'number' ? value.toLocaleString() : value}{unit && <span className="text-[10px] text-fg-muted ml-0.5">{unit}</span>}</div>
      <div className="text-[10px] text-fg-muted uppercase tracking-wider">{label}</div>
    </div>
  )
}

function AnalysisBanner({ analysis }) {
  const [expanded, setExpanded] = useState(true)
  const [findingsOpen, setFindingsOpen] = useState(false)
  const [recsOpen, setRecsOpen] = useState(false)
  if (!analysis || !analysis.summary) return null

  const sc = scoreConfig[analysis.health_score] || scoreConfig.unknown
  const findings = analysis.findings || []
  const metrics = analysis.metrics || {}
  const recs = analysis.recommendations || []
  const critCount = findings.filter(f => f.severity === 'CRITICAL').length
  const warnCount = findings.filter(f => f.severity === 'WARNING').length

  return (
    <div className="border-b border-line bg-surface flex-shrink-0">
      {/* Header bar */}
      <div className="px-5 py-2.5 flex items-center justify-between bg-surface-2 border-b border-line-2">
        <div className="flex items-center gap-3">
          <div className={`w-7 h-7 rounded-lg flex items-center justify-center ${analysis.health_score === 'critical' ? 'bg-err' : analysis.health_score === 'warning' ? 'bg-warn' : 'bg-ok'}`}>
            <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
            </svg>
          </div>
          <span className="text-sm font-semibold text-fg">Health Analysis</span>
          <span className={`px-2.5 py-0.5 rounded-full text-[10px] font-bold ${sc.bg} ${sc.text}`}>{sc.label}</span>
          {critCount > 0 && <span className="text-[10px] text-err font-medium">{critCount} critical</span>}
          {warnCount > 0 && <span className="text-[10px] text-warn font-medium">{warnCount} warning</span>}
        </div>
        <button onClick={() => setExpanded(!expanded)}
          className="text-xs text-fg-muted hover:text-fg-2 flex items-center gap-1 transition-colors">
          <svg className={`w-3.5 h-3.5 transition-transform ${expanded ? '' : 'rotate-180'}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
          </svg>
          {expanded ? 'Collapse' : 'Expand'}
        </button>
      </div>

      {expanded && (
        <div className="px-5 py-4 space-y-4 max-h-[45vh] overflow-y-auto">
          {/* Summary */}
          <p className="text-[13px] text-fg-2 leading-relaxed">{analysis.summary}</p>

          {/* Key Metrics — compact row */}
          {Object.values(metrics).some(v => v !== null) && (
            <div className="flex flex-wrap gap-1.5">
              <MetricPill label="Table Cache Hit" value={metrics.table_cache_hit_ratio} unit="%" />
              <MetricPill label="Dead Tuples" value={metrics.dead_tuple_pct} unit="%" />
              <MetricPill label="XID Age" value={metrics.xid_age_pct} unit="%" />
              <MetricPill label="Connections" value={metrics.total_connections} />
              <MetricPill label="DB Size" value={metrics.database_size_mb} unit="MB" />
              <MetricPill label="Unused Indexes" value={metrics.unused_indexes_count} />
              <MetricPill label="Index Cache Hit" value={metrics.index_cache_hit_ratio} unit="%" />
              <MetricPill label="Deadlocks" value={metrics.deadlocks} />
            </div>
          )}

          {/* Findings — collapsible, collapsed by default */}
          {findings.length > 0 && (
            <div className="rounded-lg border border-line overflow-hidden">
              <button onClick={() => setFindingsOpen(!findingsOpen)}
                className="w-full flex items-center justify-between px-3 py-2 bg-surface-2 hover:bg-surface-3 transition-colors">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-semibold text-fg-2 uppercase tracking-wider">Findings</span>
                  <span className="text-[10px] text-fg-muted bg-surface-active px-1.5 py-0.5 rounded">{findings.length}</span>
                  {!findingsOpen && critCount > 0 && <span className="text-[10px] text-err font-medium">{critCount} critical</span>}
                  {!findingsOpen && warnCount > 0 && <span className="text-[10px] text-warn font-medium">{warnCount} warning</span>}
                </div>
                <svg className={`w-3.5 h-3.5 text-fg-muted transition-transform ${findingsOpen ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              {findingsOpen && (
                <div className="divide-y divide-line-2">
                  {findings.map((f, i) => {
                    const cfg = severityConfig[f.severity] || severityConfig.INFO
                    return (
                      <div key={i} className="px-3 py-2.5">
                        <div className="flex items-center gap-2 mb-0.5">
                          <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${cfg.badge}`}>{f.severity}</span>
                          <span className="text-xs font-semibold text-fg">{f.title || f.area}</span>
                        </div>
                        {f.detail && <p className="text-xs text-fg-3 mt-1 leading-relaxed">{f.detail}</p>}
                        {f.resolution && (
                          <div className="flex items-start gap-1.5 mt-1.5 pl-0.5">
                            <svg className="w-3 h-3 text-primary flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                            </svg>
                            <p className="text-[11px] text-fg-2"><span className="font-medium text-primary">Fix:</span> {f.resolution}</p>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {/* Recommendations — collapsible, collapsed by default */}
          {recs.length > 0 && (
            <div className="rounded-lg border border-line overflow-hidden">
              <button onClick={() => setRecsOpen(!recsOpen)}
                className="w-full flex items-center justify-between px-3 py-2 bg-surface-2 hover:bg-surface-3 transition-colors">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-semibold text-fg-2 uppercase tracking-wider">Recommendations</span>
                  <span className="text-[10px] text-fg-muted bg-surface-active px-1.5 py-0.5 rounded">{recs.length}</span>
                </div>
                <svg className={`w-3.5 h-3.5 text-fg-muted transition-transform ${recsOpen ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              {recsOpen && (
                <div className="divide-y divide-line-2">
                  {recs.map((r, i) => (
                    <div key={i} className="px-3 py-2 flex items-start gap-2">
                      <span className="text-primary font-bold text-xs mt-0.5 flex-shrink-0">{i + 1}.</span>
                      <span className="text-xs text-fg-2 leading-relaxed">{r}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function ReportPanel({ api, reportMeta, onClose, onAnalysisLoaded }) {
  const [reportData, setReportData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!reportMeta?.key) return
    setLoading(true)

    // Load the analysis for THIS report (matched on its own key), not the latest scan —
    // opening an older report otherwise showed its HTML under today's health and metrics.
    const loadFromAurora = Promise.race([
      // sections=all: the scan's nine structured sections, which the report discussion
      // (Chat's [REPORT CONTEXT]) answers from alongside the findings.
      api.get(`/api/scan-results/by-report?key=${encodeURIComponent(reportMeta.key)}&sections=all`),
      new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 5000)),
    ]).catch(() => null)

    Promise.all([
      api.get(`/api/reports/${reportMeta.key}`),
      loadFromAurora,
    ]).then(([s3Data, auroraData]) => {
      // The by-report route may answer with a store row (flat columns) or, when the store
      // has no row for the key, {report_s3_key, instance_id, analysis}. Prefer the store
      // row's richer analysis; otherwise fall through to the S3 analysis below.
      if (auroraData && auroraData.analysis && !auroraData.health_score) {
        setReportData({ ...s3Data, analysis: auroraData.analysis })
        onAnalysisLoaded?.(auroraData.analysis)
      } else if (auroraData && auroraData.health_score) {
        const enrichedAnalysis = {
          health_score: auroraData.health_score,
          summary: auroraData.summary,
          findings: auroraData.findings || [],
          metrics: {
            table_cache_hit_ratio: auroraData.cache_hit_ratio,
            dead_tuple_pct: auroraData.dead_tuple_pct,
            xid_age_pct: auroraData.xid_age_pct,
            total_connections: auroraData.total_connections,
            database_size_mb: auroraData.database_size_mb,
            unused_indexes_count: auroraData.unused_indexes_count,
            index_cache_hit_ratio: auroraData.index_cache_hit_ratio,
            deadlocks: auroraData.deadlocks,
          },
          sections: auroraData.sections_data || null,
          recommendations: (auroraData.findings || [])
            .filter(f => f.resolution)
            .map(f => f.resolution),
          display_name: auroraData.display_name,
          report_date: auroraData.scan_time,
        }
        setReportData({ ...s3Data, analysis: enrichedAnalysis })
        onAnalysisLoaded?.(enrichedAnalysis)
      } else {
        setReportData(s3Data)
        onAnalysisLoaded?.(s3Data?.analysis || null)
      }
    }).catch(() => { setReportData(null); onAnalysisLoaded?.(null) })
      .finally(() => setLoading(false))
  }, [reportMeta?.key])

  if (loading) return (
    <div className="h-full flex items-center justify-center">
      <div className="flex items-center gap-3 text-fg-3">
        <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        Loading report...
      </div>
    </div>
  )

  if (!reportData) return (
    <div className="h-full flex items-center justify-center">
      <p className="text-fg-muted">Failed to load report</p>
    </div>
  )

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Report header */}
      <div className="h-9 flex-shrink-0 border-b border-line px-4 flex items-center justify-between bg-surface-2">
        <div className="flex items-center gap-3">
          <svg className="w-4 h-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          <span className="text-xs font-medium text-fg-2">
            {reportMeta.report_name || `${reportMeta.instance_id?.slice(0, 8)} — ${reportMeta.date}`}
          </span>
        </div>
        <button onClick={onClose}
          className="p-1 text-fg-muted hover:text-fg-2 hover:bg-surface-3 rounded transition-colors" title="Close">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* AI Analysis */}
      {reportData.analysis && <AnalysisBanner analysis={reportData.analysis} />}

      {/* Report content.
          The report is HTML generated by the scan from database-derived text -
          table, index and column names reach it through EXPLAIN output, and the
          generator interpolates them without escaping. So this frame renders
          untrusted content, and the sandbox is what contains it. No
          `allow-same-origin`: a frame that shares this origin could reach
          parent.localStorage, where the session tokens live; the srcdoc
          document therefore has an opaque origin and nothing to reach for. No
          `allow-scripts` either: the report's script is CSP-blocked regardless
          (the frame inherits this app's script-src), and prepareReportHtml
          replaces the two things it did with CSS, so nothing needs to run. */}
      {reportData.html ? (
        <iframe srcDoc={prepareReportHtml(reportData.html)} title="Health Report"
          className="flex-1 w-full border-0" sandbox="" style={{ minHeight: 0 }} />
      ) : (
        <div className="flex-1 overflow-y-auto p-6">
          <pre className="text-xs text-fg-2 whitespace-pre-wrap">{JSON.stringify(reportData, null, 2)}</pre>
        </div>
      )}
    </div>
  )
}
