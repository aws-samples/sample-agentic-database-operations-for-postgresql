// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// "Send to live agent" from report discussion (review: send-to-live-agent).
//
// In report mode, when an answer needs live data the agent ends it with a fixed marker.
// The client strips the marker and flags the message, carrying the live question. In context
// discussion (report or investigation), every assistant answer offers one pill: "Ask the live
// agent about <db>". Clicking it switches the toggle to Live Agent and re-sends the question
// (the marker's liveQuestion when present, else the last user question) scoped to the record's
// OWN database.
//
// Run:  node tests/test_live_handoff.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
let passed = 0, failed = 0
const check = (cond, label) => { if (cond) { passed++; console.log(`  ok   ${label}`) } else { failed++; console.log(`  FAIL ${label}`) } }

const { LIVE_MARKER, splitLiveMarker, reportDatabaseScope, investigationDatabaseScope } =
  await import(pathToFileURL(path.join(ROOT, 'frontend/src/components/liveHandoff.js')).href)

console.log('== the live-data marker is stripped from shown text and flagged ==')
let r = splitLiveMarker('You need a live check for current activity.\n' + LIVE_MARKER)
check(r.needed === true, 'a complete marker sets needed=true')
check(!r.text.includes('LIVE_DATA_NEEDED') && !r.text.includes('[['),
  'the marker is removed from the shown text')
check(r.text.includes('You need a live check'), 'the real answer text is kept')

r = splitLiveMarker('An ordinary report answer, fully covered here.')
check(r.needed === false && r.text === 'An ordinary report answer, fully covered here.',
  'no marker: text unchanged, needed=false')

console.log('')
console.log('== a partial marker split across deltas never flashes on screen ==')
r = splitLiveMarker('Live data needed. [[LIVE_DATA_')
check(r.needed === false && !r.text.includes('[[LIVE_DATA_'),
  'a trailing partial marker is held back (not shown, not yet flagged)')
// Once the rest of the marker arrives, it is recognised and stripped.
r = splitLiveMarker('Live data needed. [[LIVE_DATA_NEEDED]]')
check(r.needed === true && r.text.trim() === 'Live data needed.',
  'the completed marker is stripped and flagged')

console.log('')
console.log('== the hand-off is scoped to the report\u2019s own database, never wider ==')
const reportCtx = { meta: { instance_id: 'db-xyz' }, analysis: { display_name: 'aurora-pg-database-1' } }
const dbs = [{ instance_id: 'db-xyz', display_name: 'aurora-pg-database-1' }, { instance_id: 'db-other', display_name: 'other' }]
let scope = reportDatabaseScope(reportCtx, dbs)
check(scope && scope.instance_id === 'db-xyz', 'resolves to the report\u2019s own database record')
scope = reportDatabaseScope(reportCtx, [])
check(scope && scope.instance_id === 'db-xyz' && scope.display_name === 'aurora-pg-database-1',
  'builds a minimal entry when the record is not loaded')
check(reportDatabaseScope({ meta: {}, analysis: {} }, dbs) === null,
  'no instance id -> null (never the picker or the whole space)')

console.log('')
console.log('== an investigation hand-off is scoped to the investigation\u2019s own database(s) ==')
let invScope = investigationDatabaseScope({ database_ids: ['db-xyz'] }, dbs)
check(invScope.length === 1 && invScope[0].instance_id === 'db-xyz' && invScope[0].display_name === 'aurora-pg-database-1',
  'resolves the investigation\u2019s database to its live record')
invScope = investigationDatabaseScope({ database_ids: ['db-xyz', 'db-other'] }, dbs)
check(invScope.length === 2, 'a multi-database investigation hands off all of its databases')
invScope = investigationDatabaseScope({ database_ids: ['db-missing'] }, dbs)
check(invScope.length === 1 && invScope[0].instance_id === 'db-missing',
  'builds a minimal entry when the record is not loaded')
check(investigationDatabaseScope({}, dbs).length === 0,
  'no database ids -> [] (never the picker or the whole space)')

console.log('')
console.log('== Chat.jsx wires the hand-off: go live (pin scope) and resend ==')
const chat = fs.readFileSync(path.join(ROOT, 'frontend/src/components/Chat.jsx'), 'utf8')
// goLive is the shared switch used by both the toggle and the hand-off button: it flips the
// mode to the live agent AND pins the scope to the record's own database(s).
const goLiveFn = (chat.split('const goLive')[1] || '').split('const askLiveAgent')[0]
check(/setChatMode\('agent'\)/.test(goLiveFn), 'goLive switches the mode to the live agent')
check(/onScopeChange\?\.\(liveHandoffDbs\)/.test(goLiveFn), 'goLive pins the scope to the record\u2019s own database(s)')
const askFn = (chat.split('const askLiveAgent')[1] || '').split('const handleDownloadChat')[0]
check(/goLive\(\)/.test(askFn), 'the hand-off button goes live via the shared goLive switch')
check(/handleSend\(question,\s*\{\s*forceLiveAgent:\s*true,\s*scopeDatabases:\s*liveHandoffDbs\s*\}\)/.test(askFn),
  'it re-sends the SAME question with forceLiveAgent and the record-db scope')
