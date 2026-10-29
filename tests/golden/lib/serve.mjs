// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Builds the two apps when the harness's own build is older than their sources, then
// serves each with `vite preview` on a fixed port. The harness drives the BUILT bundles,
// made by the same `vite build` the deploy runs, never the dev server.
//
// The build goes to a directory the harness owns (BUILD_ROOT, under the OS temp dir),
// never to the app's dist/. After a real deploy, dist/config.js holds the DEPLOYED
// runtime config (written by deployment/deploy-spas.sh: the real API URL, Cognito
// domain and app name), and serving it would make every run depend on the machine's
// last deploy. The harness build carries the app's public/config.js placeholder, so the
// runs are hermetic, and dist/ (deployed config included) is never read, modified or
// deleted.
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const REPO = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..', '..')
export const APPS = {
  operator: { dir: 'frontend', port: Number(process.env.GOLDEN_OPERATOR_PORT || 4471) },
  admin: { dir: 'admin-console', port: Number(process.env.GOLDEN_ADMIN_PORT || 4472) },
}
// One build directory per checkout, so two clones on one machine do not share builds.
const CHECKOUT_TAG = Buffer.from(REPO).toString('base64url').slice(-24)
export const BUILD_ROOT = process.env.GOLDEN_BUILD_DIR || path.join(os.tmpdir(), `dbops-golden-build-${CHECKOUT_TAG}`)

// Where the harness builds and serves `appDir` from (never appDir/dist).
export function buildDir(appDir) {
  return path.join(BUILD_ROOT, path.basename(appDir))
}

function newestMtime(p) {
  let st
  try { st = fs.statSync(p) } catch { return 0 }
  if (!st.isDirectory()) return st.mtimeMs
  let m = 0
  for (const e of fs.readdirSync(p)) m = Math.max(m, newestMtime(path.join(p, e)))
  return m
}

// The harness build is fresh when it is newer than everything vite reads.
export function isFresh(appDir) {
  const built = newestMtime(path.join(buildDir(appDir), 'index.html'))
  if (!built) return false
  const inputs = ['src', 'public', 'index.html', 'vite.config.js', 'package-lock.json', 'tailwind.config.js', 'postcss.config.js']
  return inputs.every(i => newestMtime(path.join(appDir, i)) <= built)
}

export function ensureBuilt(log = console.log) {
  for (const [name, app] of Object.entries(APPS)) {
    const dir = path.join(REPO, app.dir)
    if (!fs.existsSync(path.join(dir, 'node_modules', '.bin', 'vite'))) {
      throw new Error(`${app.dir}/node_modules is missing. Run: (cd ${app.dir} && npm ci)`)
    }
    const out = buildDir(dir)
    if (isFresh(dir) && !process.env.GOLDEN_REBUILD) { log(`  reuse  ${app.dir} harness build (fresh)`); continue }
    log(`  build  ${app.dir} (${name} app) -> ${out}`)
    const r = spawnSync(path.join(dir, 'node_modules', '.bin', 'vite'),
      ['build', '--logLevel', 'error', '--outDir', out, '--emptyOutDir'], { cwd: dir, encoding: 'utf8' })
    if (r.status !== 0) throw new Error(`build of ${app.dir} failed:\n${r.stdout}\n${r.stderr}`)
  }
}

async function waitUp(url, ms = 20000) {
  const end = Date.now() + ms
  while (Date.now() < end) {
    try { if ((await fetch(url)).ok) return } catch { /* not yet */ }
    await new Promise(r => setTimeout(r, 150))
  }
  throw new Error(`no server answered at ${url}`)
}

// Starts both preview servers on the harness builds; returns { urls, stop }.
export async function serveApps() {
  const procs = []
  const stop = () => { for (const p of procs) { try { process.kill(-p.pid, 'SIGTERM') } catch { /* gone */ } } }
  process.on('exit', stop)
  const urls = {}
  for (const [name, app] of Object.entries(APPS)) {
    const dir = path.join(REPO, app.dir)
    const p = spawn(path.join(dir, 'node_modules', '.bin', 'vite'),
      ['preview', '--outDir', buildDir(dir), '--port', String(app.port), '--strictPort', '--host', '127.0.0.1'],
      { cwd: dir, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
    let err = ''
    p.stderr.on('data', d => { err += d })
    p.on('exit', code => { if (code) process.stderr.write(`vite preview for ${app.dir} exited ${code}: ${err}\n`) })
    procs.push(p)
    urls[name] = `http://127.0.0.1:${app.port}`
  }
  await Promise.all(Object.values(urls).map(u => waitUp(u)))
  // The runtime config the pages load must be the placeholder from public/, whatever a
  // deploy left in dist/: snapshots of the app name or sign-in state depend on it.
  for (const [name, app] of Object.entries(APPS)) {
    const want = fs.readFileSync(path.join(REPO, app.dir, 'public', 'config.js'), 'utf8')
    const got = await (await fetch(`${urls[name]}/config.js`)).text()
    if (got !== want) { stop(); throw new Error(`${app.dir}: the served config.js is not public/config.js; rebuild with GOLDEN_REBUILD=1`) }
  }
  return { urls, stop }
}
