// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// The four-point "AI sparkle" filled with the brand gradient. The stops read the
// same --brand-a / --brand-b tokens as .bg-brand-gradient, so the theme file is
// the single place the brand colours live. Each instance gets its own gradient
// id so several can share a page.
let seq = 0

export default function SparkleMark({ className = 'w-8 h-8', title }) {
  const id = `sparkle-g-${++seq}`
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true} role={title ? 'img' : undefined}>
      {title && <title>{title}</title>}
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" style={{ stopColor: 'var(--brand-a)' }} />
          <stop offset="1" style={{ stopColor: 'var(--brand-b)' }} />
        </linearGradient>
      </defs>
      <path fill={`url(#${id})`} d="M12 2.5c.35 3.9 1.6 6.6 3.7 8.1 1.4 1 3.2 1.4 5.8 1.4-3.9.35-6.6 1.6-8.1 3.7-1 1.4-1.4 3.2-1.4 5.8-.35-3.9-1.6-6.6-3.7-8.1-1.4-1-3.2-1.4-5.8-1.4 3.9-.35 6.6-1.6 8.1-3.7 1-1.4 1.4-3.2 1.4-5.8z" />
    </svg>
  )
}
