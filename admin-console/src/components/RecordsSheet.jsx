// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// The raw records behind one database's collection status: a side sheet with a
// Metrics (Tier 1) / Deep scans (Tier 2) switch, a time-range picker, a table of
// rows newest first, and numbered pages (usePagedRecords walks the API's keyset
// cursor as the reader moves, the same way the operator app's drawer does). Rows
// expand to show the JSON lists (table bloat, wait events, findings) that do not
// fit a column. Read-only; the admin console has no report viewer, so deep-scan
// rows point at the operator app for the full report. For a DB cluster a Members
// tab lists the writer and readers, and a member picker chooses whose rows the two
// tables read (the writer by default; every reader; or one member).
import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import SideSheet from './SideSheet'
import Pager from './Pager'
import ClusterMembers from './ClusterMembers'
import MemberPicker from './MemberPicker'
import usePagedRecords from '../hooks/usePagedRecords'

const PAGE_SIZE = 20
const T1_RANGES = [['1', '1 h'], ['6', '6 h'], ['24', '24 h'], ['168', '7 d']]
const T2_RANGES = [['7', '7 d'], ['30', '30 d'], ['90', '90 d'], ['365', '1 y']]
const HEALTH_CHIPS = [['', 'All'], ['critical', 'Critical'], ['warning', 'Warning'], ['ok', 'OK']]
const HEALTH_BADGE = {
  ok: 'bg-ok-bg text-ok-text border-ok-border',
  warning: 'bg-warn-bg text-warn-text border-warn-border',
  critical: 'bg-err-bg text-err-text border-err-border',
  unknown: 'bg-surface-2 text-fg-3 border-line',
}

const fmtTime = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
const num = (v, d = 0) => (v === null || v === undefined || v === '') ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d })
const pct = (v) => (v === null || v === undefined) ? '—' : `${Number(v).toFixed(1)}%`
const secs = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 3600 ? `${(v / 3600).toFixed(1)} h` : Number(v) >= 60 ? `${Math.round(v / 60)} min` : `${Math.round(v)} s`
const mb = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`

export function Chips({ options, value, onChange, label }) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={label}>
      {options.map(([k, text]) => (
        <button key={k} onClick={() => onChange(k)} aria-pressed={value === k}
          className={`px-2.5 py-1 text-[11px] rounded-full border transition-colors ${
            value === k ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
          {text}
        </button>
      ))}
    </div>
  )
}

// The row count + page position label and the page controls, shared by both tables
// (and the audit log). `onMore`, when given, replaces `page.more` for the trailing "…".
export function PageBar({ page, noun, onTurn, onMore }) {
  const count = page.total ?? page.all.length
  return (
    <>
      <span className="text-[11px] text-fg-muted ml-auto">
        {count}{page.open ? '+' : ''} {noun}{count === 1 ? '' : 's'}{page.pageCount > 1 ? ` · page ${page.page} of ${page.pageCount}${page.open ? '+' : ''}` : ''}
      </span>
      <Pager page={page.page} pageCount={page.pageCount} onPage={onTurn(page.goTo)} open={page.open} onMore={onTurn(onMore || page.more)} loading={page.loading} label={`${noun} pages`} />
    </>
  )
}
const errorText = (e) => e ? (e.detail || e.message || 'Request failed') : ''

const th = 'py-2 px-2 text-left text-[10px] uppercase tracking-wider text-fg-3 font-semibold whitespace-nowrap'
// One text colour per cell: Tailwind emits .text-err before .text-fg-2, so stacking
// both would let the muted default win; the helper picks exactly one.
const tdBase = 'py-1.5 px-2 text-xs whitespace-nowrap tabular-nums'
const td = `${tdBase} text-fg-2`
const cell = (flag, cls) => `${tdBase} ${flag ? `${cls} font-medium` : 'text-fg-2'}`

