// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// CloudWatch metrics tab of the Dashboard's database drawer: EVERY series this
// deployment publishes for one database, over windows up to 15 months, as charts.
//
// This is the long-term, every-series view; it differs from its two neighbours on
// purpose. Metrics (Tier 1) is the record view: one exact row per collection, with
// the lists (table bloat, wait events) that have no series. Trends is the curated
// handful of headline charts with the deep scans as markers. Here, two groups:
//   * Tier 1 series — the collector's numerics, averaged per bucket with the per-bucket
//     min/max envelope, so a 5-minute spike is still visible in a 90-day window;
//   * Deep-scan daily series — the numerics each deep scan publishes (findings counts,
//     index and maintenance counts, the top query's cost, …), one point per scan.
// The deep-scan group loads only when opened, so the default view costs one
// GetMetricData call, not two. For a DB cluster `member` picks whose series.
import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Panel, RANGES, pct, int, mb, dayClock } from './TrendChart'

const secsFmt = (v) => (v === null ? '—' : v >= 3600 ? `${(v / 3600).toFixed(1)} h` : v >= 60 ? `${Math.round(v / 60)} min` : `${Math.round(v)} s`)
const lagFmt = (v) => (v === null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`)
const rateFmt = (v) => (v === null ? '—' : v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1))
const msFmt = (v) => (v === null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`)
const one = (v) => (v === null ? '—' : v.toFixed(1))

// Every Tier 1 numeric the collector publishes (metrics_store.TIER1_NUMERIC_METRICS,
// minus the raw cumulative counters, which only mean something as the per-interval
// rates listed here). `good` colours the change over the window; `domain` pins a
// percentage to its fixed scale.
export const TIER1_PANELS = [
  { key: 'cache_hit_ratio', label: 'Cache hit ratio', fmt: pct, good: 'high', domain: [0, 100], warn: (v) => v < 99, bad: (v) => v < 90 },
  { key: 'total_connections', label: 'Connections', fmt: int, good: 'low', extra: { key: 'active_connections', label: 'active' } },
  { key: 'connections_used_pct', label: 'Connections used', fmt: pct, good: 'low', domain: [0, 100], warn: (v) => v >= 80, bad: (v) => v >= 95 },
  { key: 'active_connections', label: 'Active connections', fmt: int, good: null },
  { key: 'idle_connections', label: 'Idle connections', fmt: int, good: null },
  { key: 'idle_in_txn_connections', label: 'Idle in transaction', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 10 },
  { key: 'waiting_queries', label: 'Waiting queries', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 10 },
  { key: 'blocked_queries', label: 'Blocked queries', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 5 },
  { key: 'longest_transaction_sec', label: 'Longest transaction', fmt: secsFmt, good: 'low', warn: (v) => v > 300, bad: (v) => v > 3600 },
  { key: 'deadlocks_delta', label: 'Deadlocks / interval', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 5 },
  { key: 'txn_per_sec', label: 'Transactions / s', fmt: rateFmt, good: null },
  { key: 'rollback_ratio_pct', label: 'Rollback ratio', fmt: pct, good: 'low', warn: (v) => v > 2, bad: (v) => v > 10 },
  { key: 'dead_tuple_pct', label: 'Dead tuples', fmt: pct, good: 'low', warn: (v) => v > 5, bad: (v) => v > 20 },
  { key: 'xid_age_pct', label: 'XID age', fmt: pct, good: 'low', domain: [0, 100], warn: (v) => v > 50, bad: (v) => v > 80 },
  { key: 'autovacuum_workers_active', label: 'Autovacuum workers', fmt: int, good: null },
  { key: 'tables_needing_vacuum', label: 'Tables needing vacuum', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 10 },
  { key: 'temp_files', label: 'Temp files (cumulative)', fmt: int, good: null },
  { key: 'temp_bytes_mb', label: 'Temp bytes (cumulative)', fmt: mb, good: null },
  { key: 'database_size_mb', label: 'Database size', fmt: mb, good: null },
  { key: 'replica_lag_ms', label: 'Replica lag', fmt: lagFmt, good: 'low', warn: (v) => v > 1000, bad: (v) => v > 10000, readersOnly: true },
]

