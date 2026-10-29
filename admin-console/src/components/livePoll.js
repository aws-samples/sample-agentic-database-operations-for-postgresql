// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Keeps a panel that shows live state (a cluster's member roles, lag, reach) current
// while it is open: one load at once, then one every `intervalMs` while the browser tab
// is visible, and one more whenever the window regains focus or the tab is shown again.
// Nothing is loaded while the tab is hidden, and a load never overlaps another. The
// caller keeps its current data while a load runs, so a refresh never blanks a panel.
// No React here, so tests/test_live_poll.mjs drives it with fake timers and a fake
// document.
// The operator app keeps an identical copy (frontend/src/components).

export const LIVE_POLL_MS = 30000

export function startLivePoll({ load, onData, onError, intervalMs = LIVE_POLL_MS, env = {} }) {
  const doc = 'document' in env ? env.document : (typeof document !== 'undefined' ? document : null)
  const win = 'window' in env ? env.window : (typeof window !== 'undefined' ? window : null)
  const setTimer = env.setInterval || ((fn, ms) => setInterval(fn, ms))
  const clearTimer = env.clearInterval || ((t) => clearInterval(t))
  let stopped = false
  let inFlight = false
  let timer = null
  const visible = () => !doc || doc.visibilityState !== 'hidden'

  const run = (force = false) => {
    if (stopped || inFlight || (!force && !visible())) return
    inFlight = true
    Promise.resolve()
      .then(load)
      .then((d) => { if (!stopped) onData(d) }, (e) => { if (!stopped) onError?.(e) })
      .finally(() => { inFlight = false })
  }
  const arm = () => { if (!timer && visible()) timer = setTimer(() => run(), intervalMs) }
  const disarm = () => { if (timer) { clearTimer(timer); timer = null } }
  const onVisibility = () => { if (visible()) { run(); arm() } else disarm() }
  const onFocus = () => run()

  doc?.addEventListener?.('visibilitychange', onVisibility)
  win?.addEventListener?.('focus', onFocus)
  run(true)       // the first load happens even in a background tab, so the panel is never empty
  arm()
  const stop = () => {
    stopped = true
    disarm()
    doc?.removeEventListener?.('visibilitychange', onVisibility)
    win?.removeEventListener?.('focus', onFocus)
  }
  stop.refresh = () => run(true)   // one load now (skipped if one is running)
  return stop
}
