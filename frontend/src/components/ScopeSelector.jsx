// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useEffect, useMemo } from 'react'
import { useLiveMembers, reachFrom, liveRoles, reachLabel } from './MemberPicker'
import { canonicalEngine, engineLabel } from './DbListControls'

// Database scope picker, rendered as the dropdown trigger in the composer's
// header row. The trigger reads the
// current scope at a glance; the popover holds search, filters, the list and
// the Aurora endpoint focus.


const STATUS_BADGE = {
  critical: 'bg-err-bg text-err-text',
  warning: 'bg-warn-bg text-warn-text',
  ok: 'bg-ok-bg text-ok-text',
}

function StatusDot({ score }) {
  const c = score === 'critical' ? 'bg-err' : score === 'warning' ? 'bg-warn' : score === 'ok' ? 'bg-ok' : 'bg-line-strong'
  return <span className={`inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${c}`} />
}

function DbIcon({ className = 'w-4 h-4' }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
      <ellipse cx="12" cy="5.5" rx="7" ry="2.6" />
      <path strokeLinecap="round" d="M5 5.5v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6v-6" />
      <path strokeLinecap="round" d="M5 11.5v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6v-6" />
    </svg>
  )
}

// One-line summary of the scope for the trigger.
function scopeLabel(databases, selected, roleFocus) {
  let base
  if (!databases.length) base = 'No databases'
  else if (selected.length === 0) base = `All databases (${databases.length})`
  else if (selected.length === 1) base = selected[0].display_name
  else base = `${selected[0].display_name} +${selected.length - 1}`
  if (!roleFocus) return base
  const focus = roleFocus.startsWith('instance:') ? roleFocus.slice('instance:'.length) : roleFocus
  return `${base} · ${focus}`
}

