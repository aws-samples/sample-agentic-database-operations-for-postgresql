// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Investigation run lifecycle: Cancel actually stops the client stream, and a run is
// finalized exactly once as success OR failure — never a failed/cancelled run shown as a
// finished diagnosis.
//
// Why source assertions: the lifecycle lives inside the InvestigationDetail React
// component (stream callbacks + a one-shot finalize guard) and the FastAPI complete/
// cancel handlers. There is no headless DOM here, so — like test_live_handoff.mjs and
// test_operator_single_role.py — we assert the wiring is present and ordered correctly.
//
// Run:  node tests/test_investigation_lifecycle.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
let passed = 0, failed = 0
const check = (cond, label) => { if (cond) { passed++; console.log(`  ok   ${label}`) } else { failed++; console.log(`  FAIL ${label}`) } }

const detail = fs.readFileSync(path.join(ROOT, 'frontend/src/components/InvestigationDetail.jsx'), 'utf8')
const investigations = fs.readFileSync(path.join(ROOT, 'frontend/src/components/Investigations.jsx'), 'utf8')
const app = fs.readFileSync(path.join(ROOT, 'frontend/src/App.jsx'), 'utf8')
const backend = fs.readFileSync(path.join(ROOT, 'backend/app.py'), 'utf8')

console.log('== Cancel stops the client stream, not just the DB record ==')
// stopStreaming is threaded App -> Investigations -> InvestigationDetail.
check(/stopStreaming=\{stopStreaming\}/.test(app), 'App passes stopStreaming to Investigations')
check(/stopStreaming,/.test(investigations) && /stopStreaming=\{stopStreaming\}/.test(investigations),
  'Investigations receives stopStreaming and passes it to InvestigationDetail')
check(/function InvestigationDetail\([^)]*stopStreaming[^)]*\)/.test(detail),
  'InvestigationDetail accepts the stopStreaming prop')

const cancelFn = (detail.split('const cancelInvestigation')[1] || '').split('const retryInvestigation')[0]
check(cancelFn.length > 0, 'the cancel handler is a named function (cancelInvestigation)')
// Order matters: block the stream's own finalize BEFORE aborting, so the abort's
// result/close callback cannot re-finalize the run after we cancel it.
const idxFinalize = cancelFn.indexOf('finalizedRef.current = true')
const idxAbort = cancelFn.indexOf('stopStreaming?.(`inv-')
check(idxFinalize !== -1 && idxAbort !== -1 && idxFinalize < idxAbort,
  'cancel blocks re-finalization (finalizedRef=true) BEFORE aborting the stream')
check(/stopStreaming\?\.\(`inv-\$\{investigation\.investigation_id\}`\)/.test(cancelFn),
  'cancel aborts THIS tab\u2019s stream by its inv-<id> session key')
