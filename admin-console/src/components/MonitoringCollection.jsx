// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Monitoring › Collection: is collection actually happening? One row per configured
// database with, for each tier, the newest record judged against the configured
// cadence (fresh / stale / none / paused), the recent volume, and the retention;
// "Records" opens the raw rows in a side sheet. Same role scoping as Cadence — the
// API reuses monitoring-config's — so a space admin sees only their databases.
import { useCallback, useEffect, useState } from 'react'
import { Database, RefreshCw, Search } from 'lucide-react'
import RecordsSheet from './RecordsSheet'
import { memberCountLabel } from './MemberPicker'

const timeAgo = (iso) => {
  if (!iso) return 'never'
  const ms = Date.now() - new Date(iso).getTime()
  if (!Number.isFinite(ms)) return 'never'
  const m = Math.round(ms / 60000)
  if (m < 2) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h} h ago`
  return `${Math.round(h / 24)} d ago`
}
const CADENCE = { '1min': 'every minute', '2min': 'every 2 min', '5min': 'every 5 min', '10min': 'every 10 min', '15min': 'every 15 min', '30min': 'every 30 min',
  '1h': 'hourly', '3h': 'every 3 h', '6h': 'every 6 h', '12h': 'every 12 h', daily: 'daily', weekly: 'weekly' }
const STATE = {
  fresh: { dot: 'bg-ok', text: 'text-fg', label: 'Collecting' },
  stale: { dot: 'bg-warn', text: 'text-warn-text', label: 'Stale' },
  none: { dot: 'bg-fg-muted', text: 'text-fg-3', label: 'No data yet' },
  paused: { dot: 'bg-fg-muted', text: 'text-fg-3', label: 'Paused' },
  // The store could not be read: not "no data" (which reads as the collector never ran).
  unknown: { dot: 'bg-fg-muted', text: 'text-fg-3', label: 'Unknown' },
}
const HEALTH_BADGE = {
  ok: 'bg-ok-bg text-ok-text border-ok-border',
  warning: 'bg-warn-bg text-warn-text border-warn-border',
  critical: 'bg-err-bg text-err-text border-err-border',
  unknown: 'bg-surface-2 text-fg-3 border-line',
}
const attention = (d) => ['stale', 'none'].includes(d.tier1_state) || ['stale', 'none'].includes(d.tier2_state)
const paused = (d) => d.tier1_state === 'paused' || d.tier2_state === 'paused'
const healthy = (d) => !attention(d) && !paused(d)

function TierCell({ state, lastAt, cadence, volume, health }) {
  const s = STATE[state] || STATE.none
  return (
    <div className="min-w-[150px]">
      <div className={`flex items-center gap-1.5 text-xs ${s.text}`}>
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${s.dot}`} aria-hidden />
        <span className="font-medium">{state === 'paused' ? 'Paused' : state === 'none' ? 'No data yet' : state === 'unknown' ? 'Unknown' : timeAgo(lastAt)}</span>
        {health && state !== 'none' && state !== 'unknown' && (
          <span className={`inline-flex px-1.5 py-px rounded-full text-[10px] font-medium border capitalize ${HEALTH_BADGE[health] || HEALTH_BADGE.unknown}`}>{health}</span>
        )}
      </div>
      <div className="text-[10px] text-fg-muted mt-0.5 pl-3">
        {CADENCE[cadence] || cadence || '—'}{volume ? ` · ${volume}` : ''}{state === 'stale' ? ' · behind schedule' : ''}
      </div>
    </div>
  )
}

// Problems the Tier 1 collector reported on the record itself: the store refused its
// rows (an older store schema), or the cluster member sync has failed run after run.
function CollectionNotes({ d }) {
  const notes = []
  if (d.tier1_store_error?.message) {
    notes.push({ key: 'store', text: d.tier1_store_error.message, at: d.tier1_store_error.at })
  }
  if (d.member_sync_failing_since) {
    notes.push({
      key: 'sync',
      text: `Member sync failing since ${new Date(d.member_sync_failing_since).toLocaleString()}: failover detection and member roles are not current.`,
    })
  }
  if (!notes.length) return null
  return (
    <ul className="mt-1 pl-3 space-y-0.5 max-w-[320px]">
      {notes.map(n => (
        <li key={n.key} role="status" className="text-[10px] leading-snug text-err-text">
          {n.text}{n.at ? <span className="text-fg-muted"> · {timeAgo(n.at)}</span> : null}
        </li>
      ))}
    </ul>
  )
}

