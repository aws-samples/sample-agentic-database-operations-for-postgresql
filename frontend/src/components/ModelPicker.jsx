// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import useApi from '../hooks/useApi'

// In-chat model picker (model-and-knowledge-governance). Lists the models the
// current space has enabled and lets the operator pick one for the next message
// (cost vs. capability on the fly). Hidden when the space has no enabled models
// or no space is scoped — resolution then uses the space/account default.
//
// Custom (non-native) dropdown so the panel matches the app theme in every mode
// and can show friendly display names + a cost hint instead of raw ids.

// global.anthropic.claude-haiku-5-5 -> "Claude Haiku 5.5"
function displayName(id) {
  const raw = String(id || '')
  const s = raw
    .replace(/^(global|us|eu|apac)\./, '')     // region prefix
    .replace(/^anthropic\.claude-/, '')         // provider/family prefix
    .replace(/-\d{8}-v\d+:\d+$/, '')            // -20251001-v1:0
    .replace(/-v\d+(:\d+)?$/, '')               // -v1 / -v1:0
    .replace(/:\d+$/, '')                       // trailing :0
  const parts = s.split('-').filter(Boolean)
  if (parts.length === 0) return raw
  const FAMILY = { haiku: 'Haiku', sonnet: 'Sonnet', opus: 'Opus' }
  const family = parts[0]
  const nums = parts.slice(1).filter(p => /^\d+$/.test(p))
  if (FAMILY[family]) return `Claude ${FAMILY[family]}${nums.length ? ' ' + nums.join('.') : ''}`
  return parts.map(p => p.charAt(0).toUpperCase() + p.slice(1)).join(' ')
}

// cost_tier -> compact price hint ($/$$/$$$)
const COST = {
  low: { dollars: '$', cls: 'text-ok', title: 'Lower cost' },
  mid: { dollars: '$$', cls: 'text-fg-3', title: 'Standard cost' },
  high: { dollars: '$$$', cls: 'text-warn-mid', title: 'Higher cost' },
}

function BoltIcon({ className = 'w-3.5 h-3.5' }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
    </svg>
  )
}

export default function ModelPicker({ api: apiProp, getValidAccessToken, accessToken, value, onChange }) {
  // Prefer the app's shared api: it carries the forced-refresh 401 retry, so a models
  // fetch that fires on a first sign-in before a token is ready retries with a fresh
  // token instead of failing silently and leaving the picker blank until a hard refresh.
  // A private client (no retry) is only a fallback for a caller that passes no api.
  const ownApi = useApi(getValidAccessToken)
  const api = apiProp || ownApi
  const [list, setList] = useState([])            // [{ id, cost_tier, recommended }]
  const [defaultModel, setDefaultModel] = useState('')
  const [open, setOpen] = useState(false)
  const spaceId = new URLSearchParams(window.location.search).get('space_id') || ''

  useEffect(() => {
    // Wait for authentication before fetching. On a first sign-in the component mounts
    // before a token exists; firing then went out unauthenticated, failed, and never
    // re-ran, so the picker stayed blank until a hard refresh. accessToken transitions
    // from empty to the signed-in token once auth resolves, which re-runs this effect and
    // loads the list — the same "fetch after sign-in" pattern used for the database list.
    if (!spaceId || !accessToken) return
    let alive = true
    api.get(`/api/spaces/${spaceId}/models`)
      .then(d => {
        if (!alive) return
        const meta = Object.fromEntries((d.available || []).map(m => [m.model_id, m]))
        const merged = (d.enabled_models || []).map(id => ({
          id,
          cost_tier: meta[id]?.cost_tier || '',
          recommended: !!meta[id]?.recommended,
        }))
        setList(merged)
        // The model a message with no explicit pick will actually run. The API
        // resolves this with the same code path the agent uses, so prefer it over
        // re-deriving the cascade here. default_model / inherited_default are kept
        // as a fallback for an older API that predates effective_default.
        setDefaultModel(d.effective_default || d.default_model || d.inherited_default || '')
      })
      .catch(() => { if (alive) setList([]) })
    return () => { alive = false }
  }, [spaceId, accessToken]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!spaceId || list.length === 0) return null

  // No `|| list[0].id`. Falling back to the first enabled model claimed a model the
  // backend had not chosen, so the label showed one model while the agent ran
  // another — and since the list is sorted by id, the winner was whichever name
  // sorted first. If the API reports no default, show none and let the backend decide.
  const current = value || defaultModel

  return (
    <div className="relative">
      <button type="button" onClick={() => setOpen(o => !o)}
        title="Model for the next message"
        className="flex items-center gap-1 pl-1.5 pr-1 py-1 rounded-md text-[13px]
                   text-fg-2 hover:bg-surface-3 transition-colors max-w-[180px]">
        <BoltIcon className="w-3.5 h-3.5 flex-shrink-0 text-fg-muted" />
        <span className="truncate">{current ? displayName(current) : 'Model'}</span>
        <svg className={`w-3 h-3 flex-shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute z-50 bottom-full right-0 mb-2 w-72 max-h-72 overflow-y-auto
                          rounded-lg border border-line bg-surface
                          shadow-lg py-1">
            <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-fg-muted">
              Model for the next message
            </div>
            {list.map(m => {
              const selected = m.id === current
              const cost = COST[m.cost_tier]
              return (
                <button key={m.id} type="button"
                  onClick={() => { onChange(m.id); setOpen(false) }}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left transition-colors ${
                    selected ? 'bg-primary-subtle' : 'hover:bg-surface-3'
                  }`}>
                  <span className={`flex-shrink-0 ${selected ? 'text-primary' : 'text-transparent'}`}>
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className="block text-sm text-fg truncate">{displayName(m.id)}</span>
                  </span>
                  {m.recommended && (
                    <span className="flex-shrink-0 text-[10px] font-semibold uppercase tracking-wide text-primary bg-primary-subtle rounded px-1.5 py-0.5">
                      Rec
                    </span>
                  )}
                  {cost && (
                    <span className={`flex-shrink-0 text-[11px] font-semibold ${cost.cls}`} title={cost.title}>{cost.dollars}</span>
                  )}
                </button>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
