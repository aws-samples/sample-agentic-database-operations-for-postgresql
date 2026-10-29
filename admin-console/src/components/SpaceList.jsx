// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import { Plus, Search } from 'lucide-react'
import { APP_NAME } from '../appName'

export default function SpaceList({ api, onCreate, onOpen }) {
  const [spaces, setSpaces] = useState([])
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')

  const fetchSpaces = async (quiet = false) => {
    try {
      if (!quiet) setLoading(true)
      const data = await api.get('/api/spaces')
      // A deletion whose server process stopped part way (flagged teardown_stalled) is
      // shown as Delete failed: deleting the space again takes it over and finishes it.
      setSpaces((data || []).map(sp => (sp.status === 'deleting' && sp.teardown_stalled
        ? { ...sp, status: 'delete_failed' } : sp)))
    } catch (e) { console.error('Failed to load spaces:', e) }
    finally { if (!quiet) setLoading(false) }
  }

  useEffect(() => { fetchSpaces() }, [])

  // A deleted space stays listed as "Deleting…" while its databases are torn down in the
  // background, then disappears: poll until none is deleting.
  useEffect(() => {
    if (!spaces.some(sp => sp.status === 'deleting')) return
    const interval = setInterval(() => fetchSpaces(true), 5000)
    return () => clearInterval(interval)
  }, [spaces])

  if (loading) return (
    <div className="flex items-center justify-center py-24 gap-3 text-fg-muted">
      <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
      <span className="text-sm">Loading spaces...</span>
    </div>
  )

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-fg">Agent Spaces</h1>
          <p className="text-sm text-fg-3 mt-1">
            Agent Spaces define the boundary of what {APP_NAME} can access and investigate.
          </p>
        </div>
        <button onClick={onCreate}
          className="px-4 py-2 bg-primary hover:bg-primary-hover text-primary-fg text-sm font-medium rounded-lg transition-colors">
          <span className="inline-flex items-center gap-1.5"><Plus className="w-4 h-4" aria-hidden />Create Agent Space</span>
        </button>
      </div>
      {spaces.length > 5 && (
        <label className="relative block max-w-sm mb-4">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-fg-muted" aria-hidden />
          <input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search by name, account or owner…"
            className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-line bg-surface text-fg placeholder:text-fg-muted focus:outline-none focus:border-primary-line" aria-label="Search Agent Spaces" />
        </label>
      )}

      {spaces.length === 0 ? (
        <div className="text-center py-20 bg-surface rounded-xl border border-line">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-surface-3 flex items-center justify-center">
            <svg className="w-8 h-8 text-fg-muted" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 3C7.58 3 4 4.79 4 7v10c0 2.21 3.58 4 8 4s8-1.79 8-4V7c0-2.21-3.58-4-8-4z"/>
            </svg>
          </div>
          <h3 className="text-lg font-semibold text-fg mb-2">No Agent Spaces</h3>
          <p className="text-fg-3 mb-4">Create your first Agent Space to start monitoring databases.</p>
          <button onClick={onCreate}
            className="px-4 py-2 bg-primary hover:bg-primary-hover text-primary-fg text-sm font-medium rounded-lg transition-colors">
            Create Agent Space
          </button>
        </div>
      ) : (
        <div className="bg-surface rounded-xl border border-line overflow-hidden">
          {/* Every cell is left-aligned and vertically centred; the name button is a
              block so its text aligns with the column instead of centring inside the
              button. Regions render as chips (with an overflow count) so a space with
              many regions does not stretch its row. The whole row opens the space. */}
          <table className="w-full table-fixed">
            <colgroup>
              <col className="w-[26%]" />
              <col className="w-[13%]" />
              <col className="w-[22%]" />
              <col className="w-[9%]" />
              <col className="w-[20%]" />
              <col className="w-[10%]" />
            </colgroup>
            <thead className="bg-surface-2 border-b border-line">
              <tr>
                {['Name', 'Primary account', 'Regions', 'Databases', 'Owner', 'Status'].map(h => (
                  <th key={h} scope="col" className="px-5 py-3 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line-2">
              {spaces.filter(sp => { const q = query.trim().toLowerCase(); return !q || [sp.name, sp.description, sp.primary_account_id, sp.owner].some(v => String(v || '').toLowerCase().includes(q)) }).map(space => {
                const regions = space.regions || []
                const shown = regions.slice(0, 3)
                return (
                  <tr key={space.space_id} onClick={() => onOpen(space)}
                    className="hover:bg-surface-3 transition-colors cursor-pointer align-middle">
                    <td className="px-5 py-3 text-left">
                      <button onClick={(e) => { e.stopPropagation(); onOpen(space) }}
                        className="block w-full text-left text-sm font-medium text-primary hover:underline truncate" title={space.name}>
                        {space.name}
                      </button>
                      {space.description && (
                        <p className="text-xs text-fg-muted mt-0.5 truncate" title={space.description}>{space.description}</p>
                      )}
                    </td>
                    <td className="px-5 py-3 text-left text-sm text-fg-2 font-mono whitespace-nowrap">{space.primary_account_id || '—'}</td>
                    <td className="px-5 py-3 text-left">
                      {regions.length === 0 ? <span className="text-sm text-fg-muted">—</span> : (
                        <div className="flex flex-wrap items-center gap-1" title={regions.join(', ')}>
                          {shown.map(r => (
                            <span key={r} className="px-1.5 py-0.5 rounded text-[11px] font-mono text-fg-2 bg-surface-2 border border-line whitespace-nowrap">{r}</span>
                          ))}
                          {regions.length > shown.length && (
                            <span className="text-[11px] text-fg-muted whitespace-nowrap">+{regions.length - shown.length} more</span>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="px-5 py-3 text-left text-sm text-fg-2 tabular-nums">{space.database_count || 0}</td>
                    <td className="px-5 py-3 text-left text-sm text-fg-2 truncate" title={space.owner ? `Created by ${space.owner}` : undefined}>
                      {space.owner || '—'}
                    </td>
                    <td className="px-5 py-3 text-left">
                      <span className={`inline-block px-2 py-0.5 rounded-full text-[11px] font-medium whitespace-nowrap ${
                        space.status === 'active' ? 'bg-ok-bg text-ok-text border border-ok-border'
                        : space.status === 'creating' ? 'bg-warn-bg text-warn-text border border-warn-border'
                        : space.status === 'deleting' ? 'bg-info-bg text-info-text border border-info-border animate-pulse'
                        : space.status === 'delete_failed' ? 'bg-err-bg text-err-text border border-err-border'
                        : 'bg-surface-2 text-fg-2 border border-line'
                      }`} title={space.status === 'delete_failed' ? (space.teardown_stalled ? 'The deletion stopped before it finished (the service restarted). Open the space and delete it again.' : 'A database could not be fully removed. Open the space, Retry it, then delete the space again.') : undefined}>
                        {{ active: 'Active', creating: 'Creating', deleting: 'Deleting…', delete_failed: 'Delete failed' }[space.status] || space.status || 'Unknown'}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
