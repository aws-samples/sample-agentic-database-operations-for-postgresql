// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import DbInstanceCard from './DbInstanceCard'
import DatabaseDrawer from './DatabaseDrawer'
import { memberCountLabel } from './MemberPicker'
import { useNotifications } from './Notifications'
// Engine display names come from the shared list-controls module so the operator app and
// the admin console label engines identically (one vocabulary, one place to extend it).
import { canonicalEngine, engineLabel } from './DbListControls'

// Tier 1 liveness for the fleet table: "stale" once the newest row is older than
// two collection intervals (plus a minute of scheduler jitter) — the same rule the
// admin console's Collection page applies server-side.
const TIER1_MINUTES = { '1min': 1, '2min': 2, '5min': 5, '10min': 10, '15min': 15, '30min': 30 }
const tier1State = (inst, latest) => {
  if (inst.tier1_enabled === false) return 'paused'
  if (!latest?.scan_time) return 'none'
  const age = (Date.now() - new Date(latest.scan_time).getTime()) / 1000
  const minutes = TIER1_MINUTES[inst.tier1_frequency] || 5
  return age > minutes * 60 * 2 + 60 ? 'stale' : 'fresh'
}
const timeAgo = (iso) => {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 2) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} d ago`
}
const TIER1_DOT = { fresh: 'bg-ok', stale: 'bg-warn', none: 'bg-fg-muted', paused: 'bg-fg-muted' }

const PAGE_SIZES = [20, 50, 100]

function StatusBadge({ score }) {
  const colors = {
    critical: 'bg-err-bg text-err-text border-err-border',
    warning: 'bg-warn-bg text-warn-text border-warn-border',
    ok: 'bg-ok-bg text-ok-text border-ok-border',
  }
  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${colors[score] || 'bg-surface-2 text-fg-3 border-line'}`}>
      {(score || 'unknown').toUpperCase()}
    </span>
  )
}

function FilterPill({ label, onRemove }) {
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-primary-subtle text-primary border border-primary-line">
      {label}
      <button onClick={onRemove} className="hover:text-err transition-colors">×</button>
    </span>
  )
}

