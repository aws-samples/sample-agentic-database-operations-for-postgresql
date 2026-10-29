// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Trends tab of the Dashboard's database drawer: the Tier 1 series for one database
// (every lightweight collection in the window, from the metrics store) drawn on a
// time-proportional axis, with the Tier 2 deep scans of the same window as markers.
// The two tiers are different signals — a sample every few minutes versus a full
// scan every day — so they share the axis but are never merged into one line.
// For a DB cluster `member` (the drawer's member picker) chooses whose series: the
// writer's (with its deep scans), one member's, or every reader's, drawn as one
// line per reader (with each reader's reader scans) so they are never averaged.
import { useEffect, useMemo, useState } from 'react'
import { COLOR, HEALTH_COLORS, SERIES_COLORS } from '../theme'
import { targetsReaders } from './MemberPicker'

// The series come from CloudWatch (bucketed server-side), which keeps 5-minute points for
// 63 days and hourly for 15 months, so the drawer reaches back a year.
export const RANGES = [['1', '1 h'], ['6', '6 h'], ['24', '24 h'], ['168', '7 d'], ['720', '30 d'], ['2160', '90 d'], ['8760', '1 y']]
const num = (v) => (v === null || v === undefined || v === '') ? null : Number(v)
export const pct = (v) => (v === null ? '—' : `${v.toFixed(1)}%`)
export const int = (v) => (v === null ? '—' : Math.round(v).toLocaleString())
export const mb = (v) => (v === null ? '—' : v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`)
export const clock = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
export const dayClock = (t) => new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

// One panel per metric. `good` says which direction is healthy, so the change since
// the start of the window can be coloured; `domain` pins percentages that only make
// sense against a fixed scale.
const PANELS = [
  { key: 'cache_hit_ratio', label: 'Cache hit ratio', fmt: pct, good: 'high', domain: [0, 100], warn: (v) => v < 99, bad: (v) => v < 90 },
  { key: 'total_connections', label: 'Connections', fmt: int, good: 'low', extra: { key: 'active_connections', label: 'active' } },
  { key: 'dead_tuple_pct', label: 'Dead tuples', fmt: pct, good: 'low', warn: (v) => v > 5, bad: (v) => v > 20 },
  { key: 'xid_age_pct', label: 'XID age', fmt: pct, good: 'low', domain: [0, 100], warn: (v) => v > 50, bad: (v) => v > 80 },
  { key: 'blocked_queries', label: 'Blocked queries', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 5 },
  { key: 'waiting_queries', label: 'Waiting queries', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 10 },
  { key: 'idle_in_txn_connections', label: 'Idle in transaction', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 10 },
  { key: 'longest_transaction_sec', label: 'Longest transaction', fmt: (v) => (v === null ? '—' : v >= 3600 ? `${(v / 3600).toFixed(1)} h` : v >= 60 ? `${Math.round(v / 60)} min` : `${Math.round(v)} s`), good: 'low', warn: (v) => v > 300, bad: (v) => v > 3600 },
  { key: 'deadlocks_delta', label: 'Deadlocks / interval', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 5 },
  { key: 'txn_per_sec', label: 'Transactions / s', fmt: (v) => (v === null ? '—' : v.toFixed(1)), good: null },
  { key: 'rollback_ratio_pct', label: 'Rollback ratio', fmt: pct, good: 'low', warn: (v) => v > 2, bad: (v) => v > 10 },
  { key: 'autovacuum_workers_active', label: 'Autovacuum workers', fmt: int, good: null },
  { key: 'tables_needing_vacuum', label: 'Tables needing vacuum', fmt: int, good: 'low', warn: (v) => v > 0, bad: (v) => v > 10 },
  { key: 'temp_files', label: 'Temp files (cumulative)', fmt: int, good: null },
  { key: 'database_size_mb', label: 'Database size', fmt: mb, good: null },
]

function series(rows, key) {
  const out = []
  for (const r of rows) {
    const v = num(r[key])
    if (v !== null && Number.isFinite(v)) out.push({ t: r.t, v })
  }
  return out
}

// Polyline segments: a gap wider than three typical intervals breaks the line rather
// than drawing a slope across the time the collector was not running.
function segments(pts) {
  if (pts.length < 2) return [pts]
  const gaps = pts.slice(1).map((p, i) => p.t - pts[i].t).sort((a, b) => a - b)
  const typical = gaps[Math.floor(gaps.length / 2)] || 0
  const out = [[pts[0]]]
  for (let i = 1; i < pts.length; i++) {
    if (typical && pts[i].t - pts[i - 1].t > typical * 3) out.push([pts[i]])
    else out[out.length - 1].push(pts[i])
  }
  return out
}

export function Panel({ panel, rows, scans, t0, t1, groups = null, width = 300, height = 72 }) {
  // Several members (every reader): one line per member, no extra series.
  if (groups && groups.length > 1) return <MultiPanel panel={panel} groups={groups} scans={scans} t0={t0} t1={t1} width={width} height={height} />
  const pts = series(rows, panel.key)
  const extra = panel.extra ? series(rows, panel.extra.key) : []
  const latest = pts.length ? pts[pts.length - 1].v : null
  const first = pts.length ? pts[0].v : null
  const vals = pts.map(p => p.v).concat(extra.map(p => p.v))
  let [lo, hi] = panel.domain || [Math.min(...vals), Math.max(...vals)]
  if (!panel.domain) {
    if (!Number.isFinite(lo)) { lo = 0; hi = 1 }
    if (hi === lo) { hi = lo + (lo === 0 ? 1 : Math.abs(lo) * 0.1) }
    const pad = (hi - lo) * 0.1
    lo = lo >= 0 && lo - pad < 0 ? 0 : lo - pad
    hi = hi + pad
  }
  const padL = 4, padR = 4, padT = 6, padB = 6
  const x = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * (width - padL - padR)
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (height - padT - padB)
  const path = (ps) => ps.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')
  const tone = latest === null ? COLOR.muted : panel.bad?.(latest) ? COLOR.err : panel.warn?.(latest) ? COLOR.warn : COLOR.primary
  const delta = (latest !== null && first !== null) ? latest - first : null
  const deltaCls = delta === null || Math.abs(delta) < 1e-9 || !panel.good ? 'text-fg-muted'
    : (delta > 0) === (panel.good === 'high') ? 'text-ok' : 'text-err'
  const deltaText = delta === null ? '' : `${delta > 0 ? '+' : ''}${panel.fmt === pct ? `${delta.toFixed(1)}%` : panel.fmt === mb ? mb(Math.abs(delta)).replace(/^/, delta < 0 ? '-' : '') : Math.round(delta)}`
  const stats = pts.length ? { min: Math.min(...pts.map(p => p.v)), max: Math.max(...pts.map(p => p.v)) } : null
  return (
    <div className="bg-surface rounded-lg border border-line p-3 min-w-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold">{panel.label}</span>
        {stats && <span className="text-[10px] text-fg-muted tabular-nums">min {panel.fmt(stats.min)} · max {panel.fmt(stats.max)}</span>}
      </div>
      <div className="flex items-baseline gap-2 mt-0.5">
        <span className="text-lg font-bold tabular-nums" style={{ color: tone }}>{panel.fmt(latest)}</span>
        {delta !== null && <span className={`text-[10px] tabular-nums ${deltaCls}`} title="Change since the start of the window">{deltaText} in window</span>}
        {panel.extra && extra.length > 0 && (
          <span className="text-[10px] text-fg-muted tabular-nums ml-auto">{int(extra[extra.length - 1].v)} {panel.extra.label}</span>
        )}
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full mt-1" style={{ height }} role="img" aria-label={`${panel.label} over the window`}>
        {/* Deep scans in the window: a marker per scan in its health colour. */}
        {scans.map(s => (
          <line key={s.id || s.t} x1={x(s.t)} x2={x(s.t)} y1={padT - 4} y2={height - padB + 4}
            stroke={HEALTH_COLORS[s.health_score] || HEALTH_COLORS.unknown} strokeWidth="1.5" strokeDasharray="2 2" opacity="0.8">
            <title>{`Deep scan ${dayClock(s.t)} — ${(s.health_score || 'unknown').toUpperCase()}`}</title>
          </line>
        ))}
        {pts.length === 0 ? (
          <text x={width / 2} y={height / 2 + 4} textAnchor="middle" fontSize="10" fill={COLOR.muted}>no samples</text>
        ) : (
          <>
            {panel.extra && segments(extra).map((seg, i) => (
              <path key={`x${i}`} d={path(seg)} fill="none" stroke={COLOR.muted} strokeWidth="1" strokeDasharray="3 2" />
            ))}
            {segments(pts).map((seg, i) => seg.length === 1
              ? <circle key={i} cx={x(seg[0].t)} cy={y(seg[0].v)} r="1.8" fill={tone} />
              : <path key={i} d={path(seg)} fill="none" stroke={tone} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />)}
            <circle cx={x(pts[pts.length - 1].t)} cy={y(latest)} r="2.5" fill={tone} />
          </>
        )}
      </svg>
    </div>
  )
}

function MultiPanel({ panel, groups, scans, t0, t1, width, height }) {
  const lines = groups.map(g => ({ ...g, pts: series(g.rows, panel.key) }))
  const vals = lines.flatMap(l => l.pts.map(p => p.v))
  let [lo, hi] = panel.domain || [Math.min(...vals), Math.max(...vals)]
  if (!panel.domain) {
    if (!Number.isFinite(lo)) { lo = 0; hi = 1 }
    if (hi === lo) { hi = lo + (lo === 0 ? 1 : Math.abs(lo) * 0.1) }
    const pad = (hi - lo) * 0.1
    lo = lo >= 0 && lo - pad < 0 ? 0 : lo - pad
    hi = hi + pad
  }
  const padL = 4, padR = 4, padT = 6, padB = 6
  const x = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * (width - padL - padR)
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (height - padT - padB)
  const path = (ps) => ps.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')
  return (
    <div className="bg-surface rounded-lg border border-line p-3 min-w-0">
      <span className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold">{panel.label}</span>
      <ul className="flex flex-wrap gap-x-3 gap-y-0.5 mt-0.5" aria-label={`${panel.label}, latest per member`}>
        {lines.map(l => (
          <li key={l.key} className="text-[10px] text-fg-3 tabular-nums flex items-center gap-1">
            <span className="inline-block w-2.5 h-0.5" style={{ background: l.color }} aria-hidden />
            {l.label} <span className="text-fg font-semibold">{panel.fmt(l.pts.length ? l.pts[l.pts.length - 1].v : null)}</span>
          </li>
        ))}
      </ul>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full mt-1" style={{ height }} role="img" aria-label={`${panel.label} over the window, one line per member`}>
        {scans.map(s => {
          const g = lines.find(l => l.key === s.member_instance_id)
          return (
            <line key={s.id || s.t} x1={x(s.t)} x2={x(s.t)} y1={padT - 4} y2={height - padB + 4}
              stroke={g?.color || HEALTH_COLORS[s.health_score] || HEALTH_COLORS.unknown} strokeWidth="1.5" strokeDasharray="2 2" opacity="0.8">
              <title>{`Reader scan of ${s.member_instance_id || 'a reader'} ${dayClock(s.t)} — ${(s.health_score || 'unknown').toUpperCase()}`}</title>
            </line>
          )
        })}
        {lines.every(l => l.pts.length === 0) && (
          <text x={width / 2} y={height / 2 + 4} textAnchor="middle" fontSize="10" fill={COLOR.muted}>no samples</text>
        )}
        {lines.map(l => segments(l.pts).map((seg, i) => seg.length === 1
          ? <circle key={`${l.key}${i}`} cx={x(seg[0].t)} cy={y(seg[0].v)} r="1.8" fill={l.color} />
          : <path key={`${l.key}${i}`} d={path(seg)} fill="none" stroke={l.color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />))}
      </svg>
    </div>
  )
}

export default function TrendChart({ api, instanceId, instance = null, member = '' }) {
  const [hours, setHours] = useState('24')
  const [rows, setRows] = useState([])
  const [scans, setScans] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const readerTarget = targetsReaders(member, instance?.cluster_member_roles)

  useEffect(() => {
    if (!instanceId) return
    let cancelled = false
    setLoading(true); setError('')
    const h = Number(hours)
    const load = async () => {
      // Tier 1: ONE request for the window, server-bucketed to ~300 points per chart
      // (average per fixed bucket, per member for clusters) instead of paging thousands
      // of raw 5-minute samples. The raw-sample route stays for exports and records.
      const d = await api.get(`/api/scan-metrics/${encodeURIComponent(instanceId)}/bucketed?hours=${h}${member ? `&member=${encodeURIComponent(member)}` : ''}`)
      const all = d?.rows || []
      // Tier 2: the deep scans of the same window, as markers.
      const days = Math.max(1, Math.ceil(h / 24))
      // A reader target reads reader scans; the writer (or a single instance) deep scans.
      const kind = readerTarget ? 'reader' : 'full'
      const sr = await api.get(`/api/scan-results?instance_id=${encodeURIComponent(instanceId)}&days=${days}&limit=100&kind=${kind}${member ? `&member=${encodeURIComponent(member)}` : ''}`).catch(() => ({ results: [] }))
      return { rows: all, scans: sr?.results || [] }
    }
    load().then(({ rows: r, scans: s }) => {
      if (cancelled) return
      const cutoff = Date.now() - h * 3600e3
      const seen = new Set()
      setRows(r.filter(x => { const k = x.id ?? x.scan_time; if (seen.has(k)) return false; seen.add(k); return true })
        .map(x => ({ ...x, t: Date.parse(x.scan_time) })).filter(x => Number.isFinite(x.t)).sort((a, b) => a.t - b.t))
      setScans(s.map(x => ({ ...x, t: Date.parse(x.scan_time) })).filter(x => Number.isFinite(x.t) && x.t >= cutoff).sort((a, b) => a.t - b.t))
    }).catch(e => { if (!cancelled) setError(e?.status === 503 ? 'The metrics store is not configured for this deployment.' : (e?.detail || e?.message || 'Could not load Tier 1 metrics')) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [api, instanceId, hours, member, readerTarget])

  // Every reader: one series per member, keyed and coloured by member id.
  const groups = useMemo(() => {
    if (member !== 'reader') return null
    const by = new Map()
    for (const r of rows) {
      const k = r.member_instance_id || '?'
      if (!by.has(k)) by.set(k, [])
      by.get(k).push(r)
    }
    return [...by.keys()].sort().map((k, i) => ({ key: k, label: `reader · ${k}`, color: SERIES_COLORS[i % SERIES_COLORS.length], rows: by.get(k) }))
  }, [rows, member])

  const t1 = Date.now()
  const t0 = t1 - Number(hours) * 3600e3
  const cadence = useMemo(() => {
    // One member's samples: several readers interleave and would halve the gap.
    const one = groups && groups.length ? groups[0].rows : rows
    if (one.length < 2) return null
    const gaps = one.slice(1).map((r, i) => r.t - one[i].t).sort((a, b) => a - b)
    return gaps[Math.floor(gaps.length / 2)]
  }, [rows, groups])
  const last = rows.length ? rows[rows.length - 1] : null
  const rangeLabel = Number(hours) >= 24 ? `${dayClock(t0)} → ${dayClock(t1)}` : `${clock(t0)} → ${clock(t1)}`

  return (
    <div className="space-y-3">
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
        <div className="text-[11px] text-fg-3 flex items-center gap-3 flex-wrap">
          {last && (
            <span>
              Tier 1: {rows.length.toLocaleString()} samples{cadence ? ` · every ~${Math.max(1, Math.round(cadence / 60000))} min` : ''} · last {dayClock(last.t)}

            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-3 border-t border-dashed" style={{ borderColor: HEALTH_COLORS.ok }} aria-hidden />
            {readerTarget ? 'Reader scans' : 'Deep scans (Tier 2)'}: {scans.length} in window
          </span>
        </div>
      </div>
      {loading ? (
        <div className="text-xs text-fg-muted py-6 text-center">Loading Tier 1 samples…</div>
      ) : error ? (
        <div className="text-xs text-err py-6 text-center">{error}</div>
      ) : rows.length === 0 && scans.length === 0 ? (
        <div className="text-xs text-fg-muted py-6 text-center">
          No Tier 1 samples or deep scans in this window. Tier 1 collection runs every few minutes once monitoring is configured for the database.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {PANELS.map(p => <Panel key={p.key} panel={p} rows={rows} scans={scans} t0={t0} t1={t1} groups={groups} />)}
          </div>
          <div className="text-[10px] text-fg-muted">{rangeLabel} · lines break where samples are missing; {groups && groups.length > 1 ? 'one line per reader; a dashed marker is a reader scan, in its reader\'s colour.' : `a dashed marker is a ${readerTarget ? 'reader' : 'deep'} scan, coloured by its health.`}</div>
        </>
      )}
    </div>
  )
}
