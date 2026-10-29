// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Right-hand sheet for long, guided flows (wizards with scripts to copy). Full
// height with a sticky header and footer, so instructions read top to bottom and
// the actions never scroll away; the page behind stays visible for context.
// Escape and a click on the backdrop close it. Short forms should stay modals.
import { useEffect } from 'react'
import { X } from 'lucide-react'

export default function SideSheet({ title, subtitle, header, footer, onClose, children, width = 'max-w-[620px]' }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = prev }
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex justify-end side-sheet-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose?.() }}>
      <div role="dialog" aria-modal="true" aria-label={title}
        className={`side-sheet h-full w-full ${width} bg-surface border-l border-line shadow-2xl flex flex-col`}>
        <div className="px-6 py-4 border-b border-line flex-shrink-0">
          <div className="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <h2 className="text-lg font-semibold text-fg">{title}</h2>
              {subtitle && <p className="text-xs text-fg-3 mt-1">{subtitle}</p>}
            </div>
            <button onClick={onClose} aria-label="Close" className="p-1.5 -mr-1.5 rounded-md text-fg-3 hover:text-fg hover:bg-surface-3 transition-colors">
              <X className="w-4 h-4" />
            </button>
          </div>
          {header}
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto">{children}</div>
        {footer && <div className="px-6 py-4 border-t border-line flex-shrink-0 bg-surface">{footer}</div>}
      </div>
    </div>
  )
}

// Two-step indicator shared by the wizards.
export function StepIndicator({ step, labels }) {
  return (
    <div className="flex items-center gap-4 mt-4">
      {labels.map((label, i) => {
        const n = i + 1
        const state = step === n ? 'current' : step > n ? 'done' : 'todo'
        return (
          <div key={label} className="flex items-center gap-4">
            {i > 0 && <div className="w-8 h-px bg-line-strong" />}
            <div className="flex items-center gap-2">
              <span className={`w-5 h-5 rounded-full text-[10px] font-bold flex items-center justify-center ${
                state === 'current' ? 'bg-primary text-primary-fg' : state === 'done' ? 'bg-ok text-white' : 'bg-surface-active text-fg-3'}`}>
                {state === 'done' ? '✓' : n}
              </span>
              <span className={`text-xs ${state === 'current' ? 'text-primary font-medium' : 'text-fg-3'}`}>{label}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
