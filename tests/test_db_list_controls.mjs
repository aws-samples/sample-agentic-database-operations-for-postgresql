// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// The shared database-list controls: paging over replica groups must never split a
// primary from its replicas, the two app copies must be byte-identical, and the engine
// vocabulary is defined once. Pure logic only (no DOM, no React render).
//
// Run:  node tests/test_db_list_controls.mjs
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
let passed = 0, failed = 0
const check = (cond, msg) => { if (cond) { passed++; console.log(`  ok   ${msg}`) } else { failed++; console.log(`  FAIL ${msg}`) } }

// Extract pageNested from the module source without importing React (the file imports
// 'react', which is not resolvable from the repo root), so this stays dependency-free.
const src = readFileSync(path.join(root, 'admin-console/src/components/DbListControls.jsx'), 'utf8')
const fnSrc = src.match(/export function pageNested\([\s\S]*?\n}\n/)[0].replace(/^export /, '')
const pageNested = new Function(`${fnSrc}; return pageNested`)()

console.log('replica groups are the unit of paging')
// 3 primaries; the second has 2 replicas. Flat rows = 5, groups = 3.
const nested = [
  { row: { id: 'A' }, depth: 0 },
  { row: { id: 'B' }, depth: 0 }, { row: { id: 'B-r1' }, depth: 1 }, { row: { id: 'B-r2' }, depth: 1 },
  { row: { id: 'C' }, depth: 0 },
]
let p = pageNested(nested, 1, 2)
check(p.totalGroups === 3 && p.totalPages === 2, 'counts groups, not flat rows (3 groups -> 2 pages of 2)')
check(p.rows.map(r => r.row.id).join(',') === 'A,B,B-r1,B-r2', 'page 1 holds A and the WHOLE B group (replicas never split off)')
p = pageNested(nested, 2, 2)
check(p.rows.map(r => r.row.id).join(',') === 'C', 'page 2 holds C')
p = pageNested(nested, 99, 2)
check(p.page === 2 && p.rows.length === 1, 'an out-of-range page clamps to the last page')
check(pageNested([], 1, 10).totalPages === 1 && pageNested([], 1, 10).rows.length === 0, 'an empty list is one empty page')
// A replica whose primary was filtered out (orphan at depth>0 first) still renders.
p = pageNested([{ row: { id: 'orphan' }, depth: 1 }, { row: { id: 'X' }, depth: 0 }], 1, 10)
check(p.rows.length === 2 && p.totalGroups === 2, 'a leading depth>0 row starts its own group rather than being dropped')

console.log('the shared module is byte-identical in both apps')
const admin = readFileSync(path.join(root, 'admin-console/src/components/DbListControls.jsx'), 'utf8')
const operator = readFileSync(path.join(root, 'frontend/src/components/DbListControls.jsx'), 'utf8')
check(admin === operator, 'admin-console and frontend copies match (edit one, copy to the other)')

console.log('the engine vocabulary is defined once')
check(/export const ENGINE_LABELS\s*=/.test(admin) && /export const canonicalEngine\s*=/.test(admin)
  && /export const engineLabel\s*=/.test(admin), 'the shared module exports ENGINE_LABELS, canonicalEngine and engineLabel')
// Every view that names or filters by engine goes through the shared helpers, so the RDS
// spelling `postgres` and the record spelling `rds-postgresql` are one engine everywhere.
for (const f of ['Dashboard', 'Trends', 'ScopeSelector', 'DbInstanceCard']) {
  const src = readFileSync(path.join(root, `frontend/src/components/${f}.jsx`), 'utf8')
  check(!/const ENGINE_LABELS\s*=/.test(src) && /import \{[^}]*\bengineLabel\b[^}]*\} from '\.\/DbListControls'/.test(src),
    `${f} imports engineLabel from the shared module instead of defining its own map`)
}
for (const f of ['Dashboard', 'Trends', 'ScopeSelector']) {
  const src = readFileSync(path.join(root, `frontend/src/components/${f}.jsx`), 'utf8')
  check(/canonicalEngine\([^)]*\.engine\) [!=]== \w*[eE]ngineFilter/.test(src), `${f} filters on the canonical engine`)
}
const sdEngine = readFileSync(path.join(root, 'admin-console/src/components/SpaceDetail.jsx'), 'utf8')
check(!/<td[^>]*>\{db\.engine\}<\/td>/.test(sdEngine), 'admin console tables show the engine label, not the raw record value')

console.log('the engine filter folds the RDS spelling into RDS PostgreSQL')
const mod = await import(path.join(root, 'admin-console/src/components/DbListControls.jsx').replace(/\\/g, '/'))
  .catch(() => null)
if (mod) {
  check(mod.canonicalEngine('postgres') === 'rds-postgresql' && mod.canonicalEngine('rds-postgresql') === 'rds-postgresql'
    && mod.canonicalEngine('aurora-postgresql') === 'aurora-postgresql', 'postgres and rds-postgresql are the same engine; Aurora is untouched')
  check(mod.engineLabel('postgres') === 'RDS PostgreSQL', 'the RDS spelling is labelled RDS PostgreSQL')
} else {
  // JSX cannot be imported by plain node; pin the alias table by source instead.
  check(/postgres: 'rds-postgresql'/.test(admin), 'the alias table maps postgres to rds-postgresql')
}

console.log('both admin lists use the shared controls')
const sd = readFileSync(path.join(root, 'admin-console/src/components/SpaceDetail.jsx'), 'utf8')
check(/useDbListControls\(discoveredDbs/.test(sd) && /useDbListControls\(visibleDbs/.test(sd), 'found and onboarded lists each have controls')
check((sd.match(/<DbListToolbar /g) || []).length === 2 && (sd.match(/<DbListPager /g) || []).length === 2, 'two toolbars and two pagers rendered')
check(/pageNested\(foundNested/.test(sd) && /pageNested\(onboardedNested/.test(sd), 'both lists page by replica group')

console.log(`\npass=${passed} fail=${failed}`)
process.exit(failed ? 1 : 0)
