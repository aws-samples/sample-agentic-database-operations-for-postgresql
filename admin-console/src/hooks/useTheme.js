// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback } from 'react'

const STORAGE_KEY = 'dbops-theme'
const ACCENT_STORAGE_KEY = 'dbops-accent'
const PATTERN_STORAGE_KEY = 'dbops-pattern'

// The available themes. Adding a new one = add an entry here + a [data-theme]
// block in index.css. Nothing else needs to change.
export const THEMES = [
  { id: 'system', label: 'System', sub: 'Follow OS setting' },
  { id: 'light', label: 'Light', sub: 'Always light' },
  { id: 'dark', label: 'Dark', sub: 'Always dark' },
  { id: 'graphite', label: 'Graphite', sub: 'Charcoal dark' },
  { id: 'paper', label: 'Paper', sub: 'Warm light' },
]

// The dual-tone accents, chosen separately for light and dark. `swatch` is the
// two-stop brand pair (logo tile, avatars, sparkle) that the picker paints on
// its circles; `primary` is the solid interactive colour per mode, which the
// picker uses to tint the theme tiles. The tokens themselves live in the
// [data-accent] blocks of index.css — these values mirror them so the menu can
// preview an accent before it is applied. Teal is the default and needs no
// CSS block of its own.
export const ACCENTS = [
  { id: 'teal', label: 'Teal', swatch: ['#14b8a6', '#6366f1'], primary: { light: '#0f766e', dark: '#2dd4bf' } },
  { id: 'blue', label: 'Blue', swatch: ['#0ea5e9', '#1d4ed8'], primary: { light: '#1d4ed8', dark: '#60a5fa' } },
  { id: 'plum', label: 'Plum', swatch: ['#d946ef', '#f43f5e'], primary: { light: '#a21caf', dark: '#e879f9' } },
]

// Background patterns — a third, orthogonal axis that composes with ANY theme and
// accent. Plain is the default (no texture); Lines is a faint diagonal hatch, Dots
// a faint dot grid. The texture ink is color-mixed from the current --text (see the
// [data-pattern] blocks in index.css), so one pattern reads correctly on every
// appearance. Stored as a single global choice, not per scheme.
export const PATTERNS = [
  { id: 'plain', label: 'Plain', sub: 'No texture' },
  { id: 'lines', label: 'Lines', sub: 'Diagonal hatch' },
  { id: 'dots', label: 'Dots', sub: 'Dot grid' },
]

const VALID = new Set(THEMES.map(t => t.id))
const VALID_ACCENTS = new Set(ACCENTS.map(a => a.id))
const VALID_PATTERNS = new Set(PATTERNS.map(p => p.id))
// Dark-scheme themes. Graphite is the dark palette on a charcoal background, so
// it belongs here alongside Dark; Light and Paper are the light-scheme themes.
const DARK_THEMES = new Set(['dark', 'graphite'])
const DEFAULT_THEME = 'light'
const DEFAULT_ACCENT = 'teal'
const DEFAULT_PATTERN = 'plain'

// A theme's scheme is the light/dark half it belongs to. Accents are stored and
// applied per scheme ({light, dark}), not per theme id, so a dark-family theme
// like Graphite shares the dark accent and Paper shares the light accent.
const schemeOf = (resolved) => (DARK_THEMES.has(resolved) ? 'dark' : 'light')

// Stored ids from themes not in the current set map onto their light/dark sibling.
const LEGACY = { 'violet-light': 'light', 'violet-dark': 'dark' }

function readStoredChoice() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (!stored) return DEFAULT_THEME
    if (LEGACY[stored]) return LEGACY[stored]
    return VALID.has(stored) ? stored : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

// One accent per mode, stored together as {light, dark}. Anything unreadable or
// unknown falls back to the default for that mode.
function readStoredAccents() {
  const fallback = { light: DEFAULT_ACCENT, dark: DEFAULT_ACCENT }
  try {
    const stored = JSON.parse(localStorage.getItem(ACCENT_STORAGE_KEY) || 'null')
    if (!stored || typeof stored !== 'object') return fallback
    return {
      light: VALID_ACCENTS.has(stored.light) ? stored.light : DEFAULT_ACCENT,
      dark: VALID_ACCENTS.has(stored.dark) ? stored.dark : DEFAULT_ACCENT,
    }
  } catch {
    return fallback
  }
}

// The background pattern is a single global choice. Anything unknown or
// unreadable falls back to Plain.
function readStoredPattern() {
  try {
    const stored = localStorage.getItem(PATTERN_STORAGE_KEY)
    return VALID_PATTERNS.has(stored) ? stored : DEFAULT_PATTERN
  } catch {
    return DEFAULT_PATTERN
  }
}

function systemPrefersDark() {
  return typeof window !== 'undefined' && window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches
}

// Resolve a user choice (which may be "system") to a concrete theme id.
function resolveTheme(choice) {
  if (choice === 'system') return systemPrefersDark() ? 'dark' : 'light'
  return choice
}

export default function useTheme() {
  const [choice, setChoice] = useState(readStoredChoice)
  const [accents, setAccents] = useState(readStoredAccents)
  const [pattern, setPatternState] = useState(readStoredPattern)

  useEffect(() => {
    const apply = () => {
      const resolved = resolveTheme(choice)
      const root = document.documentElement
      root.setAttribute('data-theme', resolved)
      // The accent follows the scheme that is actually showing, so a "system"
      // user gets their light accent by day and their dark accent by night, and
      // Graphite/Paper reuse the dark/light accent respectively.
      root.setAttribute('data-accent', accents[schemeOf(resolved)])
      // The pattern is orthogonal to the theme, so it is applied as-is.
      root.setAttribute('data-pattern', pattern)
      // Keep the `dark` class in sync so Tailwind `dark:` variants still work.
      root.classList.toggle('dark', DARK_THEMES.has(resolved))
    }
    apply()
    try {
      localStorage.setItem(STORAGE_KEY, choice)
      localStorage.setItem(ACCENT_STORAGE_KEY, JSON.stringify(accents))
      localStorage.setItem(PATTERN_STORAGE_KEY, pattern)
    } catch {}

    // When following the OS, react to OS light/dark changes live.
    if (choice === 'system' && window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)')
      const handler = () => apply()
      mq.addEventListener('change', handler)
      return () => mq.removeEventListener('change', handler)
    }
  }, [choice, accents, pattern])

  const setTheme = useCallback((id) => setChoice(VALID.has(id) ? id : DEFAULT_THEME), [])
  const resolvedTheme = resolveTheme(choice)
  const isDark = DARK_THEMES.has(resolvedTheme)
  // Back-compat helper: a simple light/dark flip.
  const toggle = useCallback(() => setChoice(c => (DARK_THEMES.has(resolveTheme(c)) ? 'light' : 'dark')), [])

  // The accent shown now, and a setter that changes it for the current scheme
  // only. The scheme is resolved at call time so a "system" user who picks an
  // accent after the OS flipped still lands it on the mode they are looking at.
  const accent = accents[schemeOf(resolvedTheme)]
  const setAccent = useCallback((id) => {
    if (!VALID_ACCENTS.has(id)) return
    setAccents(a => ({ ...a, [schemeOf(resolveTheme(choice))]: id }))
  }, [choice])

  const setPattern = useCallback((id) => setPatternState(VALID_PATTERNS.has(id) ? id : DEFAULT_PATTERN), [])

  return {
    theme: choice, resolvedTheme, isDark, setTheme, toggle, themes: THEMES,
    accent, setAccent, accents: ACCENTS,
    pattern, setPattern, patterns: PATTERNS,
  }
}
