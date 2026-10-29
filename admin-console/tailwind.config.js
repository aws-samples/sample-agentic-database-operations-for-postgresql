// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Every colour utility resolves to a CSS variable declared per theme in
// src/index.css, so components carry semantic names (bg-surface, text-fg-3,
// border-line, bg-warn-bg) and re-skin by editing the token blocks alone.
// These are plain var() colours: Tailwind opacity modifiers (bg-surface/50)
// do not apply — use the explicit wash tokens (bg-primary-subtle, bg-err-bg…).
const v = (name) => `var(--${name})`

export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      // Nunito Sans is self-hosted (public/fonts, @font-face in src/index.css);
      // the system chain covers glyphs outside its latin subset.
      fontFamily: {
        sans: ['Nunito Sans', 'Helvetica Neue', 'Segoe UI', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['SF Mono', 'Monaco', 'Menlo', 'Consolas', 'monospace'],
      },
      colors: {
        canvas: v('app-bg'),
        nav: v('nav-bg'),
        surface: { DEFAULT: v('surface'), 2: v('surface-2'), 3: v('surface-3'), active: v('surface-active'), code: v('code-bg') },
        fg: { DEFAULT: v('text'), 2: v('text-2'), 3: v('text-3'), muted: v('text-muted'), code: v('code-text') },
        line: { DEFAULT: v('border'), 2: v('border-2'), strong: v('border-strong') },
        primary: {
          DEFAULT: v('primary'), hover: v('primary-hover'), fg: v('primary-fg'), light: v('primary-light'),
          wash: v('primary-wash'), subtle: v('primary-subtle'), line: v('primary-border'),
        },
        bubble: { DEFAULT: v('user-bubble'), fg: v('user-bubble-text') },
        warn: { DEFAULT: v('warn-fg'), mid: v('warn-fg-2'), inverse: v('warn-inverse'), text: v('warn-text'), bg: v('warn-bg'), 'bg-2': v('warn-bg-2'), hover: v('warn-hover'), border: v('warn-border') },
        ok: { DEFAULT: v('ok-fg'), text: v('ok-text'), bg: v('ok-bg'), 'bg-2': v('ok-bg-2'), hover: v('ok-hover'), border: v('ok-border') },
        err: { DEFAULT: v('err-fg'), text: v('err-text'), bg: v('err-bg'), 'bg-2': v('err-bg-2'), hover: v('err-hover'), border: v('err-border') },
        info: { DEFAULT: v('info-fg'), text: v('info-text'), bg: v('info-bg'), 'bg-2': v('info-bg-2'), hover: v('info-hover'), border: v('info-border') },
        // Categorical hue kept for the trace/topology views (sub-agent tooling).
        cat: { cyan: { DEFAULT: v('cat-cyan'), bg: v('cat-cyan-bg'), border: v('cat-cyan-border') } },
      },
      borderRadius: {
        composer: '1.5rem',
      },
      keyframes: {
        'cursor-blink': { '0%, 100%': { opacity: '1' }, '50%': { opacity: '0' } }
      },
      animation: {
        'cursor-blink': 'cursor-blink 1s ease-in-out infinite'
      }
    }
  },
  plugins: []
}
