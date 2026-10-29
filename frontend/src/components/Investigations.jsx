// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import InvestigationDetail from './InvestigationDetail'
import ModelPicker from './ModelPicker'

const QUICK_STARTS = [
  { id: 'high_cpu', label: 'High CPU', prompt: 'Investigate high CPU utilization — check metrics, top SQL, and recent changes' },
  { id: 'replication_lag', label: 'Replication lag', prompt: 'Investigate replication lag — check replica status, WAL, and write load' },
  { id: 'lock_contention', label: 'Lock contention', prompt: 'Investigate lock contention — check active locks, blocked queries, and deadlocks' },
  { id: 'connection_spike', label: 'Connection spike', prompt: 'Investigate connection spike — check connection count, pool settings, and client sources' },
  { id: 'storage_growth', label: 'Storage growth', prompt: 'Investigate storage growth — check table sizes, bloat, WAL, and temp files' },
  { id: 'slow_queries', label: 'Slow queries', prompt: 'Investigate slow queries — check Performance Insights, missing indexes, and query plans' },
]

const STATUS_STYLES = {
  running: 'bg-info-bg-2 text-info-text border-info-border',
  completed: 'bg-ok-bg-2 text-ok-text border-ok-border',
  failed: 'bg-err-bg-2 text-err-text border-err-border',
  cancelled: 'bg-surface-3 text-fg-2 border-line',
}

