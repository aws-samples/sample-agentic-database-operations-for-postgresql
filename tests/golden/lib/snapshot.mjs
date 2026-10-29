// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Golden snapshots as TEXT, not pixels: the visible text of a page region (headings,
// tiles, table rows, banners, messages), one line per rendered line, with whatever
// changes from run to run masked. Compared with tests/golden/snapshots/<scenario>/
// <checkpoint>.txt; GOLDEN_UPDATE=1 (make golden-update) rewrites them.
import fs from 'node:fs'
import path from 'node:path'

const MONTH = '(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
const MASKS = [
  [/\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g, '<ts>'],
  [/\b\d{1,2}\/\d{1,2}\/\d{2,4}(?:,? \d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)?/g, '<date>'],
  [new RegExp(`\\b${MONTH} \\d{1,2},? \\d{4}(?:,? \\d{1,2}:\\d{2}(?::\\d{2})?(?:\\s?[AP]M)?)?`, 'g'), '<date>'],
  [new RegExp(`\\b\\d{1,2} ${MONTH},? \\d{4}(?:,? \\d{1,2}:\\d{2}(?::\\d{2})?)?`, 'g'), '<date>'],
  // Year-less "Mon D" dates (e.g. the audit log renders month+day only, no year): the
  // year-ful masks above have already consumed any dated-with-year form, so whatever
  // "Mon D" remains is a bare date and must be masked too, or a midnight rollover flips
  // a live-generated timestamp ("Oct 6" -> "Oct 7") and breaks the snapshot.
  [new RegExp(`\\b${MONTH} \\d{1,2}\\b`, 'g'), '<date>'],
  [/\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?\b/g, '<time>'],
  [/\b(?:just now|\d+ (?:s|sec|min|mins|h|d|hours?|days?|minutes?) ago)\b/g, '<ago>'],
  [/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, '<uuid>'],
  [/\b(?:vpce|pcx|rtb|sg|vpc|eni|rcfg|rgw)-[0-9a-f]{6,}\b/g, '<aws-id>'],
]

export function normalise(text) {
  return String(text || '')
    .split('\n')
    .map(l => MASKS.reduce((s, [re, to]) => s.replace(re, to), l).replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join('\n') + '\n'
}

// Line diff (LCS). Small inputs only, which snapshots are.
export function diff(a, b) {
  const x = a.split('\n'), y = b.split('\n')
  const n = x.length, m = y.length
  const L = Array.from({ length: n + 1 }, () => new Int32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1])
  const out = []
  let i = 0, j = 0
  while (i < n || j < m) {
    if (i < n && j < m && x[i] === y[j]) { out.push(`   ${x[i]}`); i++; j++ }
    else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { out.push(` + ${y[j]}`); j++ }
    else { out.push(` - ${x[i]}`); i++ }
  }
  // Show changed lines with two lines of context.
  const keep = new Set()
  out.forEach((l, k) => { if (!l.startsWith('   ')) for (let d = -2; d <= 2; d++) keep.add(k + d) })
  const shown = []
  let last = -2
  out.forEach((l, k) => { if (keep.has(k)) { if (k !== last + 1) shown.push('   …'); shown.push(l); last = k } })
  return shown.join('\n')
}

export class Snapshots {
  constructor(dir, { update = false } = {}) { this.dir = dir; this.update = update; this.count = 0; this.written = 0 }
  // Returns '' when the text matches (or was written), else a readable diff.
  check(scenario, checkpoint, rawText) {
    const text = normalise(rawText)
    const file = path.join(this.dir, scenario, `${checkpoint}.txt`)
    this.count++
    const have = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null
    if (this.update) {
      if (have !== text) {
        fs.mkdirSync(path.dirname(file), { recursive: true })
        fs.writeFileSync(file, text)
        this.written++
      }
      return ''
    }
    if (have === null) return `no golden snapshot ${path.relative(process.cwd(), file)}. Run make golden-update to record it, review it, and commit it.\n--- what the page shows now ---\n${text}`
    if (have === text) return ''
    return `snapshot ${scenario}/${checkpoint} differs (- golden, + now). Run make golden-update if the change is intended.\n${diff(have, text)}`
  }
}
