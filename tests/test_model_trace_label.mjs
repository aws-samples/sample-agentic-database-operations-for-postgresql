// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// The agent trace's model entry names the model and, when the server reports them, the
// effort level and thinking mode the turn runs with. The chat and investigation traces
// share one formatter (frontend/src/components/modelTrace.js).
//
// Run:  node tests/test_model_trace_label.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
let passed = 0, failed = 0
const check = (cond, label) => { if (cond) { passed++; console.log(`  ok   ${label}`) } else { failed++; console.log(`  FAIL ${label}`) } }
const { modelTraceLabel, modelTraceDetail } = await import(
  pathToFileURL(path.join(ROOT, 'frontend/src/components/modelTrace.js')).href)

const full = { type: 'model', model_id: 'eu.anthropic.claude-haiku-5-5', surface: 'chat', effort: 'low', thinking: 'model default' }
check(modelTraceLabel(full) === 'Model: eu.anthropic.claude-haiku-5-5 · effort low · thinking model default',
  'model, effort and thinking in one line')
check(modelTraceLabel({ type: 'model', model_id: 'm', effort: null, thinking: 'model default' }) === 'Model: m · thinking model default',
  'a model without an effort level shows none')
check(modelTraceLabel({ type: 'model', model_id: 'm' }) === 'Model: m', 'a bare model event reads as before')
const d = modelTraceDetail(full)
check(!('type' in d) && d.effort === 'low' && d.surface === 'chat', 'the detail keeps every reported field')

for (const f of ['Chat.jsx', 'InvestigationDetail.jsx']) {
  const src = fs.readFileSync(path.join(ROOT, 'frontend/src/components', f), 'utf8')
  check(src.includes("from './modelTrace'") && src.includes('label: modelTraceLabel(event)'),
    `${f} uses the shared formatter`)
}
console.log(`\npass=${passed} fail=${failed}`)
process.exit(failed ? 1 : 0)
