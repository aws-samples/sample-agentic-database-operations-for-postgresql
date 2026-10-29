// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// The docs/PRODUCT-RULES.md rules a page can show, checked after every scenario step.
// Each check names the rule number it enforces and returns the violations it finds as
// { rule, message }. They read what the user sees (the page) and, where the page alone
// cannot tell, what the product asked the backend to do (the fake's request log): a
// control a non-owner could press is a violation even if the fake refused the call.

export const RULE_TITLES = {
  2: 'Operators never use the admin console',
  3: 'A space is managed by its creator or a super admin, nobody else',
  5: 'Creating a space never makes anyone an operator of it',
  15: 'Every page shows current data without a manual refresh',
  17: 'Nothing reports success when it failed',
  18: 'Errors say what to do, never a raw AWS error alone',
}

// Raw AWS / SDK error text. Any of these on screen means an error reached the user
// unexplained (rule 18): the product must translate it into the cause and the fix.
const RAW_AWS = [
  /An error occurred \([A-Za-z]+\) when calling the [A-Za-z]+ operation/,
  /\b(AccessDenied|AccessDeniedException|UnauthorizedOperation|ThrottlingException|ValidationException|ResourceNotFoundException|InvalidClientTokenId|ExpiredToken)\b:/,
  /is not authorized to perform: [a-z0-9-]+:[A-Za-z]+/,
  /arn:aws:sts::\d{12}:assumed-role\//,
  /\bbotocore\b|\bboto3\b|Traceback \(most recent call last\)/,
  /no identity-based policy allows/,
]

// Success wording that must never be on screen right after a refusal (rule 17).
const SUCCESS_AFTER_FAILURE = [
  /✓ Secret validated/,
  /Access granted/,
  /Onboarded into this space/,
  /All accounts validated/,
]

// Controls that change a space. A non-owner may see them only disabled (rule 3).
const MANAGE_CONTROL = /^(Onboard|Add to this space|Run discovery|Run discovery again|Validate access|Validate access again|Delete Agent Space|Add user|Add|Add source|Configure monitoring|Apply monitoring|Retry|Repair|Invite)$|^(Remove|Dismiss|Reconfigure|Retry removing|Repair the host routes of) /

export async function visibleText(page, selector = 'body') {
  try { return await page.locator(selector).first().innerText({ timeout: 2000 }) } catch { return '' }
}

export function rule18(text) {
  return RAW_AWS.filter(re => re.test(text)).map(re => ({ rule: 18, message: `raw AWS error text on screen (${re.source.slice(0, 50)}…): "${(text.match(re) || [''])[0]}"` }))
}

export function rule17(text, failures) {
  if (!failures.length) return []
  return SUCCESS_AFTER_FAILURE.filter(re => re.test(text)).map(re => ({ rule: 17,
    message: `success shown ("${text.match(re)[0]}") right after a refusal: ${failures.map(f => `${f.method} ${f.path} -> ${f.status}${f.stage ? ` ${f.stage}` : ''}`).join('; ')}` }))
}

// Rule 2: an operator who opens the admin console gets the access-denied page and the
// console never reads any data on their behalf.
export async function rule2(actor, requests) {
  if (actor.app !== 'admin' || actor.role !== 'operators') return []
  const out = []
  const text = await visibleText(actor.page)
  if (!/Access Denied/.test(text)) out.push({ rule: 2, message: 'an operator reached the admin console (no Access Denied page)' })
  if (/Agent Spaces|Create Agent Space|Settings/.test(text)) out.push({ rule: 2, message: 'admin console navigation is visible to an operator' })
  const read = requests.filter(r => r.user === actor.userKey && r.app === 'admin')
  if (read.length) out.push({ rule: 2, message: `the admin console called the API for an operator: ${read.map(r => `${r.method} ${r.path}`).join(', ')}` })
  return out
}

// Rule 3: on a space the actor does not manage, every manage control is disabled, the
// read-only notice says why, and the product sent no change for that space.
export async function rule3(actor, { space, canManage }, requests, failures) {
  if (actor.app !== 'admin' || !space || canManage) return []
  const out = []
  const page = actor.page
  const text = await visibleText(page, 'main')
  if (!/Read-only\./.test(text)) out.push({ rule: 3, message: 'a non-owner sees the space without the read-only notice' })
  const buttons = page.locator('main button:visible')
  const n = await buttons.count()
  for (let i = 0; i < n; i++) {
    const b = buttons.nth(i)
    const name = ((await b.getAttribute('aria-label')) || (await b.innerText().catch(() => '')) || '').replace(/\s+/g, ' ').trim()
    if (MANAGE_CONTROL.test(name) && !(await b.isDisabled())) out.push({ rule: 3, message: `a non-owner can press "${name}"` })
  }
  const writes = requests.filter(r => r.user === actor.userKey && r.method !== 'GET' && r.path.includes(space))
  for (const w of writes) out.push({ rule: 3, message: `the console sent a change for a non-owner: ${w.method} ${w.path} -> ${w.status}` })
  // A refused READ (the console asks for candidates, which only a manager may list) is
  // harmless; a refused CHANGE means a control let the non-owner try.
  const refused = failures.filter(f => f.status === 403 && f.method !== 'GET' && f.user === actor.userKey)
  for (const f of refused) out.push({ rule: 3, message: `a manage call reached the backend for a non-owner: ${f.method} ${f.path}` })
  return out
}

// Rule 5: nobody became an operator of a space without being added on purpose.
export function rule5(fake, intended = new Set()) {
  const out = []
  for (const s of fake.spaces.values()) {
    for (const sub of s.operators) {
      const key = fake.users.get(sub)?.key
      if (!intended.has(`${s.space_id}:${key}`)) out.push({ rule: 5, message: `${key} is an operator of "${s.name}" without being added on purpose` })
    }
  }
  return out
}

// Every check that applies to this actor's current page, after a step.
export async function checkStep(actor, { fake, requests, failures, intendedOperators, space = null, canManage = true }) {
  const text = await visibleText(actor.page)
  return [
    ...rule18(text),
    ...rule17(text, failures),
    ...(await rule2(actor, requests)),
    ...(await rule3(actor, { space, canManage }, requests, failures)),
    ...rule5(fake, intendedOperators),
  ]
}
