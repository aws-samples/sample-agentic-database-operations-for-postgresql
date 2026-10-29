// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Playwright is deliberately not a dependency of either app (it would add ~150 MB to
// every `npm ci`). The harness resolves it at run time, in this order:
//   1. GOLDEN_PLAYWRIGHT: a path to playwright's index.mjs, or to a directory that holds
//      node_modules/playwright (or is the playwright package itself)
//   2. a playwright the harness can import by name (installed globally or in a parent)
//   3. the npx cache (~/.npm/_npx/*/node_modules/playwright), newest first
// and fails with an install hint when none is found.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const HINT = [
  'Playwright was not found. The golden harness drives the built apps in Chrome and needs it.',
  'Either:',
  '  npx -y playwright@1 --version          # puts it in the npx cache, which the harness searches',
  'or point the harness at an install:',
  '  GOLDEN_PLAYWRIGHT=/path/to/node_modules/playwright make golden',
  'Chrome itself must be installed (the harness launches it with channel "chrome").',
].join('\n')

function candidatesFrom(p) {
  if (!p) return []
  if (p.endsWith('.mjs') || p.endsWith('.js')) return [p]
  return [
    path.join(p, 'index.mjs'),
    path.join(p, 'playwright', 'index.mjs'),
    path.join(p, 'node_modules', 'playwright', 'index.mjs'),
  ]
}

function npxCache() {
  const root = path.join(os.homedir(), '.npm', '_npx')
  let dirs = []
  try { dirs = fs.readdirSync(root) } catch { return [] }
  return dirs
    .map(d => path.join(root, d, 'node_modules', 'playwright', 'index.mjs'))
    .filter(f => fs.existsSync(f))
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)
}

export async function loadPlaywright() {
  const tried = []
  for (const f of candidatesFrom(process.env.GOLDEN_PLAYWRIGHT)) {
    tried.push(f)
    if (fs.existsSync(f)) return { mod: await import(pathToFileURL(f).href), from: f }
  }
  if (process.env.GOLDEN_PLAYWRIGHT) {
    throw new Error(`GOLDEN_PLAYWRIGHT=${process.env.GOLDEN_PLAYWRIGHT} does not hold playwright (tried ${tried.join(', ')}).\n${HINT}`)
  }
  try { return { mod: await import('playwright'), from: 'playwright (module resolution)' } } catch { /* next */ }
  for (const f of npxCache()) {
    try { return { mod: await import(pathToFileURL(f).href), from: f } } catch { /* next */ }
  }
  throw new Error(HINT)
}
