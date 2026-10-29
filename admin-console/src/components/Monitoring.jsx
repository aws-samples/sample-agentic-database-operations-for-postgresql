// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import Toggle from './Toggle'
import MonitoringCollection from './MonitoringCollection'

// Monitoring has two sections, listed by the console's navigation rail (hideTabs) or,
// rendered standalone, as its own top tabs:
//   • Cadence — how often each database is collected (this file).
//   • Collection — whether collection is actually happening (MonitoringCollection).
export const MONITORING_SECTIONS = [
  { id: 'cadence', label: 'Cadence' },
  { id: 'collection', label: 'Collection' },
]

// Collection cadence is DB-level (one schedule per physical instance); spaces are an
// access boundary, not a collection boundary. This super-admin view owns the cadence
// for every configured DB across all spaces (spec: platform/monitoring-config).
// 1 min and 2 min are not offered: a cluster's Tier 1 run can take up to 3 minutes, so a
// shorter cadence overlaps runs. The server resolves any legacy stored value to 5 min.
const TIER1_OPTIONS = [
  { value: '5min', label: '5 min' }, { value: '10min', label: '10 min' },
  { value: '15min', label: '15 min' }, { value: '30min', label: '30 min' },
]
const TIER2_OPTIONS = [
  { value: '1h', label: 'Hourly' }, { value: '3h', label: 'Every 3h' },
  { value: '6h', label: 'Every 6h' }, { value: '12h', label: 'Every 12h' },
  { value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' },
]
// Tier 1 retention (days); rows older than this are pruned by the collector.
const RETENTION_OPTIONS = [7, 14, 30, 60, 90, 180, 365]

// Pausing Tier 1 on a database that depends on it (a cluster: failover detection; a
// cross-Region host-route path: route checks) is allowed, after this confirmation. The
// text is the server's (tier1_pause_warning), the same the API returns as `warning`.
function PauseTier1Dialog({ name, warning, onConfirm, onCancel }) {
  const cancelRef = useRef(null)
  useEffect(() => {
    cancelRef.current?.focus()
    const onKey = (e) => { if (e.key === 'Escape') onCancel() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onCancel])
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center side-sheet-backdrop p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel() }}>
      <div role="alertdialog" aria-modal="true" aria-labelledby="pause-t1-title" aria-describedby="pause-t1-desc"
        className="w-full max-w-md bg-surface border border-line rounded-xl shadow-2xl p-5">
        <h2 id="pause-t1-title" className="text-base font-semibold text-fg">Pause Tier 1 for {name}?</h2>
        <p id="pause-t1-desc" className="text-xs text-fg-3 mt-2 leading-relaxed">{warning}</p>
        <div className="flex justify-end gap-2 mt-4">
          <button ref={cancelRef} type="button" onClick={onCancel}
            className="px-3 py-1.5 text-fg-3 text-xs rounded-lg hover:bg-surface-3 transition-colors">Keep collecting</button>
          <button type="button" onClick={onConfirm}
            className="px-3 py-1.5 bg-warn text-warn-inverse text-xs font-medium rounded-lg transition-colors">Pause Tier 1</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function Row({ db, onSaved, api }) {
  const readOnly = db.editable === false
  const [t1Freq, setT1Freq] = useState(db.tier1_frequency || '5min')
  const [t2Freq, setT2Freq] = useState(db.tier2_frequency || 'daily')
  const [t1On, setT1On] = useState(db.tier1_enabled !== false)
  const [t2On, setT2On] = useState(db.tier2_enabled !== false)
  const [retention, setRetention] = useState(Number(db.retention_days) || 30)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const dirty = t1Freq !== (db.tier1_frequency || '5min') || t2Freq !== (db.tier2_frequency || 'daily') ||
    t1On !== (db.tier1_enabled !== false) || t2On !== (db.tier2_enabled !== false) ||
    retention !== (Number(db.retention_days) || 30)

  const [confirmPause, setConfirmPause] = useState(false)
  const pausing = !t1On && db.tier1_enabled !== false

  // Reset a tier's controls back to the stored values (used when the server could not
  // apply that tier, so the row shows what is actually running, not the attempt).
  const resetTier1 = () => { setT1Freq(db.tier1_frequency || '5min'); setT1On(db.tier1_enabled !== false) }
  const resetTier2 = () => { setT2Freq(db.tier2_frequency || 'daily'); setT2On(db.tier2_enabled !== false) }

  const doSave = async () => {
    setConfirmPause(false)
    setSaving(true); setErr('')
    try {
      const patch = { tier1_frequency: t1Freq, tier1_enabled: t1On, tier2_frequency: t2Freq, tier2_enabled: t2On, retention_days: retention }
      const res = await api.put(`/api/admin/monitoring-config/${db.instance_id}`, patch)
      // The server returns the state that actually took effect. On a full success this is
      // the whole patch; apply exactly what it reports so a tier is never shown as changed
      // when its schedule did not change.
      onSaved(db.instance_id, res.applied || patch)
    } catch (e) {
      // A scheduler failure. The body names which tier could not be applied and carries
      // the state that DID take effect; keep the old values for the failed tier.
      const body = e.body || {}
      if (body.applied) onSaved(db.instance_id, body.applied)
      if (body.failed && body.failed.tier1) resetTier1()
      if (body.failed && body.failed.tier2) resetTier2()
      setErr(e.detail || e.message)
    }
    setSaving(false)
  }
  const save = () => (pausing && db.tier1_pause_warning ? setConfirmPause(true) : doSave())
  const cancelPause = useCallback(() => setConfirmPause(false), [])

  const selCls = 'text-xs bg-surface border border-line-strong rounded px-1.5 py-1 disabled:opacity-40'

  return (
    <tr className="border-b border-line-2">
      <td className="py-2 pr-3">
        <div className="text-sm font-medium text-fg">{db.display_name}</div>
        <div className="text-[10px] text-fg-muted font-mono">{db.instance_id}</div>
      </td>
      <td className="py-2 pr-3 text-[11px] text-fg-3 font-mono">{db.account_id || '—'}</td>
      <td className="py-2 pr-3 text-[11px] text-fg-2">
        {db.spaces && db.spaces.length
          ? <span title={db.spaces.map(s => s.id).join(', ')}>{db.spaces.map(s => s.name).join(', ')}</span>
          : (db.space_id || '—')}
      </td>
      <td className="py-2 pr-3">
        <div className="flex items-center gap-2">
          <Toggle size="sm" checked={t1On} disabled={readOnly} onChange={setT1On} title={t1On ? 'Disable metrics collection' : 'Enable metrics collection'} />
          <select value={t1Freq} onChange={e => setT1Freq(e.target.value)} disabled={readOnly || !t1On} className={selCls} aria-label="Tier 1 frequency">
            {TIER1_OPTIONS.map(o => {
              // A cluster's run collects every member, so a cadence shorter than one run is refused.
              const off = Array.isArray(db.tier1_allowed) && !db.tier1_allowed.includes(o.value)
              return <option key={o.value} value={o.value} disabled={off}>{o.label}{off ? ' (too short for a cluster)' : ''}</option>
            })}
          </select>
        </div>
        {db.tier1_frequency_note && (
          <div className="mt-1 text-[10px] text-warn-text" title={db.tier1_frequency_note}>
            {db.tier1_frequency_note}
          </div>
        )}
      </td>
      <td className="py-2 pr-3">
        <div className="flex items-center gap-2">
          <Toggle size="sm" checked={t2On} disabled={readOnly} onChange={setT2On} title={t2On ? 'Disable deep scan' : 'Enable deep scan'} />
          <select value={t2Freq} onChange={e => setT2Freq(e.target.value)} disabled={readOnly || !t2On} className={selCls} aria-label="Tier 2 frequency">
            {TIER2_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      </td>
      <td className="py-2 pr-3">
        <select value={RETENTION_OPTIONS.includes(retention) ? retention : 30} onChange={e => setRetention(Number(e.target.value))} disabled={readOnly} className={selCls} aria-label="Tier 1 retention">
          {RETENTION_OPTIONS.map(d => <option key={d} value={d}>{d} days</option>)}
        </select>
      </td>
      <td className="py-2 pr-1 text-right">
        {readOnly ? (
          <span className="text-[10px] text-fg-muted italic"
            title={db.edit_block || 'Shared, or in a space you do not own'}>
            Read-only — shared, or in a space you do not own; contact a super admin to change.
          </span>
        ) : (
          <>
            {err && <span className="text-[10px] text-err mr-2">{err}</span>}
            <button onClick={save} disabled={!dirty || saving}
              className="text-xs px-2.5 py-1 rounded bg-primary text-primary-fg disabled:opacity-30 disabled:cursor-not-allowed hover:bg-primary-hover">
              {saving ? '…' : 'Save'}
            </button>
            {confirmPause && (
              <PauseTier1Dialog name={db.display_name} warning={db.tier1_pause_warning}
                onConfirm={doSave} onCancel={cancelPause} />
            )}
          </>
        )}
      </td>
    </tr>
  )
}

function Cadence({ api }) {
  const [dbs, setDbs] = useState(null)
  const [scope, setScope] = useState('account')
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [spaceFilter, setSpaceFilter] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await api.get('/api/admin/monitoring-config')
      setDbs(res.databases || [])
      setScope(res.scope || 'account')
    } catch (e) {
      setError(e.detail || e.message)
    }
  }, [api])

  useEffect(() => { load() }, [load])

  const onSaved = (id, patch) => {
    setDbs(prev => prev.map(d => d.instance_id === id ? { ...d, ...patch } : d))
  }

  // Space dropdown options: every space that appears across the loaded databases.
  const spaceOptions = (() => {
    const m = new Map()
    ;(dbs || []).forEach(d => (d.spaces || []).forEach(s => m.set(s.id, s.name)))
    return [...m.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name))
  })()

  // Client-side filter over the loaded list: free-text (database, account, space name/id)
  // plus an optional space membership filter.
  const q = search.trim().toLowerCase()
  const filtered = (dbs || []).filter(d => {
    if (spaceFilter && !(d.member_space_ids || []).includes(spaceFilter)) return false
    if (!q) return true
    const hay = [d.display_name, d.instance_id, d.account_id,
      ...(d.spaces || []).map(s => s.name), ...(d.spaces || []).map(s => s.id)]
      .filter(Boolean).join(' ').toLowerCase()
    return hay.includes(q)
  })

  return (
    <div>
      <p className="text-sm text-fg-3 -mt-3 mb-5">
        {scope === 'space-admin'
          ? 'Tier 1 (real-time metrics) and Tier 2 (deep scan) collection cadence for the configured databases in your spaces. A database is editable when you are its only space admin; shared ones are read-only (a super admin arbitrates). Higher cadence increases cost.'
          : 'Tier 1 (real-time metrics) and Tier 2 (deep scan) collection cadence for every configured database. Collection runs once per physical database regardless of how many spaces use it; spaces see the effective cadence read-only. Higher cadence increases cost.'}
      </p>

      {error && (
        <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg">
          <p className="text-xs text-err">{error}</p>
        </div>
      )}

      {dbs === null ? (
        <p className="text-sm text-fg-muted">Loading…</p>
      ) : dbs.length === 0 ? (
        <p className="text-sm text-fg-muted">No configured databases yet.</p>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap gap-2 items-center">
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search database, account or space…"
              className="text-sm bg-surface border border-line-strong rounded px-2.5 py-1.5 w-72"
            />
            <select
              value={spaceFilter}
              onChange={e => setSpaceFilter(e.target.value)}
              className="text-sm bg-surface border border-line-strong rounded px-2 py-1.5">
              <option value="">All spaces</option>
              {spaceOptions.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            {(search || spaceFilter) && (
              <button onClick={() => { setSearch(''); setSpaceFilter('') }}
                className="text-xs text-fg-3 hover:text-fg-2 underline">
                Clear
              </button>
            )}
            <span className="text-[11px] text-fg-muted ml-auto">{filtered.length} of {dbs.length}</span>
          </div>
          <div className="bg-surface rounded-lg border border-line p-4 overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-fg-3 border-b border-line">
                  <th className="py-2 pr-3 font-semibold">Database</th>
                  <th className="py-2 pr-3 font-semibold">Account</th>
                  <th className="py-2 pr-3 font-semibold">Space(s)</th>
                  <th className="py-2 pr-3 font-semibold">Tier 1 (metrics)</th>
                  <th className="py-2 pr-3 font-semibold">Tier 2 (scan)</th>
                  <th className="py-2 pr-3 font-semibold" title="Tier 1 rows older than this are pruned">Retention</th>
                  <th className="py-2 pr-1"></th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <tr><td colSpan={7} className="py-4 text-sm text-fg-muted text-center">No databases match the filter.</td></tr>
                ) : (
                  filtered.map(db => <Row key={db.instance_id} db={db} api={api} onSaved={onSaved} />)
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

export default function Monitoring({ api, section: controlled, onSectionChange, hideTabs = false }) {
  const [internal, setInternal] = useState('cadence')
  const section = controlled ?? internal
  const setSection = (id) => { onSectionChange ? onSectionChange(id) : setInternal(id) }
  const current = MONITORING_SECTIONS.find(s => s.id === section) || MONITORING_SECTIONS[0]

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-fg">{hideTabs ? current.label : 'Monitoring'}</h1>
      {!hideTabs && <div className="border-b border-line">
        <div className="flex gap-0">
          {MONITORING_SECTIONS.map(s => (
            <button key={s.id} onClick={() => setSection(s.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                section === s.id ? 'border-primary text-primary' : 'border-transparent text-fg-3 hover:text-fg-2'}`}>{s.label}</button>
          ))}
        </div>
      </div>}
      {current.id === 'cadence' ? <Cadence api={api} /> : <MonitoringCollection api={api} />}
    </div>
  )
}
