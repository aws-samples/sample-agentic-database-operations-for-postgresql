// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useEffect } from 'react'

// The composer card, three rows:
//   1. header  — the scope picker
//   2. prompt  — an auto-growing textarea (Enter sends, Shift+Enter breaks)
//   3. toolbar — model picker + send, on the right
// `large` is the home-screen variant (taller resting height); the frame itself
// is identical so the card looks the same wherever it sits (see .composer-frame).
// `busyLabel` replaces the disabled placeholder when the pause has a reason other
// than the agent thinking (an investigation the chat is waiting on).
const MAX_HEIGHT = 160

export default function ChatInput({ onSend, disabled, large, modelPicker, scopeControl, placeholder, busyLabel }) {
  const [input, setInput] = useState('')
  const textareaRef = useRef(null)

  // Grow with the content up to MAX_HEIGHT, then scroll inside.
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
    el.style.overflowY = el.scrollHeight > MAX_HEIGHT ? 'auto' : 'hidden'
  }, [input, large])

  const handleSubmit = (e) => {
    e?.preventDefault?.()
    if (!input.trim() || disabled) return
    onSend(input)
    setInput('')
  }

  return (
    <form onSubmit={handleSubmit} className={`composer-frame flex flex-col gap-2 ${large ? 'px-4 pt-3.5 pb-3' : 'px-4 pt-3 pb-2.5'}`}>
      {scopeControl && (
        <div className="flex items-center justify-between gap-2 min-w-0">
          <div className="min-w-0 flex-1">{scopeControl}</div>
        </div>
      )}

      <textarea
        ref={textareaRef}
        rows={large ? 2 : 1}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(e) } }}
        disabled={disabled}
        placeholder={disabled ? (busyLabel || 'Agent is thinking…') : (placeholder || 'Ask about your databases…')}
        aria-label="Message"
        className="chat-composer-input w-full resize-none text-[15px] leading-[22px] text-fg placeholder-fg-muted focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed px-0 py-0.5"
      />

      <div className="flex items-center justify-end gap-2">
        {/* Model picker + send. (Attach/voice arrive here when they exist.) */}
        <div className="flex items-center gap-2 min-w-0">
          {modelPicker}
          <button type="submit" disabled={disabled || !input.trim()} title="Send"
            className="flex-shrink-0 w-8 h-8 bg-primary hover:bg-primary-hover text-primary-fg rounded-lg flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2.2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
      </div>
    </form>
  )
}
