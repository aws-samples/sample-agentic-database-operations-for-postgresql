// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback } from 'react'
import Toggle from './Toggle'

// Shared "Agent Capabilities → Knowledge" experience, reused across
// the account (admin), space (admin), and operator (self) layers:
//   • A narrow, searchable master list on the left (Built-in · Custom · Inherited)
//     and a broad preview/detail pane on the right.
//   • Built-in knowledge is locked / always-on — shown with a locked "on" toggle, and
//     its full Markdown body is rendered read-only, so an operator can read exactly
//     what the agent was told to do.
//   • Custom docs live in S3 and can be viewed / added / removed at THIS layer.
//   • Inherited custom docs (from higher layers) toggle off for this layer (cascade).
// Uploads accept Markdown/text (.md/.txt) or PDF (.pdf) — a PDF is stored plus an
// extracted-text sidecar the agent reads. useApi is JSON-only, so a PDF is sent
// as base64 in the JSON body.

const ICON = {
  doc: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
  lock: 'M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4',
  pdf: 'M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z',
  search: 'M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z',
}

function Icon({ path, className = 'w-4 h-4' }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" strokeWidth={1.7} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d={path} />
    </svg>
  )
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result).split(',')[1] || '')
    r.onerror = reject
    r.readAsDataURL(file)
  })
}
function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result || ''))
    r.onerror = reject
    r.readAsText(file)
  })
}

