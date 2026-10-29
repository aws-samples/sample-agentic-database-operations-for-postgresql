// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'

// Rendered inline in the chat when the orchestrator calls recommend_investigation.
// Chat stays lightweight; this offers an explicit hand-off into a full, persisted
// investigation. Once one is started (or on a rehydrated chat, matched from the
// session's investigations), the card becomes a link with a live status badge so
// completion is reflected back into the conversation.

const STATUS = {
  running: { label: 'Running', cls: 'text-info-text', dot: 'bg-info animate-pulse' },
  completed: { label: 'Completed', cls: 'text-ok-text', dot: 'bg-ok' },
  failed: { label: 'Failed', cls: 'text-err-text', dot: 'bg-err' },
  cancelled: { label: 'Cancelled', cls: 'text-fg-2', dot: 'bg-fg-muted' },
}

// The stored investigation title is "<suggestion title> — <rationale>"[:100], so a
// loose startsWith/contains match links the card to its investigation without an
// exact-equality dependency.
function matchInvestigation(list, suggestion) {
  if (!Array.isArray(list) || !list.length) return null
  const t = (suggestion?.title || '').trim().toLowerCase()
  if (!t) return list[list.length - 1] || null
  return list.find(i => {
    const it = (i.title || '').trim().toLowerCase()
    return it && (it.startsWith(t) || it.includes(t) || t.includes(it))
  }) || null
}

export default function InvestigationSuggestion({ suggestion, onStart, onOpen, linkedInvestigations, databases = [] }) {
  const [dismissed, setDismissed] = useState(false)
  const [startedInv, setStartedInv] = useState(null)
  // The database this investigation will run on. No default on purpose: the user must
  // explicitly pick a target before Start enables, so an investigation can never run
  // against a database the user did not choose. The dropdown lists the space's databases
  // (or this turn's scope) behind a "Select a database…" placeholder.
  const scopeDbs = suggestion.scopeDbs || []
  const options = databases.length ? databases : scopeDbs
  const [dbId, setDbId] = useState('')

  if (dismissed) return null

  // Prefer the refetched session list (current status); fall back to the just-
  // started record before the first refetch lands.
  const linked = matchInvestigation(linkedInvestigations, suggestion) || startedInv
  const status = linked ? (STATUS[linked.status] || STATUS.running) : null

  const handleStart = async () => {
    const chosen = options.find(d => d.instance_id === dbId)
    const inv = await onStart?.(suggestion, chosen ? [chosen] : undefined)
    if (inv?.investigation_id) setStartedInv(inv)
  }

  // Rendered as a "brief" row: gradient icon tile, title + rationale,
  // and the hand-off action on the right.
  return (
    <div className="brief-row my-2 items-start">
      <span className="brief-icon mt-0.5" aria-hidden>
        <svg fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-fg">This looks like a full investigation</div>
        <div className="text-[12px] text-fg-2 mt-0.5">
          {suggestion.title}{suggestion.rationale ? ` — ${suggestion.rationale}` : ''}
        </div>

        {linked ? (
          <div className="mt-2 flex items-center gap-3 flex-wrap">
            <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${status.cls}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${status.dot}`} />
              {status.label}
            </span>
            <button
              onClick={() => onOpen?.(linked.investigation_id, linked.status)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-primary-fg text-[12px] font-medium transition-colors">
              Open investigation
            </button>
          </div>
        ) : (
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {options.length > 0 && (
              <select
                value={dbId}
                onChange={(e) => setDbId(e.target.value)}
                title="Database to investigate"
                className="px-2 py-1.5 rounded-lg border border-line bg-surface text-fg-2 text-[12px] max-w-[200px]">
                <option value="" disabled>Select a database…</option>
                {options.map(d => (
                  <option key={d.instance_id} value={d.instance_id}>{d.display_name || d.instance_id}</option>
                ))}
              </select>
            )}
            <button
              onClick={handleStart}
              disabled={!dbId}
              title={!dbId ? 'Select a database first' : undefined}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-primary-fg text-[12px] font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-primary">
              Start investigation
            </button>
            <button
              onClick={() => setDismissed(true)}
              className="px-3 py-1.5 rounded-lg border border-line text-fg-2 hover:bg-surface-3 text-[12px] font-medium transition-colors">
              Dismiss
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
