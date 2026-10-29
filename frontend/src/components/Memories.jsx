// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback, useRef, useMemo } from 'react'

// What the agent has learned about the caller (facts + preferences). Backed by
// /api/memory, which scopes every read/delete to the caller's Cognito sub.

const TYPE_META = {
  fact: { label: 'Fact', dot: 'bg-info', badge: 'bg-info-bg text-info' },
  preference: { label: 'Preference', dot: 'bg-primary', badge: 'bg-primary-subtle text-primary' },
  summary: { label: 'Summary', dot: 'bg-fg-muted', badge: 'bg-surface-3 text-fg-3' },
}

function fmtDate(iso) {
  if (!iso) return ''
  try { return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }) } catch { return '' }
}

function MemoryCard({ m, showScore, onDelete }) {
  const meta = TYPE_META[m.type] || TYPE_META.fact
  return (
    <div className="group relative bg-surface border border-line rounded-xl px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <span className={`inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full mb-1.5 ${meta.badge}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${meta.dot}`} />
            {meta.label}
          </span>
          <p className="text-sm text-fg leading-relaxed break-words">{m.content}</p>
          {m.categories?.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {m.categories.map((c, i) => (
                <span key={i} className="inline-flex items-center text-[10px] font-medium px-2 py-0.5 rounded-full bg-surface-3 text-fg-3">
                  {c}
                </span>
              ))}
            </div>
          )}
          <div className="flex items-center gap-3 mt-1.5 text-[11px] text-fg-muted">
            {m.created_at && <span>{fmtDate(m.created_at)}</span>}
            {showScore && typeof m.score === 'number' && (
              <span className="text-primary font-medium">{Math.round(m.score * 100)}% match</span>
            )}
          </div>
        </div>
        <button onClick={() => onDelete(m.id)} title="Delete this memory"
          className="opacity-0 group-hover:opacity-100 flex-shrink-0 p-1.5 text-fg-muted hover:text-err hover:bg-err-hover rounded-lg transition-all">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
        </button>
      </div>
    </div>
  )
}

export default function Memories({ api }) {
  const apiRef = useRef(api)
  apiRef.current = api

  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [enabled, setEnabled] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async (q) => {
    setLoading(true)
    setError(null)
    try {
      const qs = q ? `?q=${encodeURIComponent(q)}&k=50` : '?k=50'
      const data = await apiRef.current.get(`/api/memory${qs}`)
      setItems(Array.isArray(data?.memories) ? data.memories : [])
      setEnabled(data?.memory_enabled !== false)
    } catch (e) {
      setError(e.detail || 'Failed to load memories')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load('') }, [load])

  const onSearch = (e) => { e.preventDefault(); const q = query.trim(); setSubmitted(q); load(q) }
  const onClear = () => { setQuery(''); setSubmitted(''); load('') }
  const onDelete = async (id) => {
    if (!window.confirm('Delete this memory? The agent will forget it.')) return
    setItems(prev => prev.filter(m => m.id !== id))
    try { await apiRef.current.del(`/api/memory/${id}`) } catch { load(submitted) }
  }

  const groups = useMemo(() => {
    const g = { fact: [], preference: [], summary: [] }
    for (const m of items) (g[m.type] || (g[m.type] = [])).push(m)
    return g
  }, [items])

  return (
    <div className="max-w-3xl mx-auto">
      <div className="mb-4">
        <h1 className="text-xl font-semibold text-fg">Memories</h1>
        <p className="text-sm text-fg-2 mt-1">What the agent has learned about you. Search it, or delete anything it should forget.</p>
      </div>

      <form onSubmit={onSearch} className="flex items-center gap-2 mb-5">
        <div className="relative flex-1">
          <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-fg-muted" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search your memories..."
            className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-line bg-surface text-fg focus:outline-none focus:ring-2 focus:ring-primary"
          />
        </div>
        <button type="submit"
          className="px-4 py-2 text-sm font-medium rounded-lg bg-primary hover:bg-primary-hover text-primary-fg transition-colors">
          Search
        </button>
        {submitted && (
          <button type="button" onClick={onClear}
            className="px-3 py-2 text-sm rounded-lg border border-line text-fg-2 hover:bg-surface-3 transition-colors">
            Clear
          </button>
        )}
      </form>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-fg-3 py-10 justify-center">
          <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          Loading memories...
        </div>
      ) : !enabled ? (
        <div className="text-center py-12 text-sm text-fg-3">
          Long-term memory isn't enabled for this deployment.
        </div>
      ) : error ? (
        <div className="text-center py-12 text-sm text-err">{error}</div>
      ) : items.length === 0 ? (
        <div className="text-center py-12">
          <p className="text-sm text-fg-2 font-medium">{submitted ? 'No memories match your search.' : 'No memories yet.'}</p>
          {!submitted && <p className="text-xs text-fg-muted mt-1">As you chat, the agent will remember useful facts and preferences here.</p>}
        </div>
      ) : submitted ? (
        /* Search results — relevance-ordered, flat */
        <div className="space-y-2">
          {items.map(m => <MemoryCard key={m.id} m={m} showScore onDelete={onDelete} />)}
        </div>
      ) : (
        /* Default view — grouped by type */
        <div className="space-y-6">
          {['fact', 'preference', 'summary'].map(type => (
            groups[type] && groups[type].length > 0 ? (
              <div key={type}>
                <p className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted mb-2">
                  {TYPE_META[type].label}s ({groups[type].length})
                </p>
                <div className="space-y-2">
                  {groups[type].map(m => <MemoryCard key={m.id} m={m} onDelete={onDelete} />)}
                </div>
              </div>
            ) : null
          ))}
        </div>
      )}
    </div>
  )
}
