// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import Toggle from './Toggle'

// Shared "Agent Capabilities → Skills" experience, identical across
// all layers: a narrow, searchable list on the left and a broad detail pane on
// the right. Built-in skills are locked / always-on — shown with a locked "on"
// toggle — and the SKILL.md body is rendered read-only in the detail pane. Custom
// skills are intentionally not supported yet — a "coming soon" note keeps the
// surface consistent with Knowledge and Model.

const ICON = {
  skill: 'M13 3 4 14h7l-1 7 9-11h-7l1-7z',
  lock: 'M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4',
  search: 'M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z',
}
function Icon({ path, className = 'w-4 h-4' }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" strokeWidth={1.7} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d={path} />
    </svg>
  )
}

export default function CapabilitiesSkills({ api }) {
  const [skills, setSkills] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState(null)
  const [body, setBody] = useState('')
  const [bodyLoading, setBodyLoading] = useState(false)

  // Fetch the SKILL.md body when a skill is picked. Read-only: these ship with the
  // deployment and apply to every space, so there is nothing to edit or switch off.
  const select = async (item) => {
    setSelected(item); setBodyLoading(true); setBody('')
    try {
      const d = await api.get(`/api/knowledge/builtin/skill/${encodeURIComponent(item.id)}`)
      setBody(d.body || '')
    } catch (e) {
      setBody(`(could not load: ${e?.detail || e?.message || 'error'})`)
    } finally {
      setBodyLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    api.get('/api/knowledge/builtin')
      .then(r => { if (!cancelled) setSkills(r.skills || []) })
      .catch(e => { if (!cancelled) setError(e?.detail || e?.message || 'Failed to load skills') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [api])

  const q = search.trim().toLowerCase()
  const filtered = skills.filter(s => !q || `${s.title} ${s.description}`.toLowerCase().includes(q))

  return (
    <div className="flex flex-col lg:flex-row gap-4 items-stretch">
      {/* ── Master list (narrow) ── */}
      <div className="w-full lg:w-80 flex-shrink-0 bg-surface rounded-lg border border-line flex flex-col">
        <div className="p-2 border-b border-line-2">
          <div className="relative">
            <Icon path={ICON.search} className="w-3.5 h-3.5 text-fg-muted absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search skills…"
              className="w-full pl-8 pr-2 py-1.5 text-xs rounded-md border border-line bg-surface-2 text-fg-2 outline-none focus:border-primary" />
          </div>
        </div>

        <div className="p-2 overflow-y-auto max-h-[560px]">
          <div className="flex items-center gap-1.5 px-1 mb-1.5">
            <Icon path={ICON.lock} className="w-3.5 h-3.5 text-fg-muted" />
            <span className="text-[11px] font-semibold text-fg-3 uppercase tracking-wide">
              Built-in{loading ? '' : ` (${skills.length})`}
            </span>
          </div>

          {loading ? (
            <p className="text-xs text-fg-muted px-2 py-1">Loading…</p>
          ) : error ? (
            <div className="text-xs text-err-text bg-err-bg border border-err-border rounded-md px-3 py-2">{error}</div>
          ) : filtered.length === 0 ? (
            <p className="text-xs text-fg-muted px-2 py-1">{q ? `No matches for “${search}”.` : 'No built-in skills.'}</p>
          ) : (
            <div className="space-y-0.5">
              {filtered.map(it => (
                <div key={it.id} onClick={() => select(it)}
                  className={`flex items-center gap-2 px-2.5 py-2 rounded-md cursor-pointer transition-colors ${
                    selected?.id === it.id ? 'bg-primary-subtle' : 'hover:bg-surface-3'
                  }`}>
                  <div className="w-7 h-7 rounded-lg bg-primary-subtle text-primary flex items-center justify-center flex-shrink-0">
                    <Icon path={ICON.skill} className="w-3.5 h-3.5" />
                  </div>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm text-fg truncate">{it.title}</span>
                    {it.description && <span className="block text-[11px] text-fg-muted truncate">{it.description}</span>}
                  </span>
                  <Toggle checked disabled size="sm" title="Always on (built-in)" />
                </div>
              ))}
            </div>
          )}

          <div className="mt-4 px-1">
            <span className="text-[11px] font-semibold text-fg-3 uppercase tracking-wide">Custom · coming soon</span>
            <p className="text-[11px] text-fg-muted mt-1">Uploading custom skills is coming soon.</p>
          </div>
        </div>
      </div>

      {/* ── Detail (broad) ── */}
      <div className="w-full lg:flex-1 min-w-0 bg-surface rounded-lg border border-line p-5 min-h-[420px]">
        {!selected ? (
          <div className="h-full flex flex-col items-center justify-center text-center py-16">
            <Icon path={ICON.skill} className="w-8 h-8 text-fg-muted mb-2" />
            <p className="text-sm text-fg-muted">Select a skill to see what it does.</p>
          </div>
        ) : (
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className="inline-flex items-center gap-1 text-[10px] font-medium text-fg-3 bg-surface-3 rounded px-1.5 py-0.5">
                <Icon path={ICON.lock} className="w-3 h-3" /> Built-in
              </span>
            </div>
            <h3 className="text-lg font-semibold text-fg">{selected.title}</h3>
            {selected.description && <p className="text-sm text-fg-2 mt-2 leading-relaxed">{selected.description}</p>}
            <div className="mt-5 border-t border-line-2 pt-4">
              <div className="flex items-center justify-between mb-2">
                <span className="text-[11px] font-semibold text-fg-3 uppercase tracking-wide">
                  Instructions
                </span>
                <span className="text-[11px] text-fg-muted">read-only</span>
              </div>
              {bodyLoading ? (
                <p className="text-xs text-fg-muted px-1 py-2">Loading…</p>
              ) : (
                <pre className="bg-surface-2 rounded-lg p-4 text-[11px] text-fg-2 overflow-auto max-h-[520px] whitespace-pre-wrap font-mono leading-relaxed">{body}</pre>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