const memberParam = (member) => member ? `&member=${encodeURIComponent(member)}` : ''
const lagMs = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`

function Tier1Table({ api, instanceId, member = '' }) {
  const [hours, setHours] = useState('24')
  const [open, setOpen] = useState(null)
  const page = usePagedRecords(api, (cursor, chunk) => `/api/scan-metrics/${instanceId}?hours=${hours}&limit=${chunk}${memberParam(member)}${cursor ? `&before=${encodeURIComponent(cursor)}` : ''}`, [instanceId, hours, member], { pageSize: PAGE_SIZE })
  const showMember = !!member
  const turn = (fn) => (...a) => { setOpen(null); fn(...a) }
  return (
    <div>
      <div className="flex items-center gap-3 px-6 py-3 border-b border-line-2 flex-wrap">
        <Chips options={T1_RANGES} value={hours} onChange={(v) => { setOpen(null); setHours(v) }} label="Time range" />
        <PageBar page={page} noun="row" onTurn={turn} />
        <button onClick={turn(page.reload)} title="Refresh" className="p-1 rounded text-fg-3 hover:text-fg hover:bg-surface-3"><RefreshCw size={13} strokeWidth={1.75} className={page.loading ? 'animate-spin' : ''} /></button>
      </div>
      {page.error && <p className="px-6 py-3 text-xs text-err">{errorText(page.error)}</p>}
      {!page.loading && !page.error && page.rows.length === 0 && (
        <p className="px-6 py-8 text-sm text-fg-muted text-center">No metrics collected in this window.</p>
      )}
      {page.rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead><tr className="border-b border-line">
              <th className={th}></th>
              <th className={th}>Time</th>
              {showMember && <th className={th}>Member</th>}
              {showMember && <th className={th} title="Replica lag at collection">Lag</th>}
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
              {page.rows.map(r => {
                const id = r.id ?? r.scan_time
                const isOpen = open === id
                const hasDetail = (r.table_bloat?.length || 0) + (r.wait_events?.length || 0) > 0
                return [
                  <tr key={id} className="border-b border-line-2 hover:bg-surface-3">
                    <td className="py-1.5 pl-3 pr-0 w-6">
                      {hasDetail && (
                        <button onClick={() => setOpen(isOpen ? null : id)} aria-expanded={isOpen} aria-label={isOpen ? 'Hide details' : 'Show details'}
                          className="p-0.5 rounded text-fg-muted hover:text-fg">
                          {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                        </button>
                      )}
                    </td>
                    <td className={`${tdBase} text-fg`}>{fmtTime(r.scan_time)}</td>
                    {showMember && <td className={`${tdBase} text-fg-2 font-mono`}>{r.member_instance_id || '—'} <span className="text-fg-muted">{r.member_role}</span></td>}
                    {showMember && <td className={td}>{lagMs(r.replica_lag_ms)}</td>}
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
                    <td className={td}>{num(r.txn_per_sec, 1)}</td>
                    <td className={cell(Number(r.rollback_ratio_pct) >= 5, 'text-warn-text')}>{pct(r.rollback_ratio_pct)}</td>
                    <td className={td}>{num(r.temp_files)} <span className="text-fg-muted">· {mb(r.temp_bytes_mb)}</span></td>
                  </tr>,
                  isOpen && (
                    <tr key={`${id}-d`} className="border-b border-line-2 bg-surface-2">
                      <td colSpan={showMember ? 18 : 16} className="px-6 py-3">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                          <div>
                            <p className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold mb-1.5">Table bloat (top {r.table_bloat?.length || 0})</p>
                            {r.table_bloat?.length ? (
                              <ul className="space-y-0.5">
                                {r.table_bloat.map((t, i) => (
                                  <li key={i} className="text-xs text-fg-2 flex justify-between gap-3 font-mono">
                                    <span className="truncate">{t.schema}.{t.table}</span>
                                    <span className="text-fg-3 flex-shrink-0">{pct(t.bloat_pct)} · {num(t.dead_tuples)} dead</span>
                                  </li>
                                ))}
                              </ul>
                            ) : <p className="text-xs text-fg-muted">None recorded.</p>}
                          </div>
                          <div>
                            <p className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold mb-1.5">Wait events (top {r.wait_events?.length || 0})</p>
                            {r.wait_events?.length ? (
                              <ul className="space-y-0.5">
                                {r.wait_events.map((w, i) => (
                                  <li key={i} className="text-xs text-fg-2 flex justify-between gap-3 font-mono">
                                    <span className="truncate">{w.type}/{w.event}</span>
                                    <span className="text-fg-3 flex-shrink-0">{num(w.sessions)} session{w.sessions === 1 ? '' : 's'}</span>
                                  </li>
                                ))}
                              </ul>
                            ) : <p className="text-xs text-fg-muted">None recorded.</p>}
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
      {(page.pageCount > 1 || page.open) && (
        <div className="px-6 py-3 flex justify-end">
          <Pager page={page.page} pageCount={page.pageCount} onPage={turn(page.goTo)} open={page.open} onMore={turn(page.more)} loading={page.loading} label="Pages (bottom)" />
        </div>
      )}
    </div>
  )
}

function Tier2Table({ api, instanceId, member = '' }) {
  const [days, setDays] = useState('30')
  const [health, setHealth] = useState('')
  const [open, setOpen] = useState(null)
  const page = usePagedRecords(api, (cursor, chunk) => `/api/scan-results?instance_id=${encodeURIComponent(instanceId)}&days=${days}&limit=${chunk}${memberParam(member)}${health ? `&health=${health}` : ''}${cursor ? `&before=${encodeURIComponent(cursor)}` : ''}`, [instanceId, days, health, member], { pageSize: PAGE_SIZE })
  const showMember = !!member
  const turn = (fn) => (...a) => { setOpen(null); fn(...a) }
  const counts = (f) => {
    const list = Array.isArray(f) ? f : []
    return { critical: list.filter(x => x?.severity === 'CRITICAL').length, warning: list.filter(x => x?.severity === 'WARNING').length, total: list.length }
  }
  return (
    <div>
      <div className="flex items-center gap-3 px-6 py-3 border-b border-line-2 flex-wrap">
        <Chips options={T2_RANGES} value={days} onChange={(v) => { setOpen(null); setDays(v) }} label="Time range" />
        <span className="w-px h-4 bg-line" />
        <Chips options={HEALTH_CHIPS} value={health} onChange={(v) => { setOpen(null); setHealth(v) }} label="Health" />
        <PageBar page={page} noun="scan" onTurn={turn} />
        <button onClick={turn(page.reload)} title="Refresh" className="p-1 rounded text-fg-3 hover:text-fg hover:bg-surface-3"><RefreshCw size={13} strokeWidth={1.75} className={page.loading ? 'animate-spin' : ''} /></button>
      </div>
      {page.error && <p className="px-6 py-3 text-xs text-err">{errorText(page.error)}</p>}
      {!page.loading && !page.error && page.rows.length === 0 && (
        <p className="px-6 py-8 text-sm text-fg-muted text-center">No deep scans in this window.</p>
      )}
      {page.rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead><tr className="border-b border-line">
              <th className={th}></th>
              <th className={th}>Scanned</th>
              {showMember && <th className={th}>Member</th>}
              {showMember && <th className={th}>Kind</th>}
              <th className={th}>Health</th>
              <th className={th} title="Findings: critical · warning · total">Findings</th>
              <th className={th}>Cache</th>
              <th className={th}>Conns</th>
              <th className={th}>Size</th>
              <th className={th} title="Unused indexes">Unused idx</th>
              <th className={`${th} w-full`}>Summary</th>
            </tr></thead>
            <tbody>
              {page.rows.map(r => {
                const id = r.id ?? r.scan_time
                const isOpen = open === id
                const c = counts(r.findings)
                const badge = HEALTH_BADGE[r.health_score] || HEALTH_BADGE.unknown
                return [
                  <tr key={id} className="border-b border-line-2 hover:bg-surface-3">
                    <td className="py-1.5 pl-3 pr-0 w-6">
                      <button onClick={() => setOpen(isOpen ? null : id)} aria-expanded={isOpen} aria-label={isOpen ? 'Hide details' : 'Show details'}
                        className="p-0.5 rounded text-fg-muted hover:text-fg">
                        {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                      </button>
                    </td>
                    <td className={`${tdBase} text-fg`}>{fmtTime(r.scan_time)}</td>
                    {showMember && <td className={`${tdBase} text-fg-2 font-mono`}>{r.member_instance_id || '—'}</td>}
                    {showMember && <td className={td}>{r.scan_kind === 'reader' ? 'Reader scan' : 'Deep scan'}</td>}
                    <td className={td}><span className={`inline-flex px-2 py-0.5 rounded-full text-[10px] font-medium border capitalize ${badge}`}>{r.health_score || 'unknown'}</span></td>
                    <td className={td}>
                      <span className={c.critical ? 'text-err font-medium' : 'text-fg-muted'}>{c.critical}</span>
                      <span className="text-fg-muted"> · </span>
                      <span className={c.warning ? 'text-warn-text font-medium' : 'text-fg-muted'}>{c.warning}</span>
                      <span className="text-fg-muted"> · {c.total}</span>
                    </td>
                    <td className={td}>{pct(r.cache_hit_ratio)}</td>
                    <td className={td}>{num(r.total_connections)}</td>
                    <td className={td}>{mb(r.database_size_mb)}</td>
                    <td className={td}>{num(r.unused_indexes_count)}</td>
                    <td className="py-1.5 px-2 text-xs text-fg-3 max-w-[260px] truncate" title={r.summary || ''}>{r.summary || '—'}</td>
                  </tr>,
                  isOpen && (
                    <tr key={`${id}-d`} className="border-b border-line-2 bg-surface-2">
                      <td colSpan={showMember ? 11 : 9} className="px-6 py-3">
                        {r.summary && <p className="text-xs text-fg-2 mb-3 leading-relaxed">{r.summary}</p>}
                        <p className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold mb-1.5">Findings ({c.total})</p>
                        {c.total ? (
                          <ul className="space-y-1">
                            {(r.findings || []).map((f, i) => (
                              <li key={i} className="text-xs text-fg-2 flex gap-2">
                                <span className={`flex-shrink-0 w-16 text-[10px] font-semibold uppercase ${f.severity === 'CRITICAL' ? 'text-err' : f.severity === 'WARNING' ? 'text-warn-text' : 'text-fg-muted'}`}>{f.severity || 'INFO'}</span>
                                <span><span className="text-fg font-medium">{f.title || f.area || 'Finding'}</span>{f.detail ? <span className="text-fg-3"> — {f.detail}</span> : null}</span>
                              </li>
                            ))}
                          </ul>
                        ) : <p className="text-xs text-fg-muted">No findings recorded.</p>}
                        {r.report_s3_key && <p className="text-[10px] text-fg-muted mt-3 font-mono truncate" title={r.report_s3_key}>Report: {r.report_s3_key.split('/').slice(-2).join('/')}</p>}
                      </td>
                    </tr>
                  ),
                ]
              })}
            </tbody>
          </table>
        </div>
      )}
      {(page.pageCount > 1 || page.open) && (
        <div className="px-6 py-3 flex justify-end">
          <Pager page={page.page} pageCount={page.pageCount} onPage={turn(page.goTo)} open={page.open} onMore={turn(page.more)} loading={page.loading} label="Pages (bottom)" />
        </div>
      )}
    </div>
  )
}

export default function RecordsSheet({ api, db, initialTier = 1, onClose }) {
  const [tier, setTier] = useState(initialTier)
  const [member, setMember] = useState('writer')
  useEffect(() => { setMember('writer') }, [db?.instance_id])
  if (!db) return null
  const cluster = (db.cluster_member_roles || []).length > 0
  const tabs = [...(cluster ? [[0, 'Members']] : []), [1, 'Metrics (Tier 1)'], [2, 'Deep scans (Tier 2)']]
  const selector = cluster ? member : ''
  const header = (
    <div className="flex items-center gap-3 mt-3 flex-wrap">
      <div className="flex items-center gap-1 p-0.5 rounded-lg bg-surface-2 border border-line-2 w-fit" role="tablist" aria-label="Record type">
        {tabs.map(([t, label]) => (
          <button key={t} role="tab" aria-selected={tier === t} onClick={() => setTier(t)}
            className={`px-3 h-7 rounded-md text-xs transition-colors ${tier === t ? 'bg-surface text-fg font-medium shadow-sm' : 'text-fg-3 hover:text-fg'}`}>
            {label}
          </button>
        ))}
      </div>
      {cluster && tier !== 0 && <MemberPicker api={api} instance={db} value={member} onChange={setMember} id={`records-member-${db.instance_id}`} />}
    </div>
  )
  const subtitle = tier === 0
    ? 'The writer and readers of this cluster, with the endpoints and each member\'s collection status'
    : tier === 1
      ? `Every Tier 1 collection, newest first · every ${db.tier1_frequency || '5min'}${db.tier1_enabled === false ? ' · paused' : ''}`
      : `Every deep scan, newest first · ${db.tier2_frequency || 'daily'}${db.tier2_enabled === false ? ' · paused' : ''} · full reports open in the operator app`
  return (
    <SideSheet title={db.display_name || db.instance_id} width="max-w-[960px]" onClose={onClose} header={header} subtitle={subtitle}>
      {tier === 0 && (
        <div className="px-6 py-4">
          <ClusterMembers api={api} instance={db} onSelectMember={(sel) => { setMember(sel); setTier(1) }} />
        </div>
      )}
      {tier === 1 && <Tier1Table api={api} instanceId={db.instance_id} member={selector} />}
      {tier === 2 && <Tier2Table api={api} instanceId={db.instance_id} member={selector} />}
    </SideSheet>
  )
}
