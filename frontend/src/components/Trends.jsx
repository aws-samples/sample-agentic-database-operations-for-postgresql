// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo } from 'react'

import { COLOR, HEALTH_COLORS, SERIES_COLORS as METRIC_COLORS, colorId } from '../theme'
import { canonicalEngine, engineLabel } from './DbListControls'

// Tier 1 lines come from CloudWatch (15 months kept) and deep scans are kept for years,
// so a year is in reach.
const TIME_RANGES = [
  { value: 7, label: '7d' },
  { value: 30, label: '30d' },
  { value: 90, label: '90d' },
  { value: 365, label: '1y' },
]

const SHORT_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
function sampleAge(iso) {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000))
  return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`
}
function fmtDate(d) { if (!d) return ''; const [,m,day] = d.split('-'); return `${parseInt(day)} ${SHORT_MONTHS[parseInt(m)-1]}` }

// The headline metrics the fleet series carries by default (the API's FLEET_SERIES_METRICS,
// with `connections` as the API renames total_connections) plus the two deep-scan values
// the Tier 2 summaries carry. Each is in the first load, so switching between them is
// instant.
const METRICS = [
  { key: 'cache_hit_ratio', label: 'Cache Hit Ratio', unit: '%', good: 'high', threshold: [99, 90] },
  { key: 'dead_tuple_pct', label: 'Dead Tuples', unit: '%', good: 'low', threshold: [5, 20] },
  { key: 'xid_age_pct', label: 'XID Age', unit: '%', good: 'low', threshold: [50, 80] },
  { key: 'connections', label: 'Connections', unit: '', good: 'low', threshold: [100, 500] },
  { key: 'database_size_mb', label: 'DB Size', unit: 'MB', good: null, threshold: null },
  { key: 'blocked_queries', label: 'Blocked Queries', unit: '', good: 'low', threshold: [1, 5] },
  { key: 'deadlocks_delta', label: 'Deadlocks', unit: '', good: 'low', threshold: [1, 5] },
  { key: 'txn_per_sec', label: 'Txn / s', unit: '', good: null, threshold: null },
  { key: 'rollback_ratio_pct', label: 'Rollback Ratio', unit: '%', good: 'low', threshold: [2, 10] },
  { key: 'longest_transaction_sec', label: 'Longest Txn (s)', unit: '', good: 'low', threshold: [300, 3600] },
  { key: 'replica_lag_ms', label: 'Replica Lag (ms)', unit: '', good: 'low', threshold: [1000, 10000] },
  { key: 'unused_indexes', label: 'Unused Indexes', unit: '', good: 'low', threshold: [5, 20] },
  { key: 'index_cache_hit_ratio', label: 'Index Cache Hit', unit: '%', good: 'high', threshold: [99, 90] },
]

// Every OTHER series the deployment publishes, chartable across the fleet on demand:
// picking one fetches that single series for every database (one CloudWatch read),
// so the full set is reachable without loading it all up front. Grouped for the picker.
const MORE_METRICS = [
  // Tier 1 — the collector's numerics
  { key: 'connections_used_pct', label: 'Connections Used', unit: '%', good: 'low', threshold: [80, 95], group: 'Tier 1' },
  { key: 'active_connections', label: 'Active Connections', unit: '', good: null, group: 'Tier 1' },
  { key: 'idle_connections', label: 'Idle Connections', unit: '', good: null, group: 'Tier 1' },
  { key: 'idle_in_txn_connections', label: 'Idle in Transaction', unit: '', good: 'low', threshold: [1, 10], group: 'Tier 1' },
  { key: 'waiting_queries', label: 'Waiting Queries', unit: '', good: 'low', threshold: [1, 10], group: 'Tier 1' },
  { key: 'autovacuum_workers_active', label: 'Autovacuum Workers', unit: '', good: null, group: 'Tier 1' },
  { key: 'tables_needing_vacuum', label: 'Tables Needing Vacuum', unit: '', good: 'low', threshold: [1, 10], group: 'Tier 1' },
  { key: 'temp_files', label: 'Temp Files', unit: '', good: null, group: 'Tier 1' },
  { key: 'temp_bytes_mb', label: 'Temp Bytes', unit: 'MB', good: null, group: 'Tier 1' },
  // Deep scan — the daily numerics each scan publishes
  { key: 'findings_total', label: 'Findings', unit: '', good: 'low', group: 'Deep scan' },
  { key: 'findings_critical', label: 'Critical Findings', unit: '', good: 'low', threshold: [1, 3], group: 'Deep scan' },
  { key: 'findings_warning', label: 'Warning Findings', unit: '', good: 'low', group: 'Deep scan' },
  { key: 'unused_indexes_count', label: 'Unused Indexes (scan)', unit: '', good: 'low', threshold: [5, 20], group: 'Deep scan' },
  { key: 'duplicate_indexes_count', label: 'Duplicate Indexes', unit: '', good: 'low', threshold: [1, 5], group: 'Deep scan' },
  { key: 'invalid_indexes_count', label: 'Invalid Indexes', unit: '', good: 'low', threshold: [1, 1], group: 'Deep scan' },
  { key: 'fk_without_index_count', label: 'FKs Without Index', unit: '', good: 'low', threshold: [1, 5], group: 'Deep scan' },
  { key: 'seq_scan_heavy_tables_count', label: 'Seq-Scan-Heavy Tables', unit: '', good: 'low', threshold: [1, 5], group: 'Deep scan' },
  { key: 'unused_tables_count', label: 'Unused Tables', unit: '', good: 'low', group: 'Deep scan' },
  { key: 'unvalidated_constraints_count', label: 'Unvalidated Constraints', unit: '', good: 'low', threshold: [1, 1], group: 'Deep scan' },
  { key: 'stale_statistics_tables_count', label: 'Stale Statistics', unit: '', good: 'low', threshold: [1, 5], group: 'Deep scan' },
  { key: 'never_vacuumed_tables_count', label: 'Never Vacuumed', unit: '', good: 'low', threshold: [1, 5], group: 'Deep scan' },
  { key: 'max_sequence_used_pct', label: 'Max Sequence Used', unit: '%', good: 'low', threshold: [50, 80], group: 'Deep scan' },
  { key: 'forced_checkpoint_ratio_pct', label: 'Forced Checkpoints', unit: '%', good: 'low', threshold: [10, 30], group: 'Deep scan' },
  { key: 'autovacuum_worker_utilization_pct', label: 'Autovacuum Utilisation', unit: '%', good: 'low', threshold: [80, 95], group: 'Deep scan' },
  { key: 'temp_file_queries_count', label: 'Temp-File Queries', unit: '', good: 'low', threshold: [1, 5], group: 'Deep scan' },
  { key: 'top_query_total_exec_ms', label: 'Top Query Total (ms)', unit: '', good: null, group: 'Deep scan' },
  { key: 'top_query_mean_exec_ms', label: 'Top Query Mean (ms)', unit: '', good: 'low', group: 'Deep scan' },
  { key: 'top_query_wal_mb', label: 'Top Query WAL', unit: 'MB', good: null, group: 'Deep scan' },
  { key: 'connection_utilization_pct', label: 'Connection Util. at Scan', unit: '%', good: 'low', threshold: [80, 95], group: 'Deep scan' },
  { key: 'settings_outside_recommendation_count', label: 'Settings Outside Rec.', unit: '', good: 'low', threshold: [1, 5], group: 'Deep scan' },
  { key: 'high_risk_roles_count', label: 'High-Risk Roles', unit: '', good: 'low', threshold: [1, 3], group: 'Deep scan' },
  { key: 'plain_connections_count', label: 'Unencrypted Connections', unit: '', good: 'low', threshold: [1, 1], group: 'Deep scan' },
]
const ALL_METRICS = [...METRICS, ...MORE_METRICS]
const metricDef = (key) => ALL_METRICS.find(m => m.key === key)

// ── Utility functions ──
function formatVal(v, unit) {
  if (v === null || v === undefined) return '—'
  if (unit === '%') return `${Number(v).toFixed(1)}%`
  if (unit === 'MB') return v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`
  return String(Math.round(v))
}

