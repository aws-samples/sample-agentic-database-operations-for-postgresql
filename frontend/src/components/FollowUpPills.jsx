// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Pills under the agent's latest answer. Items arrive on the stream as a
// `suggestions` event after `result` and are stored on the assistant message as
// `followups` (+ `followupsKind`), so they survive a rehydrate.
//   kind = 'questions' — follow-up questions the operator might ask next (arrow).
//   kind = 'replies'   — after a clarifying turn: answers the operator could give
//                        to the agent's question (reply glyph), so a click answers
//                        the agent rather than echoing its question back.
export default function FollowUpPills({ items, kind = 'questions', onPick, disabled }) {
  if (!Array.isArray(items) || items.length === 0) return null
  const replies = kind === 'replies'
  return (
    <div className="suggestion-pills flex flex-wrap gap-2 pl-1 pr-4" aria-label={replies ? 'Suggested replies' : 'Suggested follow-ups'}>
      {items.slice(0, 4).map((q, i) => (
        <button key={`${i}-${q}`} type="button" className="suggestion-pill" onClick={() => onPick?.(q)} disabled={disabled}
          title={replies ? 'Send this reply' : 'Ask this next'}>
          {replies ? (
            <svg fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 14l-4-4 4-4M5 10h9a5 5 0 015 5v3" />
            </svg>
          ) : (
            <svg fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14M13 6l6 6-6 6" />
            </svg>
          )}
          <span>{q}</span>
        </button>
      ))}
    </div>
  )
}
