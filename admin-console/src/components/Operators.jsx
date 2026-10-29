// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo } from 'react'
import { useNotifications } from './Notifications'

/**
 * Operators — a read-only, super-admin-only view of every operator (base role
 * `operators`) and the spaces each one can access. The single mutation is a
 * "leaver offboarding" delete: it removes the operator from Cognito AND every
 * space in one action (the backend refuses privileged accounts and writes a
 * per-space audit trail). No invite / role-edit here — that lives in Users.
 */
export default function Operators({ api }) {
  const { addNotification } = useNotifications()
  const [operators, setOperators] = useState([])
  const [loading, setLoading] = useState(true)
  const [message, setMessage] = useState(null)
  const [search, setSearch] = useState('')
  const [deleting, setDeleting] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const data = await api.get('/api/operators')
      setOperators(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error(e)
      flash('err', e.detail || e.message || 'Failed to load operators')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const flash = (kind, text, title = 'Operators') => {
    setMessage({ kind, text })
    setTimeout(() => setMessage(null), 3500)
    addNotification({ type: kind === 'ok' ? 'success' : 'error', title, message: text })
  }

  const statusLabel = (op) => {
    if (op.email_verification === 'pending') return 'Pending email verification'
    if (op.status === 'FORCE_CHANGE_PASSWORD' && op.invite_sent) return 'Invite sent'
    if (op.email_verification === 'verified' && op.status === 'FORCE_CHANGE_PASSWORD') return 'Verified — ready to invite'
    const map = {
      CONFIRMED: 'Active',
      FORCE_CHANGE_PASSWORD: 'Pending login',
      UNCONFIRMED: 'Unconfirmed',
      RESET_REQUIRED: 'Reset required',
    }
    return map[op.status] || op.status || '—'
  }

  const statusCls = (op) => {
    const l = statusLabel(op)
    if (l === 'Active') return 'bg-ok-bg text-ok-text'
    if (l.startsWith('Pending') || l === 'Invite sent') return 'bg-warn-bg text-warn-text'
    if (l.startsWith('Verified')) return 'bg-info-bg text-info-text'
    return 'bg-surface-3 text-fg-2'
  }

  const removeOperator = async (op) => {
    const email = op.email || op.user_id
    const spaces = op.spaces || []
    const names = spaces.map(s => s.name || s.space_id)
    const msg = spaces.length
      ? `Delete operator ${email} and revoke access to ${spaces.length} space(s):\n\n` +
        `${names.join('\n')}\n\n` +
        `This deletes their account and removes them from every space above. This cannot be undone.`
      : `Delete operator ${email}?\n\nThey currently have no space access. This deletes their account and cannot be undone.`
    if (!confirm(msg)) return
    setDeleting(op.user_id)
    try {
      const res = await api.del(`/api/operators/${encodeURIComponent(op.user_id)}`)
      const n = res.spaces_revoked ?? spaces.length
      flash('ok', `Deleted ${email}${n ? ` — removed from ${n} space${n === 1 ? '' : 's'}` : ''}`, 'Operator offboarded')
      load()
    } catch (e) {
      flash('err', e.detail || e.message || 'Failed to delete operator')
    } finally {
      setDeleting('')
    }
  }

  const q = search.trim().toLowerCase()
  const visible = useMemo(() => operators
    .filter(o => !q
      || (o.name || '').toLowerCase().includes(q)
      || (o.email || o.user_id || '').toLowerCase().includes(q)
      || (o.spaces || []).some(s => (s.name || '').toLowerCase().includes(q)))
    .sort((a, b) => (a.name || a.email || '').localeCompare(b.name || b.email || '')),
    [operators, q])

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-fg">Operators</h2>
          <p className="text-xs text-fg-3 mt-0.5 max-w-2xl">
            Read-only view of every operator and the spaces they can access. Deleting an operator
            (e.g. when they leave the company) removes their account and their access to every
            space in one step — no need to visit each space. Admins and space admins are managed
            under <span className="font-medium">Users</span>.
          </p>
        </div>
        <button onClick={load}
          className="shrink-0 text-xs font-medium text-primary hover:underline">
          Refresh
        </button>
      </div>

      {message && (
        <div className={`text-xs rounded-md px-3 py-2 ${
          message.kind === 'ok'
            ? 'bg-ok-bg text-ok-text'
            : 'bg-err-bg text-err-text'
        }`}>{message.text}</div>
      )}

      <input
        type="text"
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="Search operators by name, email, or space…"
        className="w-full max-w-sm text-sm rounded-md border border-line-strong bg-surface px-3 py-1.5 text-fg placeholder:text-fg-muted focus:outline-none focus:ring-2 focus:ring-primary"
      />

      <div className="rounded-lg border border-line overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-surface-2 text-left text-fg-3">
            <tr>
              <th className="px-5 py-2 text-[10px] font-semibold uppercase tracking-wider">Name</th>
              <th className="px-5 py-2 text-[10px] font-semibold uppercase tracking-wider">Email</th>
              <th className="px-5 py-2 text-[10px] font-semibold uppercase tracking-wider">Status</th>
              <th className="px-5 py-2 text-[10px] font-semibold uppercase tracking-wider">Spaces</th>
              <th className="px-5 py-2 text-[10px] font-semibold uppercase tracking-wider text-right">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line-2">
            {loading ? (
              <tr><td colSpan={5} className="px-5 py-8 text-center text-fg-muted text-sm">Loading operators…</td></tr>
            ) : visible.length === 0 ? (
              <tr><td colSpan={5} className="px-5 py-8 text-center text-fg-muted text-sm">
                {operators.length === 0 ? 'No operators yet. Operators are added from a space’s Web App section.' : 'No operators match your search.'}
              </td></tr>
            ) : visible.map(op => (
              <tr key={op.user_id} className="hover:bg-surface-3">
                <td className="px-5 py-2.5 text-fg">{op.name || '—'}</td>
                <td className="px-5 py-2.5 text-fg-2">{op.email || op.user_id}</td>
                <td className="px-5 py-2.5">
                  <span className={`inline-block rounded px-2 py-0.5 text-[11px] font-medium ${statusCls(op)}`}>
                    {statusLabel(op)}
                  </span>
                </td>
                <td className="px-5 py-2.5">
                  {(op.spaces || []).length === 0 ? (
                    <span className="text-fg-muted text-xs">No space access</span>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {op.spaces.map(s => (
                        <span key={s.space_id}
                          title={s.name || s.space_id}
                          className="inline-block rounded bg-surface-3 text-fg-2 px-2 py-0.5 text-[11px]">
                          {s.name || s.space_id}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="px-5 py-2.5 text-right">
                  <button
                    onClick={() => removeOperator(op)}
                    disabled={deleting === op.user_id}
                    className="text-xs font-medium text-err hover:underline disabled:opacity-40 disabled:no-underline">
                    {deleting === op.user_id ? 'Deleting…' : 'Delete'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