// The deep scan's daily numerics (batch/deep_sections.py tier2_metrics), one point per
// scan. Counts of things the findings flag, plus the top query's cost.
export const TIER2_PANELS = [
  { key: 'findings_total', label: 'Findings', fmt: int, good: 'low', extra: { key: 'findings_critical', label: 'critical' } },
  { key: 'findings_critical', label: 'Critical findings', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 2 },
  { key: 'findings_warning', label: 'Warning findings', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'index_cache_hit_ratio', label: 'Index cache hit ratio', fmt: pct, good: 'high', domain: [0, 100], warn: (v) => v < 99, bad: (v) => v < 90 },
  { key: 'unused_indexes_count', label: 'Unused indexes', fmt: int, good: 'low', warn: (v) => v > 5, bad: (v) => v > 20 },
  { key: 'duplicate_indexes_count', label: 'Duplicate indexes', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'invalid_indexes_count', label: 'Invalid indexes', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'fk_without_index_count', label: 'Foreign keys without an index', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'seq_scan_heavy_tables_count', label: 'Sequential-scan-heavy tables', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'unused_tables_count', label: 'Unused tables', fmt: int, good: 'low' },
  { key: 'unvalidated_constraints_count', label: 'Unvalidated constraints', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'stale_statistics_tables_count', label: 'Tables with stale statistics', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'never_vacuumed_tables_count', label: 'Never-vacuumed tables', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'max_sequence_used_pct', label: 'Most-used sequence', fmt: pct, good: 'low', domain: [0, 100], warn: (v) => v > 50, bad: (v) => v > 80 },
  { key: 'forced_checkpoint_ratio_pct', label: 'Forced checkpoints', fmt: pct, good: 'low', domain: [0, 100], warn: (v) => v > 10, bad: (v) => v > 30 },
  { key: 'autovacuum_worker_utilization_pct', label: 'Autovacuum worker utilisation', fmt: pct, good: 'low', domain: [0, 100], warn: (v) => v > 80 },
  { key: 'temp_file_queries_count', label: 'Queries writing temp files', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'top_query_total_exec_ms', label: 'Top query · total time', fmt: msFmt, good: null },
  { key: 'top_query_mean_exec_ms', label: 'Top query · mean time', fmt: msFmt, good: 'low' },
  { key: 'top_query_wal_mb', label: 'Top query · WAL generated', fmt: mb, good: null },
  { key: 'connection_utilization_pct', label: 'Connection utilisation at scan', fmt: pct, good: 'low', domain: [0, 100], warn: (v) => v >= 80 },
  { key: 'settings_outside_recommendation_count', label: 'Settings outside recommendation', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'high_risk_roles_count', label: 'High-risk roles', fmt: int, good: 'low', warn: (v) => v > 0 },
  { key: 'plain_connections_count', label: 'Unencrypted connections', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 0 },
  { key: 'health_score_num', label: 'Health (0 critical · 1 warning · 2 ok)', fmt: one, good: 'high', domain: [0, 2] },
]

function toRows(d) {
  const seen = new Set()
  return (d?.rows || [])
    .filter(x => { const k = x.id ?? `${x.scan_time}|${x.member_instance_id || ''}`; if (seen.has(k)) return false; seen.add(k); return true })
    .map(x => ({ ...x, t: Date.parse(x.scan_time) })).filter(x => Number.isFinite(x.t)).sort((a, b) => a.t - b.t)
}

function useSeries(api, instanceId, hours, member, series, enabled) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!instanceId || !enabled) return
    let cancelled = false
    setLoading(true); setError('')
    api.get(`/api/scan-metrics/${encodeURIComponent(instanceId)}/bucketed?hours=${Number(hours)}&series=${series}${member ? `&member=${encodeURIComponent(member)}` : ''}`)
      .then(d => { if (!cancelled) setRows(toRows(d)) })
      .catch(e => { if (!cancelled) setError(e?.status === 503 ? 'The metrics store is not configured for this deployment.' : (e?.detail || e?.message || 'Could not load the series')) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [api, instanceId, hours, member, series, enabled])
  return { rows, loading, error }
}

