// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// What a scenario talks to. One Harness per scenario: a fresh FakeBackend, the browser
// contexts of the people in it (one per user and app, each signed in with an unsigned
// JWT the fake mints from its current state), and `step`, which runs one user step and
// then checks the product rules on the page, the fake's coverage and, at a named
// checkpoint, the golden text snapshot.
//
// Network: every /api/** request goes to the fake; the two preview servers are reached
// normally; ANY other request (Cognito, AWS, fonts on a CDN) is aborted and listed, so a
// run can never reach AWS.
import fs from 'node:fs'
import path from 'node:path'
import { FakeBackend } from '../fake_api.mjs'
import { checkStep, visibleText, RULE_TITLES } from '../rules.mjs'
import { KNOWN_BUGS } from '../known_bugs.mjs'

const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-headers': 'authorization, content-type',
  'access-control-allow-methods': 'GET, POST, PUT, DELETE, OPTIONS',
}

export class StepFailure extends Error {
  constructor(message, rule = null) { super(rule ? `[rule ${rule}: ${RULE_TITLES[rule] || ''}] ${message}` : message); this.rule = rule }
}

export class Harness {
  constructor({ browser, urls, scenario, snapshots, outDir, log = () => {} }) {
    this.browser = browser
    this.urls = urls
    this.scenario = scenario
    this.snapshots = snapshots
    this.outDir = path.join(outDir, scenario.id)
    fs.mkdirSync(this.outDir, { recursive: true })
    this.log = log
    this.fake = new FakeBackend(scenario.fake || {})
    this.actors = []
    this.steps = []
    this.intendedOperators = new Set()
    this.shot = 0
  }

  // A signed-in browser tab for `userKey` in `app` ('admin' or 'operator').
  // `clock: true` installs Playwright's fake clock in the page (time still flows), so a
  // step can fast-forward it (actor.page.clock.runFor) and fire the page's own timers:
  // "updates on its own within N minutes" is then testable in a second.
  async open(app, userKey, { path: p = '/', clock = false } = {}) {
    const user = this.fake.userByKey(userKey)
    if (!user) throw new Error(`no fixture user ${userKey}`)
    const ctx = await this.browser.newContext({ viewport: { width: 1440, height: 900 } })
    const token = this.fake.tokenFor(userKey)
    const keys = app === 'admin' ? ['admin_access_token', 'admin_id_token'] : ['access_token', 'id_token']
    await ctx.addInitScript(([t, ks]) => { for (const k of ks) localStorage.setItem(k, t) }, [token, keys])
    const actor = { app, userKey, role: user.role, email: user.email, sub: user.sub, ctx, page: null, errors: [], dialogs: [], blocked: [], navigations: 0 }
    const local = new Set(Object.values(this.urls).map(u => new URL(u).host))
    await ctx.route('**/*', async (route) => {
      const req = route.request()
      const u = new URL(req.url())
      if (u.pathname.startsWith('/api/')) {
        if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
        let body = null
        try { body = req.postDataJSON() } catch { body = null }
        const res = this.fake.handle({ method: req.method(), url: req.url(), headers: req.headers(), body, app })
        return route.fulfill({ status: res.status, contentType: 'application/json', headers: CORS, body: JSON.stringify(res.body) })
      }
      if (local.has(u.host)) return route.continue()
      actor.blocked.push(`${req.method()} ${u.origin}${u.pathname}`)
      return route.abort('blockedbyclient')
    })
    const page = await ctx.newPage()
    page.setDefaultTimeout(Number(process.env.GOLDEN_TIMEOUT_MS || 8000))
    if (clock) await page.clock.install()
    actor.page = page
    page.on('pageerror', e => actor.errors.push(`page error: ${e.message}`))
    page.on('dialog', d => { actor.dialogs.push(d.message()); d.accept().catch(() => {}) })
    page.on('framenavigated', f => { if (f === page.mainFrame()) actor.navigations++ })
    await page.goto(`${this.urls[app]}${p}`, { waitUntil: 'networkidle' })
    this.actors.push(actor)
    return actor
  }

  // A user added on purpose as an operator (rule 5 allows exactly these).
  intendOperator(spaceId, userKey) { this.intendedOperators.add(`${spaceId}:${userKey}`) }