check(/\/investigations\/\$\{investigation\.investigation_id\}\/cancel`\)/.test(cancelFn),
  'cancel marks the record cancelled on the server')
check(/setStreaming\(false\)/.test(cancelFn), 'cancel drops the live spinner immediately')
check(/onClick=\{cancelInvestigation\}/.test(detail), 'the Cancel button calls the handler')

console.log('')
console.log('== a run is finalized exactly once, as success OR failure ==')
// finalizedRef is the one-shot guard; the first branch returns if it is already set.
check(/const finalizedRef = useRef\(false\)/.test(detail) &&
  /if \(finalizedRef\.current\) return\s*\n\s*finalizedRef\.current = true/.test(detail),
  'finalizeInvestigation runs once (finalizedRef one-shot guard)')
// A real result event is the only success path.
check(/event\.type === 'result'[\s\S]*?finalizeInvestigation\('completed'/.test(detail),
  'a result event finalizes as completed')
// Error event, a stream that closed without a result, and a rejected stream are failures.
check(/event\.type === 'error'[\s\S]*?finalizeInvestigation\('failed'/.test(detail),
  'an error event finalizes as failed')
check(/if \(!gotResultRef\.current\)[\s\S]*?finalizeInvestigation\('failed'/.test(detail),
  'a stream that closes without a result finalizes as failed')
check(/\.catch\(err =>[\s\S]*?finalizeInvestigation\('failed'/.test(detail),
  'a rejected stream finalizes as failed')
// A failure keeps the partial text as detail and never posts a completed diagnosis. The
// outcome posted is the EFFECTIVE one: a 'completed' run with no text is a failure too
// (f1c2e01), and a failure always carries a detail, falling back to a fixed sentence.
check(/const effectiveFailed = failed \|\| \(outcome === 'completed' && !hasText\)/.test(detail) &&
  /const effectiveOutcome = effectiveFailed \? 'failed' : 'completed'/.test(detail) &&
  /status: effectiveOutcome/.test(detail) &&
  /effectiveFailed \? \{ failure_detail: failureDetail \|\|/.test(detail),
  'the /complete post carries the real outcome and the failure detail')

console.log('')
console.log('== the server finalize is idempotent and distinguishes failure ==')
const completeFn = (backend.split('def complete_investigation')[1] || '').split('\n@app.')[0]
check(/if inv\.get\("status"\) in \("completed", "cancelled"\):/.test(completeFn),
  'a completed or cancelled run is a no-op (cannot be overwritten by a late client post)')
check(/if data\.status == "failed":/.test(completeFn) &&
  /update_status\(\s*\n?\s*investigation_id, "failed"/.test(completeFn),
  'status=failed records a failure, storing no findings or severity')
// The server-side sweep is the backstop for a tab that closed/crashed mid-run.
check(/def sweep_stale_investigations/.test(backend) &&
  /INVESTIGATION_STALE_MINUTES = 10/.test(backend),
  'a server-side sweep retires stranded running runs (reload/close backstop)')

console.log('')
console.log('== an abandoned run (browser close/reload) is finalized promptly via an unload beacon ==')
// drivingRef marks the one tab responsible for saving the result: true at stream start,
// cleared the moment the run reaches any terminal outcome.
check(/const drivingRef = useRef\(false\)/.test(detail), 'a drivingRef marks the live driver tab')
check(/setStreamStarted\(true\)[\s\S]{0,120}?drivingRef\.current = true/.test(detail),
  'drivingRef is set when THIS tab starts the stream')
check(/finalizedRef\.current = true\s*\n\s*drivingRef\.current = false/.test(detail),
  'drivingRef clears on any terminal outcome (so the beacon never re-fails a finished run)')
const cancelFn2 = (detail.split('const cancelInvestigation')[1] || '').split('const retryInvestigation')[0]
check(/drivingRef\.current = false/.test(cancelFn2), 'cancel also clears drivingRef')
// The beacon itself: pagehide, guards, keepalive POST of status=failed with the id token.
const beacon = (detail.split('const onPageHide')[1] || '').split("window.addEventListener('pagehide'")[0]
check(/e\.persisted \|\| !drivingRef\.current\) return/.test(beacon),
  'the beacon skips a bfcache page and any non-driver / finished run')
check(/localStorage\.getItem\('id_token'\)/.test(beacon),
  'the beacon reads the id token synchronously (no async refresh at unload)')
check(/keepalive: true/.test(beacon) && /status: 'failed'/.test(beacon) &&
  /\/investigations\/\$\{invId\}\/complete`/.test(beacon),
  'the beacon posts status=failed to /complete with keepalive so it outlives the page')
check(/window\.addEventListener\('pagehide', onPageHide\)/.test(detail) &&
  /removeEventListener\('pagehide', onPageHide\)/.test(detail),
  'the pagehide listener is registered and cleaned up (SPA nav unmount does not fire it)')
// Server-side, a late beacon is safe: /complete already distinguishes failed and is
// idempotent for completed/cancelled (asserted above).

console.log('')
console.log(`pass=${passed} fail=${failed}`)
process.exit(failed ? 1 : 0)