function Group({ title, subtitle, panels, rows, loading, error, t0, t1, open, onToggle, count }) {
  // A panel with no points in the window is listed last, folded, so the ones with data
  // come first and the reader still sees that the series exists.
  const [withData, empty] = useMemo(() => {
    const has = (p) => rows.some(r => r[p.key] !== null && r[p.key] !== undefined && r[p.key] !== '')
    return [panels.filter(has), panels.filter(p => !has(p))]
  }, [panels, rows])
  return (
    <section className="space-y-2">
      <button onClick={onToggle} aria-expanded={open} className="flex items-center gap-2 text-left w-full group">
        {open ? <ChevronDown size={14} className="text-fg-muted" /> : <ChevronRight size={14} className="text-fg-muted" />}
        <span className="text-xs font-semibold text-fg group-hover:text-primary">{title}</span>
        <span className="text-[10px] text-fg-muted">{subtitle}</span>
        {count !== null && <span className="ml-auto text-[10px] text-fg-muted tabular-nums">{count}</span>}
      </button>
      {open && (
        loading ? <div className="text-xs text-fg-muted py-4 text-center">Loading series…</div>
        : error ? <div className="text-xs text-err py-4 text-center">{error}</div>
        : rows.length === 0 ? <div className="text-xs text-fg-muted py-4 text-center">No points in this window.</div>
        : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {withData.map(p => <Panel key={p.key} panel={p} rows={rows} scans={[]} t0={t0} t1={t1} />)}
            </div>
            {empty.length > 0 && (
              <details className="text-[10px] text-fg-muted">
                <summary className="cursor-pointer hover:text-fg-3">{empty.length} series with no points in this window</summary>
                <p className="mt-1">{empty.map(p => p.label).join(' · ')}</p>
              </details>
            )}
          </>
        )
      )}
    </section>
  )
}

export default function CloudWatchMetrics({ api, instance, member = '' }) {
  const instanceId = instance?.instance_id
  const [hours, setHours] = useState('168')
  const [tier2Open, setTier2Open] = useState(false)
  const isReaderTarget = member === 'reader' || (instance?.cluster_member_roles || []).some(m => m.role === 'reader' && `instance:${m.instance_id}` === member)
  const t1 = useSeries(api, instanceId, hours, member, 'tier1', true)
  const t2 = useSeries(api, instanceId, hours, member, 'tier2', tier2Open)
  const tier1Panels = useMemo(() => TIER1_PANELS.filter(p => !p.readersOnly || isReaderTarget), [isReaderTarget])
  const end = Date.now()
  const start = end - Number(hours) * 3600e3
  const samples = t1.rows.reduce((n, r) => n + (Number(r.samples) || 0), 0)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-1" role="group" aria-label="Window">
          {RANGES.map(([k, label]) => (
            <button key={k} onClick={() => setHours(k)} aria-pressed={hours === k}
              className={`px-2.5 py-1 text-[11px] rounded-full border transition-colors ${
                hours === k ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
              {label}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-fg-muted">
          Every series this deployment publishes to CloudWatch for this database · {dayClock(start)} → {dayClock(end)}
          {samples > 0 && ` · ${samples.toLocaleString()} Tier 1 samples averaged into ${t1.rows.length.toLocaleString()} points`}
        </span>
      </div>
      <Group title="Tier 1 series" subtitle="the collector's numerics, every few minutes; each point is the bucket's average"
        panels={tier1Panels} rows={t1.rows} loading={t1.loading} error={t1.error} t0={start} t1={end}
        open onToggle={() => {}} count={null} />
      <Group title="Deep-scan daily series" subtitle="the numerics each deep scan publishes; one point per scan"
        panels={TIER2_PANELS} rows={t2.rows} loading={t2.loading} error={t2.error} t0={start} t1={end}
        open={tier2Open} onToggle={() => setTier2Open(o => !o)} count={tier2Open ? null : `${TIER2_PANELS.length} series`} />
      <p className="text-[10px] text-fg-muted">
        CloudWatch keeps 1-minute points for 15 days, 5-minute points for 63 days and hourly points for 15 months; longer windows show coarser buckets. The Metrics (Tier 1) tab has the exact per-collection rows, with table bloat and wait events.
      </p>
    </div>
  )
}