  // Runs one user step, then the rule checks on `actor`'s page. opts:
  //   checkpoint   name of a golden text snapshot taken after the step
  //   region       CSS selector the snapshot reads (default: main / #root)
  //   space        the space the page shows, and canManage whether actor manages it (rule 3)
  async step(name, actor, fn, opts = {}) {
    const mark = this.fake.requests.length
    const fmark = this.fake.failures.length
    const nav = actor ? actor.navigations : 0
    const started = Date.now()
    const rec = { name, ok: true, ms: 0, problems: [], xfail: null }
    this.steps.push(rec)
    try {
      await fn()
    } catch (e) {
      rec.ok = false
      rec.problems.push(e instanceof StepFailure ? e.message : `step error: ${e.message.split('\n')[0]}`)
      rec.fatal = true
    }
    if (actor) {
      const requests = this.fake.since(mark)
      const failures = this.fake.failures.slice(fmark)
      const violations = await checkStep(actor, { fake: this.fake, requests, failures, intendedOperators: this.intendedOperators,
        space: opts.space || null, canManage: opts.canManage !== false })
      for (const v of violations) { rec.ok = false; rec.problems.push(`[rule ${v.rule}: ${RULE_TITLES[v.rule]}] ${v.message}`) }
      if (!opts.allowNavigation && actor.navigations > nav && actor.app === 'admin') {
        rec.ok = false
        rec.problems.push(`[rule 15: ${RULE_TITLES[15]}] the page reloaded during the step`)
      }
      for (const e of actor.errors.splice(0)) { rec.ok = false; rec.problems.push(e) }
      if (opts.checkpoint && !rec.fatal) {
        const region = opts.region || (actor.app === 'admin' ? 'main' : '#root')
        const text = await visibleText(actor.page, region)
        const d = this.snapshots.check(this.scenario.id, opts.checkpoint, text)
        if (d) { rec.ok = false; rec.problems.push(d) }
      }
      await actor.page.screenshot({ path: path.join(this.outDir, `${String(++this.shot).padStart(2, '0')}-${name.replace(/\W+/g, '-').slice(0, 60)}.png`), fullPage: true }).catch(() => {})
    }
    const unhandled = this.fake.unhandled.splice(0)
    if (unhandled.length) { rec.ok = false; rec.problems.push(`the fake backend does not implement: ${[...new Set(unhandled)].join(', ')} (add it to tests/golden/fake_api.mjs)`) }
    rec.ms = Date.now() - started
    this.log(`    ${rec.ok ? 'ok  ' : 'FAIL'} ${name} (${rec.ms} ms)`)
    for (const p of rec.problems) this.log(p.split('\n').map(l => `         ${l}`).join('\n'))
    if (rec.fatal) throw new StepAbort()
    return rec
  }

  // Waits (without touching the page) until `fn` returns truthy. A timeout names the
  // rule: the state was supposed to appear on its own (rule 15) unless told otherwise.
  async eventually(fn, { timeout = 10000, what = 'the expected state', rule = 15 } = {}) {
    const end = Date.now() + timeout
    let last = ''
    while (Date.now() < end) {
      this.fake.tick()
      try { if (await fn()) return } catch (e) { last = e.message.split('\n')[0] }
      await new Promise(r => setTimeout(r, 200))
    }
    throw new StepFailure(`${what} did not appear within ${timeout / 1000}s without a manual refresh${last ? ` (${last})` : ''}`, rule)
  }

  expect(cond, message, rule = null) { if (!cond) throw new StepFailure(message, rule) }

  // A known PRODUCT bug (known_bugs.mjs): `fn` asserts the correct behaviour. When it
  // fails, the step is an expected failure carrying the bug's text; when it passes, the
  // bug is fixed and the marker should go (reported as XPASS; GOLDEN_STRICT_XFAIL=1
  // turns that into a failure).
  async xfail(bugId, fn) {
    const bug = KNOWN_BUGS[bugId]
    if (!bug) throw new Error(`unknown bug id ${bugId}`)
    const rec = this.steps[this.steps.length - 1]
    try {
      await fn()
      rec.xpass = (rec.xpass || []).concat(bugId)
      this.log(`         XPASS ${bugId}: fixed? remove the expected-failure marker (${bug.title})`)
      if (process.env.GOLDEN_STRICT_XFAIL) { rec.ok = false; rec.problems.push(`${bugId} passes now: remove its xfail marker`) }
      return true
    } catch (e) {
      rec.xfail = (rec.xfail || []).concat({ bugId, got: e.message.split('\n')[0] })
      this.log(`         XFAIL ${bugId} [rule ${bug.rule}] ${bug.title}`)
      return false
    }
  }

  async close() { for (const a of this.actors) await a.ctx.close().catch(() => {}) }
}

export class StepAbort extends Error { constructor() { super('scenario stopped at a failed step') } }

// Fixed fixture people. Keys are stable so snapshots and request logs are too.
export function standardUsers(fake) {
  fake.addUser('super', { email: 'super@example.com', name: 'Sam Super', role: 'admins' })
  fake.addUser('admin-a', { email: 'alice@example.com', name: 'Alice Admin', role: 'space-admins' })
  fake.addUser('admin-b', { email: 'bob@example.com', name: 'Bob Admin', role: 'space-admins' })
  fake.addUser('op-1', { email: 'olga@example.com', name: 'Olga Operator', role: 'operators' })
  fake.addUser('op-2', { email: 'omar@example.com', name: 'Omar Operator', role: 'operators' })
}
