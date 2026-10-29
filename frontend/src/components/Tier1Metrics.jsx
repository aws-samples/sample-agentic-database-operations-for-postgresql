// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Metrics (Tier 1) tab of the Dashboard's database drawer: every lightweight
// collection for one database, newest first, over a chosen window. Rows expand to
// the two lists that don't fit a column (table bloat, wait events). Pages are
// numbered like the Deep scans tab beside it (usePagedRecords walks the API's
// keyset cursor as the reader moves, so deep history still costs one index range
// scan per chunk). Mirrors the admin console's records sheet so both audiences
// read the same numbers the same way. For a DB cluster `member` (the drawer's
// member picker) chooses whose rows: the writer, every reader, or one member.
import { useState } from 'react'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import usePagedRecords from '../hooks/usePagedRecords'
import Pager from './Pager'
import { targetsReaders } from './MemberPicker'

const PAGE_SIZE = 10
const RANGES = [['1', '1 h'], ['6', '6 h'], ['24', '24 h'], ['168', '7 d']]

const fmtTime = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
const num = (v) => (v === null || v === undefined || v === '') ? '—' : Number(v).toLocaleString()
const pct = (v) => (v === null || v === undefined) ? '—' : `${Number(v).toFixed(1)}%`
const secs = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 3600 ? `${(v / 3600).toFixed(1)} h` : Number(v) >= 60 ? `${Math.round(v / 60)} min` : `${Math.round(v)} s`
const mb = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`
const lagMs = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`
const rate = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 100 ? Math.round(v).toLocaleString() : Number(v).toFixed(1)

const th = 'py-2 px-2 text-left text-[10px] uppercase tracking-wider text-fg-3 font-semibold whitespace-nowrap'
// One text colour per cell (Tailwind emits .text-err before .text-fg-2, so stacking
// both would let the muted default win).
const tdBase = 'py-1.5 px-2 text-[11px] whitespace-nowrap tabular-nums'
const td = `${tdBase} text-fg-2`
const cell = (flag, cls) => `${tdBase} ${flag ? `${cls} font-medium` : 'text-fg-2'}`