export default function MonitoringCollection({ api }) {
  const [dbs, setDbs] = useState(null)
  const [scope, setScope] = useState('account')
  const [storeConfigured, setStoreConfigured] = useState(true)
  const [storeError, setStoreError] = useState('')
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [search, setSearch] = useState('')
  const [spaceFilter, setSpaceFilter] = useState('')
  const [chip, setChip] = useState('all')
  const [sheet, setSheet] = useState(null) // { db, tier }

  const load = useCallback(async () => {
    setRefreshing(true)
    try {
      const res = await api.get('/api/admin/monitoring-status')
      setDbs(res.databases || [])
      setScope(res.scope || 'account')
      setStoreConfigured(res.store_configured !== false)
      setStoreError(res.store_error || '')
      setError('')
    } catch (e) {
      setError(e.detail || e.message)
      if (dbs === null) setDbs([])
    }
    setRefreshing(false)
  }, [api]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load() }, [load])

  const spaceOptions = (() => {
    const m = new Map()
    ;(dbs || []).forEach(d => (d.spaces || []).forEach(s => m.set(s.id, s.name)))
    return [...m.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name))
  })()
  const counts = { all: (dbs || []).length, attention: (dbs || []).filter(attention).length, paused: (dbs || []).filter(paused).length, healthy: (dbs || []).filter(healthy).length }
  const q = search.trim().toLowerCase()
  const filtered = (dbs || []).filter(d => {
    if (spaceFilter && !(d.member_space_ids || []).includes(spaceFilter)) return false
    if (chip === 'attention' && !attention(d)) return false
    if (chip === 'paused' && !paused(d)) return false
    if (chip === 'healthy' && !healthy(d)) return false
    if (!q) return true
    const hay = [d.display_name, d.instance_id, d.account_id, ...(d.spaces || []).map(s => s.name), ...(d.spaces || []).map(s => s.id)]
      .filter(Boolean).join(' ').toLowerCase()
    return hay.includes(q)
  })

  return (
    <div>
      <p className="text-sm text-fg-3 -mt-3 mb-5">
        {scope === 'space-admin'
          ? 'Whether metrics and deep scans are actually being collected for the databases in your spaces, judged against each database\'s configured cadence.'
          : 'Whether metrics and deep scans are actually being collected, judged against each database\'s configured cadence. Open the records to see every row the collectors wrote.'}
      </p>

      {error && (
        <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg"><p className="text-xs text-err">{error}</p></div>
      )}
      {!storeConfigured && dbs && dbs.length > 0 && (
        <div className="mb-4 px-3 py-2 bg-warn-bg border border-warn-border rounded-lg">
          <p className="text-xs text-warn-text">The metrics store is not configured for this deployment, so collection status and records are unavailable. Cadence is still shown.</p>
        </div>
      )}
      {storeConfigured && storeError && (
        <div className="mb-4 px-3 py-2 bg-warn-bg border border-warn-border rounded-lg">
          <p className="text-xs text-warn-text">Collection status could not be read just now ({storeError}), so it is shown as unknown rather than "no data". Cadence is accurate; retry shortly.</p>
        </div>
      )}

      {dbs === null ? (
        <p className="text-sm text-fg-muted">Loading…</p>
      ) : dbs.length === 0 ? (
        <p className="text-sm text-fg-muted">No configured databases yet.</p>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap gap-2 items-center">
            <div className="relative">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-muted" aria-hidden />
              <input type="text" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search database, account or space…"
                className="text-sm bg-surface border border-line-strong rounded pl-7 pr-2.5 py-1.5 w-72" />
            </div>
            <select value={spaceFilter} onChange={e => setSpaceFilter(e.target.value)} className="text-sm bg-surface border border-line-strong rounded px-2 py-1.5" aria-label="Space">
              <option value="">All spaces</option>
              {spaceOptions.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <div className="flex items-center gap-1" role="group" aria-label="Filter by collection status">
              {[['all', 'All'], ['attention', 'Needs attention'], ['paused', 'Paused'], ['healthy', 'Collecting']]
                .filter(([k]) => k === 'all' || counts[k] > 0)
                .map(([k, label]) => (
                  <button key={k} onClick={() => setChip(k)} aria-pressed={chip === k}
                    className={`px-2.5 py-1 text-[11px] rounded-full border transition-colors ${
                      chip === k ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
                    {label} <span className="opacity-70">{counts[k]}</span>
                  </button>
                ))}
            </div>
            {(search || spaceFilter || chip !== 'all') && (
              <button onClick={() => { setSearch(''); setSpaceFilter(''); setChip('all') }} className="text-xs text-fg-3 hover:text-fg-2 underline">Clear</button>
            )}
            <span className="text-[11px] text-fg-muted ml-auto">{filtered.length} of {dbs.length}</span>
            <button onClick={load} title="Refresh" className="p-1.5 rounded-md text-fg-3 hover:text-fg hover:bg-surface-3 transition-colors">
              <RefreshCw size={14} strokeWidth={1.75} className={refreshing ? 'animate-spin' : ''} />
            </button>
          </div>

          <div className="bg-surface rounded-lg border border-line p-4 overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-fg-3 border-b border-line">
                  <th className="py-2 pr-3 font-semibold">Database</th>
                  <th className="py-2 pr-3 font-semibold">Space(s)</th>
                  <th className="py-2 pr-3 font-semibold">Metrics (Tier 1)</th>
                  <th className="py-2 pr-3 font-semibold">Deep scan (Tier 2)</th>
                  <th className="py-2 pr-3 font-semibold" title="Tier 1 rows older than this are pruned">Retention</th>
                  <th className="py-2 pr-1 text-right font-semibold">Records</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <tr><td colSpan={6} className="py-4 text-sm text-fg-muted text-center">No databases match the filter.</td></tr>
                ) : filtered.map(d => (
                  <tr key={d.instance_id} className="border-b border-line-2">
                    <td className="py-2.5 pr-3">
                      <div className="text-sm font-medium text-fg">{d.display_name}</div>
                      {memberCountLabel(d.cluster_member_roles) && (
                        <div className="text-[10px] text-fg-muted">{memberCountLabel(d.cluster_member_roles)}</div>
                      )}
                      <div className="text-[10px] text-fg-muted font-mono">{d.instance_id}</div>
                    </td>
                    <td className="py-2.5 pr-3 text-[11px] text-fg-2">
                      {d.spaces && d.spaces.length
                        ? <span title={d.spaces.map(s => s.id).join(', ')}>{d.spaces.map(s => s.name).join(', ')}</span>
                        : (d.space_id || '—')}
                    </td>
                    <td className="py-2.5 pr-3">
                      <TierCell state={d.tier1_state} lastAt={d.tier1_last_at} cadence={d.tier1_frequency || '5min'}
                        volume={d.tier1_count_24h ? `${d.tier1_count_24h.toLocaleString()} rows / 24 h` : ''} />
                      <CollectionNotes d={d} />
                    </td>
                    <td className="py-2.5 pr-3">
                      <TierCell state={d.tier2_state} lastAt={d.tier2_last_at} cadence={d.tier2_frequency || 'daily'}
                        volume={d.tier2_count_30d ? `${d.tier2_count_30d} scan${d.tier2_count_30d === 1 ? '' : 's'} / 30 d` : ''}
                        health={d.tier2_last_health} />
                    </td>
                    <td className="py-2.5 pr-3 text-xs text-fg-2 tabular-nums">{d.retention_days ?? 30} d</td>
                    <td className="py-2.5 pr-1 text-right">
                      <div className="inline-flex items-center gap-1">
                        <button onClick={() => setSheet({ db: d, tier: 1 })} disabled={!storeConfigured}
                          className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle disabled:opacity-40 transition-colors">
                          <Database size={12} strokeWidth={1.75} aria-hidden /> Metrics
                        </button>
                        <button onClick={() => setSheet({ db: d, tier: 2 })} disabled={!storeConfigured}
                          className="px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle disabled:opacity-40 transition-colors">
                          Scans
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {sheet && <RecordsSheet api={api} db={sheet.db} initialTier={sheet.tier} onClose={() => setSheet(null)} />}
    </div>
  )
}
