// SPDX-License-Identifier: Apache-2.0
//
// Accounts & regions in the admin console: one list, one row per account (the primary
// included), no "default" concept. The saved shape is the server's account_regions with
// every account explicit and `regions` as the union, so the IAM step and older readers
// keep working and a legacy space (space-wide regions only) seeds every row unchanged.
// Pure logic, extracted from the component source (no React render).
//
// Run:  node tests/test_regions_per_account_ui.mjs
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
let passed = 0, failed = 0
const check = (cond, msg) => { if (cond) { passed++; console.log(`  ok   ${msg}`) } else { failed++; console.log(`  FAIL ${msg}`) } }

const src = readFileSync(path.join(root, 'admin-console/src/components/SpaceDetail.jsx'), 'utf8')
const grab = name => {
  const m = src.match(new RegExp(`^  const ${name} = [\\s\\S]*?\\n(?=  (?:const|let|\\/\\/|useEffect|\\n))`, 'm'))
  if (!m) throw new Error(`cannot find ${name}`)
  return m[0]
}
// Evaluate the three pure helpers against a given space + per-account state.
const helpers = (space, accountRegions) => new Function('space', 'accountRegions', `
  ${grab('allAccountIds')}
  ${grab('seedAccountRegions')}
  ${grab('regionsFor')}
  ${grab('regionPayload')}
  return { allAccountIds, seedAccountRegions, regionsFor, regionPayload }
`)(space, accountRegions)

console.log('the UI has one list of accounts and no default-regions concept')
check(!/Same as default/.test(src) && !/Default regions/.test(src) && !/selectedRegions/.test(src),
  'the default picker and the Same as default / Custom toggle are gone')
check(/allAccountIds\(\)\.map\(id => \{[\s\S]*?role="listitem"/.test(src), 'every account, the primary included, gets a row')
check(/Apply to all accounts/.test(src), 'one account\'s regions can be copied to all (what a default was for)')
check(/disabled=\{configuringAccess \|\| scansNowhere\}/.test(src),
  'Validate is gated only on the space scanning somewhere, not on every account having a region')
check(/const scansNowhere = scannedRegions\.length === 0/.test(src),
  'an account with no regions is allowed (not scanned); only an all-empty space blocks')
check(/Leave an account empty to skip it/.test(src),
  'the help text tells the admin an empty account is skipped (so the deployment account can stay empty)')

console.log('a legacy space seeds every account from its space-wide regions')
let h = helpers({ primary_account_id: '111', secondary_account_ids: ['222', '333'], regions: ['eu-west-1', 'us-east-1'] }, {})
const seed = h.seedAccountRegions()
check(JSON.stringify(seed) === JSON.stringify({ '111': ['eu-west-1', 'us-east-1'], '222': ['eu-west-1', 'us-east-1'], '333': ['eu-west-1', 'us-east-1'] }),
  'no account_regions -> every row shows the regions the space was already scanning')
h = helpers({ primary_account_id: '111', secondary_account_ids: ['222'], regions: ['us-east-1'], account_regions: { '222': ['eu-west-1'] } }, {})
check(JSON.stringify(h.seedAccountRegions()) === JSON.stringify({ '111': ['us-east-1'], '222': ['eu-west-1'] }),
  'a saved per-account entry wins over the space-wide list for that account (absent 111 inherits)')

console.log('a saved explicit-empty account stays empty, so "Regions changed" can clear')
// Regression: the deployment account saved as [] must seed as [], not re-expand to the
// space-wide regions. Otherwise the recomputed seed never matches the saved payload and
// the "Regions changed — validate again" banner is stuck on forever after validating.
const savedSpace = { primary_account_id: '111', secondary_account_ids: ['222'], regions: ['eu-west-1'], account_regions: { '111': [], '222': ['eu-west-1'] } }
h = helpers(savedSpace, {})
check(JSON.stringify(h.seedAccountRegions()['111']) === '[]',
  'a saved [] for the deployment account seeds as [] (not re-expanded to the space-wide regions)')
const seeded = h.seedAccountRegions()
const norm = m => JSON.stringify(Object.entries(m).map(([k, v]) => [k, [...v].sort()]).sort())
const roundTrip = helpers(savedSpace, seeded)
check(norm(roundTrip.regionPayload().account_regions) === norm(seeded),
  'the payload rebuilt from the seeded state equals the saved state (regionsChanged is false after a save)')
check(norm(roundTrip.regionPayload().account_regions) === norm(savedSpace.account_regions),
  'and it equals what the server stored (no phantom change left behind)')

console.log('the payload is explicit per account with regions as the union')
h = helpers({ primary_account_id: '111', secondary_account_ids: ['222', '333'] },
  { '111': ['us-east-1'], '222': ['eu-west-1', 'eu-central-1'], '333': ['eu-west-1'] })
let p = h.regionPayload()
check(JSON.stringify(p.account_regions) === JSON.stringify({ '111': ['us-east-1'], '222': ['eu-central-1', 'eu-west-1'], '333': ['eu-west-1'] }),
  'account_regions names every account, lists sorted')
check(JSON.stringify(p.regions) === JSON.stringify(['eu-central-1', 'eu-west-1', 'us-east-1']), 'regions is the sorted union')
h = helpers({ primary_account_id: '111', secondary_account_ids: ['222'] }, { '111': [], '222': ['eu-west-1'] })
p = h.regionPayload()
check(JSON.stringify(p.account_regions['111']) === '[]' && JSON.stringify(p.regions) === '["eu-west-1"]',
  'the deployment account is sent as an explicit empty list = "skip"; the union is just the scanned account')
check(h.regionsFor('999').length === 0, 'an unknown account reads as no regions, never undefined')

console.log('the deployment account is optional unless it is the only account')
// Primary empty + a secondary with regions: the space scans somewhere, so Validate is allowed.
h = helpers({ primary_account_id: '111', secondary_account_ids: ['222'] }, { '111': [], '222': ['eu-west-1'] })
let union = [...new Set(h.allAccountIds().flatMap(h.regionsFor))]
check(union.length > 0, 'primary empty but a secondary has regions -> the space scans somewhere (Validate enabled)')
// Primary is the only account and empty: nothing is scanned, so Validate must stay blocked.
h = helpers({ primary_account_id: '111', secondary_account_ids: [] }, { '111': [] })
union = [...new Set(h.allAccountIds().flatMap(h.regionsFor))]
check(union.length === 0, 'the deployment account alone and empty -> nothing scanned (Validate blocked)')

console.log(`pass=${passed} fail=${failed}`)
process.exit(failed ? 1 : 0)