check(chat.includes('reportDatabaseScope(reportContext, allDatabases)'),
  'the hand-off database is the report\u2019s own, resolved from the report context')
check(/askLiveAgent\(lastMessage\.liveQuestion \|\| lastUserText\)/.test(chat) && chat.includes('Ask the live agent about'),
  'the button is offered after any assistant answer in context and resends the liveQuestion marker or the last user question')
check(chat.includes('forceLiveAgent === true') && chat.includes('opts.scopeDatabases !== undefined'),
  'handleSend honours forceLiveAgent and the pinned scope')
check(chat.includes('end your reply with the exact marker ${LIVE_MARKER}'),
  'the report-mode prompt instructs the agent to emit the marker when live data is needed')

console.log('')
console.log('== the hand-off is the SAME experience in report and investigation discussions ==')
// The marker mechanism (strip + flag + button) is driven by a context-agnostic flag so an
// investigation turn behaves like a report turn, not just report mode.
check(chat.includes('const inContextMode = inReportMode || inInvestigationMode'),
  'the marker mechanism keys on inContextMode (report OR investigation), not report alone')
check(/if \(inContextMode\) \{\s*\n\s*const \{ text, needed \} = splitLiveMarker/.test(chat),
  'the streamed text strips the marker in either context')
check(chat.includes('const liveTurn = inContextMode && (liveNeeded || marker.needed)'),
  'the committed message is flagged liveDataNeeded in either context')
// The investigation prompt is grounded (no live tools, no new investigation) AND can emit
// the marker, exactly like the report prompt.
const invPrompt = (chat.split('[INVESTIGATION CONTEXT]')[1] || '').split('[END INVESTIGATION CONTEXT]')[0]
check(/do NOT call any live database tool/.test(invPrompt) && /do NOT suggest or offer to start another investigation/.test(invPrompt),
  'the investigation prompt is grounded: no live tools, no new investigation')
check(invPrompt.includes('end your reply with the exact marker ${LIVE_MARKER}'),
  'the investigation prompt instructs the agent to emit the marker when live data is needed')
// The button is a primary button (not the old faint pill) and shows in either context.
check(/!isStreaming && !investigating && inContext\s*\n\s*&& lastMessage\?\.role === 'assistant' && \(/.test(chat),
  'the hand-off button renders in either context after any assistant answer (gated on inContext), not report alone')
check(chat.includes('bg-primary hover:bg-primary-hover text-primary-fg') &&
  /Ask the live agent about \{liveHandoffLabel\}/.test(chat),
  'the hand-off is a primary button labelled from the active context')
// Both contexts offer the SAME top Discussion/Live Agent toggle (shared modeToggle), and the
// hand-off keeps the context (no clear) in both: an investigation chat behaves like a report
// chat, not a one-way transfer.
check(chat.includes("const modeToggle = (discussionLabel) =>"),
  'a shared Discussion/Live Agent toggle component exists')
check(chat.includes("modeToggle('Report Discussion')") && chat.includes("modeToggle('Investigation Discussion')"),
  'both the report and investigation banners render the shared toggle')
const askFn2 = (chat.split('const askLiveAgent')[1] || '').split('const handleDownloadChat')[0]
check(!/onClearInvestigationContext/.test(askFn2),
  'the hand-off does NOT clear the investigation context (same experience as report)')
check(/const investigationDiscussion = !liveAgent && !!activeInvestigationContext/.test(chat)
  && /opts\.investigationContext \|\| chatMode === 'discussion'/.test(chat),
  'investigation grounding fires on the Discussion toggle OR an explicit hand-off context, so a pill from a plain chat still grounds (and the Live Agent toggle still goes live)')

console.log('')
console.log('== App keeps chat context single-slot (most recent wins) ==')
const app = fs.readFileSync(path.join(ROOT, 'frontend/src/App.jsx'), 'utf8')
check(/const openReport = \(r\) => \{ setActiveReport\(r\); setInvestigationContext\(null\);/.test(app),
  'opening a report clears any investigation context')
const putFn = (app.split('const putInvestigationInContext')[1] || '').split('\n  }')[0]
check(/setInvestigationContext\(inv\)/.test(putFn) && /openTabs\.includes\('report'\)\) closePanel\('report'\)/.test(putFn),
  'putting an investigation in context closes any open report (so it cannot silently win)')
check(app.includes('onChatAbout={putInvestigationInContext}') && app.includes('onSetInvestigationContext={putInvestigationInContext}'),
  'both chat entry points route through the single-slot setter')

console.log('')
console.log(`pass=${passed} fail=${failed}`)
process.exit(failed ? 1 : 0)
