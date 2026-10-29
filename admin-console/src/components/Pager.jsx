// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Numbered page controls, the same on every records table in both apps: ‹ 1 … 4 [5] 6 … 29 ›.
// Long runs of pages collapse to the current page's neighbours plus the first and
// last, so the strip stays one line however deep the history. `pageCount` is the
// number of pages known so far; `open` marks that older rows exist beyond them,
// and renders a trailing "…" button that asks the host for more.
import { ChevronLeft, ChevronRight } from 'lucide-react'

const BTN = 'w-6 h-6 rounded text-[10px] font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed'

// Which page numbers to show: 1, the last, and a window of one either side of
// the current page. Gaps become a single "…".
export function pageWindow(page, pageCount) {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, i) => i + 1)
  const keep = new Set([1, pageCount, page - 1, page, page + 1])
  if (page <= 3) [2, 3, 4].forEach(p => keep.add(p))
  if (page >= pageCount - 2) [pageCount - 3, pageCount - 2, pageCount - 1].forEach(p => keep.add(p))
  const out = []
  let prev = 0
  for (const p of [...keep].filter(p => p >= 1 && p <= pageCount).sort((a, b) => a - b)) {
    if (p - prev > 1) out.push('…')
    out.push(p)
    prev = p
  }
  return out
}

export default function Pager({ page, pageCount, onPage, open = false, onMore, loading = false, label = 'Pages' }) {
  if (pageCount <= 1 && !open) return null
  const last = Math.max(pageCount, 1)
  return (
    <nav aria-label={label} className="flex items-center gap-0.5">
      <button onClick={() => onPage(page - 1)} disabled={page <= 1 || loading} aria-label="Previous page"
        className={`${BTN} text-fg-3 hover:bg-surface-3 flex items-center justify-center`}>
        <ChevronLeft size={13} strokeWidth={2} />
      </button>
      {pageWindow(page, last).map((p, i) => p === '…'
        ? <span key={`gap-${i}`} className="w-6 text-center text-[10px] text-fg-muted select-none" aria-hidden>…</span>
        : (
          <button key={p} onClick={() => onPage(p)} disabled={loading} aria-current={p === page ? 'page' : undefined}
            className={`${BTN} ${p === page ? 'bg-primary text-primary-fg' : 'text-fg-3 hover:bg-surface-3'}`}>
            {p}
          </button>
        ))}
      {open && (
        <button onClick={onMore} disabled={loading} title="Load older rows" aria-label="Load older rows"
          className={`${BTN} text-fg-3 hover:bg-surface-3`}>…</button>
      )}
      <button onClick={() => (page < last ? onPage(page + 1) : onMore?.())} disabled={loading || (page >= last && !open)} aria-label="Next page"
        className={`${BTN} text-fg-3 hover:bg-surface-3 flex items-center justify-center`}>
        <ChevronRight size={13} strokeWidth={2} />
      </button>
    </nav>
  )
}