function StatusBadge({ status }) {
  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${STATUS_STYLES[status] || STATUS_STYLES.cancelled}`}>
      {status?.toUpperCase()}
    </span>
  )
}

function formatDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
    d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function Investigations({ api, spaceId, databases, onChatAbout, onTrace, accessToken, getValidAccessToken, streamMessage, isSessionStreaming, stopStreaming, backendUrl, openInvestigation, onOpenInvestigationConsumed }) {
  const [investigations, setInvestigations] = useState([])
  const [loading, setLoading] = useState(true)
  const [prompt, setPrompt] = useState('')
  const [starting, setStarting] = useState(false)
  const [selectedInvestigation, setSelectedInvestigation] = useState(null)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [selectedDbs, setSelectedDbs] = useState([])
  const [timeRange, setTimeRange] = useState('auto')
  const [dbSearch, setDbSearch] = useState('')
  const [dbEngineFilter, setDbEngineFilter] = useState('')
  const [dbRegionFilter, setDbRegionFilter] = useState('')

  const TIME_RANGES = [
    { value: 'auto', label: 'Auto (recommended)' },
    { value: '1h', label: 'Last 1 hour' },
    { value: '2h', label: 'Last 2 hours' },
    { value: '6h', label: 'Last 6 hours' },
    { value: '12h', label: 'Last 12 hours' },
    { value: '24h', label: 'Last 24 hours' },
    { value: '3d', label: 'Last 3 days' },
    { value: '7d', label: 'Last 7 days' },
  ]

  const fetchInvestigations = async () => {
    if (!spaceId) return
    try {
      setLoading(true)
      const data = await api.get(`/api/spaces/${spaceId}/investigations`)
      setInvestigations(data.investigations || data || [])
    } catch (e) {
      console.error('Failed to load investigations:', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchInvestigations() }, [spaceId])

  // Auto-open an investigation handed off from chat (the "Start investigation"
  // pill). It's already created + running, so we drop straight into its detail
  // view, which streams it. Clear the hand-off so it fires only once.
  useEffect(() => {
    if (openInvestigation) {
      setSelectedInvestigation(openInvestigation)
      onOpenInvestigationConsumed?.()
    }
  }, [openInvestigation])

  // Poll for running investigations (with backoff)
  useEffect(() => {
    const hasRunning = investigations.some(i => i.status === 'running')
    if (!hasRunning) return
    let errorCount = 0
    const poll = async () => {
      try {
        const data = await api.get(`/api/spaces/${spaceId}/investigations`)
        setInvestigations(data.investigations || data || [])
        errorCount = 0
      } catch {
        errorCount++
      }
    }
    const id = setInterval(poll, errorCount > 3 ? 30000 : 15000)
    return () => clearInterval(id)
  }, [investigations])

  const handleCancel = async (invId) => {
    try {
      await api.post(`/api/spaces/${spaceId}/investigations/${invId}/cancel`)
      fetchInvestigations()
    } catch (e) {
      console.error('Failed to cancel:', e)
    }
  }

  const [selectedModel, setSelectedModel] = useState('')  // model pinned for this investigation

  const handleStart = async (quickStart = null) => {
    const triggerPrompt = quickStart?.prompt || prompt
    if (!triggerPrompt.trim()) return
    if (selectedDbs.length === 0) {
      alert('Please select a database to investigate')
      return
    }
    setStarting(true)
    const timeLabel = timeRange === 'auto'
      ? 'Auto (recommended per check)'
      : (TIME_RANGES.find(t => t.value === timeRange)?.label || 'Auto (recommended per check)')
    try {
      const body = {
        prompt: triggerPrompt,
        database_ids: selectedDbs.map(d => d.instance_id),
        database_names: selectedDbs.map(d => d.display_name),
        quick_start_type: quickStart?.id || null,
        time_range: timeLabel,
        model_id: selectedModel || undefined,
      }
      const result = await api.post(`/api/spaces/${spaceId}/investigations`, body)
      setPrompt('')
      setSelectedInvestigation(result)
      fetchInvestigations()
    } catch (e) {
      alert(e.detail || 'Failed to start investigation')
    } finally {
      setStarting(false)
    }
  }

  const filtered = investigations.filter(inv => {
    if (search && !inv.title?.toLowerCase().includes(search.toLowerCase()) &&
        !inv.trigger?.toLowerCase().includes(search.toLowerCase())) return false
    if (statusFilter && inv.status !== statusFilter) return false
    return true
  })

  if (selectedInvestigation) {
    return (
      <InvestigationDetail
        // Keyed by id: the detail view keeps the record in local state, so the key forces
        // a remount when a chat-started investigation arrives while another one is open,
        // ensuring the view switches to it instead of holding the earlier record.
        key={selectedInvestigation.investigation_id}
        api={api}
        spaceId={spaceId}
        investigation={selectedInvestigation}
        isSessionStreaming={isSessionStreaming}
        stopStreaming={stopStreaming}
        databases={databases}
        onBack={() => { setSelectedInvestigation(null); fetchInvestigations() }}
        onChatAbout={onChatAbout}
        onTrace={onTrace}
        accessToken={accessToken}
        getValidAccessToken={getValidAccessToken}
        streamMessage={streamMessage}
        backendUrl={backendUrl}
      />
    )
  }

  return (
    <div className="space-y-6">
      {/* Start Investigation Card */}
      <div className="bg-surface rounded-xl border border-line p-6">
        <h2 className="text-lg font-semibold text-fg mb-1">Start an Investigation</h2>
        <p className="text-sm text-fg-3 mb-4">
          Describe what you'd like to investigate. The agent will check metrics, logs, queries, and recent changes to find the root cause.
        </p>
        <textarea
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          placeholder="Describe what you'd like to investigate. Include any details about the issue, areas to explore, or relevant information..."
          className="w-full h-24 px-4 py-3 rounded-lg border border-line bg-surface text-fg placeholder-fg-muted text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
        />
        <div className="flex flex-wrap gap-2 mt-3">
          {QUICK_STARTS.map(qs => (
            <button key={qs.id} onClick={() => handleStart(qs)} disabled={starting}
              className="px-3 py-1.5 text-xs rounded-lg border border-line text-fg-2 hover:bg-surface-3 hover:border-primary transition-colors disabled:opacity-50">
              {qs.label}
            </button>
          ))}
        </div>

        {/* Database selector — search + filter */}
        {databases?.length > 0 && (() => {
          const engines = [...new Set(databases.map(d => d.engine))].sort()
          const regions = [...new Set(databases.map(d => d.region))].sort()
          const EL = { 'aurora-postgresql': 'Aurora PG', 'rds-postgresql': 'RDS PG', postgres: 'PostgreSQL' }
          const filteredDbs = databases.filter(db => {
            if (dbSearch) {
              const q = dbSearch.toLowerCase()
              if (!db.display_name?.toLowerCase().includes(q) &&
                  !(EL[db.engine] || db.engine || '').toLowerCase().includes(q) &&
                  !db.region?.toLowerCase().includes(q)) return false
            }
            if (dbEngineFilter && db.engine !== dbEngineFilter) return false
            if (dbRegionFilter && db.region !== dbRegionFilter) return false
            return true
          })
          return (
            <div className="mt-4 pt-4 border-t border-line-2">
              <p className="text-xs text-fg-3 mb-2">Database <span className="text-err">*</span></p>

              {selectedDbs.length > 0 ? (
                <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-primary-subtle border border-primary-line">
                  <span className="w-2 h-2 rounded-full bg-primary flex-shrink-0" />
                  <span className="text-xs font-medium text-primary truncate">{selectedDbs[0].display_name}</span>
                  <span className="text-[10px] text-fg-muted">{EL[selectedDbs[0].engine] || selectedDbs[0].engine} · {selectedDbs[0].region}</span>
                  <button onClick={() => setSelectedDbs([])} className="ml-auto text-fg-muted hover:text-err transition-colors" title="Change database">
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
                  </button>
                </div>
              ) : (
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <div className="flex-1 relative">
                      <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
                      <input value={dbSearch} onChange={e => setDbSearch(e.target.value)} placeholder="Search databases..."
                        className="w-full pl-8 pr-3 py-1.5 text-xs border border-line rounded-lg bg-surface text-fg outline-none focus:border-primary" />
                    </div>
                    {engines.length > 1 && (
                      <select value={dbEngineFilter} onChange={e => setDbEngineFilter(e.target.value)}
                        className="px-2 py-1.5 text-[11px] border border-line rounded-lg bg-surface text-fg-2 outline-none">
                        <option value="">All engines</option>
                        {engines.map(e => <option key={e} value={e}>{EL[e] || e}</option>)}
                      </select>
                    )}
                    {regions.length > 1 && (
                      <select value={dbRegionFilter} onChange={e => setDbRegionFilter(e.target.value)}
                        className="px-2 py-1.5 text-[11px] border border-line rounded-lg bg-surface text-fg-2 outline-none">
                        <option value="">All regions</option>
                        {regions.map(r => <option key={r} value={r}>{r}</option>)}
                      </select>
                    )}
                  </div>
                  <div className="max-h-40 overflow-y-auto border border-line rounded-lg divide-y divide-line-2">
                    {filteredDbs.map(db => (
                      <button key={db.instance_id}
                        onClick={() => { setSelectedDbs([db]); setDbSearch(''); setDbEngineFilter(''); setDbRegionFilter('') }}
                        className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-left hover:bg-surface-3 transition-colors">
                        <span className="w-2 h-2 rounded-full bg-line-strong flex-shrink-0" />
                        <span className="truncate text-fg-2">{db.display_name}</span>
                        <span className="text-[10px] text-fg-muted ml-auto flex-shrink-0">{EL[db.engine] || db.engine} · {db.region}</span>
                      </button>
                    ))}
                    {filteredDbs.length === 0 && (
                      <div className="px-3 py-4 text-xs text-fg-muted text-center">No databases match your filters</div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )
        })()}

        {/* Time range selector */}
        <div className="mt-4 pt-4 border-t border-line-2">
          <div className="flex items-center gap-3">
            <p className="text-xs text-fg-3">Time range:</p>
            <div className="flex flex-wrap gap-1.5">
              {TIME_RANGES.map(tr => (
                <button key={tr.value} onClick={() => setTimeRange(tr.value)}
                  className={`px-2.5 py-1 text-[11px] rounded border transition-colors ${
                    timeRange === tr.value
                      ? 'bg-primary-subtle text-primary border-primary-line font-medium'
                      : 'text-fg-3 border-line hover:border-line-strong'
                  }`}>
                  {tr.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="flex justify-between items-center mt-4">
          <div className="flex items-center gap-2 text-xs text-fg-3">
            <span>Model:</span>
            <ModelPicker api={api} getValidAccessToken={getValidAccessToken} accessToken={accessToken} value={selectedModel} onChange={setSelectedModel} />
          </div>
          <button onClick={() => handleStart()} disabled={starting || !prompt.trim() || selectedDbs.length === 0}
            title={selectedDbs.length === 0 ? 'Select a database first' : undefined}
            className="px-5 py-2 bg-primary hover:bg-primary-hover text-primary-fg text-sm font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2">
            {starting && <div className="w-3.5 h-3.5 border-2 border-line border-t-white rounded-full animate-spin" />}
            Start investigation
          </button>
        </div>
      </div>

      {/* Investigation List */}
      <div className="bg-surface rounded-xl border border-line">
        <div className="p-4 border-b border-line-2 flex items-center gap-3">
          <input
            value={search} onChange={e => setSearch(e.target.value)}
            placeholder="Search investigations..."
            className="flex-1 px-3 py-1.5 text-sm rounded-lg border border-line bg-surface text-fg placeholder-fg-muted focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
            className="px-3 py-1.5 text-sm rounded-lg border border-line bg-surface text-fg-2">
            <option value="">All Statuses</option>
            <option value="running">Running</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="cancelled">Cancelled</option>
          </select>
        </div>

        {loading ? (
          <div className="p-8 text-center">
            <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-2" />
            <p className="text-sm text-fg-3">Loading investigations...</p>
          </div>
        ) : filtered.length === 0 ? (
          <div className="p-8 text-center">
            <div className="w-12 h-12 mx-auto mb-3 rounded-full bg-surface-3 flex items-center justify-center">
              <svg className="w-6 h-6 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            </div>
            <p className="text-sm font-medium text-fg-2">No investigations</p>
            <p className="text-xs text-fg-muted mt-1">Start one above to begin root cause analysis.</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-fg-3 border-b border-line-2">
                <th className="px-4 py-2.5 font-medium">Investigation</th>
                <th className="px-4 py-2.5 font-medium">Status</th>
                <th className="px-4 py-2.5 font-medium">Started</th>
                <th className="px-4 py-2.5 font-medium">Last Updated</th>
                <th className="px-4 py-2.5 font-medium w-20"></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(inv => (
                <tr key={inv.investigation_id}
                  onClick={() => setSelectedInvestigation(inv)}
                  className="border-b border-line-2 hover:bg-surface-3 cursor-pointer transition-colors">
                  <td className="px-4 py-3">
                    <div className="font-medium text-fg">{inv.title || inv.trigger}</div>
                    {inv.severity && (
                      <span className={`text-[10px] font-semibold ${
                        inv.severity === 'critical' ? 'text-err' : inv.severity === 'warning' ? 'text-warn' : 'text-info'
                      }`}>{inv.severity.toUpperCase()}</span>
                    )}
                  </td>
                  <td className="px-4 py-3"><StatusBadge status={inv.status} /></td>
                  <td className="px-4 py-3 text-fg-3 text-xs">{formatDate(inv.started_at)}</td>
                  <td className="px-4 py-3 text-fg-3 text-xs">{formatDate(inv.completed_at || inv.started_at)}</td>
                  <td className="px-4 py-3">
                    {inv.status === 'running' && (
                      <button onClick={(e) => { e.stopPropagation(); handleCancel(inv.investigation_id) }}
                        className="px-2 py-1 text-[10px] font-medium text-err border border-err-border rounded hover:bg-err-hover transition-colors">
                        Cancel
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