export default function ScopeSelector({ api, databases = [], selected = [], onChange, onRefresh, roleFocus = '', onRoleFocusChange, disabled }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [engineFilter, setEngineFilter] = useState('')
  const [regionFilter, setRegionFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const searchRef = useRef(null)
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false) }
    const onEsc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onEsc)
    // Focus the search box once the popover is in the DOM.
    const t = setTimeout(() => searchRef.current?.focus(), 0)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onEsc); clearTimeout(t) }
  }, [open])

  const engines = useMemo(() => [...new Set(databases.map(d => canonicalEngine(d.engine)).filter(Boolean))].sort(), [databases])
  const regions = useMemo(() => [...new Set(databases.map(d => d.region))].sort(), [databases])

  const filtered = useMemo(() => {
    let list = databases
    if (search) {
      const q = search.toLowerCase()
      list = list.filter(db => {
        const engineText = engineLabel(db.engine).toLowerCase()
        const status = (db.last_health_score || 'unknown').toLowerCase()
        return db.display_name?.toLowerCase().includes(q) ||
          db.engine?.toLowerCase().includes(q) ||
          engineText.includes(q) ||
          db.region?.toLowerCase().includes(q) ||
          db.database_name?.toLowerCase().includes(q) ||
          db.tags?.engine_version?.toLowerCase().includes(q) ||
          status.includes(q)
      })
    }
    if (engineFilter) list = list.filter(db => canonicalEngine(db.engine) === engineFilter)
    if (regionFilter) list = list.filter(db => db.region === regionFilter)
    if (statusFilter) list = list.filter(db => (db.last_health_score || 'unknown') === statusFilter)
    return list
  }, [databases, search, engineFilter, regionFilter, statusFilter])

  // Role focus binds to ONE database: it is only offered when the effective
  // scope is exactly one Aurora cluster that has readers. RDS instances (and
  // mixed/multi-DB scopes, where "the writer" is ambiguous) get no control.
  const focusTarget = useMemo(() => {
    const effective = selected.length ? selected : databases
    return (effective.length === 1 && effective[0].reader_endpoint) ? effective[0] : null
  }, [selected, databases])

  // Reset a stale focus when the scope stops qualifying (e.g. the user swaps
  // the Aurora cluster for an RDS instance, or widens to multiple databases).
  useEffect(() => {
    if (!focusTarget && roleFocus && onRoleFocusChange) onRoleFocusChange('')
  }, [focusTarget, roleFocus, onRoleFocusChange])

  const isSelected = (db) => selected.some(s => s.instance_id === db.instance_id)
  const toggle = (db) => {
    if (isSelected(db)) onChange(selected.filter(s => s.instance_id !== db.instance_id))
    else onChange([...selected, db])
  }
  const addAllFiltered = () => onChange([...selected, ...filtered.filter(db => !isSelected(db))])
  const allFilteredSelected = filtered.length > 0 && filtered.every(db => isSelected(db))
  const toggleAllFiltered = () => {
    if (allFilteredSelected) {
      const ids = new Set(filtered.map(db => db.instance_id))
      onChange(selected.filter(s => !ids.has(s.instance_id)))
    } else addAllFiltered()
  }

  const label = scopeLabel(databases, selected, roleFocus)
  // Roles from the members API while it is loaded (kept current, so a failover shows),
  // else from the record.
  const { data: liveMembers } = useLiveMembers(api, focusTarget?.instance_id)
  const focusMembers = liveRoles(focusTarget, liveMembers).filter(m => m.instance_id && m.endpoint)
  const writer = focusMembers.find(m => m.is_writer)
  const readers = focusMembers.filter(m => !m.is_writer)
  // A member (or the reader endpoint) this database's network path does not reach is
  // offered disabled, with the reason; the server refuses such a focus anyway.
  const reach = reachFrom(liveMembers)
  const memberSkip = (m) => reach.skips[m.instance_id] || ''
  const readerSkip = reach.reader

  return (
    <div className="relative min-w-0" ref={rootRef}>
      {/* Trigger: 14px/500 neutral text + chevron. */}
      <button type="button" onClick={() => !disabled && setOpen(o => !o)} disabled={disabled}
        title="Databases in scope for this conversation"
        aria-haspopup="dialog" aria-expanded={open}
        className="max-w-full flex items-center gap-1.5 -ml-1.5 pl-1.5 pr-1 py-1 rounded-md text-sm font-medium text-fg hover:bg-surface-3 transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
        <DbIcon className="w-4 h-4 text-fg-3 flex-shrink-0" />
        {selected.length === 1 && <StatusDot score={selected[0].last_health_score} />}
        <span className="truncate">{label}</span>
        <svg className={`w-3.5 h-3.5 text-fg-muted flex-shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div role="dialog" aria-label="Database scope"
          className="absolute bottom-full left-0 mb-2 w-full max-w-[360px] bg-surface border border-line rounded-xl shadow-lg z-50 overflow-hidden">
          {/* Search + actions */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-line-2">
            <svg className="w-3.5 h-3.5 text-fg-muted flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input ref={searchRef} type="text" value={search} onChange={(e) => setSearch(e.target.value)}
              placeholder="Search databases…"
              className="chat-composer-input flex-1 min-w-0 text-[13px] text-fg placeholder-fg-muted focus:outline-none" />
            {onRefresh && (
              <button type="button" onClick={onRefresh} title="Refresh databases"
                className="p-1 rounded-md text-fg-muted hover:text-fg-2 hover:bg-surface-3 transition-colors">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
            )}
            {selected.length > 0 && (
              <button type="button" onClick={() => onChange([])}
                className="text-[11px] font-medium text-primary hover:underline whitespace-nowrap">Clear</button>
            )}
          </div>

          {/* Filters */}
          <div className="flex items-center gap-1.5 px-3 py-1.5 border-b border-line-2">
            <select value={engineFilter} onChange={(e) => setEngineFilter(e.target.value)}
              className="text-[11px] border border-line rounded-md px-1.5 py-1 bg-surface text-fg-2 flex-1 min-w-0">
              <option value="">Engine</option>
              {engines.map(e => <option key={e} value={e}>{engineLabel(e)}</option>)}
            </select>
            <select value={regionFilter} onChange={(e) => setRegionFilter(e.target.value)}
              className="text-[11px] border border-line rounded-md px-1.5 py-1 bg-surface text-fg-2 flex-1 min-w-0">
              <option value="">Region</option>
              {regions.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
              className="text-[11px] border border-line rounded-md px-1.5 py-1 bg-surface text-fg-2 flex-1 min-w-0">
              <option value="">Status</option>
              <option value="critical">Critical</option>
              <option value="warning">Warning</option>
              <option value="ok">Healthy</option>
            </select>
          </div>

          {/* List */}
          <div className="max-h-60 overflow-y-auto">
            {filtered.length > 0 && (
              <button type="button" onClick={toggleAllFiltered}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-surface-3 transition-colors border-b border-line-2 sticky top-0 bg-surface z-10">
                <input type="checkbox" checked={allFilteredSelected} readOnly className="w-3.5 h-3.5 rounded border-line-strong pointer-events-none" />
                <span className="text-[12px] font-medium text-fg-2">
                  {allFilteredSelected ? 'Deselect all' : 'Select all'} ({filtered.length})
                </span>
              </button>
            )}
            {filtered.length === 0 ? (
              <div className="px-3 py-4 text-[12px] text-fg-muted text-center">No databases match</div>
            ) : filtered.map(db => (
              <button key={db.instance_id} type="button" onClick={() => toggle(db)}
                className={`w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-surface-3 transition-colors ${isSelected(db) ? 'bg-primary-subtle' : ''}`}>
                <input type="checkbox" checked={isSelected(db)} readOnly className="w-3.5 h-3.5 rounded border-line-strong pointer-events-none" />
                <StatusDot score={db.last_health_score} />
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-medium text-fg truncate">{db.display_name}</div>
                  <div className="text-[11px] text-fg-3 truncate">
                    {engineLabel(db.engine)}
                    {db.tags?.engine_version ? ` ${db.tags.engine_version}` : ''}
                    {' · '}{db.region}
                  </div>
                </div>
                <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded-full ${STATUS_BADGE[db.last_health_score] || 'bg-surface-2 text-fg-muted'}`}>
                  {(db.last_health_score || '—').toUpperCase()}
                </span>
              </button>
            ))}
          </div>

          {/* Aurora endpoint focus — bound to the single Aurora cluster in scope.
              Defaults to the cluster endpoint; writer / each reader connects via
              that member's own endpoint. Hidden for RDS and multi-DB scopes. */}
          {onRoleFocusChange && focusTarget && (
            <div className="flex items-center gap-2 px-3 py-2 border-t border-line-2 bg-surface-2">
              <span className="text-[11px] font-medium text-fg-3 flex-shrink-0">Endpoint</span>
              <select value={roleFocus} onChange={(e) => onRoleFocusChange(e.target.value)}
                title="Chat with the whole cluster (default), the writer, or a specific reader instance"
                className={`text-[11px] border rounded-md px-1.5 py-1 bg-surface flex-1 min-w-0 truncate ${roleFocus ? 'border-primary text-primary' : 'border-line text-fg-2'}`}>
                <option value="">Cluster — {focusTarget.endpoint}</option>
                <option value="writer">Writer — {(writer && writer.endpoint) || focusTarget.endpoint}</option>
                {readers.length > 0
                  ? readers.map(m => (
                    <option key={m.instance_id} value={`instance:${m.instance_id}`} disabled={!!memberSkip(m)} title={memberSkip(m) || undefined}>
                      Reader — {m.endpoint}{memberSkip(m) ? ` (${reachLabel(memberSkip(m))})` : ''}
                    </option>))
                  : <option value="reader" disabled={!!readerSkip} title={readerSkip || undefined}>
                      Reader — {focusTarget.reader_endpoint}{readerSkip ? ` (${reachLabel(readerSkip)})` : ''}
                    </option>}
              </select>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
