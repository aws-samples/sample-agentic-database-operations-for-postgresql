// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Live member panels refresh by themselves while open (product rule 21, GH-1).
//
// ClusterMembers, the member picker and the chat's focus picker read the members API
// through useLiveMembers, which runs startLivePoll (components/livePoll.js, identical in
// both apps). Driven here with fake timers, a fake document and a fake window:
//   * one load at once, then one per 30 s interval while the tab is visible;
//   * none while the tab is hidden; showing it again, or focusing the window, loads at once;
//   * loads never overlap; a failed load is reported and the next one still runs;
//   * after stop, nothing loads and no late answer is delivered;
//   * a failover between two loads reaches the panel as the new roles (the roles swap).
//
// Run:  node tests/test_live_poll.mjs
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
let passed = 0, failed = 0
const check = (cond, label) => { if (cond) { passed++; console.log(`  ok   ${label}`) } else { failed++; console.log(`  FAIL ${label}`) } }
const settle = () => new Promise(r => setTimeout(r, 0))

function fakeEnv() {
  const listeners = { doc: {}, win: {} }
  const timers = new Map()
  let nextId = 1
  const env = {
    document: {
      visibilityState: 'visible',
      addEventListener: (t, fn) => { listeners.doc[t] = fn },
      removeEventListener: (t, fn) => { if (listeners.doc[t] === fn) delete listeners.doc[t] },
    },
    window: {
      addEventListener: (t, fn) => { listeners.win[t] = fn },
      removeEventListener: (t, fn) => { if (listeners.win[t] === fn) delete listeners.win[t] },
    },
    setInterval: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id },
    clearInterval: (id) => { timers.delete(id) },
  }
  const tick = async () => { for (const t of [...timers.values()]) t.fn(); await settle(); await settle() }
  const hide = async () => { env.document.visibilityState = 'hidden'; listeners.doc.visibilitychange?.(); await settle() }
  const show = async () => { env.document.visibilityState = 'visible'; listeners.doc.visibilitychange?.(); await settle(); await settle() }
  const focus = async () => { listeners.win.focus?.(); await settle(); await settle() }
  return { env, timers, listeners, tick, hide, show, focus }
}

for (const app of ['admin-console', 'frontend']) {
  const { startLivePoll, LIVE_POLL_MS } = await import(pathToFileURL(path.join(ROOT, app, 'src/components/livePoll.js')).href)
  console.log(`== ${app}: polls while visible ==`)
  check(LIVE_POLL_MS === 30000, 'the interval is 30 s')
  let f = fakeEnv(), loads = 0, got = []
  let stop = startLivePoll({ load: async () => ++loads, onData: d => got.push(d), env: f.env })
  await settle(); await settle()
  check(loads === 1 && got.join() === '1', `one load at once (${loads})`)
  check([...f.timers.values()].every(t => t.ms === 30000) && f.timers.size === 1, 'one 30 s timer')
  await f.tick(); await f.tick()
  check(loads === 3 && got.join() === '1,2,3', `one load per interval (${got.join()})`)

  console.log(`== ${app}: hidden tab, focus ==`)
  await f.hide()
  check(f.timers.size === 0, 'the timer stops while the tab is hidden')
  await f.tick()
  check(loads === 3, 'nothing loads while hidden')
  await f.focus()
  check(loads === 3, 'a focus event while hidden loads nothing')
  await f.show()
  check(loads === 4 && f.timers.size === 1, `showing the tab loads at once and restarts the timer (${loads})`)
  await f.focus()
  check(loads === 5, 'focusing the window loads at once')
  stop.refresh(); await settle(); await settle()
  check(loads === 6, 'refresh() loads at once (a panel joining a shared poll)')
  stop()
  check(f.timers.size === 0 && !f.listeners.doc.visibilitychange && !f.listeners.win.focus, 'stop clears the timer and the listeners')
  await f.tick(); await f.focus()
  check(loads === 6, 'nothing loads after stop')

  console.log(`== ${app}: first load in a background tab ==`)
  f = fakeEnv(); f.env.document.visibilityState = 'hidden'; loads = 0
  stop = startLivePoll({ load: async () => ++loads, onData: () => {}, env: f.env })
  await settle(); await settle()
  check(loads === 1 && f.timers.size === 0, 'the first load still happens (the panel is never empty), no timer')
  stop()

  console.log(`== ${app}: no overlap, errors, late answers ==`)
  f = fakeEnv(); loads = 0
  let release
  stop = startLivePoll({ load: () => { loads++; return new Promise(r => { release = r }) }, onData: d => got.push(d), env: f.env })
  await settle()
  await f.tick(); await f.focus()
  check(loads === 1, `a load never overlaps another (${loads})`)
  release('x'); await settle(); await settle()
  await f.tick()
  check(loads === 2, 'the next interval loads once the first finished')
  got = []
  stop(); release('late'); await settle(); await settle()
  check(got.length === 0, 'an answer that arrives after stop is dropped')

  f = fakeEnv(); loads = 0
  const errs = []; got = []
  stop = startLivePoll({ load: async () => { loads++; if (loads === 2) throw new Error('HTTP 503'); return loads },
    onData: d => got.push(d), onError: e => errs.push(e.message), env: f.env })
  await settle(); await settle(); await f.tick(); await f.tick()
  check(errs.join() === 'HTTP 503' && got.join() === '1,3', `a failed load is reported and the next one runs (${got.join()} / ${errs.join()})`)
  stop()

  console.log(`== ${app}: a failover reaches the panel ==`)
  f = fakeEnv()
  let writer = 'orders-1'
  const answer = () => ({ members: [
    { instance_id: 'orders-1', role: writer === 'orders-1' ? 'writer' : 'reader' },
    { instance_id: 'orders-2', role: writer === 'orders-2' ? 'writer' : 'reader' }] })
  let shown = null
  stop = startLivePoll({ load: async () => answer(), onData: d => { shown = d }, env: f.env })
  await settle(); await settle()
  const roleOf = (id) => shown.members.find(m => m.instance_id === id).role
  check(roleOf('orders-1') === 'writer', 'orders-1 is the writer')
  writer = 'orders-2'          // Tier 1 records the failover
  await f.tick()
  check(roleOf('orders-2') === 'writer' && roleOf('orders-1') === 'reader', 'within one poll the roles swap, with the panel open')
  stop()
}

console.log(`\npass=${passed} fail=${failed}`)
process.exit(failed ? 1 : 0)
