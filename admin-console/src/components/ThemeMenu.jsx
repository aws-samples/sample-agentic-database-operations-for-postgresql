// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useEffect } from 'react'

// The glyph for each theme. All are stroke icons that follow `color`, so each
// tints to its accent inside a preview tile and to currentColor in the header.
function ThemeGlyph({ id, className, color }) {
  switch (id) {
    case 'light':
      return (
        <svg className={className} fill="none" stroke={color} strokeWidth={1.8} viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="4" />
          <path strokeLinecap="round" d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5 5l1.6 1.6M17.4 17.4L19 19M19 5l-1.6 1.6M6.6 17.4L5 19" />
        </svg>
      )
    case 'dark':
      return (
        <svg className={className} fill="none" stroke={color} strokeWidth={1.8} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M21 12.8A8.5 8.5 0 1111.2 3a6.5 6.5 0 009.8 9.8z" />
        </svg>
      )
    case 'system':
      return (
        <svg className={className} fill="none" stroke={color} strokeWidth={1.8} viewBox="0 0 24 24">
          <rect x="3" y="4" width="18" height="12.5" rx="1.5" />
          <path strokeLinecap="round" d="M8.5 20.5h7M12 16.5v4" />
        </svg>
      )
    case 'graphite':
      return (
        <svg className={className} fill="none" stroke={color} strokeWidth={1.8} viewBox="0 0 24 24">
          <rect x="4" y="4" width="16" height="16" rx="3.5" />
          <path strokeLinecap="round" d="M8.5 15.5 15.5 8.5" />
        </svg>
      )
    case 'paper':
      return (
        <svg className={className} fill="none" stroke={color} strokeWidth={1.8} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 3h8l4 4v14H6z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 3v4h4M9 12.5h6M9 16h4" />
        </svg>
      )
    default:
      return null
  }
}

// Mini preview of each background pattern for the picker. The ink is boosted
// relative to the real page texture so it still reads inside a small swatch;
// it is color-mixed from --text so it follows the current theme.
function patternSwatch(id) {
  const ink = 'color-mix(in srgb, var(--text) 22%, transparent)'
  if (id === 'lines') return { backgroundImage: `repeating-linear-gradient(45deg, ${ink} 0, ${ink} 1px, transparent 1px, transparent 6px)` }
  if (id === 'dots') return { backgroundImage: `radial-gradient(${ink} 1px, transparent 1.5px)`, backgroundSize: '7px 7px' }
  return {}
}

