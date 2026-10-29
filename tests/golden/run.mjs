#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Tier A golden harness: builds (or reuses) the two apps, serves them with vite preview,
// and walks every scenario in tests/golden/scenarios against the stateful fake backend.
//
//   node tests/golden/run.mjs                 all scenarios          (make golden)
//   node tests/golden/run.mjs S1 S4           only these
//   GOLDEN_UPDATE=1 node tests/golden/run.mjs rewrite the snapshots  (make golden-update)
//
// Exit 0 when every step passed (expected failures of known bugs included), 1 otherwise.
import fs from 'node:fs'
import path from 'node:path'
import { loadPlaywright } from './lib/playwright.mjs'
import { REPO, ensureBuilt, serveApps } from './lib/serve.mjs'
import { Snapshots } from './lib/snapshot.mjs'
import { Harness, StepAbort } from './lib/harness.mjs'

const HERE = path.dirname(new URL(import.meta.url).pathname)
const OUT = path.join(REPO, '.agents', 'golden-out')
const update = !!process.env.GOLDEN_UPDATE
const only = process.argv.slice(2).map(s => s.toUpperCase())

const log = (s) => process.stdout.write(`${s}\n`)
const t0 = Date.now()

const scenarioFiles = fs.readdirSync(path.join(HERE, 'scenarios')).filter(f => f.endsWith('.mjs')).sort()
const scenarios = []
for (const f of scenarioFiles) {
  const mod = (await import(path.join(HERE, 'scenarios', f))).default
  if (!only.length || only.includes(mod.id.toUpperCase())) scenarios.push(mod)
}
if (!scenarios.length) { log(`no scenario matches ${only.join(' ')}`); process.exit(1) }

let pw
try { pw = await loadPlaywright() } catch (e) { log(`❌ ${e.message}`); process.exit(1) }

log('── golden harness (Tier A) ──')
log(`  playwright from ${pw.from}`)
ensureBuilt((s) => log(s))
const { urls, stop } = await serveApps()
const browser = await pw.mod.chromium.launch({ channel: 'chrome', headless: !process.env.GOLDEN_HEADED })
const snapshots = new Snapshots(path.join(HERE, 'snapshots'), { update })
const results = []
try {
  for (const sc of scenarios) {
    const st = Date.now()
    log(`  ${sc.id} ${sc.name}`)
    const h = new Harness({ browser, urls, scenario: sc, snapshots, outDir: OUT, log })
    let aborted = ''
    try { await sc.run(h) } catch (e) {
      if (!(e instanceof StepAbort)) { aborted = e.stack || e.message; log(`    FAIL scenario error: ${e.message}`) }
    } finally { await h.close() }
    const failed = h.steps.filter(s => !s.ok).length + (aborted ? 1 : 0)
    results.push({ id: sc.id, name: sc.name, ms: Date.now() - st, steps: h.steps.length, failed,
      xfail: h.steps.flatMap(s => s.xfail || []), xpass: h.steps.flatMap(s => s.xpass || []), aborted })
  }
} finally {
  await browser.close()
  stop()
}

log('')
log('── summary ──')
for (const r of results) {
  log(`  ${r.failed ? 'FAIL' : 'ok  '} ${r.id} ${r.name}: ${r.steps} steps, ${(r.ms / 1000).toFixed(1)} s` +
    `${r.xfail.length ? `, ${r.xfail.length} expected failure(s): ${r.xfail.map(x => x.bugId).join(', ')}` : ''}` +
    `${r.xpass.length ? `, XPASS ${r.xpass.join(', ')}` : ''}`)
}
const failed = results.filter(r => r.failed)
log(`  scenarios=${results.length} failed=${failed.length} snapshots=${snapshots.count}${update ? ` rewritten=${snapshots.written}` : ''} time=${((Date.now() - t0) / 1000).toFixed(1)}s`)
log(`  screenshots (for people, never compared): ${path.relative(REPO, OUT)}/`)
if (process.env.GOLDEN_RESULTS) fs.writeFileSync(process.env.GOLDEN_RESULTS, JSON.stringify({ results, snapshots: snapshots.count, seconds: (Date.now() - t0) / 1000 }, null, 2))
process.exit(failed.length ? 1 : 0)
