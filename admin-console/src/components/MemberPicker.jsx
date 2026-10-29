// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Which member of a DB cluster (Aurora, or an RDS Multi-AZ DB cluster) the drawer's
// charts and records read. The values are the API's member selector: `writer` (the
// default; continuous across a failover, because rows carry the role observed at
// collection), `reader` (every reader) or `instance:<id>` (one member). The labels
// read the record's current roles, which the Tier 1 collector keeps in step with RDS.
// The admin console keeps an identical copy (admin-console/src/components).
import { useEffect, useState } from 'react'
import { startLivePoll } from './livePoll'

// "1 writer" / "1 writer, 2 readers" for a cluster row; '' for a single instance.
export function memberCountLabel(roles) {
  const list = roles || []
  if (list.length < 2) return ''
  const writers = list.filter(m => m.is_writer).length
  const readers = list.length - writers
  const w = `${writers} writer${writers === 1 ? '' : 's'}`
  return readers ? `${w}, ${readers} reader${readers === 1 ? '' : 's'}` : w
}

// Writer first, then readers by id.
export function orderedMembers(roles) {
  return [...(roles || [])].sort((a, b) =>
    (a.is_writer === b.is_writer ? 0 : a.is_writer ? -1 : 1) || String(a.instance_id).localeCompare(String(b.instance_id)))
}

// Whether a selector reads reader rows (for the Member and Lag columns).
export function targetsReaders(member, roles) {
  if (member === 'reader') return true
  if (!member?.startsWith('instance:')) return false
  const id = member.slice('instance:'.length)
  const m = (roles || []).find(r => r.instance_id === id)
  return m ? !m.is_writer : false
}

// The members API's answer for one database, kept current while the caller is mounted
// (startLivePoll: every 30 s while the tab is visible, and on focus), so a failover the
// Tier 1 collector records shows without reopening the panel. Returns `{ data, error,
// loading }`. A refresh keeps the current data, and a failed refresh keeps it too (with
// `error` set). Every panel mounted for the same database shares ONE poll (a drawer's
// table, its picker and the chat's focus picker do not each poll); a panel that joins
// gets the last answer at once and asks for a fresh one. The answer is kept with the
// database it is for and returned only while that is still the one asked about, so
// switching databases never shows the previous one's members, not even for a render.
// Depends on the stable get function, not the api object.
const NO_MEMBERS = { key: '', data: null, error: '' }
const livePolls = new Map()   // instance id -> { listeners, last, stop }
function subscribeMembers(get, instanceId, listener) {
  let entry = livePolls.get(instanceId)
  if (entry) {
    if (entry.last) listener(entry.last)
    entry.stop.refresh()
  } else {
    entry = { listeners: new Set(), last: null, stop: null }
    const publish = (last) => { entry.last = last; entry.listeners.forEach(l => l(last)) }
    livePolls.set(instanceId, entry)
    entry.stop = startLivePoll({
      load: () => get(`/api/database-members/${encodeURIComponent(instanceId)}`),
      onData: (data) => publish({ key: instanceId, data, error: '' }),
      onError: (e) => publish({ key: instanceId, data: entry.last?.data ?? null,
        error: e?.detail || e?.message || 'Could not load the members' }),
    })
  }
  entry.listeners.add(listener)
  return () => {
    entry.listeners.delete(listener)
    if (!entry.listeners.size) { entry.stop(); livePolls.delete(instanceId) }
  }
}
export function useLiveMembers(api, instanceId) {
  const [state, setState] = useState(NO_MEMBERS)
  const get = api?.get
  useEffect(() => {
    setState(NO_MEMBERS)
    if (!get || !instanceId) return undefined
    return subscribeMembers(get, instanceId, setState)
  }, [get, instanceId])
  const mine = instanceId && state.key === instanceId ? state : NO_MEMBERS
  return { data: mine.data, error: mine.error, loading: !!instanceId && !!get && !mine.key }
}

// Which members this database's network path does not reach, from the members API
// (the gate Tier 1, the reader scan and the chat's focus share): `{ skips: {id:
// reason}, reader: reason }`. Empty until loaded, without an api, or when the first
// request failed; kept current like useLiveMembers. Never another database's reasons.
const NO_REACH = { skips: {}, reader: '' }
export function reachFrom(data) {
  if (!data) return NO_REACH
  const skips = {}
  ;(data.members || []).forEach(m => { if (m.skip_reason) skips[m.instance_id] = m.skip_reason })
  return { skips, reader: data.reader_skip_reason || '' }
}
export function useMemberReach(api, instanceId) {
  return reachFrom(useLiveMembers(api, instanceId).data)
}

// The member roles to show: the live members API's when loaded (roles change on a
// failover), else the record's `cluster_member_roles`.
export function liveRoles(instance, data) {
  const live = (data?.members || []).filter(m => m.instance_id)
  if (live.length && (instance?.cluster_member_roles || []).length) {
    return live.map(m => ({ instance_id: m.instance_id, is_writer: m.role === 'writer', endpoint: m.endpoint }))
  }
  return instance?.cluster_member_roles || []
}

// "skipped: not routed (...)" -> "not routed (...)", for an option label.
export const reachLabel = (reason) => String(reason || '').replace(/^skipped: /, '')

export default function MemberPicker({ api, instance, value, onChange, id = 'member-picker' }) {
  const { data } = useLiveMembers(api, instance?.instance_id)
  const reach = reachFrom(data)
  const roles = liveRoles(instance, data)
  if (!roles.length) return null
  const members = orderedMembers(roles)
  const writer = members.find(m => m.is_writer)
  const readers = members.filter(m => !m.is_writer)
  // A member this database's network path does not reach has no rows to read: shown
  // disabled, with the reason.
  const skipOf = (m) => reach.skips[m.instance_id] || ''
  const allSkipped = readers.length > 0 && readers.every(m => skipOf(m))
  return (
    <div className="flex items-center gap-1.5">
      <label htmlFor={id} className="text-[11px] text-fg-3">Member</label>
      <select id={id} value={value || 'writer'} onChange={e => onChange?.(e.target.value)}
        className="text-[11px] bg-surface border border-line rounded px-2 py-1 text-fg-2 focus:outline-none focus:ring-1 focus:ring-primary">
        <option value="writer">Writer (now: {writer?.instance_id || '—'})</option>
        <option value="reader" disabled={!readers.length || allSkipped}>All readers{allSkipped ? ' (none reachable)' : ''}</option>
        {members.map(m => (
          <option key={m.instance_id} value={`instance:${m.instance_id}`} disabled={!!skipOf(m)}
            title={skipOf(m) || undefined}>
            {m.is_writer ? 'writer' : 'reader'} · {m.instance_id}{skipOf(m) ? ` (${reachLabel(skipOf(m))})` : ''}
          </option>
        ))}
      </select>
    </div>
  )
}
