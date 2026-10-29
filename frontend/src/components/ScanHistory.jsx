// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import Pager from './Pager'

const statusColors = {
  SUCCEEDED: 'text-ok bg-ok-bg',
  FAILED: 'text-err bg-err-bg',
  RUNNING: 'text-info bg-info-bg',
  SUBMITTED: 'text-warn-mid bg-warn-bg',
  PENDING: 'text-warn-mid bg-warn-bg',
  RUNNABLE: 'text-warn-mid bg-warn-bg',
  STARTING: 'text-info bg-info-bg',
}

const PAGE_SIZE = 10

const HEALTH_BADGE = {
  ok: 'bg-ok-bg text-ok-text border-ok-border',
  warning: 'bg-warn-bg text-warn-text border-warn-border',
  critical: 'bg-err-bg text-err-text border-err-border',
  unknown: 'bg-surface-2 text-fg-3 border-line',
}
const HEALTH_CHIPS = [['', 'All'], ['critical', 'Critical'], ['warning', 'Warning'], ['ok', 'OK']]
const numV = (v) => (v === null || v === undefined || v === '') ? '—' : Number(v).toLocaleString()
const pctV = (v) => (v === null || v === undefined || v === '') ? '—' : `${Number(v).toFixed(1)}%`
const mbV = (v) => (v === null || v === undefined || v === '') ? '—' : Number(v) >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`
const toCompareSide = (row) => ({ id: row.scanId, key: row.key, scan_time: row.scanTime, health: row.health, report: row.report })

// Compare: pick two completed scans of THIS database and hand the pair to the host,
// which opens the side-by-side view. Each side carries the store row id when the
// scan has one (the exact scan) and always the S3 key (the fallback the API accepts).
//
// For a DB cluster the drawer's member picker chooses the view: the writer's deep
// scans (as below), or with a reader target the reader scans the Tier 2 job runs on
// each reader. Reader scans have no HTML report and are not compared; a row expands
// to its findings.
export default function ScanHistory(props) {
  const { member = '' } = props
  if (member && member !== 'writer') return <ReaderScans {...props} />
  return <DeepScans {...props} />
}

const SEV_CLS = { CRITICAL: 'text-err', WARNING: 'text-warn-text', INFO: 'text-fg-3' }

function ReaderScans({ api, instance, member, embedded = false }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(null)
  const [page, setPage] = useState(1)
  const id = instance?.instance_id
  useEffect(() => {
    if (!id) return
    let cancelled = false
    setLoading(true); setError(''); setOpen(null); setPage(1)
    api.get(`/api/scan-results?instance_id=${encodeURIComponent(id)}&days=365&limit=500&kind=reader&member=${encodeURIComponent(member)}`)
      .then(d => { if (!cancelled) setRows(d?.results || []) })
      .catch(e => { if (!cancelled) setError(e?.status === 503 ? 'The metrics store is not configured for this deployment.' : (e?.detail || e?.message || 'Could not load reader scans')) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [api, id, member])
  const fmt = (ts) => ts ? new Date(ts).toLocaleString() : '—'
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE))
  const paged = rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  return (
    <div className={embedded ? '' : 'bg-surface rounded-lg border border-line p-5 shadow-sm'}>
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <span className="text-[11px] text-fg-3">Reader scans: replica lag, recovery conflicts, long queries, connections and cache as seen from each reader. {rows.length} in the last year.</span>
        <Pager page={page} pageCount={totalPages} onPage={(p) => { setOpen(null); setPage(p) }} label="Reader scan pages" />
      </div>
      {loading ? (
        <div className="text-sm text-fg-muted py-4 text-center">Loading reader scans...</div>
      ) : error ? (
        <p className="text-xs text-err py-3">{error}</p>
      ) : rows.length === 0 ? (
        <div className="text-sm text-fg-muted py-4 text-center">No reader scans yet. The daily scan runs one on each reader after the writer's deep scan.</div>
      ) : (
        <table className="w-full text-xs">
          <caption className="sr-only">Reader scans of {instance?.display_name}</caption>
          <thead>
            <tr className="border-b border-line">
              <th scope="col" className="text-left py-2 px-2 font-medium text-fg-3"><span className="sr-only">Expand</span></th>
              <th scope="col" className="text-left py-2 px-2 font-medium text-fg-3">Kind</th>
              <th scope="col" className="text-left py-2 px-2 font-medium text-fg-3">Member</th>
              <th scope="col" className="text-left py-2 px-2 font-medium text-fg-3">Health</th>
              <th scope="col" className="text-left py-2 px-2 font-medium text-fg-3" title="Findings: critical · warning · total">Findings</th>
              <th scope="col" className="text-left py-2 px-2 font-medium text-fg-3">Completed</th>
              <th scope="col" className="text-left py-2 px-2 font-medium text-fg-3">Summary</th>
            </tr>
          </thead>
          <tbody>
            {paged.map(r => {
              const findings = Array.isArray(r.findings) ? r.findings : []
              const isOpen = open === r.id
              const crit = findings.filter(f => f?.severity === 'CRITICAL').length
              const warn = findings.filter(f => f?.severity === 'WARNING').length
              return [
                <tr key={r.id} className="border-b border-line-2 hover:bg-surface-3">
                  <td className="py-2 px-2 w-6">
                    {findings.length > 0 && (
                      <button onClick={() => setOpen(isOpen ? null : r.id)} aria-expanded={isOpen}
                        aria-label={isOpen ? 'Hide findings' : 'Show findings'} className="text-fg-muted hover:text-fg">{isOpen ? '▾' : '▸'}</button>
                    )}
                  </td>
                  <td className="py-2 px-2"><span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-info-bg text-info">Reader scan</span></td>
                  <td className="py-2 px-2 font-mono text-fg-2">{r.member_instance_id || '—'}</td>
                  <td className="py-2 px-2">
                    <span className={`inline-flex px-2 py-0.5 rounded-full text-[10px] font-medium border capitalize ${HEALTH_BADGE[r.health_score] || HEALTH_BADGE.unknown}`}>{r.health_score || 'unknown'}</span>
                  </td>
                  <td className="py-2 px-2 whitespace-nowrap tabular-nums">
                    <span className={crit ? 'text-err font-medium' : 'text-fg-muted'}>{crit}</span>
                    <span className="text-fg-muted"> · </span>
                    <span className={warn ? 'text-warn-text font-medium' : 'text-fg-muted'}>{warn}</span>
                    <span className="text-fg-muted"> · {findings.length}</span>
                  </td>
                  <td className="py-2 px-2 text-fg-2 whitespace-nowrap">{fmt(r.scan_time)}</td>
                  <td className="py-2 px-2 text-fg-3 truncate max-w-[360px]" title={r.summary}>{r.summary}</td>
                </tr>,
                isOpen && (
                  <tr key={`${r.id}-f`} className="border-b border-line-2 bg-surface-2">
                    <td colSpan={7} className="px-4 py-2">
                      <ul className="space-y-1">
                        {findings.map((f, i) => (
                          <li key={i} className="text-[11px]">
                            <span className={`font-semibold ${SEV_CLS[f.severity] || 'text-fg-3'}`}>{f.severity}</span>
                            <span className="text-fg"> {f.title}</span>
                            {f.detail && <span className="text-fg-3"> — {f.detail}</span>}
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                ),
              ]
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}

function DeepScans({ api, instance, onClose, onViewReport, onCompare, embedded = false }) {
  const [compareMode, setCompareMode] = useState(false)
  const [picked, setPicked] = useState([])   // up to two row keys, in click order
  const togglePick = (key) => setPicked(prev => prev.includes(key) ? prev.filter(k => k !== key) : prev.length >= 2 ? [prev[1], key] : [...prev, key])
  const [jobs, setJobs] = useState([])
  const [reports, setReports] = useState([])
  // Tier 2 rows from the store, keyed by their S3 report key. They ENRICH the S3-driven
  // rows below (health, findings, summary) and never replace them: the S3 list is the
  // durable record of every completed scan, whether or not the store is configured.
  const [results, setResults] = useState({})
  const [healthFilter, setHealthFilter] = useState('')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)

  // Merge new jobs with existing to prevent disappearing during status transitions
  const mergeJobs = (newJobs, existing) => {
    const map = new Map()
    existing.forEach(j => map.set(j.job_id, j))
    newJobs.forEach(j => map.set(j.job_id, j)) // newer data wins
    return [...map.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
  }

  const fetchData = (initial = false) => {
    if (!instance) return
    if (initial) setLoading(true)
    Promise.all([
      api.get(`/api/scan/${instance.instance_id}/jobs?limit=100`).then(d => d.jobs || []).catch(() => []),
      api.get(`/api/reports?instance_id=${instance.instance_id}&date_from=2020-01-01&date_to=2030-01-01`).then(d => Array.isArray(d) ? d : []).catch(() => []),
      // Fail-soft: a 503 (store not configured) simply leaves the rows un-enriched.
      api.get(`/api/scan-results?instance_id=${encodeURIComponent(instance.instance_id)}&days=365&limit=500`).then(d => d.results || []).catch(() => []),
    ]).then(([j, r, sr]) => {
      setJobs(prev => initial ? j : mergeJobs(j, prev))
      setReports(r)
      setResults(Object.fromEntries(sr.filter(x => x.report_s3_key).map(x => [x.report_s3_key, x])))
      if (initial) setPage(1)
    }).finally(() => setLoading(false))
  }

  useEffect(() => { fetchData(true) }, [instance?.instance_id])

  useEffect(() => {
    if (!jobs.some(j => ['RUNNING','STARTING','SUBMITTED','PENDING','RUNNABLE'].includes(j.status))) return
    const iv = setInterval(() => fetchData(), 5000)
    return () => clearInterval(iv)
  }, [jobs, instance?.instance_id])

  const fmt = (ts) => ts ? new Date(ts).toLocaleString() : '—'

  // Build the history rows. Successful scans come from the DURABLE S3 report list
  // (one HTML report per completed scan): that is complete and current, unlike the
  // Batch job list, which AWS retains only ~7 days and which the backend fetches
  // capped at 100-per-status across the whole queue, so a specific DB's recent
  // success can fall outside it. The Batch jobs are still used, but only to overlay
  // runs that have NO success report yet: in-progress scans and failures.
  const NON_TERMINAL = ['RUNNING', 'STARTING', 'SUBMITTED', 'PENDING', 'RUNNABLE']

  const buildRows = () => {
    const rows = []
    // 1. One row per successful scan, from S3 reports (durable, uncapped).
    reports.filter(r => r.type === 'html').forEach(r => {
      const sr = results[r.key]
      const findings = Array.isArray(sr?.findings) ? sr.findings : []
      rows.push({
        key: r.key,
        status: 'SUCCEEDED',
        started: null,                       // S3 records completion time, not start
        completed: r.last_modified || null,
        details: sr?.summary || `✅ ${r.report_name || 'Report generated'}`,
        report: r,
        scanId: sr?.id ?? null,
        scanTime: sr?.scan_time || r.last_modified || null,
        health: sr?.health_score || null,
        // The headline numerics the scan stored (the summary item), so the data sits
        // beside the report link rather than only inside it.
        data: sr ? {
          cache: sr.cache_hit_ratio, conns: sr.total_connections, size: sr.database_size_mb,
          dead: sr.dead_tuple_pct, xid: sr.xid_age_pct, unusedIdx: sr.unused_indexes_count,
          idxCache: sr.index_cache_hit_ratio, deadlocks: sr.deadlocks,
        } : null,
        critical: findings.filter(f => f?.severity === 'CRITICAL').length,
        warning: findings.filter(f => f?.severity === 'WARNING').length,
        total: findings.length,
        ts: r.last_modified ? Date.parse(r.last_modified) : 0,
      })
    })
    // 2. Overlay in-progress and failed runs from Batch (these have no success report).
    jobs.filter(j => NON_TERMINAL.includes(j.status) || j.status === 'FAILED').forEach(j => {
      rows.push({
        key: j.job_id,
        status: j.status,
        started: j.created_at || null,
        completed: j.stopped_at || null,
        details: j.status === 'FAILED' ? (j.status_reason || 'Scan failed')
          : (j.status_reason || 'In progress…'),
        report: null,
        ts: j.created_at || 0,
      })
    })
    return rows.sort((a, b) => (b.ts || 0) - (a.ts || 0))
  }

  const enriched = Object.keys(results).length > 0
  const q = query.trim().toLowerCase()
  const rows = buildRows().filter(row => {
    if (healthFilter && row.health !== healthFilter) return false
    if (q && !String(row.details || '').toLowerCase().includes(q)) return false
    return true
  })
  // Every scan in the window is loaded up front (the S3 list is the durable record),
  // so the pages are cut client-side; the shared Pager windows long runs of them.
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE))
  const pagedRows = rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  return (
    <div className={embedded ? '' : 'bg-surface rounded-lg border border-line p-5 shadow-sm'}>
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap">
          {!embedded && <h2 className="text-sm font-semibold text-fg">Scan History: {instance?.display_name}</h2>}
          {enriched && (
            <div className="flex items-center gap-1" role="group" aria-label="Filter by health">
              {HEALTH_CHIPS.map(([k, label]) => (
                <button key={k} onClick={() => { setHealthFilter(k); setPage(1) }} aria-pressed={healthFilter === k}
                  className={`px-2.5 py-1 text-[11px] rounded-full border transition-colors ${
                    healthFilter === k ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
                  {label}
                </button>
              ))}
            </div>
          )}
          {enriched && (
            <input type="search" value={query} onChange={e => { setQuery(e.target.value); setPage(1) }} placeholder="Search summaries…"
              className="text-[11px] bg-surface border border-line rounded px-2 py-1 w-44" aria-label="Search scan summaries" />
          )}
          {rows.length > 0 && (
            <span className="text-[10px] text-fg-muted">{rows.length} scans</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {onCompare && rows.filter(r => r.report).length >= 2 && (
            compareMode ? (
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-fg-3">{picked.length}/2 selected</span>
                <button onClick={() => { const [a, b] = picked.map(k => rows.find(r => r.key === k)); if (a && b) onCompare(toCompareSide(a), toCompareSide(b)) }}
                  disabled={picked.length !== 2}
                  className="px-2.5 py-1 text-[11px] font-medium rounded-md bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
                  Compare selected
                </button>
                <button onClick={() => { setCompareMode(false); setPicked([]) }} className="text-[11px] text-fg-3 hover:text-fg-2">Cancel</button>
              </div>
            ) : (
              <button onClick={() => setCompareMode(true)} title="Pick two scans of this database to compare side by side"
                className="px-2.5 py-1 text-[11px] font-medium rounded-md border border-line text-fg-2 hover:bg-surface-3 transition-colors">
                Compare…
              </button>
            )
          )}
          <Pager page={page} pageCount={totalPages} onPage={setPage} label="Deep scan pages" />
          <button onClick={fetchData} className="text-fg-muted hover:text-primary transition-colors" title="Refresh">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          {!embedded && <button onClick={onClose} className="text-xs text-fg-3 hover:text-fg-2">✕ Close</button>}
        </div>
      </div>
      {loading ? (
        <div className="text-sm text-fg-muted py-4 text-center">Loading scan history...</div>
      ) : rows.length === 0 ? (
        <div className="text-sm text-fg-muted py-4 text-center">No scans found</div>
      ) : (
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-line">
              {compareMode && <th className="py-2 px-2 font-medium text-fg-3 text-left w-8"><span className="sr-only">Select</span></th>}
              <th className="text-left py-2 px-2 font-medium text-fg-3">Status</th>
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3">Health</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3" title="Findings: critical · warning · total">Findings</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3" title="Buffer cache hit ratio · index cache hit ratio">Cache</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3">Conns</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3" title="Dead tuples, worst table">Dead</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3" title="Transaction ID age, % of wraparound">XID</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3">Size</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3" title="Unused indexes">Unused idx</th>}
              {enriched && <th className="text-left py-2 px-2 font-medium text-fg-3" title="Deadlocks (cumulative at scan time)">Deadlocks</th>}
              <th className="text-left py-2 px-2 font-medium text-fg-3">Started</th>
              <th className="text-left py-2 px-2 font-medium text-fg-3">Completed</th>
              <th className="text-left py-2 px-2 font-medium text-fg-3">{enriched ? 'Summary' : 'Details'}</th>
              <th className="text-right py-2 px-2 font-medium text-fg-3">Report</th>
            </tr>
          </thead>
          <tbody>
            {pagedRows.map(row => (
              <tr key={row.key} className={`border-b border-line-2 hover:bg-surface-3 ${compareMode && picked.includes(row.key) ? 'bg-primary-subtle' : ''}`}
                onClick={compareMode && row.report ? () => togglePick(row.key) : undefined} style={compareMode && row.report ? { cursor: 'pointer' } : undefined}>
                {compareMode && (
                  <td className="py-2 px-2">
                    {row.report ? (
                      <input type="checkbox" checked={picked.includes(row.key)} onChange={() => togglePick(row.key)} onClick={e => e.stopPropagation()}
                        aria-label={`Select scan completed ${fmt(row.completed)} for comparison`} className="accent-[var(--primary)]" />
                    ) : <span className="text-fg-muted">—</span>}
                  </td>
                )}
                <td className="py-2 px-2">
                  <span className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${statusColors[row.status] || 'text-fg-3 bg-surface-2'}`}>
                    {row.status}
                  </span>
                </td>
                {enriched && (
                  <td className="py-2 px-2">
                    {row.health
                      ? <span className={`inline-flex px-2 py-0.5 rounded-full text-[10px] font-medium border capitalize ${HEALTH_BADGE[row.health] || HEALTH_BADGE.unknown}`}>{row.health}</span>
                      : <span className="text-fg-muted">—</span>}
                  </td>
                )}
                {enriched && (
                  <td className="py-2 px-2 whitespace-nowrap tabular-nums">
                    {row.health ? (
                      <>
                        <span className={row.critical ? 'text-err font-medium' : 'text-fg-muted'}>{row.critical}</span>
                        <span className="text-fg-muted"> · </span>
                        <span className={row.warning ? 'text-warn-text font-medium' : 'text-fg-muted'}>{row.warning}</span>
                        <span className="text-fg-muted"> · {row.total}</span>
                      </>
                    ) : <span className="text-fg-muted">—</span>}
                  </td>
                )}
                {enriched && <td className="py-2 px-2 text-fg-2 whitespace-nowrap tabular-nums">{pctV(row.data?.cache)}{row.data?.idxCache != null && <span className="text-fg-muted"> · {pctV(row.data.idxCache)}</span>}</td>}
                {enriched && <td className="py-2 px-2 text-fg-2 tabular-nums">{numV(row.data?.conns)}</td>}
                {enriched && <td className={`py-2 px-2 tabular-nums ${Number(row.data?.dead) >= 20 ? 'text-warn-text font-medium' : 'text-fg-2'}`}>{pctV(row.data?.dead)}</td>}
                {enriched && <td className={`py-2 px-2 tabular-nums ${Number(row.data?.xid) >= 50 ? 'text-warn-text font-medium' : 'text-fg-2'}`}>{pctV(row.data?.xid)}</td>}
                {enriched && <td className="py-2 px-2 text-fg-2 whitespace-nowrap tabular-nums">{mbV(row.data?.size)}</td>}
                {enriched && <td className={`py-2 px-2 tabular-nums ${Number(row.data?.unusedIdx) > 0 ? 'text-warn-text' : 'text-fg-2'}`}>{numV(row.data?.unusedIdx)}</td>}
                {enriched && <td className="py-2 px-2 text-fg-2 tabular-nums">{numV(row.data?.deadlocks)}</td>}
                <td className="py-2 px-2 text-fg-2 whitespace-nowrap">{fmt(row.started)}</td>
                <td className="py-2 px-2 text-fg-2 whitespace-nowrap">{fmt(row.completed)}</td>
                <td className={`py-2 px-2 text-fg-3 truncate ${enriched ? 'max-w-[220px]' : 'max-w-[200px]'}`} title={row.details}>{row.details}</td>
                <td className="py-2 px-2 text-right">
                  {row.report ? (
                    <button onClick={(e) => { e.stopPropagation(); onViewReport?.(row.report) }} className="text-primary hover:underline">View →</button>
                  ) : (
                    <span className="text-fg-muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  )
}