export default function ThemeMenu({ theme, setTheme, themes, isDark, accent, setAccent, accents, pattern, setPattern, patterns }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    const onEsc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onEsc)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onEsc) }
  }, [])

  const current = accents.find(a => a.id === accent) || accents[0]
  const modeLabel = isDark ? 'dark' : 'light'

  return (
    <div className="relative" ref={ref}>
      {/* Trigger shows the active theme's glyph, so the mode is readable at a glance. */}
      <button onClick={() => setOpen(o => !o)} title="Appearance"
        className="p-1.5 text-fg-3 hover:text-fg hover:bg-surface-3 rounded-md transition-colors">
        <ThemeGlyph id={theme} className="w-5 h-5" color="currentColor" />
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-72 bg-surface rounded-xl border border-line shadow-xl py-1.5 z-50">
          <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-fg-muted">Appearance</div>

          {/* Preview-tile grid. Each preview carries its own data-theme/data-accent, so the
              scoped CSS variables resolve to that theme's real backdrop, surface and accent —
              a true mini preview built from the existing palette, no new colours. System is a
              follow-OS toggle below the grid rather than a tile. */}
          <div className="px-3 pb-1 grid grid-cols-4 gap-1.5" role="radiogroup" aria-label="Theme">
            {themes.filter(t => t.id !== 'system').map(t => {
              const active = theme === t.id
              return (
                <button key={t.id} type="button" role="radio" aria-checked={active} title={t.sub}
                  onClick={() => { setTheme(t.id); setOpen(false) }}
                  className="flex flex-col items-center gap-1 rounded-lg border p-1 transition-colors hover:bg-surface-3"
                  style={active ? { borderColor: 'var(--primary)', background: 'var(--primary-subtle)' } : { borderColor: 'transparent' }}>
                  <span data-theme={t.id} data-accent={accent} aria-hidden
                    className="relative block w-full h-10 rounded-md overflow-hidden border"
                    style={{ background: 'var(--app-bg)', borderColor: 'var(--border-strong)' }}>
                    <span className="absolute inset-x-0 top-0 h-2" style={{ background: 'var(--nav-bg)', borderBottom: '1px solid var(--border)' }} />
                    <span className="absolute left-1.5 bottom-1.5 h-4 w-[52%] rounded-[2px] border" style={{ background: 'var(--surface)', borderColor: 'var(--border-strong)' }} />
                    <span className="absolute right-1.5 bottom-1.5 h-4 w-[28%] rounded-[2px] border" style={{ background: 'var(--surface)', borderColor: 'var(--border-strong)' }} />
                    <span className="absolute right-1.5 top-2.5 h-2 w-2 rounded-full" style={{ background: 'var(--primary)' }} />
                  </span>
                  <span className={`text-[10px] font-medium ${active ? 'text-fg' : 'text-fg-3'}`}>{t.label}</span>
                </button>
              )
            })}
          </div>

          {/* Follow system (OS light/dark) */}
          <button type="button" onClick={() => { setTheme('system'); setOpen(false) }} aria-pressed={theme === 'system'}
            className={`mt-0.5 w-full flex items-center justify-between px-3 py-2 hover:bg-surface-3 transition-colors ${theme === 'system' ? 'bg-surface-2' : ''}`}>
            <span className="inline-flex items-center gap-2 text-xs text-fg">
              <ThemeGlyph id="system" className="w-4 h-4" color="currentColor" /> Follow system
            </span>
            {theme === 'system' && (
              <svg className="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
            )}
          </button>

          {/* Accent: one dual-tone pair per mode. The row edits the accent for
              the mode currently showing; switch modes to set the other one. */}
          <div className="mt-1.5 pt-1.5 border-t border-line-2">
            <div className="px-3 py-1.5 flex items-baseline justify-between">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted">Accent</span>
              <span className="text-[10px] text-fg-3">{modeLabel} mode · {current.label}</span>
            </div>
            <div role="radiogroup" aria-label={`Accent for ${modeLabel} mode`} className="px-3 pb-1.5 flex items-center gap-3">
              {accents.map(a => {
                const selected = a.id === accent
                return (
                  <button key={a.id} type="button" role="radio" aria-checked={selected} aria-label={a.label} title={a.label}
                    onClick={() => setAccent(a.id)}
                    className={`w-7 h-7 rounded-full flex-shrink-0 transition-transform hover:scale-110 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-surface ${selected ? 'ring-2 ring-primary ring-offset-2 ring-offset-surface' : ''}`}
                    style={{ backgroundImage: `linear-gradient(135deg, ${a.swatch[0]} 0%, ${a.swatch[1]} 100%)` }}>
                    {selected && (
                      <svg className="w-4 h-4 mx-auto text-white drop-shadow" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24" aria-hidden>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                      </svg>
                    )}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Pattern: an orthogonal background texture that composes with any
              appearance and accent. Each swatch previews its own texture. */}
          {patterns && patterns.length > 0 && (
            <div className="mt-1.5 pt-1.5 border-t border-line-2">
              <div className="px-3 py-1.5 flex items-baseline justify-between">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted">Pattern</span>
                <span className="text-[10px] text-fg-3">{(patterns.find(p => p.id === pattern) || patterns[0]).label}</span>
              </div>
              <div role="radiogroup" aria-label="Background pattern" className="px-3 pb-1.5 grid grid-cols-3 gap-1.5">
                {patterns.map(p => {
                  const active = pattern === p.id
                  return (
                    <button key={p.id} type="button" role="radio" aria-checked={active} title={p.sub}
                      onClick={() => setPattern(p.id)}
                      className="flex flex-col items-center gap-1 rounded-lg border p-1 transition-colors hover:bg-surface-3"
                      style={active ? { borderColor: 'var(--primary)', background: 'var(--primary-subtle)' } : { borderColor: 'transparent' }}>
                      <span aria-hidden className="relative block w-full h-7 rounded-md overflow-hidden border"
                        style={{ background: 'var(--surface-2)', borderColor: 'var(--border-strong)', ...patternSwatch(p.id) }} />
                      <span className={`text-[10px] font-medium ${active ? 'text-fg' : 'text-fg-3'}`}>{p.label}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
