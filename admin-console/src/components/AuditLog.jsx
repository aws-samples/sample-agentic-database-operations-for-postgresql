// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// The admin audit log: every privileged admin action, newest first. One component,
// two places. Without `spaceId` it is the account-wide log in Settings → Audit
// (super admins, GET /api/audit, with a Space filter); with `spaceId` it is a space's
// Audit tab (its creator or a super admin, GET /api/spaces/{id}/audit). The API pages
// by an opaque cursor and never counts (that would need a Scan), so the pager runs in
// open mode and a sparse filter is searched one chunk per click ("Search older records").
// A row opens a side sheet with the field diff and a "Verify against vault" check
// that compares the index row with its locked S3 copy.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Building2, ChevronRight, Cpu, Database, FileText, RefreshCw, Shield,
  ShieldCheck, SlidersHorizontal, User,
} from 'lucide-react'
import SideSheet from './SideSheet'
import Pager from './Pager'
import { Chips, PageBar } from './RecordsSheet'
import usePagedRecords from '../hooks/usePagedRecords'

const PAGE_SIZE = 25
const CHUNK = 100
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
// The server's actor rule: a Cognito sub or a full email address.
const ACTOR_RE = /^(?:[A-Za-z0-9-]{1,64}|[^@\s]{1,64}@[^@\s]{1,255})$/

const RANGES = [['1', '24 h'], ['7', '7 d'], ['30', '30 d'], ['90', '90 d'], ['all', 'All'], ['custom', 'Custom']]
const DEFAULT_RANGE = '30'
// A relative range is fixed when it is chosen (to the minute), so the filter key, and
// with it the loaded pages, stay put while the reader browses.
const sinceDays = (days) => {
  const d = new Date(Date.now() - Number(days) * 86400000)
  d.setUTCSeconds(0, 0)
  return d.toISOString().replace('.000Z', 'Z')
}

const TARGET_GROUPS = [
  ['space', 'Spaces'], ['database', 'Databases'], ['user', 'Users and operators'],
  ['knowledge_doc', 'Documents'], ['model', 'Models'], ['governance', 'Governance'], ['setting', 'Settings'],
]
const TARGET_ICONS = {
  database: Database, space: Building2, user: User, knowledge_doc: FileText, model: Cpu,
  setting: SlidersHorizontal, governance: SlidersHorizontal,
}
const ROLE_BADGES = {
  super_admin: ['Super admin', 'bg-primary-subtle text-primary border-primary-line'],
  space_admin: ['Space admin', 'bg-surface-2 text-fg-2 border-line'],
  system: ['System', 'bg-surface-2 text-fg-3 border-line'],
  operator: ['Agent', 'bg-info-bg text-info-text border-info-border'],
}
const BADGE = 'inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium border whitespace-nowrap'
const MISMATCH_WORDS = {
  hash: 'the record hash differs',
  s3_checksum: 'the stored object checksum differs',
  timestamp: 'the time differs',
  single_version: 'the object has more than one version',
  fields: 'recorded fields differ',
  key_not_derived: 'the vault key does not match the record',
  index_hides_vault_copy: 'the index says there is no vault copy, but there is one',
}

const th = 'py-2 px-2 text-left text-[10px] uppercase tracking-wider text-fg-3 font-semibold whitespace-nowrap'
const td = 'py-1.5 px-2 text-xs text-fg-2 align-top'
const inputCls = 'px-2 py-1 text-xs rounded-lg border border-line bg-surface text-fg placeholder:text-fg-muted focus:outline-none focus:border-primary-line'

