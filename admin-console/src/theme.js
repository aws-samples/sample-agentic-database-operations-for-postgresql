// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Colours for code that paints outside Tailwind classes (inline styles, SVG
// charts). Semantic colours resolve to the theme variables in index.css so
// they follow light/dark; the categorical series palette is fixed and chosen
// to read on both themes.

export const COLOR = {
  primary: 'var(--primary)',
  text: 'var(--text)',
  muted: 'var(--text-muted)',
  surface3: 'var(--surface-3)',
  ok: 'var(--ok-fg)',
  warn: 'var(--warn-fg)',
  err: 'var(--err-fg)',
  info: 'var(--info-fg)',
}

// Health score -> colour (dots, heat cells, sparklines).
export const HEALTH_COLORS = { critical: COLOR.err, warning: COLOR.warn, ok: COLOR.ok, unknown: COLOR.muted }

// Multi-series chart palette.
export const SERIES_COLORS = ['#4338ca', '#0891b2', '#7c3aed', '#db2777', '#d97706', '#059669', '#dc2626', '#64748b']

// SVG <defs> ids must be plain identifiers; derive one from any colour string.
export const colorId = (c) => String(c).replace(/[^a-zA-Z0-9]/g, '')
