// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import FollowUpPills from './FollowUpPills'

// The hand-off back into a chat when the investigation it started has finished
// (App appends it as an assistant turn; see buildHandoffMessage). Same "brief"
// row as the suggestion card it answers, with the outcome, an "Open
// investigation" action and pills that discuss the result right here — the chat
// puts the investigation in context for those, so the answer is grounded in its
// stored findings rather than a fresh diagnosis.

const OUTCOME = {
  completed: { label: 'Investigation complete', cls: 'text-ok-text', dot: 'bg-ok' },
  failed: { label: 'Investigation failed', cls: 'text-err-text', dot: 'bg-err' },
  cancelled: { label: 'Investigation cancelled', cls: 'text-fg-2', dot: 'bg-fg-muted' },
}
const SEVERITY = {
  critical: 'bg-err-bg-2 text-err',
  warning: 'bg-warn-bg-2 text-warn-mid',
  info: 'bg-info-bg-2 text-info',
}
export const HANDOFF_PILLS = ['Summarize the investigation here', 'What should I fix first?']

export default function InvestigationHandoff({ investigation, onOpen, onPick, disabled }) {
  if (!investigation) return null
  const outcome = OUTCOME[investigation.status] || OUTCOME.completed
  const done = investigation.status === 'completed'
  return (
    <div className="my-2">
      <div className="brief-row items-start">
        <span className="brief-icon mt-0.5" aria-hidden>
          <svg fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`inline-flex items-center gap-1.5 text-[13px] font-medium ${outcome.cls}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${outcome.dot}`} />
              {outcome.label}
            </span>
            {done && investigation.severity && (
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-bold uppercase ${SEVERITY[investigation.severity] || SEVERITY.info}`}>
                {investigation.severity}
              </span>
            )}
            {done && (
              <span className="text-[11px] text-fg-3">{investigation.findings} finding{investigation.findings === 1 ? '' : 's'}</span>
            )}
          </div>
          {/* The title is in the text line above this block; not repeated here. */}
          <div className="mt-2">
            <button onClick={() => onOpen?.(investigation.investigation_id, investigation.status)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-primary-fg text-[12px] font-medium transition-colors">
              Open investigation
            </button>
          </div>
        </div>
      </div>
      {done && (
        <div className="mt-2">
          <FollowUpPills items={HANDOFF_PILLS} kind="questions" disabled={disabled}
            onPick={(label) => onPick?.(investigation, label)} />
        </div>
      )}
    </div>
  )
}