// The index sort key is `<iso time>#<suffix>`: the time is the part before '#'.
const isoOf = (ts) => (ts || '').split('#')[0]
const fmtTime = (ts) => {
  const d = new Date(isoOf(ts))
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
const fmtDate = (iso) => {
  const d = new Date(iso)
  return iso && !Number.isNaN(d.getTime()) ? d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—'
}
// `scanned_to` is a month ('YYYY-MM') on the account-wide walk and an index ts in a space.
const fmtScanned = (s) => {
  if (!s) return ''
  if (/^\d{4}-\d{2}$/.test(s)) return new Date(`${s}-01T00:00:00Z`).toLocaleDateString(undefined, { year: 'numeric', month: 'long', timeZone: 'UTC' })
  return fmtDate(isoOf(s))
}
// A 502's body is the app's generic one, so it gets its own words; the rest carry a
// fixed server message.
const errorText = (e, fallback) => !e ? '' : e.status === 502 ? fallback : (e.detail || e.message || fallback)
const who = (row) => row.actor_role === 'system' ? 'System' : (row.actor_email || row.actor_sub || 'System')
const rowKey = (row, spaceId) => spaceId ? row.ts : `${row.entity || ''}|${row.ts}`

// The verify URL is built with URLSearchParams, never concatenated: `ts` holds '+' and '#'.
export function verifyPath(row, spaceId) {
  if (spaceId) return `/api/spaces/${encodeURIComponent(spaceId)}/audit/verify?${new URLSearchParams({ ts: row.ts })}`
  return `/api/audit/verify?${new URLSearchParams({ entity: row.entity || '', ts: row.ts })}`
}

function VaultCell({ vault }) {
  const [Icon, cls, text] = vault === 'stored' ? [ShieldCheck, 'text-ok', 'In vault']
    : vault === 'failed' ? [AlertTriangle, 'text-warn', 'Not in vault']
      : vault === 'off' ? [Shield, 'text-fg-muted', 'Index only']
        : [Shield, 'text-fg-muted', 'Legacy']
  return (
    <span className="inline-flex items-center" title={text}>
      <Icon size={14} strokeWidth={1.75} className={cls} aria-hidden />
      <span className="sr-only">{text}</span>
    </span>
  )
}

function RoleBadge({ role }) {
  const b = ROLE_BADGES[role]
  return b ? <span className={`${BADGE} ${b[1]}`}>{b[0]}</span> : null
}

const fieldText = (v) => v === undefined ? '' : (typeof v === 'string' ? v : JSON.stringify(v, null, 2))

function DiffTable({ before, after }) {
  const b = before && typeof before === 'object' ? before : {}
  const a = after && typeof after === 'object' ? after : {}
  const keys = [...new Set([...Object.keys(b), ...Object.keys(a)])].sort()
  if (!keys.length) return <p className="text-xs text-fg-muted">No field changes recorded for this action.</p>
  return (
    <table className="w-full table-fixed">
      <thead><tr className="border-b border-line">
        <th scope="col" className={`${th} w-1/4`}>Field</th><th scope="col" className={th}>Before</th><th scope="col" className={th}>After</th>
      </tr></thead>
      <tbody>
        {keys.map(k => {
          const changed = fieldText(b[k]) !== fieldText(a[k])
          return (
            <tr key={k} className={`border-b border-line-2 ${changed ? 'bg-info-bg' : ''}`}>
              <th scope="row" className="py-1.5 px-2 text-left text-xs font-mono font-normal text-fg align-top break-all">
                {k}{changed && <span className="sr-only"> (changed)</span>}
              </th>
              {[b[k], a[k]].map((v, i) => (
                <td key={i} className={td}>
                  {v === undefined ? <span className="text-fg-muted">—</span>
                    : <pre className="whitespace-pre-wrap break-all font-mono text-[11px] text-fg-2 m-0">{fieldText(v)}</pre>}
                </td>
              ))}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function VerifyResult({ result }) {
  if (!result) return null
  if (result.error) return <p className="text-xs text-err">{result.error}</p>
  const s = result.status
  const line = (badgeCls, label, text) => (
    <p className="text-xs text-fg-2 flex items-start gap-2">
      <span className={`${BADGE} ${badgeCls}`}>{label}</span><span className="leading-relaxed">{text}</span>
    </p>
  )
  if (s === 'verified') return line('bg-ok-bg text-ok-text border-ok-border', 'Verified',
    result.retain_until ? `Locked until ${fmtDate(result.retain_until)}${result.lock_mode ? ` (${result.lock_mode})` : ''}.` : 'The vault copy matches the index.')
  if (s === 'mismatch') {
    const reasons = (result.reasons || []).map(r => MISMATCH_WORDS[r] || r.replace(/_/g, ' ')).join('; ')
    return line('bg-err-bg text-err-text border-err-border', 'Mismatch',
      `The vault copy differs from the index (${reasons || 'unspecified'}). The vault copy is shown below and is authoritative.`)
  }
  if (s === 'not_in_vault') return line('bg-warn-bg text-warn-text border-warn-border', 'Not in vault',
    result.vault === 'off' ? 'The vault was off when this happened.'
      : result.vault === 'failed' ? 'The vault write failed.' : 'This record was written before the vault existed.')
  if (s === 'expired') return line('bg-surface-2 text-fg-3 border-line', 'Expired',
    `Retention ended ${fmtDate(result.retain_until)}; the vault copy has been removed as configured.`)
  if (s === 'missing') return line('bg-err-bg text-err-text border-err-border', 'Missing',
    'The index names a vault copy that is not there.')
  return <p className="text-xs text-fg-3">Unexpected result.</p>
}

function AuditEventSheet({ api, row, spaceId, isSuperAdmin, onClose }) {
  const [verifying, setVerifying] = useState(false)
  const [result, setResult] = useState(null)
  // A mismatch makes the vault copy the record shown: it is the authoritative one.
  const shown = result?.status === 'mismatch' && result.record ? { ...row, ...result.record } : row
  const verify = async () => {
    setVerifying(true); setResult(null)
    try { setResult(await api.get(verifyPath(row, spaceId))) }
    catch (e) { setResult({ error: errorText(e, 'Could not read the audit vault. Try again.') }) }
    finally { setVerifying(false) }
  }
  const items = [
    ['Who', who(shown)],
    ['Role', ROLE_BADGES[shown.actor_role]?.[0] || 'Not recorded'],
    ['Action', <span key="a">{shown.action_label} <span className="font-mono text-fg-muted">{shown.action}</span></span>],
    ['Target', [shown.target_type, shown.target_label, shown.target_id].filter(Boolean).join(' · ') || '—'],
    ['Space', shown.space_id ? `${shown.space_name || 'Unnamed'} · ${shown.space_id}` : 'Account'],
    ["Acted on another admin's space", shown.on_other_owner ? 'Yes' : 'No'],
  ]
  if (isSuperAdmin) {
    items.push(
      ['Owner admin', shown.owner_admin || '—'],
      ['Request id', shown.request_id || '—'],
      ['Source IP', shown.source_ip ? `${shown.source_ip} (as reported by API Gateway)` : '—'],
      ['S3 key', shown.s3_key ? `${shown.s3_key}${shown.s3_version_id ? ` · version ${shown.s3_version_id}` : ''}` : '—'],
    )
  }
  return (
    <SideSheet title={shown.action_label || shown.action || 'Audit event'} width="max-w-[720px]" onClose={onClose}
      subtitle={`${fmtTime(shown.ts)} · ${who(shown)}`}>
      <div className="px-6 py-5 space-y-6">
        <dl className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)] gap-x-4 gap-y-2 text-xs">
          {items.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-fg-3">{k}</dt>
              <dd className="text-fg-2 break-all">{v}</dd>
            </div>
          ))}
        </dl>
        <div>
          <h3 className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold mb-2">Changes</h3>
          <DiffTable before={shown.before} after={shown.after} />
        </div>
        <div className="space-y-2">
          <button onClick={verify} disabled={verifying}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-line rounded-lg text-fg-2 hover:bg-surface-3 disabled:opacity-50">
            <ShieldCheck size={13} strokeWidth={1.75} aria-hidden />{verifying ? 'Verifying…' : 'Verify against vault'}
          </button>
          <div aria-live="polite"><VerifyResult result={result} /></div>
        </div>
      </div>
    </SideSheet>
  )
}

export default function AuditLog({ api, spaceId = null, isSuperAdmin }) {
  const global = !spaceId
  const [range, setRange] = useState(DEFAULT_RANGE)
  const [since, setSince] = useState(() => sinceDays(DEFAULT_RANGE))
  const [customFrom, setCustomFrom] = useState('')
  const [customTo, setCustomTo] = useState('')
  const [action, setAction] = useState('')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [whoInput, setWhoInput] = useState('')
  const [actor, setActor] = useState('')
  const [spaceSel, setSpaceSel] = useState('')
  const [otherId, setOtherId] = useState('')
  const [spaceFilter, setSpaceFilter] = useState('')
  const [meta, setMeta] = useState(null)
  const [spaces, setSpaces] = useState([])
  const [scannedTo, setScannedTo] = useState(null)
  const [openRow, setOpenRow] = useState(null)
  const otherRef = useRef(null)

  // Search and Who are debounced; Who only filters on a value the server accepts.
  useEffect(() => { const t = setTimeout(() => setQ(search.trim()), 300); return () => clearTimeout(t) }, [search])
  const whoValue = whoInput.trim()
  const whoInvalid = !!whoValue && !ACTOR_RE.test(whoValue)
  useEffect(() => {
    if (whoInvalid) return
    const t = setTimeout(() => setActor(whoValue), 300)
    return () => clearTimeout(t)
  }, [whoValue, whoInvalid])
  // "Other space id…" fires only for a well-formed id; until then the last filter stays.
  const otherInvalid = spaceSel === 'other' && !!otherId.trim() && !UUID_RE.test(otherId.trim())
  useEffect(() => {
    if (spaceSel !== 'other') setSpaceFilter(spaceSel)
    else if (UUID_RE.test(otherId.trim())) setSpaceFilter(otherId.trim().toLowerCase())
  }, [spaceSel, otherId])

  useEffect(() => {
    let live = true
    api.get('/api/audit/meta').then(d => { if (live) setMeta(d) }).catch(() => {})
    if (global) api.get('/api/spaces').then(d => { if (live) setSpaces(Array.isArray(d) ? d : []) }).catch(() => {})
    return () => { live = false }
  }, [api, global])

  const filters = {
    from: range === 'custom' ? customFrom : range === 'all' ? '' : since,
    to: range === 'custom' ? customTo : '',
    action, actor, q, space_id: global ? spaceFilter : '',
  }
  const filtersKey = JSON.stringify(filters)
  const anyFilter = !!(filters.action || filters.actor || filters.q || filters.space_id || range !== DEFAULT_RANGE)
  const buildPath = useCallback((cursor, chunk) => {
    const f = JSON.parse(filtersKey)
    const p = new URLSearchParams({ limit: String(chunk) })
    for (const k of ['from', 'to', 'action', 'actor', 'q', 'space_id']) if (f[k]) p.set(k, f[k])
    if (cursor) p.set('cursor', cursor)
    const base = spaceId ? `/api/spaces/${encodeURIComponent(spaceId)}/audit` : '/api/audit'
    return `${base}?${p}`
  }, [spaceId, filtersKey])
  const onResponse = useCallback((d) => setScannedTo(d?.scanned_to || null), [])
  const page = usePagedRecords(api, buildPath, [spaceId, filtersKey], { pageSize: PAGE_SIZE, chunk: CHUNK, onResponse })
  const turn = (fn) => (...a) => { setOpenRow(null); fn(...a) }

  const actionGroups = useMemo(() => {
    const list = meta?.actions || []
    const groups = TARGET_GROUPS.map(([t, label]) => [label, list.filter(a => a.target_type === t)])
    const other = list.filter(a => !TARGET_GROUPS.some(([t]) => t === a.target_type))
    if (other.length) groups.push(['Other', other])
    return groups.filter(([, items]) => items.length)
  }, [meta])

  const chooseRange = (v) => {
    setOpenRow(null); setRange(v)
    if (v !== 'all' && v !== 'custom') setSince(sinceDays(v))
  }
  const openDeletedSpace = (id) => { setSpaceSel('other'); setOtherId(id) }
  const scanned = fmtScanned(scannedTo)

  return (
    <section aria-label="Audit log" className="bg-surface rounded-xl border border-line">
      <div className="px-4 pt-4">
        <h2 className="text-sm font-semibold text-fg">Audit log</h2>
        <p className="text-xs text-fg-3 mt-0.5">
          {global ? 'Every privileged admin action in this deployment, newest first.' : 'Every admin action on this space, newest first.'}
          {meta && (meta.vault_enabled
            ? ` Vault on: new records are locked for ${meta.effective_retention_days} days.`
            : ' Vault off: new records go to the index only.')}
        </p>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-line-2 flex-wrap">
        <input type="search" aria-label="Search audit log" value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search by person, action, target or space" className={`${inputCls} w-64`} />
        <Chips options={RANGES} value={range} onChange={chooseRange} label="Time range" />
        {range === 'custom' && (
          <span className="flex items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-fg-3">From
              <input type="date" value={customFrom} max={customTo || undefined} onChange={e => setCustomFrom(e.target.value)} className={inputCls} />
            </label>
            <label className="flex items-center gap-1 text-[11px] text-fg-3">To
              <input type="date" value={customTo} min={customFrom || undefined} onChange={e => setCustomTo(e.target.value)} className={inputCls} />
            </label>
          </span>
        )}
        <select aria-label="Action" value={action} onChange={e => setAction(e.target.value)} className={inputCls}>
          <option value="">All actions</option>
          {actionGroups.map(([label, items]) => (
            <optgroup key={label} label={label}>
              {items.map(a => <option key={a.code} value={a.code}>{a.label}</option>)}
            </optgroup>
          ))}
        </select>
        <span className="flex flex-col">
          <input aria-label="Filter by person (email)" value={whoInput} onChange={e => setWhoInput(e.target.value)}
            placeholder="Who (email)" aria-invalid={whoInvalid || undefined} aria-describedby={whoInvalid ? 'audit-who-err' : undefined}
            className={`${inputCls} w-44`} />
          {whoInvalid && <span id="audit-who-err" className="text-[10px] text-err mt-0.5">Enter a full email address</span>}
        </span>
        {global && (
          <select aria-label="Space" value={spaceSel} onChange={e => { setSpaceSel(e.target.value); if (e.target.value === 'other') setTimeout(() => otherRef.current?.focus(), 0) }} className={`${inputCls} max-w-[14rem]`}>
            <option value="">All spaces</option>
            {spaces.map(s => <option key={s.space_id} value={s.space_id}>{s.name}</option>)}
            <option value="account">Account-level only</option>
            <option value="other">Other space id…</option>
          </select>
        )}
        {global && spaceSel === 'other' && (
          <span className="flex flex-col">
            <input ref={otherRef} aria-label="Space id" pattern="[0-9a-f-]{36}" value={otherId} onChange={e => setOtherId(e.target.value)}
              placeholder="Use this for a deleted space" aria-invalid={otherInvalid || undefined}
              aria-describedby="audit-space-hint" className={`${inputCls} w-72 font-mono`} />
            <span id="audit-space-hint" className={`text-[10px] mt-0.5 ${otherInvalid ? 'text-err' : 'text-fg-muted'}`}>
              {otherInvalid ? 'Enter a space id like 1b2c3d4e-0000-0000-0000-000000000000' : 'Use this for a deleted space'}
            </span>
          </span>
        )}
        <PageBar page={page} noun="event" onTurn={turn} onMore={page.moreOnce} />
        <button onClick={turn(page.reload)} aria-label="Refresh" title="Refresh" className="p-1 rounded text-fg-3 hover:text-fg hover:bg-surface-3">
          <RefreshCw size={13} strokeWidth={1.75} className={page.loading ? 'animate-spin' : ''} aria-hidden />
        </button>
      </div>

      {page.loading && page.all.length === 0 && !page.error && !page.open && (
        <p className="px-4 py-8 text-sm text-fg-muted text-center">Loading audit log…</p>
      )}
      {page.error && (
        <div className="px-4 py-3 flex items-center gap-3" role="alert">
          <p className="text-xs text-err">{errorText(page.error, 'Could not read the audit log.')}</p>
          <button onClick={turn(page.reload)} className="px-2.5 py-1 text-xs border border-line rounded-lg text-fg-2 hover:bg-surface-3">Retry</button>
        </div>
      )}
      {!page.loading && !page.error && page.all.length === 0 && !page.open && (
        <p className="px-4 py-8 text-sm text-fg-muted text-center">
          {anyFilter ? 'No audit events match these filters.' : 'No admin actions recorded yet.'}
        </p>
      )}
      {!page.error && page.all.length === 0 && page.open && (
        <div className="px-4 py-8 text-center space-y-3">
          <p className="text-sm text-fg-muted">No matches in the most recent records{scanned ? ` (searched back to ${scanned})` : ''}.</p>
          <button onClick={page.moreOnce} disabled={page.loading}
            className="px-3 py-1.5 text-xs font-medium border border-line rounded-lg text-fg-2 hover:bg-surface-3 disabled:opacity-50">
            {page.loading ? 'Searching…' : `Search older records${scanned ? ` (before ${scanned})` : ''}`}
          </button>
        </div>
      )}

      {page.rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead><tr className="border-b border-line">
              <th scope="col" className={th}><span className="sr-only">Details</span></th>
              <th scope="col" className={th}>Time</th>
              <th scope="col" className={th}>Who</th>
              <th scope="col" className={th}>Action</th>
              <th scope="col" className={th}>Target</th>
              {global && <th scope="col" className={th}>Space</th>}
              <th scope="col" className={th}>Vault</th>
            </tr></thead>
            <tbody>
              {page.rows.map(row => {
                const TIcon = TARGET_ICONS[row.target_type] || FileText
                const other = !!row.on_other_owner
                return (
                  <tr key={rowKey(row, spaceId)} className={`border-b border-line-2 hover:bg-surface-3 ${other ? 'border-l-2 border-l-warn-border bg-warn-bg' : ''}`}>
                    <td className="py-1.5 pl-3 pr-0 w-6 align-top">
                      <button onClick={() => setOpenRow(row)} aria-label="Show details" className="p-0.5 rounded text-fg-muted hover:text-fg">
                        <ChevronRight size={13} aria-hidden />
                      </button>
                    </td>
                    <td className={`${td} whitespace-nowrap tabular-nums text-fg`} title={isoOf(row.ts)}>{fmtTime(row.ts)}</td>
                    <td className={td}>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-fg truncate max-w-[16rem]" title={row.actor_sub || undefined}>{who(row)}</span>
                        <RoleBadge role={row.actor_role} />
                        {other && <span className={`${BADGE} bg-warn-bg text-warn-text border-warn-border`}>On another admin's space</span>}
                      </div>
                    </td>
                    <td className={td}>
                      <div className="text-fg">{row.action_label || row.action}</div>
                      <div className="text-fg-muted font-mono text-[10px]">{row.action}</div>
                    </td>
                    <td className={td} title={row.target_id || undefined}>
                      <span className="inline-flex items-center gap-1.5">
                        <TIcon size={13} strokeWidth={1.75} className="text-fg-muted flex-shrink-0" aria-hidden />
                        <span className="truncate max-w-[14rem]">{row.target_label || row.target_id || '—'}</span>
                      </span>
                    </td>
                    {global && (
                      <td className={td}>
                        {row.action === 'space_delete' && row.space_id ? (
                          <button onClick={() => openDeletedSpace(row.space_id)} title="Show this space's log"
                            className="text-primary hover:underline text-left">{row.space_name || row.space_id}</button>
                        ) : (row.space_id ? (row.space_name || row.space_id) : 'Account')}
                      </td>
                    )}
                    <td className={td}><VaultCell vault={row.vault} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {(page.pageCount > 1 || page.open) && page.all.length > 0 && (
        <div className="px-4 py-3 flex justify-end">
          <Pager page={page.page} pageCount={page.pageCount} onPage={turn(page.goTo)} open={page.open} onMore={turn(page.moreOnce)}
            loading={page.loading} label="Audit pages (bottom)" />
        </div>
      )}
      {openRow && (
        <AuditEventSheet api={api} row={openRow} spaceId={spaceId} isSuperAdmin={isSuperAdmin} onClose={() => setOpenRow(null)} />
      )}
    </section>
  )
}