export default function Tier1Metrics({ api, instance, member = '' }) {
  const [hours, setHours] = useState('24')
  const [open, setOpen] = useState(null)
  const id = instance?.instance_id
  const pager = usePagedRecords(api,
    (cursor, chunk) => `/api/scan-metrics/${id}?hours=${hours}&limit=${chunk}${member ? `&member=${encodeURIComponent(member)}` : ''}${cursor ? `&before=${encodeURIComponent(cursor)}` : ''}`,
    [id, hours, member], { pageSize: PAGE_SIZE })
  const showMember = member === 'reader'
  const showLag = targetsReaders(member, instance?.cluster_member_roles)
  const cols = 16 + (showMember ? 1 : 0) + (showLag ? 1 : 0)
  const { rows, loading } = pager
  const error = pager.error ? (pager.error.status === 503 ? 'The metrics store is not configured for this deployment.' : (pager.error.detail || pager.error.message || 'Could not load metrics')) : ''
  const turn = (fn) => (...a) => { setOpen(null); fn(...a) }
  const count = pager.total ?? pager.all.length

  return (
    <div>
      <div className="flex items-center gap-3 mb-3 flex-wrap">
        <div className="flex items-center gap-1" role="group" aria-label="Time range">
          {RANGES.map(([k, label]) => (
            <button key={k} onClick={() => { setOpen(null); setHours(k) }} aria-pressed={hours === k}
              className={`px-2.5 py-1 text-[11px] rounded-full border transition-colors ${
                hours === k ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
              {label}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-fg-muted">PostgreSQL-internal metrics collected every {instance?.tier1_frequency || '5min'}</span>
        <span className="text-[11px] text-fg-muted ml-auto">
          {count}{pager.open ? '+' : ''} row{count === 1 ? '' : 's'}{pager.pageCount > 1 ? ` · page ${pager.page} of ${pager.pageCount}${pager.open ? '+' : ''}` : ''}
        </span>
        <Pager page={pager.page} pageCount={pager.pageCount} onPage={turn(pager.goTo)} open={pager.open} onMore={turn(pager.more)} loading={loading} label="Metrics pages" />
        <button onClick={turn(pager.reload)} title="Refresh" className="p-1 rounded text-fg-muted hover:text-primary transition-colors">
          <RefreshCw size={13} strokeWidth={1.75} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>
      {error && <p className="text-xs text-err py-3">{error}</p>}
      {loading && rows.length === 0 && !error && <div className="text-sm text-fg-muted py-4 text-center">Loading metrics...</div>}
      {!loading && !error && rows.length === 0 && <div className="text-sm text-fg-muted py-4 text-center">No metrics collected in this window.</div>}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead><tr className="border-b border-line">
              <th className={th}></th>
              <th className={th}>Time</th>
              {showMember && <th className={th}>Member</th>}
              {showLag && <th className={th} title="Replica lag at collection">Lag</th>}
              <th className={th} title="Buffer cache hit ratio">Cache</th>
              <th className={th} title="Connections: total · active · idle · idle in transaction">Conns</th>
              <th className={th} title="Connections as a share of max_connections (the connection-saturation alarm)">Conn %</th>
              <th className={th}>Blocked</th>
              <th className={th}>Waiting</th>
              <th className={th} title="Dead tuples, worst table">Dead</th>
              <th className={th} title="Transaction ID age, % of wraparound">XID</th>
              <th className={th}>Size</th>
              <th className={th} title="Autovacuum workers active · tables needing vacuum">Vacuum</th>
              <th className={th} title="Longest open transaction">Longest txn</th>
              <th className={th} title="Deadlocks since the previous sample">Deadlocks</th>
              <th className={th} title="Transactions per second since the previous sample">Txn/s</th>
              <th className={th} title="Rolled-back share of transactions since the previous sample">Rollback</th>
              <th className={th} title="Temporary files written (cumulative) · size">Temp files</th>
            </tr></thead>
            <tbody>
              {rows.map(r => {
                const key = r.id ?? r.scan_time
                const isOpen = open === key
                const hasDetail = (r.table_bloat?.length || 0) + (r.wait_events?.length || 0) > 0
                return [
                  <tr key={key} className="border-b border-line-2 hover:bg-surface-3">
                    <td className="py-1.5 pl-1 pr-0 w-6">
                      {hasDetail && (
                        <button onClick={() => setOpen(isOpen ? null : key)} aria-expanded={isOpen} aria-label={isOpen ? 'Hide details' : 'Show details'}
                          className="p-0.5 rounded text-fg-muted hover:text-fg">
                          {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                        </button>
                      )}
                    </td>
                    <td className={`${tdBase} text-fg`}>{fmtTime(r.scan_time)}</td>
                    {showMember && <td className={`${tdBase} text-fg-2 font-mono`}>{r.member_instance_id || '—'}</td>}
                    {showLag && <td className={td}>{lagMs(r.replica_lag_ms)}</td>}
                    <td className={td}>{pct(r.cache_hit_ratio)}</td>
                    <td className={td}>{num(r.total_connections)} <span className="text-fg-muted">· {num(r.active_connections)} · {num(r.idle_connections)} · {num(r.idle_in_txn_connections)}</span></td>
                    <td className={cell(Number(r.connections_used_pct) >= 80, 'text-warn-text')}>{pct(r.connections_used_pct)}</td>
                    <td className={cell(Number(r.blocked_queries) > 0, 'text-err')}>{num(r.blocked_queries)}</td>
                    <td className={td}>{num(r.waiting_queries)}</td>
                    <td className={cell(Number(r.dead_tuple_pct) >= 20, 'text-warn-text')}>{pct(r.dead_tuple_pct)}</td>
                    <td className={cell(Number(r.xid_age_pct) >= 50, 'text-warn-text')}>{pct(r.xid_age_pct)}</td>
                    <td className={td}>{mb(r.database_size_mb)}</td>
                    <td className={td}>{num(r.autovacuum_workers_active)} <span className="text-fg-muted">· {num(r.tables_needing_vacuum)}</span></td>
                    <td className={td}>{secs(r.longest_transaction_sec)}</td>
                    <td className={cell(Number(r.deadlocks_delta) > 0, 'text-err')}>{num(r.deadlocks_delta)}</td>
                    <td className={td}>{rate(r.txn_per_sec)}</td>
                    <td className={cell(Number(r.rollback_ratio_pct) >= 5, 'text-warn-text')}>{pct(r.rollback_ratio_pct)}</td>
                    <td className={td}>{num(r.temp_files)} <span className="text-fg-muted">· {mb(r.temp_bytes_mb)}</span></td>
                  </tr>,
                  isOpen && (
                    <tr key={`${key}-d`} className="border-b border-line-2 bg-surface-2">
                      <td colSpan={cols} className="px-4 py-3">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                          <div>
                            <p className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold mb-1.5">Table bloat (top {r.table_bloat?.length || 0})</p>
                            {r.table_bloat?.length ? (
                              <ul className="space-y-0.5">
                                {r.table_bloat.map((t, i) => (
                                  <li key={i} className="text-[11px] text-fg-2 flex justify-between gap-3 font-mono">
                                    <span className="truncate">{t.schema}.{t.table}</span>
                                    <span className="text-fg-3 flex-shrink-0">{pct(t.bloat_pct)} · {num(t.dead_tuples)} dead</span>
                                  </li>
                                ))}
                              </ul>
                            ) : <p className="text-[11px] text-fg-muted">None recorded.</p>}
                          </div>
                          <div>
                            <p className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold mb-1.5">Wait events (top {r.wait_events?.length || 0})</p>
                            {r.wait_events?.length ? (
                              <ul className="space-y-0.5">
                                {r.wait_events.map((w, i) => (
                                  <li key={i} className="text-[11px] text-fg-2 flex justify-between gap-3 font-mono">
                                    <span className="truncate">{w.type}/{w.event}</span>
                                    <span className="text-fg-3 flex-shrink-0">{num(w.sessions)} session{w.sessions === 1 ? '' : 's'}</span>
                                  </li>
                                ))}
                              </ul>
                            ) : <p className="text-[11px] text-fg-muted">None recorded.</p>}
                          </div>
                        </div>
                      </td>
                    </tr>
                  ),
                ]
              })}
            </tbody>
          </table>
        </div>
      )}
      {(pager.pageCount > 1 || pager.open) && (
        <div className="pt-3 flex justify-end">
          <Pager page={pager.page} pageCount={pager.pageCount} onPage={turn(pager.goTo)} open={pager.open} onMore={turn(pager.more)} loading={loading} label="Metrics pages (bottom)" />
        </div>
      )}
    </div>
  )
}
