// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Report- and investigation-discussion follow-up pills stay inside their context.
//
// A report (or investigation) discussion has no live connection and cannot launch an
// investigation, so its follow-up pills must be grounded in the report's own findings and
// must never offer a live check or starting an investigation. The generators in
// components/contextFollowups.js produce them from the report/investigation the client
// already holds.
//
// Run:  node tests/test_context_followups.mjs
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
let passed = 0, failed = 0
const check = (cond, label) => { if (cond) { passed++; console.log(`  ok   ${label}`) } else { failed++; console.log(`  FAIL ${label}`) } }

const mod = await import(pathToFileURL(path.join(ROOT, 'frontend/src/components/contextFollowups.js')).href)
const { generateReportFollowups, generateInvestigationFollowups } = mod

// Wording a context follow-up must never contain: it would push the discussion out of the
// saved report into a live run or a new investigation.
const BANNED = [/\binvestigat/i, /\blive\b/i, /\breal[- ]?time\b/i, /\bconnect to\b/i, /\brun a (?:query|check|scan)\b/i]
const anyBanned = (s) => BANNED.find(re => re.test(s))

const analysis = {
  display_name: 'aurora-pg-database-1',
  health_score: 'critical',
  findings: [
    { severity: 'CRITICAL', title: 'chk007_demo.orders at 53.85% dead tuples' },
    { severity: 'WARNING', title: 'rdsadmin role warning' },
  ],
  metrics: { table_cache_hit_ratio: 92.4 },
  recommendations: [{ title: 'Vacuum chk007_demo.orders' }],
}

console.log('== report-mode follow-ups are report-grounded and never live/investigation ==')
const rf = generateReportFollowups(analysis)
check(rf.length >= 3, `several follow-ups are produced (${rf.length})`)
check(rf.every(s => !anyBanned(s)),
  `none contain investigation or live wording (${rf.map(s => anyBanned(s)).filter(Boolean).join(', ') || 'clean'})`)
check(rf.some(s => s.includes('chk007_demo.orders at 53.85% dead tuples')),
  'a pill references the critical finding by title')
check(rf.some(s => s.includes('rdsadmin role warning')),
  'a pill references the warning finding by title')
check(rf.some(s => s.includes('92.4')),
  'a pill references the report metric (cache hit ratio)')
check(rf.some(s => /recommendation/i.test(s)),
  'a pill points at the report recommendation')

console.log('')
console.log('== a report with no findings still yields safe, grounded pills ==')
const rf2 = generateReportFollowups({ findings: [], metrics: {}, recommendations: [] })
check(rf2.length >= 1 && rf2.every(s => !anyBanned(s)),
  'no-finding report: pills are generic-but-safe (prioritize / summarize), none banned')
check(generateReportFollowups(null).every(s => !anyBanned(s)),
  'a null analysis does not throw and yields only safe pills')

console.log('')
console.log('== investigation follow-ups are finding-grounded and never live ==')
const inv = { title: 'Slow orders', findings: ['Lock contention on orders', 'Autovacuum lagging'], summary: 'Vacuum lag' }
const invf = generateInvestigationFollowups(inv)
check(invf.length >= 2, `several investigation follow-ups are produced (${invf.length})`)
check(invf.every(s => !/\blive\b/i.test(s) && !/\breal[- ]?time\b/i.test(s)),
  'none contain live/real-time wording')
check(invf.some(s => s.includes('Lock contention on orders')),
  'a pill references an investigation finding')
check(generateInvestigationFollowups(null).length >= 1,
  'a null investigation does not throw')

console.log('')
console.log(`pass=${passed} fail=${failed}`)
process.exit(failed ? 1 : 0)