function trendArrow(curr, prev, good) {
  if (curr === null || prev === null || curr === undefined || prev === undefined) return null
  const diff = curr - prev
  if (Math.abs(diff) < 0.01) return { dir: '→', cls: 'text-fg-muted' }
  const up = diff > 0
  if (good === 'high') return up ? { dir: '↑', cls: 'text-ok' } : { dir: '↓', cls: 'text-err' }
  if (good === 'low') return up ? { dir: '↑', cls: 'text-err' } : { dir: '↓', cls: 'text-ok' }
  return { dir: up ? '↑' : '↓', cls: 'text-fg-muted' }
}

// ── SVG Mini Chart (sparkline) ──
function SparkLine({ points, color = COLOR.primary, height = 32, width = 120 }) {
  // A scan without the metric is a gap, not a zero.
  const vals = (points || []).filter(p => p !== null && p !== undefined && p !== '').map(Number).filter(Number.isFinite)
  if (vals.length < 2) return <div style={{ width, height }} className="bg-surface-2 rounded" />
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const range = max - min || 1
  const step = width / (vals.length - 1)
  const pathPoints = vals.map((v, i) => `${i * step},${height - 2 - ((v - min) / range) * (height - 4)}`).join(' ')
  const areaPath = `M0,${height} L${pathPoints} L${width},${height} Z`
  return (
    <svg width={width} height={height} className="flex-shrink-0">
      <defs><linearGradient id={`sg-${colorId(color)}`} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" style={{ stopColor: color, stopOpacity: 0.2 }} /><stop offset="100%" style={{ stopColor: color, stopOpacity: 0 }} />
      </linearGradient></defs>
      <path d={areaPath} fill={`url(#sg-${colorId(color)})`} />
      <polyline points={pathPoints} fill="none" style={{ stroke: color }} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={(vals.length - 1) * step} cy={height - 2 - ((vals[vals.length - 1] - min) / range) * (height - 4)} r="2.5" style={{ fill: color }} />
    </svg>
  )
}

// ── Stacked Bar Chart ──
function StackedBar({ critical = 0, warning = 0, info = 0, height = 20 }) {
  const total = critical + warning + info
  if (!total) return <div style={{ height }} className="w-full bg-surface-3 rounded" />
  const cw = (critical / total) * 100
  const ww = (warning / total) * 100
  return (
    <div className="w-full flex rounded overflow-hidden" style={{ height }}>
      {critical > 0 && <div style={{ width: `${cw}%` }} className="bg-err" title={`${critical} critical`} />}
      {warning > 0 && <div style={{ width: `${ww}%` }} className="bg-warn" title={`${warning} warning`} />}
      {info > 0 && <div className="bg-info flex-1" title={`${info} info`} />}
    </div>
  )
}