// ── Upload modal — file (.md/.pdf) or pasted markdown ────────────────────────
function UploadModal({ onClose, onSubmit, busy }) {
  const [mode, setMode] = useState('file')   // 'file' | 'paste'
  const [name, setName] = useState('')
  const [content, setContent] = useState('')
  const [pdfB64, setPdfB64] = useState(null)
  const [fileErr, setFileErr] = useState(null)

  const onFile = async (file) => {
    setFileErr(null); setPdfB64(null)
    if (!file) return
    const lower = file.name.toLowerCase()
    setName(file.name)
    if (lower.endsWith('.pdf')) {
      try { setPdfB64(await readFileAsBase64(file)); setContent('') }
      catch { setFileErr('Could not read the PDF file.') }
    } else if (lower.endsWith('.md') || lower.endsWith('.markdown') || lower.endsWith('.txt')) {
      try { setContent(await readFileAsText(file)) }
      catch { setFileErr('Could not read the file.') }
    } else {
      setFileErr('Only Markdown (.md/.txt) or PDF (.pdf) files are supported.')
    }
  }

  const canSubmit = name.trim() && (pdfB64 ? true : content.trim()) && !busy
  const submit = async () => {
    if (!canSubmit) return
    try {
      await onSubmit(pdfB64 ? { name: name.trim(), pdf_base64: pdfB64 } : { name: name.trim(), content })
    } catch { /* surfaced by parent */ }
  }

  const TABS = [['file', 'Upload file'], ['paste', 'Paste markdown']]

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-surface rounded-xl shadow-xl w-full max-w-lg border border-transparent"
        onClick={e => e.stopPropagation()}>
        <div className="px-6 py-4 border-b border-line-2">
          <h3 className="text-sm font-semibold text-fg">Add knowledge document</h3>
          <p className="text-xs text-fg-3 mt-0.5">
            Upload a Markdown or PDF file, or paste Markdown. The agent reads its text as guidance.
          </p>
        </div>

        <div className="px-6 pt-3 pb-4 space-y-3">
          {/* Source tabs */}
          <div className="flex gap-4 border-b border-line">
            {TABS.map(([id, label]) => (
              <button key={id} type="button" onClick={() => setMode(id)}
                className={`pb-2 -mb-px text-xs font-medium border-b-2 transition-colors ${
                  mode === id
                    ? 'border-primary text-primary'
                    : 'border-transparent text-fg-3 hover:text-fg-2'
                }`}>{label}</button>
            ))}
          </div>

          {mode === 'file' ? (
            <div>
              <input type="file" accept=".md,.markdown,.txt,.pdf" disabled={busy}
                onChange={e => onFile(e.target.files?.[0])}
                className="block w-full text-xs text-fg-2 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-xs file:font-medium file:bg-primary file:text-white hover:file:bg-primary" />
              {pdfB64 && <p className="text-[11px] text-fg-3 mt-2 flex items-center gap-1"><Icon path={ICON.pdf} className="w-3.5 h-3.5" /> PDF ready — text will be extracted on upload.</p>}
              {fileErr && <p className="text-[11px] text-err mt-2">{fileErr}</p>}
            </div>
          ) : (
            <textarea value={content} onChange={e => setContent(e.target.value)} rows={12}
              placeholder={'# Our runbook\n\nWrite guidance in Markdown…'}
              className="w-full px-3 py-2 text-xs font-mono leading-relaxed border border-line bg-surface text-fg rounded-lg outline-none focus:border-primary" />
          )}

          <div>
            <label className="text-[11px] font-medium text-fg-2 block mb-1">Document name</label>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. our-escalation-policy.md"
              className="w-full px-3 py-2 text-xs border border-line bg-surface text-fg rounded-lg outline-none focus:border-primary" />
          </div>
        </div>

        <div className="px-6 py-4 border-t border-line-2 flex justify-end gap-2">
          <button onClick={onClose}
            className="px-4 py-2 text-xs text-fg-2 border border-line rounded-lg hover:bg-surface-3">Cancel</button>
          <button onClick={submit} disabled={!canSubmit}
            className="px-4 py-2 text-xs font-medium text-primary-fg bg-primary rounded-lg hover:bg-primary-hover disabled:opacity-50">
            {busy ? 'Uploading…' : 'Add document'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function CapabilitiesKnowledge({
  api,
  customBase,               // e.g. /api/knowledge/custom or /api/spaces/{id}/knowledge/custom
  disabledPath = null,      // PUT {disabled_items} — omit at the top (account) layer
  canWrite = false,         // upload / delete custom docs at this layer
  canToggle = false,        // toggle inherited docs off for this layer
  showBuiltin = true,
  subtitle = '',
  scopeLabel = 'Custom',    // this layer's name, e.g. Global / This Space / Personal
  inheritedLabel = '',      // where inherited items come from, e.g. Global / Global & Space
}) {
  const [builtin, setBuiltin] = useState([])
  const [custom, setCustom] = useState([])
  const [inherited, setInherited] = useState([])
  const [disabled, setDisabled] = useState(new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [search, setSearch] = useState('')

  const [selected, setSelected] = useState(null)   // {name, source, description?}
  const [viewContent, setViewContent] = useState('')
  const [viewLoading, setViewLoading] = useState(false)
  const [showUpload, setShowUpload] = useState(false)

  const loadCustom = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const d = await api.get(customBase)
      setCustom(d.custom || [])
      setInherited(d.inherited || [])
      setDisabled(new Set(d.disabled_items || []))
    } catch (e) { setError(e?.detail || e?.message || 'Failed to load knowledge') }
    finally { setLoading(false) }
  }, [api, customBase])

  useEffect(() => {
    let cancelled = false
    if (showBuiltin) {
      api.get('/api/knowledge/builtin')
        .then(r => { if (!cancelled) setBuiltin(r.knowledge_documents || []) })
        .catch(() => {})
    }
    return () => { cancelled = true }
  }, [api, showBuiltin])

  useEffect(() => { loadCustom() }, [loadCustom])

  const view = async (item) => {
    setSelected(item)
    setViewLoading(true); setViewContent('')
    try {
      // Built-in documents ship with the deployment and are read-only, so they come
      // from the builtin endpoint by id; custom ones are looked up by name.
      const d = item.source === 'builtin'
        ? await api.get(`/api/knowledge/builtin/knowledge_document/${encodeURIComponent(item.id)}`)
        : await api.get(`${customBase}/${encodeURIComponent(item.name)}`)
      setViewContent(d.body || d.content || '')
    } catch (e) { setViewContent(`(could not load: ${e?.detail || e?.message || 'error'})`) }
    finally { setViewLoading(false) }
  }

  const remove = async (name) => {
    if (!confirm(`Delete "${name}"? This removes it for everyone at this layer.`)) return
    setBusy(true); setError(null)
    try {
      await api.del(`${customBase}/${encodeURIComponent(name)}`)
      if (selected?.name === name) { setSelected(null); setViewContent('') }
      await loadCustom()
    } catch (e) { setError(e?.detail || e?.message || 'Delete failed') }
    finally { setBusy(false) }
  }

  // Inherited item is ENABLED when it is NOT in the disabled set.
  const toggleInherited = async (item, nextEnabled) => {
    if (!disabledPath || !canToggle) return
    const next = new Set(disabled)
    if (nextEnabled) next.delete(item.name); else next.add(item.name)
    const prev = disabled
    setDisabled(next); setBusy(true); setError(null)
    try {
      await api.put(disabledPath, { disabled_items: Array.from(next) })
    } catch (e) { setError(e?.detail || e?.message || 'Save failed'); setDisabled(prev) }
    finally { setBusy(false) }
  }

  const doUpload = async (payload) => {
    setBusy(true); setError(null)
    try {
      await api.post(customBase, payload)
      setShowUpload(false)
      await loadCustom()
    } catch (e) { setError(e?.detail || e?.message || 'Upload failed'); throw e }
    finally { setBusy(false) }
  }

  // ── search filter ──
  const q = search.trim().toLowerCase()
  const match = (...fields) => !q || fields.some(f => String(f || '').toLowerCase().includes(q))
  const fBuiltin = builtin.filter(it => match(it.title, it.description))
  const fCustom = custom.filter(it => match(it.name))
  const fInherited = inherited.filter(it => match(it.name))
  const nothing = q && fBuiltin.length === 0 && fCustom.length === 0 && fInherited.length === 0

  const SectionLabel = ({ children, count }) => (
    <div className="text-[11px] font-semibold text-fg-3 uppercase tracking-wide">
      {children}{typeof count === 'number' ? ` (${count})` : ''}
    </div>
  )
  const Row = ({ children, active, onClick }) => (
    <div onClick={onClick}
      className={`group flex items-center gap-2 px-2.5 py-2 rounded-md cursor-pointer transition-colors ${
        active ? 'bg-primary-subtle' : 'hover:bg-surface-3'
      }`}>{children}</div>
  )
  const TypeBadge = ({ t }) => (
    <span className="text-[10px] uppercase tracking-wide text-fg-muted flex-shrink-0">{t === 'pdf' ? 'PDF' : 'MD'}</span>
  )

  return (
    <div className="space-y-4">
      {subtitle && <p className="text-xs text-fg-3">{subtitle}</p>}
      {error && <div className="text-xs text-err-text bg-err-bg border border-err-border rounded-md px-3 py-2">{error}</div>}

      <div className="flex flex-col lg:flex-row gap-4 items-stretch">
        {/* ── Master list (narrow) ── */}
        <div className="w-full lg:w-80 flex-shrink-0 bg-surface rounded-lg border border-line flex flex-col">
          {/* Search + add */}
          <div className="p-2 border-b border-line-2 flex items-center gap-2">
            <div className="relative flex-1">
              <Icon path={ICON.search} className="w-3.5 h-3.5 text-fg-muted absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search knowledge…"
                className="w-full pl-8 pr-2 py-1.5 text-xs rounded-md border border-line bg-surface-2 text-fg-2 outline-none focus:border-primary" />
            </div>
            {canWrite && (
              <button onClick={() => setShowUpload(true)} disabled={busy} title="Add document"
                className="flex-shrink-0 px-2 py-1.5 text-xs font-medium text-primary-fg bg-primary rounded-md hover:bg-primary-hover disabled:opacity-50">
                + Add
              </button>
            )}
          </div>

          <div className="p-2 space-y-4 overflow-y-auto max-h-[560px]">
            {nothing && <p className="text-xs text-fg-muted px-2 py-4 text-center">No matches for “{search}”.</p>}

            {/* Built-in (locked) */}
            {showBuiltin && fBuiltin.length > 0 && (
              <div>
                <div className="flex items-center gap-1.5 px-1 mb-1.5">
                  <Icon path={ICON.lock} className="w-3.5 h-3.5 text-fg-muted" />
                  <SectionLabel count={fBuiltin.length}>Built-in</SectionLabel>
                </div>
                <div className="space-y-0.5">
                  {fBuiltin.map(it => (
                    <Row key={it.id} active={selected?.source === 'builtin' && selected?.id === it.id}
                      onClick={() => view({ name: it.title, id: it.id, source: 'builtin', description: it.description })}>
                      <Icon path={ICON.doc} className="w-4 h-4 text-fg-muted flex-shrink-0" />
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm text-fg truncate">{it.title}</span>
                        {it.description && <span className="block text-[11px] text-fg-muted truncate">{it.description}</span>}
                      </span>
                      <Toggle checked disabled size="sm" title="Always on (built-in)" />
                    </Row>
                  ))}
                </div>
              </div>
            )}

            {/* Custom (this layer) */}
            {(fCustom.length > 0 || !q) && (
              <div>
                <div className="px-1 mb-1.5"><SectionLabel count={custom.length}>Custom · {scopeLabel}</SectionLabel></div>
                {loading ? (
                  <p className="text-xs text-fg-muted px-2 py-1">Loading…</p>
                ) : fCustom.length === 0 ? (
                  <p className="text-xs text-fg-muted px-2">{q ? 'No matches.' : 'None yet.'}</p>
                ) : (
                  <div className="space-y-0.5">
                    {fCustom.map(it => (
                      <Row key={it.name} active={selected?.name === it.name && selected?.source === 'custom'}
                        onClick={() => view({ name: it.name, source: 'custom' })}>
                        <Icon path={it.type === 'pdf' ? ICON.pdf : ICON.doc} className="w-4 h-4 text-fg-muted flex-shrink-0" />
                        <span className="text-sm text-fg-2 truncate flex-1">{it.name}</span>
                        <TypeBadge t={it.type} />
                        {canWrite && (
                          <button onClick={e => { e.stopPropagation(); remove(it.name) }} disabled={busy}
                            className="text-fg-muted hover:text-err opacity-0 group-hover:opacity-100 transition-all flex-shrink-0">
                            <Icon path="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </Row>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Inherited (toggle off) */}
            {((fInherited.length > 0) || (canToggle && !q)) && (
              <div>
                <div className="px-1"><SectionLabel count={inherited.length}>Inherited{inheritedLabel ? ` · ${inheritedLabel}` : ''}</SectionLabel></div>
                {canToggle && inherited.length > 0 && (
                  <p className="text-[11px] text-fg-muted px-1 mt-0.5 mb-1">Toggle off to disable one here.</p>
                )}
                {fInherited.length === 0 ? (
                  <p className="text-xs text-fg-muted px-2 mt-1">{q ? 'No matches.' : 'Nothing inherited.'}</p>
                ) : (
                  <div className="space-y-0.5 mt-1">
                    {fInherited.map(it => (
                      <Row key={`${it.source}:${it.name}`} active={selected?.name === it.name && selected?.source !== 'custom' && selected?.source !== 'builtin'}
                        onClick={() => view({ name: it.name, source: it.source })}>
                        <Icon path={it.type === 'pdf' ? ICON.pdf : ICON.doc} className="w-4 h-4 text-fg-muted flex-shrink-0" />
                        <span className={`text-sm truncate flex-1 ${disabled.has(it.name) ? 'text-fg-muted line-through' : 'text-fg-2'}`}>{it.name}</span>
                        <span className="text-[10px] text-fg-muted bg-surface-3 rounded px-1.5 py-0.5 flex-shrink-0">{it.source}</span>
                        {canToggle && (
                          <Toggle checked={!disabled.has(it.name)} disabled={busy} size="sm"
                            title={disabled.has(it.name) ? 'Disabled here — toggle to enable' : 'Active — toggle to disable here'}
                            onChange={(next) => toggleInherited(it, next)} />
                        )}
                      </Row>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Preview / detail (broad) ── */}
        <div className="w-full lg:flex-1 min-w-0 bg-surface rounded-lg border border-line p-5 min-h-[420px]">
          {!selected ? (
            <div className="h-full flex flex-col items-center justify-center text-center py-16">
              <Icon path={ICON.doc} className="w-8 h-8 text-fg-muted mb-2" />
              <p className="text-sm text-fg-muted">Select a document to preview it.</p>
            </div>
          ) : selected.source === 'builtin' ? (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <span className="inline-flex items-center gap-1 text-[10px] font-medium text-fg-3 bg-surface-3 rounded px-1.5 py-0.5">
                  <Icon path={ICON.lock} className="w-3 h-3" /> Built-in
                </span>
              </div>
              <h3 className="text-lg font-semibold text-fg">{selected.name}</h3>
              {selected.description && <p className="text-sm text-fg-2 mt-2 leading-relaxed">{selected.description}</p>}
              <div className="mt-5 border-t border-line-2 pt-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[11px] font-semibold text-fg-3 uppercase tracking-wide">
                    Document
                  </span>
                  <span className="text-[11px] text-fg-muted">read-only</span>
                </div>
                {viewLoading ? (
                  <p className="text-xs text-fg-muted px-1 py-2">Loading…</p>
                ) : (
                  <pre className="bg-surface-2 rounded-lg p-4 text-[11px] text-fg-2 overflow-auto max-h-[520px] whitespace-pre-wrap font-mono leading-relaxed">{viewContent}</pre>
                )}
              </div>
            </div>
          ) : (
            <div>
              <div className="flex items-center justify-between gap-2 mb-3">
                <h3 className="text-base font-semibold text-fg truncate">{selected.name}</h3>
                <button onClick={() => { setSelected(null); setViewContent('') }}
                  className="flex-shrink-0 text-xs text-fg-muted hover:text-fg-2">Close</button>
              </div>
              {viewLoading ? (
                <p className="text-xs text-fg-muted">Loading…</p>
              ) : (
                <pre className="bg-surface-2 rounded-lg p-4 text-[11px] text-fg-2 overflow-auto max-h-[520px] whitespace-pre-wrap font-mono leading-relaxed">{viewContent}</pre>
              )}
            </div>
          )}
        </div>
      </div>

      {showUpload && <UploadModal onClose={() => setShowUpload(false)} onSubmit={doUpload} busy={busy} />}
    </div>
  )
}