export default function Dashboard({ api, onViewReport, onCompareScans, onSelectDatabase, spaceId }) {
  const [instances, setInstances] = useState([])
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState({}) // { instanceId: true }
  const [scanSubmitted, setScanSubmitted] = useState({}) // { instanceId: timestamp }
  const [activeScans, setActiveScans] = useState({}) // { instanceId: true } — tracks running jobs
  const [error, setError] = useState(null)
  // The drawer under the table: { instance, tab } — one database, one of the
  // DatabaseDrawer tabs (members for a cluster | metrics | scans | trends).
  const [panel, setPanel] = useState(null)
  const openTab = (inst, tab) => setPanel(p => (p?.instance?.instance_id === inst.instance_id && p.tab === tab) ? null : { instance: inst, tab })
  // Newest Tier 1 row per database, for the fleet table's Metrics column. Fail-soft:
  // no store, no column data.
  const [tier1Latest, setTier1Latest] = useState({})
  const { addNotification } = useNotifications()

  // View & filtering state
  const [viewMode, setViewMode] = useState('list') // 'list' | 'grid'
  const [search, setSearch] = useState('')
  const [engineFilter, setEngineFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [regionFilter, setRegionFilter] = useState('')
  const [accountFilter, setAccountFilter] = useState('')
  const [sortCol, setSortCol] = useState('display_name')
  const [sortDir, setSortDir] = useState('asc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selectedRows, setSelectedRows] = useState(new Set()) // instance_ids

  const fetchInstances = async () => {
    try {
      setLoading(true)
      // The operator app's listing: membership-gated, same shape for every operator.
      const data = spaceId ? await api.get(`/api/my-spaces/${encodeURIComponent(spaceId)}/databases`) : []
      setInstances(data)
      setError(null)
    } catch { setError('Failed to load databases') }
    finally { setLoading(false) }
  }

  useEffect(() => { fetchInstances() }, [])

  useEffect(() => {
    if (!spaceId) return
    api.get(`/api/scan-metrics/fleet?space_id=${encodeURIComponent(spaceId)}&hours=168`)
      .then(d => setTier1Latest(Object.fromEntries((d.databases || []).map(m => [m.instance_id, m]))))
      .catch(() => setTier1Latest({}))
  }, [spaceId, instances.length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Active-scan tracking. ONE shared poll for the whole space (not one probe per
  // database on open), and every timer is cleared on unmount so no interval survives a
  // closed panel. `watchingRef` holds the instance ids with a scan in flight.
  const pollRef = useRef(null)
  const watchingRef = useRef(new Set())
  // instance id -> the job id this UI just submitted, so the outcome is read from THAT
  // job and not from any older SUCCEEDED job still in the recent list.
  const submittedJobRef = useRef(new Map())
  const instancesRef = useRef(instances)
  instancesRef.current = instances

  // When a scan leaves the active set, read that one database's jobs once to tell a
  // success from a failure (the space-wide active list carries no per-job status).
  const resolveOutcome = useCallback(async (iid) => {
    try {
      const d = await api.get(`/api/scan/${iid}/jobs?limit=3`)
      const jobs = d.jobs || []
      const name = (instancesRef.current.find(i => i.instance_id === iid) || {}).display_name || iid
      // Report the outcome of the job THIS session submitted, not "any SUCCEEDED in the
      // last 3": a database that scanned fine yesterday but whose scan just FAILED would
      // otherwise show "Scan completed" off the stale success. Fall back to the newest
      // job (jobs are newest-first) when we did not capture the submitted id.
      const jobId = submittedJobRef.current.get(iid)
      submittedJobRef.current.delete(iid)
      const job = (jobId && jobs.find(j => j.job_id === jobId)) || jobs[0]
      if (!job) return
      if (job.status === 'SUCCEEDED') {
        addNotification({ type: 'success', title: 'Scan completed', message: 'Report is ready.', database: name })
      } else if (job.status === 'FAILED') {
        addNotification({ type: 'error', title: 'Scan failed', message: job.status_reason || 'Check Scans for details.', database: name })
      }
    } catch { /* best-effort notification */ }
  }, [api, addNotification])

  const stopPoll = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }, [])

  const tick = useCallback(async () => {
    if (!spaceId) return
    let active
    try {
      const d = await api.get(`/api/spaces/${encodeURIComponent(spaceId)}/active-scans`)
      active = new Set(d.active || [])
    } catch { return }   // transient; try again on the next tick
    // Databases we were watching that are no longer active have finished.
    const done = [...watchingRef.current].filter(id => !active.has(id))
    watchingRef.current = new Set([...watchingRef.current, ...active].filter(id => active.has(id)))
    if (done.length) {
      setActiveScans(prev => { const n = { ...prev }; done.forEach(id => { n[id] = false }); return n })
      await fetchInstances()
      done.forEach(resolveOutcome)
    }
    if (active.size) setActiveScans(prev => { const n = { ...prev }; active.forEach(id => { n[id] = true }); return n })
    if (watchingRef.current.size === 0) stopPoll()
  }, [api, spaceId, resolveOutcome, stopPoll]) // eslint-disable-line react-hooks/exhaustive-deps

  const ensurePoll = useCallback(() => {
    if (!pollRef.current) pollRef.current = setInterval(tick, 5000)
  }, [tick])

  // On open: ONE request for every running scan in the space, then watch only those.
  useEffect(() => {
    if (!spaceId) return
    let cancelled = false
    api.get(`/api/spaces/${encodeURIComponent(spaceId)}/active-scans`).then(d => {
      if (cancelled) return
      const active = d.active || []
      if (!active.length) return
      active.forEach(id => watchingRef.current.add(id))
      setActiveScans(prev => { const n = { ...prev }; active.forEach(id => { n[id] = true }); return n })
      ensurePoll()
    }).catch(() => {})
    return () => { cancelled = true; stopPoll() }
  }, [spaceId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Clear the shared poll if the component unmounts mid-flight.
  useEffect(() => stopPoll, [stopPoll])

  const handleScan = async (instanceId) => {
    setScanning(prev => ({ ...prev, [instanceId]: true }))
    setActiveScans(prev => ({ ...prev, [instanceId]: true }))
    setError(null)
    try {
      const result = await api.post(`/api/scan/${instanceId}`)
      if (result?.job_id) submittedJobRef.current.set(instanceId, result.job_id)
      setScanSubmitted(prev => ({ ...prev, [instanceId]: Date.now() }))
      const inst = instances.find(i => i.instance_id === instanceId)
      const dbName = inst?.display_name || instanceId
      addNotification({ type: 'success', title: 'Scan submitted', message: 'Job is queued.', database: dbName })
      await fetchInstances()
      // Hand this scan to the shared poll (one interval for the whole space); it clears
      // the per-database state and notifies when the job ends. No per-scan timer.
      watchingRef.current.add(instanceId)
      ensurePoll()
    } catch (err) {
      addNotification({ type: 'error', title: 'Scan failed', message: err?.detail || err?.message || 'Unknown error', database: instances.find(i => i.instance_id === instanceId)?.display_name || instanceId })
      setActiveScans(prev => ({ ...prev, [instanceId]: false }))
    } finally { setScanning(prev => ({ ...prev, [instanceId]: false })) }
  }

  const handleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortCol(col); setSortDir('asc') }
    setPage(1)
  }

  const handleBulkScan = async () => {
    const ids = [...selectedRows]
    // Submit all scans in parallel
    await Promise.all(ids.map(id => handleScan(id)))
    setSelectedRows(new Set())
  }

  const handleBulkScope = () => {
    const toAdd = configured.filter(i => selectedRows.has(i.instance_id))
    // Hand the whole selection over at once. Calling onSelectDatabase once per row
    // would replace the scope each time, leaving only the last database.
    if (onSelectDatabase && toAdd.length) onSelectDatabase(toAdd)
    setSelectedRows(new Set())
  }

  const toggleRow = (id) => {
    setSelectedRows(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAllVisible = () => {
    const visibleIds = paginated.map(i => i.instance_id)
    const allSelected = visibleIds.every(id => selectedRows.has(id))
    if (allSelected) {
      setSelectedRows(prev => {
        const next = new Set(prev)
        visibleIds.forEach(id => next.delete(id))
        return next
      })
    } else {
      setSelectedRows(prev => {
        const next = new Set(prev)
        visibleIds.forEach(id => next.add(id))
        return next
      })
    }
  }

  // Derived data
  const configured = instances.filter(i => i.status === 'configured')

  const engines = useMemo(() => [...new Set(configured.map(i => canonicalEngine(i.engine)).filter(Boolean))].sort(), [configured])
  const regions = useMemo(() => [...new Set(configured.map(i => i.region))].sort(), [configured])
  const accounts = useMemo(() => [...new Set(configured.map(i => i.account_id).filter(Boolean))].sort(), [configured])

  const filtered = useMemo(() => {
    let list = configured
    if (search) {
      const q = search.toLowerCase()
      list = list.filter(i => {
        const status = (i.last_health_score || 'unknown').toLowerCase()
        return i.display_name?.toLowerCase().includes(q) ||
          i.endpoint?.toLowerCase().includes(q) ||
          i.engine?.toLowerCase().includes(q) ||
          engineLabel(i.engine).toLowerCase().includes(q) ||
          i.region?.toLowerCase().includes(q) ||
          i.database_name?.toLowerCase().includes(q) ||
          i.tags?.engine_version?.toLowerCase().includes(q) ||
          status.includes(q)
      })
    }
    if (engineFilter) list = list.filter(i => canonicalEngine(i.engine) === engineFilter)
    if (statusFilter) list = list.filter(i => (i.last_health_score || 'unknown') === statusFilter)
    if (regionFilter) list = list.filter(i => i.region === regionFilter)
    if (accountFilter) list = list.filter(i => i.account_id === accountFilter)
    return list
  }, [configured, search, engineFilter, statusFilter, regionFilter, accountFilter])

  const sorted = useMemo(() => {
    const arr = [...filtered]
    const scoreOrder = { critical: 0, warning: 1, ok: 2, unknown: 3 }
    arr.sort((a, b) => {
      let va, vb
      if (sortCol === 'last_health_score') {
        va = scoreOrder[a.last_health_score || 'unknown'] ?? 3
        vb = scoreOrder[b.last_health_score || 'unknown'] ?? 3
      } else {
        va = (a[sortCol] || '').toString().toLowerCase()
        vb = (b[sortCol] || '').toString().toLowerCase()
      }
      if (va < vb) return sortDir === 'asc' ? -1 : 1
      if (va > vb) return sortDir === 'asc' ? 1 : -1
      return 0
    })
    return arr
  }, [filtered, sortCol, sortDir])

  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize))
  const paginated = sorted.slice((page - 1) * pageSize, page * pageSize)

  const stats = {
    total: configured.length,
    critical: configured.filter(i => i.last_health_score === 'critical').length,
    warning: configured.filter(i => i.last_health_score === 'warning').length,
    ok: configured.filter(i => i.last_health_score === 'ok').length,
  }

  const activeFilters = [
    engineFilter && { label: `Engine: ${engineLabel(engineFilter)}`, clear: () => setEngineFilter('') },
    statusFilter && { label: `Status: ${statusFilter}`, clear: () => setStatusFilter('') },
    regionFilter && { label: `Region: ${regionFilter}`, clear: () => setRegionFilter('') },
    accountFilter && { label: `Account: ${accountFilter}`, clear: () => setAccountFilter('') },
  ].filter(Boolean)

  if (loading) return <div className="flex items-center justify-center py-20 text-fg-3">Loading databases...</div>

  const SortHeader = ({ col, label, className = '' }) => (
    <th onClick={() => handleSort(col)}
      className={`px-2 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider cursor-pointer hover:text-fg-2 select-none ${className}`}>
      <span className="inline-flex items-center gap-1">
        {label}
        {sortCol === col && <span className="text-primary">{sortDir === 'asc' ? '↑' : '↓'}</span>}
      </span>
    </th>
  )

  return (
    <div className="space-y-4">

      {/* Stats bar */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { n: stats.total, l: 'Configured', c: '', onClick: () => { setStatusFilter(''); setPage(1) } },
          { n: stats.critical, l: 'Critical', c: 'border-err-border', onClick: () => { setStatusFilter('critical'); setPage(1) } },
          { n: stats.warning, l: 'Warning', c: 'border-warn-border', onClick: () => { setStatusFilter('warning'); setPage(1) } },
          { n: stats.ok, l: 'Healthy', c: 'border-ok-border', onClick: () => { setStatusFilter('ok'); setPage(1) } },
        ].map(s => (
          <button key={s.l} onClick={s.onClick} disabled={!s.onClick}
            className={`bg-surface rounded-lg border border-line p-3 shadow-sm text-left transition-all ${s.onClick ? 'hover:shadow-md hover:border-primary-line cursor-pointer' : ''} ${
              statusFilter && s.l.toLowerCase() === statusFilter ? 'ring-2 ring-primary ring-offset-1' : ''
            } ${s.c}`}>
            <div className={`text-xl font-bold ${
              s.l === 'Critical' ? 'text-err' : s.l === 'Warning' ? 'text-warn-mid' : s.l === 'Healthy' ? 'text-ok' : 'text-fg'
            }`}>{s.n}</div>
            <div className="text-[11px] text-fg-3">{s.l}</div>
          </button>
        ))}
      </div>

      {error && <div className="bg-err-bg border border-err-border rounded-lg p-3 text-sm text-err-text">{error}</div>}

      {configured.length === 0 ? (
        <div className="text-center py-16 bg-surface rounded-lg border border-line">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-surface-3 flex items-center justify-center">
            <svg className="w-8 h-8 text-fg-muted" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 3C7.58 3 4 4.79 4 7v10c0 2.21 3.58 4 8 4s8-1.79 8-4V7c0-2.21-3.58-4-8-4z"/>
            </svg>
          </div>
          <h3 className="text-lg font-semibold text-fg mb-2">No databases configured</h3>
          <p className="text-fg-3">Add a database instance in Settings to get started</p>
        </div>
      ) : (
        <>
          {configured.length > 0 && (
            <div className="bg-surface rounded-lg border border-line shadow-sm">
              {/* Toolbar */}
              <div className="px-4 py-3 border-b border-line-2">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 flex-1">
                    <h2 className="text-sm font-semibold text-fg whitespace-nowrap">Configured Databases</h2>
                    <div className="relative flex-1 max-w-xs">
                      <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                      </svg>
                      <input type="text" placeholder="Search databases..." value={search}
                        onChange={(e) => { setSearch(e.target.value); setPage(1) }}
                        className="w-full pl-8 pr-3 py-1.5 text-xs border border-line rounded-lg focus:outline-none focus:ring-1 focus:ring-primary focus:border-primary" />
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {/* Filters */}
                    <select value={engineFilter} onChange={(e) => { setEngineFilter(e.target.value); setPage(1) }}
                      className="text-[11px] border border-line rounded-lg px-2 py-1.5 bg-surface text-fg-2 focus:outline-none focus:ring-1 focus:ring-primary">
                      <option value="">All Engines</option>
                      {engines.map(e => <option key={e} value={e}>{engineLabel(e)}</option>)}
                    </select>
                    {accounts.length > 1 && (
                      <select value={accountFilter} onChange={(e) => { setAccountFilter(e.target.value); setPage(1) }}
                        className="text-[11px] border border-line rounded-lg px-2 py-1.5 bg-surface text-fg-2 focus:outline-none focus:ring-1 focus:ring-primary">
                        <option value="">All Accounts</option>
                        {accounts.map(a => <option key={a} value={a}>{a}</option>)}
                      </select>
                    )}
                    <select value={regionFilter} onChange={(e) => { setRegionFilter(e.target.value); setPage(1) }}
                      className="text-[11px] border border-line rounded-lg px-2 py-1.5 bg-surface text-fg-2 focus:outline-none focus:ring-1 focus:ring-primary">
                      <option value="">All Regions</option>
                      {regions.map(r => <option key={r} value={r}>{r}</option>)}
                    </select>
                    <select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1) }}
                      className="text-[11px] border border-line rounded-lg px-2 py-1.5 bg-surface text-fg-2 focus:outline-none focus:ring-1 focus:ring-primary">
                      <option value="">All Status</option>
                      <option value="critical">Critical</option>
                      <option value="warning">Warning</option>
                      <option value="ok">Healthy</option>
                    </select>
                    {/* View toggle */}
                    <div className="flex border border-line rounded-lg overflow-hidden">
                      <button onClick={() => setViewMode('list')}
                        className={`p-1.5 ${viewMode === 'list' ? 'bg-primary text-primary-fg' : 'bg-surface text-fg-muted hover:text-fg-2'}`} title="List view">
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                        </svg>
                      </button>
                      <button onClick={() => setViewMode('grid')}
                        className={`p-1.5 ${viewMode === 'grid' ? 'bg-primary text-primary-fg' : 'bg-surface text-fg-muted hover:text-fg-2'}`} title="Grid view">
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
                        </svg>
                      </button>
                    </div>
                    <button onClick={fetchInstances} className="p-1.5 text-fg-muted hover:text-primary transition-colors rounded-lg hover:bg-surface-3" title="Refresh">
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                      </svg>
                    </button>
                  </div>
                </div>

                {/* Active filter pills */}
                {activeFilters.length > 0 && (
                  <div className="flex items-center gap-1.5 mt-2">
                    {activeFilters.map((f, i) => <FilterPill key={i} label={f.label} onRemove={f.clear} />)}
                    <button onClick={() => { setEngineFilter(''); setStatusFilter(''); setRegionFilter(''); setAccountFilter(''); setPage(1) }}
                      className="text-[10px] text-fg-muted hover:text-fg-2 ml-1">Clear all</button>
                  </div>
                )}

                {/* Result count + bulk actions */}
                <div className="mt-2 flex items-center justify-between">
                  <div className="text-[10px] text-fg-muted">
                    {filtered.length === configured.length
                      ? `${configured.length} database${configured.length !== 1 ? 's' : ''}`
                      : `${filtered.length} of ${configured.length} databases`}
                  </div>
                  {selectedRows.size > 0 && (
                    <div className="flex items-center gap-2 bg-primary-subtle border border-primary-line rounded-lg px-3 py-1">
                      <span className="text-[11px] font-medium text-primary">{selectedRows.size} selected</span>
                      <button onClick={handleBulkScan}
                        className="px-2 py-0.5 text-[10px] font-medium rounded bg-primary text-primary-fg hover:bg-primary-hover transition-colors">
                        ⚡ Scan All
                      </button>
                      <button onClick={handleBulkScope}
                        className="px-2 py-0.5 text-[10px] font-medium rounded bg-surface text-primary border border-primary-line hover:bg-primary-subtle transition-colors">
                        💬 Add to Scope
                      </button>
                      <button onClick={() => setSelectedRows(new Set())}
                        className="text-[10px] text-fg-muted hover:text-fg-2">✕</button>
                    </div>
                  )}
                </div>
              </div>

              {/* List view */}
              {viewMode === 'list' && (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead className="bg-surface-2 border-b border-line-2">
                      <tr>
                        <th className="px-3 py-2 w-8">
                          <input type="checkbox"
                            checked={paginated.length > 0 && paginated.every(i => selectedRows.has(i.instance_id))}
                            onChange={toggleAllVisible}
                            className="w-3 h-3 rounded border-line-strong text-primary" />
                        </th>
                        <SortHeader col="display_name" label="Name" />
                        <SortHeader col="engine" label="Engine" />
                        <th className="px-2 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Version</th>
                        <SortHeader col="account_id" label="Account" />
                        <SortHeader col="region" label="Region" />
                        <SortHeader col="last_health_score" label="Status" />
                        <th className="px-2 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider" title="Newest Tier 1 metrics collection">Metrics</th>
                        <SortHeader col="last_scan_at" label="Last Scan" />
                        <th className="px-2 py-2 text-right text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line-2">
                      {paginated.map(inst => {
                        const score = inst.last_health_score || 'unknown'
                        const scanAge = inst.last_scan_at
                          ? new Date(inst.last_scan_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + new Date(inst.last_scan_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                          : '—'
                        return (
                          <tr key={inst.instance_id} className="hover:bg-surface-3 transition-colors group">
                            <td className="px-2 py-2 w-8">
                              <input type="checkbox"
                                checked={selectedRows.has(inst.instance_id)}
                                onChange={() => toggleRow(inst.instance_id)}
                                className="w-3 h-3 rounded border-line-strong text-primary" />
                            </td>
                            <td className="px-2 py-2 max-w-[140px]">
                              <button onClick={() => onSelectDatabase?.(inst)}
                                className="text-[11px] font-medium text-fg hover:text-primary transition-colors text-left truncate block max-w-full"
                                title={inst.display_name}>
                                {inst.display_name}
                              </button>
                              {memberCountLabel(inst.cluster_member_roles) && (
                                <button onClick={() => openTab(inst, 'members')} title="Open the cluster's members"
                                  className="block text-[10px] text-fg-muted hover:text-primary transition-colors text-left">
                                  {memberCountLabel(inst.cluster_member_roles)}
                                </button>
                              )}
                            </td>
                            <td className="px-2 py-2 text-[11px] text-fg-2">{engineLabel(inst.engine)}</td>
                            <td className="px-2 py-2 text-[11px] text-fg-2">{inst.tags?.engine_version || '—'}</td>
                            <td className="px-2 py-2 text-[10px] text-fg-muted font-mono" title={inst.account_id || ''}>{inst.account_id || '—'}</td>
                            <td className="px-2 py-2 text-[11px] text-fg-2">{inst.region}</td>
                            <td className="px-2 py-2"><StatusBadge score={score} /></td>
                            <td className="px-2 py-2 whitespace-nowrap">
                              {(() => {
                                const latest = tier1Latest[inst.instance_id]
                                const state = tier1State(inst, latest)
                                const text = state === 'paused' ? 'Paused' : state === 'none' ? '—' : timeAgo(latest.scan_time)
                                return (
                                  <button onClick={() => openTab(inst, 'metrics')} title="Open metrics (Tier 1)"
                                    className={`inline-flex items-center gap-1.5 text-[10px] hover:text-primary transition-colors ${state === 'stale' ? 'text-warn-text' : 'text-fg-3'}`}>
                                    <span className={`w-1.5 h-1.5 rounded-full ${TIER1_DOT[state]}`} aria-hidden />
                                    {text}
                                  </button>
                                )
                              })()}
                            </td>
                            <td className="px-2 py-2 whitespace-nowrap">
                              <button onClick={() => openTab(inst, 'scans')} title="Open deep scans (Tier 2)"
                                className="text-[10px] text-fg-3 hover:text-primary transition-colors">{scanAge}</button>
                            </td>
                            <td className="px-2 py-2">
                              <div className="flex items-center justify-end gap-1">
                                <button onClick={() => handleScan(inst.instance_id)} disabled={scanning[inst.instance_id] || activeScans[inst.instance_id]}
                                  className="px-2 py-1 text-[10px] font-medium rounded bg-primary text-primary-fg hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                  title="Run scan">
                                  {(scanning[inst.instance_id] || activeScans[inst.instance_id]) ? 'Scanning...' : '⚡ Scan'}
                                </button>
                                <button onClick={() => onSelectDatabase?.(inst)}
                                  className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors"
                                  title="Chat about this database">
                                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                                  </svg>
                                </button>
                                {/* One action per drawer tab, in the drawer's order: (Members,) Metrics, Deep scans, Trends.
                                    Members shows only for a cluster; a single-instance database (e.g. RDS) has none. The
                                    rail is right-aligned, so a row missing this icon would pull every other action — the
                                    Scan button included — one slot right, out of line with the cluster rows. Reserve the
                                    slot (same 24px as the p-1 + w-4 icon) so the actions line up down the column. */}
                                {(inst.cluster_member_roles || []).length > 0 ? (
                                  <button onClick={() => openTab(inst, 'members')}
                                    className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors"
                                    title="Members (writer and readers)" aria-label="Members">
                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
                                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h10" />
                                    </svg>
                                  </button>
                                ) : (
                                  <span className="w-6 flex-shrink-0" aria-hidden />
                                )}
                                <button onClick={() => openTab(inst, 'metrics')}
                                  className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors"
                                  title="Metrics (Tier 1)">
                                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 12h4l3-8 4 16 3-8h4" />
                                  </svg>
                                </button>
                                <button onClick={() => openTab(inst, 'scans')}
                                  className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors"
                                  title="Deep scans (Tier 2) and reports">
                                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                                  </svg>
                                </button>
                                <button onClick={() => openTab(inst, 'trends')}
                                  className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors"
                                  title="Trends">
                                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
                                  </svg>
                                </button>
                                <button onClick={() => openTab(inst, 'cloudwatch')}
                                  className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors"
                                  title="CloudWatch metrics (every published series)">
                                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 20V10m5 10V4m5 16v-7m5 7V8M3 20h18" />
                                  </svg>
                                </button>
                              </div>
                            </td>
                          </tr>
                        )
                      })}
                      {paginated.length === 0 && (
                        <tr><td colSpan={10} className="px-3 py-8 text-center text-sm text-fg-muted">No databases match your filters</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}

              {/* Grid view */}
              {viewMode === 'grid' && (
                <div className="p-4 grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3 auto-rows-fr">
                  {paginated.map(inst => (
                    <DbInstanceCard key={inst.instance_id} instance={inst} api={api} scanSubmitted={scanSubmitted[inst.instance_id] || 0}
                      onScan={handleScan} onViewReport={onViewReport} onSelectDatabase={onSelectDatabase}
                      onMetrics={(i) => openTab(i, 'metrics')}
                      onScans={(i) => openTab(i, 'scans')}
                      onTrend={(i) => openTab(i, 'trends')}
                      onCloudWatch={(i) => openTab(i, 'cloudwatch')}
                    />
                  ))}
                  {paginated.length === 0 && (
                    <div className="col-span-full text-center py-8 text-sm text-fg-muted">No databases match your filters</div>
                  )}
                </div>
              )}

              {/* Pagination */}
              {sorted.length > pageSize && (
                <div className="px-4 py-3 border-t border-line-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-fg-3">Rows per page:</span>
                    <select value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1) }}
                      className="text-[11px] border border-line rounded px-1.5 py-0.5 bg-surface text-fg-2">
                      {PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
                    </select>
                    <span className="text-[11px] text-fg-muted ml-2">
                      {(page-1)*pageSize+1}–{Math.min(page*pageSize, sorted.length)} of {sorted.length}
                    </span>
                  </div>
                  <div className="flex items-center gap-1">
                    <button onClick={() => setPage(p => Math.max(1, p-1))} disabled={page === 1}
                      className="px-2 py-1 text-[11px] rounded border border-line text-fg-2 hover:bg-surface-3 disabled:opacity-30 disabled:cursor-not-allowed">Prev</button>
                    {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
                      let p
                      if (totalPages <= 7) p = i + 1
                      else if (page <= 4) p = i + 1
                      else if (page >= totalPages - 3) p = totalPages - 6 + i
                      else p = page - 3 + i
                      return (
                        <button key={p} onClick={() => setPage(p)}
                          className={`w-7 h-7 text-[11px] rounded ${page === p ? 'bg-primary text-primary-fg' : 'text-fg-2 hover:bg-surface-3'}`}>{p}</button>
                      )
                    })}
                    <button onClick={() => setPage(p => Math.min(totalPages, p+1))} disabled={page === totalPages}
                      className="px-2 py-1 text-[11px] rounded border border-line text-fg-2 hover:bg-surface-3 disabled:opacity-30 disabled:cursor-not-allowed">Next</button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Drawer below: Metrics (Tier 1) / Deep scans (Tier 2) / Trends / Reports for one database */}
          {panel?.instance && (
            <DatabaseDrawer api={api} instance={panel.instance} tab={panel.tab}
              onTab={(tab) => setPanel(p => ({ ...p, tab }))} onClose={() => setPanel(null)} onViewReport={onViewReport} onCompare={onCompareScans} />
          )}

        </>
      )}
    </div>
  )
}