// ── Health Heatmap ──
function HealthHeatmap({ databases, dates, onSelectDb }) {
  // Build lookup: { dbId: { date: health_score } }. Computed unconditionally so the
  // hook order stays stable when the filtered list is empty (the guard is below).
  const lookup = useMemo(() => {
    const m = {}
    databases.forEach(db => {
      m[db.instance_id] = {}
      db.data_points?.forEach(p => { m[db.instance_id][p.date] = p.health_score })
    })
    return m
  }, [databases, dates])

  if (!databases.length || !dates.length) return null

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[10px]">
        <thead>
          <tr>
            <th className="text-left py-1.5 pr-3 text-fg-3 font-medium sticky left-0 bg-surface z-10 min-w-[140px]">Database</th>
            {dates.map(d => (
              <th key={d} className="px-0.5 py-1.5 text-fg-muted font-normal whitespace-nowrap">
                {fmtDate(d)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {databases.map(db => (
            <tr key={db.instance_id} className="group cursor-pointer" onClick={() => onSelectDb?.(db)}>
              <td className="py-1 pr-3 text-fg-2 font-medium sticky left-0 bg-surface z-10 group-hover:text-primary transition-colors truncate max-w-[140px]" title={db.display_name}>
                {db.display_name}
              </td>
              {dates.map(d => {
                const score = lookup[db.instance_id]?.[d]
                return (
                  <td key={d} className="px-0.5 py-1">
                    <div
                      className="w-full h-5 rounded-sm transition-opacity hover:opacity-80"
                      style={{ backgroundColor: score ? (HEALTH_COLORS[score] || HEALTH_COLORS.unknown) : COLOR.surface3, minWidth: 14 }}
                      title={`${db.display_name} · ${d} · ${(score || 'no data').toUpperCase()}`}
                    />
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Fleet Summary Cards ──
function FleetSummary({ databases }) {
  const stats = useMemo(() => {
    let critical = 0, warning = 0, ok = 0, noData = 0
    let totalFindings = 0, totalCritical = 0
    databases.forEach(db => {
      // Health and findings come from the latest DEEP scan, not the latest point of any
      // tier. A Tier 1-only latest day carries no health and would otherwise count as
      // noData and zero findings even when a recent deep scan exists.
      const latest = db.latest_deep_scan
      if (!latest) { noData++; return }
      if (latest.health_score === 'critical') critical++
      else if (latest.health_score === 'warning') warning++
      else if (latest.health_score === 'ok') ok++
      else noData++
      totalFindings += latest.findings_total || 0
      totalCritical += latest.findings_critical || 0
    })
    return { critical, warning, ok, noData, totalFindings, totalCritical, total: databases.length }
  }, [databases])

  const cards = [
    { label: 'Databases', value: stats.total, sub: `${stats.ok} healthy`, color: COLOR.primary },
    { label: 'Critical', value: stats.critical, sub: 'need attention', color: stats.critical > 0 ? COLOR.err : COLOR.ok },
    { label: 'Warnings', value: stats.warning, sub: 'databases', color: stats.warning > 0 ? COLOR.warn : COLOR.ok },
    { label: 'Total Findings', value: stats.totalFindings, sub: `${stats.totalCritical} critical`, color: stats.totalCritical > 0 ? COLOR.err : COLOR.muted },
  ]

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      {cards.map(c => (
        <div key={c.label} className="bg-surface rounded-lg border border-line p-3">
          <div className="text-[10px] text-fg-3 uppercase tracking-wider">{c.label}</div>
          <div className="text-2xl font-bold mt-0.5" style={{ color: c.color }}>{c.value}</div>
          <div className="text-[10px] text-fg-muted mt-0.5">{c.sub}</div>
        </div>
      ))}
    </div>
  )
}

// ── Metric Comparison Table ──
function MetricTable({ databases, selectedMetric, onSelectMetric, extraLoading = false, sort = 'name', onSort }) {
  const metric = metricDef(selectedMetric)
  const isMore = MORE_METRICS.some(m => m.key === selectedMetric)
  // Rank: worst first on the chosen metric, so the database needing attention is on top.
  const ranked = useMemo(() => {
    const cur = (db) => { const pts = db.data_points?.map(p => p[selectedMetric]).filter(v => v !== null && v !== undefined) || []; return pts.length ? pts[pts.length - 1] : null }
    if (sort !== 'worst' || !metric?.good) return databases
    return [...databases].sort((a, b) => {
      const va = cur(a), vb = cur(b)
      if (va === null && vb === null) return 0
      if (va === null) return 1
      if (vb === null) return -1
      return metric.good === 'high' ? va - vb : vb - va
    })
  }, [databases, selectedMetric, sort, metric])
  return (
    <div>
      {/* Metric selector: the headline set as pills, every other series in a grouped picker */}
      <div className="flex flex-wrap items-center gap-1.5 mb-4">
        {METRICS.map(m => (
          <button key={m.key} onClick={() => onSelectMetric(m.key)}
            className={`px-2.5 py-1 text-[10px] rounded-md border transition-colors ${
              selectedMetric === m.key
                ? 'bg-primary-subtle text-primary border-primary-line font-semibold'
                : 'text-fg-3 border-line hover:border-line-strong'
            }`}>
            {m.label}
          </button>
        ))}
        <label className="flex items-center gap-1.5 text-[10px] text-fg-3 ml-1">
          <span className="sr-only">More metrics</span>
          <select value={isMore ? selectedMetric : ''} onChange={e => e.target.value && onSelectMetric(e.target.value)}
            aria-label="Every other published series"
            className={`text-[10px] rounded-md border px-2 py-1 bg-surface ${isMore ? 'border-primary-line text-primary font-semibold' : 'border-line text-fg-3'}`}>
            <option value="">{isMore ? metric?.label : 'More metrics…'}</option>
            {['Tier 1', 'Deep scan'].map(g => (
              <optgroup key={g} label={g}>
                {MORE_METRICS.filter(m => m.group === g).map(m => <option key={m.key} value={m.key}>{m.label}</option>)}
              </optgroup>
            ))}
          </select>
        </label>
        {extraLoading && <span className="text-[10px] text-fg-muted">loading series…</span>}
        {metric?.good && onSort && (
          <button onClick={() => onSort(sort === 'worst' ? 'name' : 'worst')} aria-pressed={sort === 'worst'}
            className={`ml-auto px-2.5 py-1 text-[10px] rounded-md border transition-colors ${sort === 'worst' ? 'bg-primary-subtle text-primary border-primary-line font-semibold' : 'text-fg-3 border-line hover:border-line-strong'}`}
            title="Order the fleet worst-first on this metric">
            Worst first
          </button>
        )}
      </div>

      {/* Table with sparklines */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-[10px] text-fg-3 border-b border-line-2">
              <th className="py-2 pr-3 font-medium">Database</th>
              <th className="py-2 px-3 font-medium">Current</th>
              <th className="py-2 px-3 font-medium">Trend</th>
              <th className="py-2 px-3 font-medium w-[140px]">Sparkline</th>
              <th className="py-2 px-3 font-medium">Change</th>
            </tr>
          </thead>
          <tbody>
            {ranked.map((db, di) => {
              const points = db.data_points?.map(p => p[selectedMetric]) || []
              const current = points.length > 0 ? points[points.length - 1] : null
              const prev = points.length > 1 ? points[points.length - 2] : null
              const arrow = trendArrow(current, prev, metric?.good)
              const color = METRIC_COLORS[di % METRIC_COLORS.length]

              return (
                <tr key={db.instance_id} className="border-b border-line-2 hover:bg-surface-3 transition-colors">
                  <td className="py-2.5 pr-3">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: color }} />
                      <span className="font-medium text-fg-2 truncate max-w-[160px]" title={db.display_name}>{db.display_name}</span>
                    </div>
                  </td>
                  <td className="py-2.5 px-3 font-mono text-fg">{formatVal(current, metric?.unit)}</td>
                  <td className="py-2.5 px-3">
                    {arrow && <span className={`text-xs font-medium ${arrow.cls}`}>{arrow.dir}</span>}
                  </td>
                  <td className="py-2.5 px-3"><SparkLine points={points} color={color} width={130} height={28} /></td>
                  <td className="py-2.5 px-3 text-[10px] text-fg-3">
                    {current !== null && prev !== null ? `${(current - prev) > 0 ? '+' : ''}${(current - prev).toFixed(1)}${metric?.unit || ''}` : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Findings Trend Chart ──
function FindingsTrend({ databases, dates }) {
  // Aggregate findings per date across all databases
  const aggregated = useMemo(() => {
    const byDate = {}
    dates.forEach(d => { byDate[d] = { critical: 0, warning: 0, info: 0, total: 0 } })
    databases.forEach(db => {
      db.data_points?.forEach(p => {
        if (byDate[p.date]) {
          const c = p.findings_critical || 0
          const w = p.findings_warning || 0
          const t = p.findings_total || 0
          const inf = p.findings_info || Math.max(0, t - c - w)
          byDate[p.date].critical += c
          byDate[p.date].warning += w
          byDate[p.date].info += inf
          byDate[p.date].total += t
        }
      })
    })
    return dates.map(d => ({ date: d, ...byDate[d] }))
  }, [databases, dates])

  if (!aggregated.length) return null
  const maxTotal = Math.max(...aggregated.map(a => a.total), 1)

  const chartHeight = 80

  return (
    <div>
      <div className="flex gap-0.5 items-end" style={{ height: chartHeight }}>
        {aggregated.map((a, i) => {
          const barH = Math.max((a.total / maxTotal) * chartHeight, 2)
          const total = a.critical + a.warning + a.info || 1
          return (
            <div key={i} className="flex-1 flex flex-col justify-end group" style={{ height: chartHeight }}
              title={`${a.date}: ${a.critical}C / ${a.warning}W / ${a.info}I`}>
              {a.critical > 0 && <div style={{ height: (a.critical / total) * barH }} className="bg-err w-full" />}
              {a.warning > 0 && <div style={{ height: (a.warning / total) * barH }} className="bg-warn w-full" />}
              {a.info > 0 && <div style={{ height: (a.info / total) * barH }} className="bg-info w-full" />}
            </div>
          )
        })}
      </div>
      <div className="flex justify-between text-[10px] text-fg-muted mt-1.5">
        <span>{fmtDate(dates[0])}</span>
        <span>{fmtDate(dates[dates.length - 1])}</span>
      </div>
      <div className="flex items-center gap-4 mt-2 text-[10px] text-fg-3">
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-err" />Critical</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-warn" />Warning</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-info" />Info</span>
      </div>
    </div>
  )
}

// ── Per-Database Detail Panel ──
function DatabaseDetail({ db, onBack }) {
  if (!db || !db.data_points?.length) return null
  const points = db.data_points
  const latest = points[points.length - 1]
  const prev = points.length > 1 ? points[points.length - 2] : null

  return (
    <div>
      <button onClick={onBack} className="flex items-center gap-1.5 text-sm text-fg-3 hover:text-primary transition-colors mb-4">
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" /></svg>
        Back to fleet
      </button>

      <div className="flex items-center gap-3 mb-5">
        <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white flex-shrink-0" style={{ backgroundColor: HEALTH_COLORS[latest?.health_score] || HEALTH_COLORS.unknown }}>
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M12 3C7.58 3 4 4.79 4 7v10c0 2.21 3.58 4 8 4s8-1.79 8-4V7c0-2.21-3.58-4-8-4z"/></svg>
        </div>
        <div>
          <h3 className="text-sm font-bold text-fg">{db.display_name}</h3>
          <p className="text-[10px] text-fg-3">{db.engine} · {db.scan_count ?? points.length} scans on {points.length} days</p>
        </div>
      </div>

      {/* Metric cards */}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mb-5">
        {METRICS.map(m => {
          const curr = latest?.[m.key]
          const p = prev?.[m.key]
          const arrow = trendArrow(curr, p, m.good)
          const sparkPoints = points.map(pt => pt[m.key])
          return (
            <div key={m.key} className="bg-surface rounded-lg border border-line p-3">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] text-fg-3 uppercase tracking-wider">{m.label}</span>
                {arrow && <span className={`text-[10px] font-medium ${arrow.cls}`}>{arrow.dir}</span>}
              </div>
              <div className="text-lg font-bold text-fg">{formatVal(curr, m.unit)}</div>
              <SparkLine points={sparkPoints} color={COLOR.primary} width={100} height={24} />
            </div>
          )
        })}
      </div>

      {/* Findings over time */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <h4 className="text-xs font-semibold text-fg-2 mb-3">Findings Over Time</h4>
        <FindingsTrend databases={[db]} dates={points.map(p => p.date)} />
      </div>

      {/* Health timeline */}
      <div className="bg-surface rounded-lg border border-line p-4 mt-3">
        <h4 className="text-xs font-semibold text-fg-2 mb-3">Health Score Timeline</h4>
        <div className="flex gap-0.5 items-end h-6">
          {points.map((p, i) => (
            <div key={i} className="flex-1" title={`${p.date}: ${p.health_score?.toUpperCase()}`}>
              <div className="w-full h-5 rounded-sm hover:opacity-80 transition-opacity cursor-default"
                style={{ backgroundColor: HEALTH_COLORS[p.health_score] || HEALTH_COLORS.unknown }} />
            </div>
          ))}
        </div>
        <div className="flex justify-between text-[10px] text-fg-muted mt-1">
          <span>{fmtDate(points[0]?.date)}</span>
          <span>{fmtDate(points[points.length - 1]?.date)}</span>
        </div>
      </div>
    </div>
  )
}

// ── Recommendations Panel ──
const PRIORITY_ACCENTS = {
  critical: 'border-l-red-500', high: 'border-l-amber-500',
  medium: 'border-l-blue-500', low: 'border-l-slate-400',
}
const PRIORITY_TEXT = {
  critical: 'text-err', high: 'text-warn-mid',
  medium: 'text-info', low: 'text-fg-3',
}
const STATUS_LABELS = {
  open: { label: 'Open', cls: 'bg-err-bg-2 text-err-text' },
  acknowledged: { label: 'Ack', cls: 'bg-warn-bg-2 text-warn-text' },
  in_progress: { label: 'In Progress', cls: 'bg-info-bg-2 text-info-text' },
  resolved: { label: 'Resolved', cls: 'bg-ok-bg-2 text-ok-text' },
  dismissed: { label: 'Dismissed', cls: 'bg-surface-3 text-fg-2' },
}

function RecommendationsPanel({ api, spaceId }) {
  const [recs, setRecs] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('active')
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!spaceId) return
    setLoading(true)
    api.get(`/api/spaces/${spaceId}/recommendations`)
      .then(d => setRecs(d.recommendations || []))
      .catch(() => setRecs([]))
      .finally(() => setLoading(false))
  }, [spaceId])

  const updateStatus = async (id, status) => {
    try {
      await api.put(`/api/spaces/${spaceId}/recommendations/${id}/status`, { status })
      setRecs(prev => prev.map(r => r.recommendation_id === id ? { ...r, status } : r))
    } catch (e) { console.error('Failed to update:', e) }
  }

  const filtered = useMemo(() => {
    if (filter === 'active') return recs.filter(r => ['open', 'acknowledged', 'in_progress'].includes(r.status))
    if (filter === 'resolved') return recs.filter(r => r.status === 'resolved')
    return recs
  }, [recs, filter])

  const activeCount = recs.filter(r => ['open', 'acknowledged', 'in_progress'].includes(r.status)).length
  const criticalCount = recs.filter(r => r.priority === 'critical' && ['open', 'acknowledged', 'in_progress'].includes(r.status)).length
  const highCount = recs.filter(r => r.priority === 'high' && ['open', 'acknowledged', 'in_progress'].includes(r.status)).length

  if (loading) return null
  if (!recs.length) return null

  // Collapsed: compact pill
  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
        className="w-full flex items-center justify-between rounded-lg border border-line bg-surface px-4 py-2.5 hover:border-primary-line transition-colors text-left">
        <div className="flex items-center gap-2.5">
          <svg className="w-4 h-4 text-primary" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" />
          </svg>
          <span className="text-xs font-semibold text-fg-2">{activeCount} Recommendation{activeCount !== 1 ? 's' : ''}</span>
          <div className="flex items-center gap-1.5">
            {criticalCount > 0 && <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-err-bg-2 text-err-text">{criticalCount} critical</span>}
            {highCount > 0 && <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-warn-bg-2 text-warn-text">{highCount} high</span>}
          </div>
        </div>
        <svg className="w-4 h-4 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
    )
  }

  // Expanded
  return (
    <div className="bg-surface rounded-lg border border-line">
      <div className="flex items-center justify-between p-4 cursor-pointer" onClick={() => setOpen(false)}>
        <div className="flex items-center gap-2.5">
          <svg className="w-4 h-4 text-primary" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" />
          </svg>
          <span className="text-sm font-bold text-fg">Recommendations</span>
          <span className="text-[10px] text-fg-3">{activeCount} active</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5 text-[10px]">
            {['active', 'all', 'resolved'].map(f => (
              <button key={f} onClick={(e) => { e.stopPropagation(); setFilter(f) }}
                className={`px-2 py-0.5 rounded-md border transition-colors ${
                  filter === f ? 'bg-primary-subtle text-primary border-primary-line font-semibold'
                    : 'text-fg-3 border-line'
                }`}>{f.charAt(0).toUpperCase() + f.slice(1)}</button>
            ))}
          </div>
          <svg className="w-4 h-4 text-fg-muted rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </div>
      <div className="px-4 pb-4">
        {filtered.length === 0 ? (
          <p className="text-xs text-fg-muted py-2">No {filter} recommendations.</p>
        ) : (
        <div className="space-y-1">
          {filtered.map(r => {
            const accent = PRIORITY_ACCENTS[r.priority] || PRIORITY_ACCENTS.medium
            const pText = PRIORITY_TEXT[r.priority] || PRIORITY_TEXT.medium
            const sLabel = STATUS_LABELS[r.status] || STATUS_LABELS.open
            return (
              <div key={r.recommendation_id} className={`border-l-2 ${accent} pl-3 py-2`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`text-[10px] font-bold uppercase tracking-wide ${pText}`}>{r.priority}</span>
                      <span className="text-[11px] text-fg-2 font-medium">{r.title}</span>
                      {r.display_name && <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-3 text-fg-3">{r.display_name}</span>}
                    </div>
                    {r.detail && <p className="text-[10px] text-fg-3 mt-0.5 leading-snug">{r.detail.slice(0, 200)}</p>}
                    {r.action && <p className="text-[10px] text-fg-2 mt-0.5 font-mono leading-snug">{r.action.slice(0, 150)}</p>}
                  </div>
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${sLabel.cls}`}>{sLabel.label}</span>
                    {r.status === 'open' && (
                      <>
                        <button onClick={() => updateStatus(r.recommendation_id, 'acknowledged')}
                          className="text-[10px] text-fg-muted hover:text-primary" title="Acknowledge">✓</button>
                        <button onClick={() => updateStatus(r.recommendation_id, 'dismissed')}
                          className="text-[10px] text-fg-muted hover:text-err" title="Dismiss">✕</button>
                      </>
                    )}
                    {r.status === 'acknowledged' && (
                      <button onClick={() => updateStatus(r.recommendation_id, 'in_progress')}
                        className="text-[10px] text-fg-muted hover:text-primary" title="Mark in progress">▶</button>
                    )}
                    {r.status === 'in_progress' && (
                      <button onClick={() => updateStatus(r.recommendation_id, 'resolved')}
                        className="text-[10px] text-fg-muted hover:text-ok" title="Mark resolved">✓</button>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
      </div>
    </div>
  )
}

// ── Section wrapper ──
function Section({ title, subtitle, children, action }) {
  return (
    <div className="bg-surface rounded-lg border border-line p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-sm font-bold text-fg">{title}</h3>
          {subtitle && <p className="text-[10px] text-fg-3 mt-0.5">{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </div>
  )
}

// ── Main Trends Component ──
export default function Trends({ api, spaceId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [days, setDays] = useState(30)
  const [reloadKey, setReloadKey] = useState(0)
  const [selectedMetric, setSelectedMetric] = useState('cache_hit_ratio')
  const [metricSort, setMetricSort] = useState('name')
  // A metric outside the headline set is fetched on demand — ONE series for every
  // database — and merged into the day points, so the whole published set is reachable
  // without loading it all at once.
  const [extraLoading, setExtraLoading] = useState(false)
  const [extraSeries, setExtraSeries] = useState({})   // { metricKey: { instance_id: { date: value } } }
  // Store the drill-down target by id, not the row object: changing the 7d/30d/90d range
  // refetches data.databases, and a stored object would keep showing the old range's
  // points. The live row is derived from the freshly loaded databases below.
  const [selectedDbId, setSelectedDbId] = useState(null)
  const [dbFilter, setDbFilter] = useState('')
  const [dbSearch, setDbSearch] = useState('')
  const [dbEngineFilter, setDbEngineFilter] = useState('')
  const [dbRegionFilter, setDbRegionFilter] = useState('')
  const [pulse, setPulse] = useState(null)

  useEffect(() => {
    if (!spaceId) return
    setLoading(true)
    setError(null)
    // Trends draws two tiers on one timeline, keyed by day:
    //   * Tier 1 (the CloudWatch series) gives a CONTINUOUS metric series from the first
    //     collection — cache hit, dead tuples, XID age, connections, DB size, blocked —
    //     via /metrics-series (one store read, the writer's daily average per db); and
    //   * Tier 2 deep scans (the stored summaries, paged by the keyset cursor) overlay the
    //     richer per-day points: health score and findings.
    // A day with only Tier 1 shows its metrics (health "unknown", no findings); a day
    // with a deep scan adds the health badge and findings. "No trend data yet" shows only
    // when BOTH tiers are empty. The S3-based /trends is the fallback when the store is
    // not configured (and still carries investigation_stats).
    const loadScanResults = async () => {
      const all = []
      let before = null
      // Page to completion so older days and databases are not silently dropped from the
      // heatmap, findings and scan counts. A low page cap would silently lose data once a
      // busy fleet exceeded it, so the cap is high. The high safety cap only
      // prevents an unbounded loop; hitting it is logged, not silent.
      const MAX_PAGES = 40  // 40 * 500 = 20k rows across the window
      let page = 0
      for (; page < MAX_PAGES; page++) {
        const d = await api.get(`/api/scan-results?space_id=${encodeURIComponent(spaceId)}&days=${days}&limit=500${before ? `&before=${encodeURIComponent(before)}` : ''}`)
        all.push(...(d?.results || []))
        if (!d?.has_more || !d?.next_before) break
        before = d.next_before
      }
      if (page >= MAX_PAGES) {
        console.warn(`Trends: scan-results paging hit the ${MAX_PAGES}-page safety cap; the oldest rows in this window may be omitted.`)
      }
      return all
    }
    Promise.all([
      loadScanResults().catch(() => null),
      api.get(`/api/spaces/${spaceId}/trends?days=${days}`).catch(() => ({ databases: [], dates: [] })),
      api.get(`/api/spaces/${encodeURIComponent(spaceId)}/metrics-series?days=${days}`).catch(() => ({ databases: [] })),
    ]).then(([scanResults, s3Trends, tier1Series]) => {
      const dbMap = {}
      const ensure = (iid, meta) => {
        if (!dbMap[iid]) {
          dbMap[iid] = { instance_id: iid, display_name: meta.display_name, engine: meta.engine,
                         region: meta.region, byDay: new Map(), scan_count: 0 }
        } else {
          const e = dbMap[iid]
          e.display_name = e.display_name || meta.display_name
          e.engine = e.engine || meta.engine
          e.region = e.region || meta.region
        }
        return dbMap[iid]
      }
      // Tier 1 metric series first: a point per day carrying the headline metrics.
      for (const db of (tier1Series?.databases || [])) {
        const e = ensure(db.instance_id, db)
        for (const p of (db.points || [])) {
          const prev = e.byDay.get(p.date) || { date: p.date, health_score: 'unknown',
            findings_total: 0, findings_critical: 0, findings_warning: 0, findings_info: 0 }
          // Every metric the series carries (the API lists them in `metrics`): the
          // headline set plus deadlocks, transaction rate, rollback ratio, longest
          // transaction and replica lag.
          e.byDay.set(p.date, { ...prev, ...p, date: p.date, time: p.time || prev.time })
        }
      }
      // Tier 2 deep scans overlay health + findings on the same day; they also fill the
      // metrics on a day Tier 1 did not cover (store retention is shorter for Tier 1).
      for (const sr of (scanResults || [])) {
        const e = ensure(sr.instance_id, sr)
        const date = (sr.scan_time || '').split('T')[0]
        if (!date) continue
        e.scan_count += 1
        const prev = e.byDay.get(date) || { date }
        // Rows arrive newest-first (scan_time DESC). The first deep scan seen for a day is
        // the newest; keep it as the day's state and never let an OLDER same-day scan
        // overwrite it (that showed the morning's result all day after a remediation
        // re-scan). prev.id is set only by a deep scan — Tier 1 seeding leaves it unset.
        if (prev.id != null) continue
        const findings = Array.isArray(sr.findings) ? sr.findings : []
        e.byDay.set(date, {
          ...prev, date, time: sr.scan_time, id: sr.id,
          health_score: sr.health_score || 'unknown',
          findings_total: findings.length,
          findings_critical: findings.filter(f => f?.severity === 'CRITICAL').length,
          findings_warning: findings.filter(f => f?.severity === 'WARNING').length,
          findings_info: findings.filter(f => f?.severity === 'INFO').length,
          // Prefer the Tier 1 metric when present (continuous), else the scan's own.
          cache_hit_ratio: prev.cache_hit_ratio ?? sr.cache_hit_ratio,
          dead_tuple_pct: prev.dead_tuple_pct ?? sr.dead_tuple_pct,
          xid_age_pct: prev.xid_age_pct ?? sr.xid_age_pct,
          connections: prev.connections ?? sr.total_connections,
          database_size_mb: prev.database_size_mb ?? sr.database_size_mb,
          unused_indexes: sr.unused_indexes_count,
          index_cache_hit_ratio: sr.index_cache_hit_ratio,
        })
      }
      const allDates = new Set()
      let databases = Object.values(dbMap).map(e => {
        const points = [...e.byDay.values()].sort((a, b) =>
          (a.time || a.date || '').localeCompare(b.time || b.date || ''))
        points.forEach(p => allDates.add(p.date))
        // `latest` is the newest point of any tier — the metric cards and sparklines read
        // it (Tier 1 carries the headline metrics). Health and findings must come from the
        // newest point that is actually a DEEP scan (`id` set), so a Tier 1-only latest day
        // does not blank a database's health to "unknown"/0 after 00:00 UTC.
        const latestDeepScan = [...points].reverse().find(p => p.id != null) || null
        return {
          instance_id: e.instance_id, display_name: e.display_name, engine: e.engine, region: e.region,
          data_points: points, scan_count: e.scan_count, latest: points[points.length - 1] || null,
          latest_deep_scan: latestDeepScan,
        }
      })
      if (!databases.length && s3Trends?.databases?.length) {
        // Store not configured: fall back to the S3-based trends as the API returns them.
        // Every S3 trend point comes from a deep scan, so its newest point is also the
        // latest deep scan (the health/findings source the summary and breakdown read).
        databases = s3Trends.databases.map(db => {
          const pts = db.data_points || []
          return { ...db, scan_count: pts.length,
                   latest: db.latest || pts[pts.length - 1] || null,
                   latest_deep_scan: db.latest || pts[pts.length - 1] || null }
        })
        for (const db of databases) (db.data_points || []).forEach(p => allDates.add(p.date))
      }
      setData({
        space_id: spaceId, days,
        dates: [...allDates].sort(),
        databases,
        scan_count: (scanResults || []).length,
        investigation_stats: s3Trends?.investigation_stats || {},
      })
      setError(null)
    }).catch(e => setError(e.detail || 'Failed to load trends'))
      .finally(() => setLoading(false))
  }, [spaceId, days, reloadKey])

  // On-demand series for a metric outside the headline set: one request, that single
  // metric for every database in the space, keyed by day. Cached per metric for this
  // window; a window change clears the cache since the days differ.
  useEffect(() => { setExtraSeries({}) }, [spaceId, days, reloadKey])
  useEffect(() => {
    if (!spaceId || !MORE_METRICS.some(m => m.key === selectedMetric) || extraSeries[selectedMetric]) return
    let cancelled = false
    setExtraLoading(true)
    api.get(`/api/spaces/${encodeURIComponent(spaceId)}/metrics-series?days=${days}&metrics=${encodeURIComponent(selectedMetric)}`)
      .then(d => {
        if (cancelled) return
        const byDb = {}
        for (const db of (d?.databases || [])) {
          byDb[db.instance_id] = Object.fromEntries((db.points || []).filter(p => p[selectedMetric] !== undefined).map(p => [p.date, p[selectedMetric]]))
        }
        setExtraSeries(prev => ({ ...prev, [selectedMetric]: byDb }))
      })
      .catch(() => { if (!cancelled) setExtraSeries(prev => ({ ...prev, [selectedMetric]: {} })) })
      .finally(() => { if (!cancelled) setExtraLoading(false) })
    return () => { cancelled = true }
  }, [api, spaceId, days, selectedMetric, extraSeries, reloadKey])

  useEffect(() => {
    if (!spaceId) return
    // Current Status: the newest Tier 1 sample per database from the last hour. A
    // missing metric is "unknown", never a verdict; the sample time is kept so the
    // card can say how old the reading is.
    api.get(`/api/scan-metrics/fleet?space_id=${spaceId}&hours=1`)
      .then(data => {
        if (data?.databases?.length) {
          const n = (v) => (v === null || v === undefined || v === '') ? null : Number(v)
          setPulse({
            databases: data.databases.map(m => {
              const cache = n(m.cache_hit_ratio), blocked = n(m.blocked_queries)
              const status = cache === null && blocked === null ? 'unknown'
                : (cache !== null && cache < 90) || (blocked !== null && blocked > 5) ? 'critical'
                : (cache !== null && cache < 95) || (blocked !== null && blocked > 0) ? 'warning' : 'healthy'
              return {
                instance_id: m.instance_id,
                display_name: m.display_name || m.instance_id,
                engine: m.engine || '',
                scan_time: m.scan_time,
                cache_hit_ratio: cache,
                total_connections: n(m.total_connections),
                active_connections: n(m.active_connections),
                blocked_queries: blocked,
                dead_tuple_pct: n(m.dead_tuple_pct),
                xid_age_pct: n(m.xid_age_pct),
                database_size_mb: n(m.database_size_mb),
                status,
              }
            })
          })
        }
      })
      .catch(() => {})
  }, [spaceId])

  const databases = data?.databases || []
  const dates = data?.dates || []
  const hasData = databases.some(db => db.data_points?.length > 0)
  // Derive the open drill-down row from the current databases so it tracks the selected
  // range; if the database is no longer present, the drill-down closes on its own.
  const selectedDb = selectedDbId ? (databases.find(d => d.instance_id === selectedDbId) || null) : null

  const engines = [...new Set(databases.map(db => canonicalEngine(db.engine)).filter(Boolean))]
  const regions = [...new Set(databases.map(db => db.region || '').filter(Boolean))]

  // The on-demand series, folded into each database's day points (the headline metrics
  // are already there from the first load).
  const databasesWithExtra = useMemo(() => {
    const keys = Object.keys(extraSeries)
    if (!keys.length) return databases
    return databases.map(db => {
      const pts = (db.data_points || []).map(p => {
        const out = { ...p }
        for (const k of keys) {
          const v = extraSeries[k]?.[db.instance_id]?.[p.date]
          if (v !== undefined) out[k] = v
        }
        return out
      })
      // A day the first load did not know about (a deep-scan daily on a day with no
      // Tier 1 point and no scan summary) still gets a point.
      const known = new Set(pts.map(p => p.date))
      for (const k of keys) {
        for (const [date, v] of Object.entries(extraSeries[k]?.[db.instance_id] || {})) {
          if (!known.has(date)) { known.add(date); pts.push({ date, health_score: 'unknown', [k]: v }) }
        }
      }
      pts.sort((a, b) => a.date.localeCompare(b.date))
      return { ...db, data_points: pts }
    })
  }, [databases, extraSeries])

  const filteredDatabases = useMemo(() => {
    return databasesWithExtra.filter(db => {
      if (dbFilter && db.instance_id !== dbFilter) return false
      if (dbEngineFilter && canonicalEngine(db.engine) !== dbEngineFilter) return false
      if (dbRegionFilter && (db.region || '') !== dbRegionFilter) return false
      if (dbSearch) {
        const q = dbSearch.toLowerCase()
        if (!db.display_name?.toLowerCase().includes(q) &&
            !engineLabel(db.engine).toLowerCase().includes(q)) return false
      }
      return true
    })
  }, [databasesWithExtra, dbFilter, dbSearch, dbEngineFilter, dbRegionFilter])

  if (loading) return (
    <div className="flex items-center justify-center py-20">
      <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
    </div>
  )

  if (error) return (
    <div className="text-center py-20">
      <p className="text-sm text-err mb-2">{error}</p>
      <button onClick={() => setReloadKey(k => k + 1)} className="text-sm text-primary hover:underline">Retry</button>
    </div>
  )

  if (!hasData) return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold text-fg">Trends</h2>
      </div>
      <div className="text-center py-20 bg-surface rounded-lg border border-line">
        <div className="w-12 h-12 mx-auto mb-3 rounded-full bg-surface-3 flex items-center justify-center">
          <svg className="w-6 h-6 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
          </svg>
        </div>
        <p className="text-sm font-medium text-fg-2">No trend data yet</p>
        <p className="text-xs text-fg-muted mt-1">Run health scans on your databases to start tracking trends.</p>
      </div>
    </div>
  )

  // Drill-down view
  if (selectedDb) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold text-fg">Trends</h2>
          <div className="flex items-center gap-1.5">
            {TIME_RANGES.map(t => (
              <button key={t.value} onClick={() => setDays(t.value)}
                className={`px-2.5 py-1 text-[10px] rounded-md border transition-colors ${
                  days === t.value
                    ? 'bg-primary-subtle text-primary border-primary-line font-semibold'
                    : 'text-fg-3 border-line'
                }`}>
                {t.label}
              </button>
            ))}
          </div>
        </div>
        <DatabaseDetail db={selectedDb} onBack={() => setSelectedDbId(null)} />
      </div>
    )
  }

  // Fleet overview
  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-fg">Trends</h2>
          <p className="text-xs text-fg-3">{databases.length} databases · {(data?.scan_count ?? 0).toLocaleString()} deep scans · {dates.length} days with data over the last {days} days</p>
        </div>
        <div className="flex items-center gap-1.5">
          {TIME_RANGES.map(t => (
            <button key={t.value} onClick={() => setDays(t.value)}
              className={`px-2.5 py-1 text-[10px] rounded-md border transition-colors ${
                days === t.value
                  ? 'bg-primary-subtle text-primary border-primary-line font-semibold'
                  : 'text-fg-3 border-line'
              }`}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[160px] max-w-[260px]">
          <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
          <input value={dbSearch} onChange={e => setDbSearch(e.target.value)} placeholder="Search databases..."
            className="w-full pl-8 pr-3 py-1.5 text-xs border border-line rounded-lg bg-surface text-fg outline-none focus:border-primary" />
        </div>
        {engines.length > 1 && (
          <select value={dbEngineFilter} onChange={e => setDbEngineFilter(e.target.value)}
            className="px-2 py-1.5 text-[11px] border border-line rounded-lg bg-surface text-fg-2 outline-none">
            <option value="">All engines</option>
            {engines.map(e => <option key={e} value={e}>{engineLabel(e)}</option>)}
          </select>
        )}
        {regions.length > 1 && (
          <select value={dbRegionFilter} onChange={e => setDbRegionFilter(e.target.value)}
            className="px-2 py-1.5 text-[11px] border border-line rounded-lg bg-surface text-fg-2 outline-none">
            <option value="">All regions</option>
            {regions.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        )}
        {databases.length > 0 && (
          <select value={dbFilter} onChange={e => setDbFilter(e.target.value)}
            className="px-2 py-1.5 text-[11px] border border-line rounded-lg bg-surface text-fg-2 outline-none">
            <option value="">All databases</option>
            {databases.map(db => <option key={db.instance_id} value={db.instance_id}>{db.display_name}</option>)}
          </select>
        )}
        {(dbFilter || dbSearch || dbEngineFilter || dbRegionFilter) && (
          <button onClick={() => { setDbFilter(''); setDbSearch(''); setDbEngineFilter(''); setDbRegionFilter('') }}
            className="px-2 py-1.5 text-[10px] text-fg-3 hover:text-err transition-colors">
            Clear filters
          </button>
        )}
      </div>

      {/* Current Status — Live Health Pulse */}
      {pulse?.databases?.length > 0 && (
        <Section title="Current Status" subtitle="Newest Tier 1 sample per database (PostgreSQL-internal metrics, collected every few minutes). Databases without a sample in the last hour are not listed.">
          <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2">
            {pulse.databases.filter(p => {
              if (dbFilter && p.instance_id !== dbFilter) return false
              if (dbEngineFilter && canonicalEngine(p.engine) !== dbEngineFilter) return false
              if (dbSearch && !p.display_name?.toLowerCase().includes(dbSearch.toLowerCase())) return false
              return true
            }).map(p => (
              <div key={p.instance_id} className="bg-surface rounded-lg border border-line p-3">
                <div className="flex items-center gap-2 mb-2">
                  <span className={`w-2 h-2 rounded-full flex-shrink-0 ${p.status === 'critical' ? 'bg-err' : p.status === 'warning' ? 'bg-warn' : p.status === 'healthy' ? 'bg-ok' : 'bg-fg-muted'}`} />
                  <span className="text-xs font-medium text-fg-2 truncate">{p.display_name}</span>
                  {p.scan_time && <span className="ml-auto text-[10px] text-fg-muted whitespace-nowrap" title={new Date(p.scan_time).toLocaleString()}>{sampleAge(p.scan_time)}</span>}
                </div>
                <div className="grid grid-cols-3 gap-2">
                  {p.cache_hit_ratio != null && (
                    <div>
                      <div className="text-sm font-bold text-fg">{Number(p.cache_hit_ratio).toFixed(1)}%</div>
                      <div className="text-[10px] text-fg-muted uppercase">Cache Hit</div>
                    </div>
                  )}
                  {p.total_connections != null && (
                    <div>
                      <div className="text-sm font-bold text-fg">{p.total_connections}</div>
                      <div className="text-[10px] text-fg-muted uppercase">Conns</div>
                    </div>
                  )}
                  {p.blocked_queries != null && p.blocked_queries > 0 && (
                    <div>
                      <div className="text-sm font-bold text-err">{p.blocked_queries}</div>
                      <div className="text-[10px] text-fg-muted uppercase">Blocked</div>
                    </div>
                  )}
                  {p.dead_tuple_pct != null && (
                    <div>
                      <div className={`text-sm font-bold ${Number(p.dead_tuple_pct) > 10 ? 'text-warn-mid' : 'text-fg'}`}>{Number(p.dead_tuple_pct).toFixed(1)}%</div>
                      <div className="text-[10px] text-fg-muted uppercase">Dead Tuples</div>
                    </div>
                  )}
                  {p.xid_age_pct != null && (
                    <div>
                      <div className={`text-sm font-bold ${Number(p.xid_age_pct) > 50 ? 'text-err' : 'text-fg'}`}>{Number(p.xid_age_pct).toFixed(1)}%</div>
                      <div className="text-[10px] text-fg-muted uppercase">XID Age</div>
                    </div>
                  )}
                  {p.database_size_mb != null && (
                    <div>
                      <div className="text-sm font-bold text-fg">{Number(p.database_size_mb) >= 1024 ? `${(Number(p.database_size_mb) / 1024).toFixed(1)} GB` : `${Math.round(Number(p.database_size_mb))} MB`}</div>
                      <div className="text-[10px] text-fg-muted uppercase">DB Size</div>
                    </div>
                  )}
                  {p.cache_hit_ratio == null && p.total_connections == null && (
                    <span className="text-[10px] text-fg-muted col-span-3">No metrics</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Fleet summary cards */}
      <FleetSummary databases={filteredDatabases} />

      {/* Investigation Insights */}
      {data?.investigation_stats && Object.keys(data.investigation_stats).length > 0 && (
        <Section title="Investigation Activity" subtitle="Investigation frequency and severity across databases.">
          <div className="space-y-1.5">
            {filteredDatabases.filter(db => data.investigation_stats[db.instance_id]).map(db => {
              const stats = data.investigation_stats[db.instance_id]
              return (
                <div key={db.instance_id} className="flex items-center gap-3 px-2 py-1.5 rounded-lg hover:bg-surface-3 transition-colors">
                  <span className="text-xs font-medium text-fg-2 min-w-[140px] truncate">{db.display_name}</span>
                  <div className="flex items-center gap-2 flex-1">
                    <span className="text-[10px] text-fg-3">{stats.total} investigations</span>
                    {stats.critical > 0 && <span className="text-[10px] px-1.5 py-0.5 rounded bg-err-bg-2 text-err">{stats.critical} critical</span>}
                    {stats.warning > 0 && <span className="text-[10px] px-1.5 py-0.5 rounded bg-warn-bg-2 text-warn-mid">{stats.warning} warning</span>}
                  </div>
                  {stats.last_date && <span className="text-[10px] text-fg-muted">Last: {new Date(stats.last_date).toLocaleDateString()}</span>}
                </div>
              )
            })}
          </div>
        </Section>
      )}

      {/* Active Recommendations */}
      <RecommendationsPanel api={api} spaceId={spaceId} />

      {/* Health heatmap */}
      <Section title="Fleet Health" subtitle="Health score per database per scan day (the last deep scan of each day). Click a row to drill down.">
        <HealthHeatmap databases={filteredDatabases} dates={dates} onSelectDb={(db) => setSelectedDbId(db.instance_id)} />
      </Section>

      {/* Findings trend */}
      <Section title="Findings Trend" subtitle="Aggregated findings across all databases over time.">
        <FindingsTrend databases={filteredDatabases} dates={dates} />
      </Section>

      {/* Metric comparison */}
      <Section title="Metric Comparison" subtitle="Compare any published metric across databases, with sparkline trends; 'More metrics' reaches every Tier 1 and deep-scan series.">
        <MetricTable databases={filteredDatabases} selectedMetric={selectedMetric} onSelectMetric={setSelectedMetric}
          extraLoading={extraLoading} sort={metricSort} onSort={setMetricSort} />
      </Section>

      {/* Per-database findings breakdown */}
      <Section title="Database Breakdown" subtitle="Latest findings distribution per database.">
        <div className="space-y-2">
          {filteredDatabases.filter(db => db.latest_deep_scan).map(db => (
            <div key={db.instance_id} className="flex items-center gap-3 cursor-pointer hover:bg-surface-3 rounded-lg px-2 py-1.5 -mx-2 transition-colors" onClick={() => setSelectedDbId(db.instance_id)}>
              <div className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: HEALTH_COLORS[db.latest_deep_scan?.health_score] || HEALTH_COLORS.unknown }} />
              <span className="text-xs font-medium text-fg-2 min-w-[140px] truncate">{db.display_name}</span>
              <div className="flex-1"><StackedBar critical={db.latest_deep_scan?.findings_critical || 0} warning={db.latest_deep_scan?.findings_warning || 0} info={Math.max(0, (db.latest_deep_scan?.findings_total || 0) - (db.latest_deep_scan?.findings_critical || 0) - (db.latest_deep_scan?.findings_warning || 0))} height={14} /></div>
              <span className="text-[10px] text-fg-3 min-w-[60px] text-right">{db.latest_deep_scan?.findings_total || 0} findings</span>
              <svg className="w-3.5 h-3.5 text-fg-muted flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
            </div>
          ))}
        </div>
      </Section>
    </div>
  )
}
