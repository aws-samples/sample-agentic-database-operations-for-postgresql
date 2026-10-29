// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import { useNotifications } from './Notifications'

// Sortable column header: click to sort, click again to flip direction.
function SortTh({ label, k, sortKey, sortDir, onSort }) {
  const active = sortKey === k
  return (
    <th className="px-5 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider">
      <button onClick={() => onSort(k)}
        className="inline-flex items-center gap-1 hover:text-fg-2 transition-colors uppercase"
        title={`Sort by ${label.toLowerCase()}`}>
        {label}
        <span className={`text-[10px] ${active ? 'text-fg-2' : 'text-fg-2'}`}>
          {active ? (sortDir === 'asc' ? '▲' : '▼') : '▲▼'}
        </span>
      </button>
    </th>
  )
}

/**
 * User management — invite new admins/space admins, change roles, remove users.
 * Only accessible to members of the `admins` group. Action outcomes go to the
 * persistent notification tray (header bell) in addition to the inline flash.
 */
export default function UserManagement({ api }) {
  const { addNotification } = useNotifications()
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [showInvite, setShowInvite] = useState(false)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteName, setInviteName] = useState('')
  const [inviteRole, setInviteRole] = useState('space-admins')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState(null)
  const [senderReady, setSenderReady] = useState(true)
  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState('all')
  const [sortKey, setSortKey] = useState('name')   // default: sorted by user name
  const [sortDir, setSortDir] = useState('asc')

  const ROLE_LABELS = { admins: 'Admin', 'space-admins': 'Space Admin', operators: 'Operator' }
  const roleLabel = (r) => ROLE_LABELS[r] || 'No role'

  const load = async () => {
    setLoading(true)
    try {
      const data = await api.get('/api/cognito/users')
      setUsers(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error(e)
    }
    try {
      const s = await api.get('/api/email-sender')
      setSenderReady(!!s.ready)
    } catch (e) {
      console.error(e)  // backend still enforces the gate; UI hint only
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const flash = (kind, text, title = 'Users') => {
    setMessage({ kind, text })
    setTimeout(() => setMessage(null), 3500)
    // Persist the outcome in the header notification tray (with status + time),
    // so it can be reviewed after the inline flash disappears.
    addNotification({ type: kind === 'ok' ? 'success' : 'error', title, message: text })
  }

  const invite = async (e) => {
    e.preventDefault()
    if (!inviteEmail.includes('@')) return
    setSaving(true)
    try {
      const res = await api.post('/api/cognito/users', { email: inviteEmail.trim(), name: inviteName.trim(), role: inviteRole })
      if (res.status === 'pending_verification') {
        flash('ok', res.message || `${inviteEmail} added as ${roleLabel(inviteRole)} — AWS emailed a verification link; once they confirm it, use Resend to deliver the invite.`)
      } else {
        flash('ok', res.status === 'created'
          ? `Invited ${inviteEmail} as ${roleLabel(inviteRole)} — welcome email sent`
          : `Updated ${inviteEmail} to ${roleLabel(inviteRole)}`)
      }
      setInviteEmail('')
      setInviteName('')
      setInviteRole('space-admins')
      setShowInvite(false)
      load()
    } catch (e) {
      flash('err', e.detail || e.message || 'Failed to invite user')
    } finally {
      setSaving(false)
    }
  }

  const changeRole = async (email, newRole) => {
    if (!confirm(`Change ${email} to ${roleLabel(newRole)}?`)) return
    try {
      await api.put(`/api/cognito/users/${encodeURIComponent(email)}/role`, { role: newRole })
      flash('ok', `Role updated`)
      load()
    } catch (e) {
      flash('err', e.detail || 'Failed to change role')
      load()  // show the role the user actually holds now
    }
  }

  const resendInvite = async (email) => {
    try {
      await api.post(`/api/cognito/users/${encodeURIComponent(email)}/resend-invite`, {})
      flash('ok', `Invite email sent to ${email} with a temporary password`, 'Invite sent')
      load()
    } catch (e) {
      flash('err', e.detail || 'Failed to send invite', 'Invite failed')
    }
  }

  const resendVerification = async (email) => {
    try {
      const res = await api.post(`/api/cognito/users/${encodeURIComponent(email)}/resend-verification`, {})
      flash('ok', res.message || `Fresh verification email sent to ${email}`, 'Verification re-sent')
    } catch (e) {
      flash('err', e.detail || 'Failed to resend the verification email', 'Verification failed')
    }
  }

  const removeUser = async (email) => {
    if (!confirm(`Permanently delete user ${email}? This cannot be undone.`)) return
    try {
      await api.del(`/api/cognito/users/${encodeURIComponent(email)}`)
      flash('ok', `Deleted ${email}`)
      load()
    } catch (e) {
      flash('err', e.detail || 'Failed to delete')
    }
  }

  const primaryRole = (u) => {
    if (u.groups?.includes('admins')) return 'admins'
    if (u.groups?.includes('space-admins')) return 'space-admins'
    if (u.groups?.includes('operators')) return 'operators'
    return null
  }

  // A super admin that can still sign in and administer: in the admins group and in a
  // sign-in-capable state (mirrors the backend's last-admin guard, _USABLE_ADMIN_STATES).
  const USABLE_ADMIN_STATES = ['CONFIRMED', 'FORCE_CHANGE_PASSWORD']
  const isUsableAdmin = (u) =>
    primaryRole(u) === 'admins' && USABLE_ADMIN_STATES.includes(u.status)
  const usableAdminCount = users.filter(isUsableAdmin).length
  // True for the row that is the ONLY super admin able to sign in: its role-change and
  // delete controls are disabled so the account cannot lock everyone out of the console.
  const isLastAdmin = (u) => isUsableAdmin(u) && usableAdminCount <= 1

  const statusBadge = (s) => {
    const map = {
      CONFIRMED: { label: 'Active', cls: 'bg-ok-bg text-ok-text' },
      FORCE_CHANGE_PASSWORD: { label: 'Pending login', cls: 'bg-warn-bg text-warn-text' },
      UNCONFIRMED: { label: 'Unconfirmed', cls: 'bg-surface-3 text-fg-2' },
      RESET_REQUIRED: { label: 'Reset required', cls: 'bg-warn-bg text-warn-text' },
    }
    return map[s] || { label: s || '—', cls: 'bg-surface-3 text-fg-2' }
  }

  // Single source of truth for the lifecycle status STRING shown in the Status
  // column — the filter dropdown and column sort reuse it, so they always agree
  // with what's rendered.
  const displayStatus = (u) => {
    if (u.email_verification === 'pending') return 'Pending email verification'
    if (u.status === 'FORCE_CHANGE_PASSWORD' && u.invite_sent) return 'Invite sent'
    if (u.email_verification === 'verified' && u.status === 'FORCE_CHANGE_PASSWORD') return 'Verified — ready to invite'
    return statusBadge(u.status).label
  }

  const sortVal = (u, key) => {
    if (key === 'name') return (u.name || u.email || u.user_id || '').toLowerCase()
    if (key === 'email') return (u.email || u.user_id || '').toLowerCase()
    if (key === 'role') return roleLabel(primaryRole(u)).toLowerCase()
    return displayStatus(u).toLowerCase()
  }

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortKey(key); setSortDir('asc') }
  }

  const statusOptions = [...new Set(users.map(displayStatus))].sort()
  const q = search.trim().toLowerCase()
  const visible = users
    .filter(u => !q
      || (u.name || '').toLowerCase().includes(q)
      || (u.email || u.user_id || '').toLowerCase().includes(q))
    .filter(u => roleFilter === 'all' || (primaryRole(u) || 'none') === roleFilter)
    .filter(u => statusFilter === 'all' || displayStatus(u) === statusFilter)
    .sort((a, b) => sortVal(a, sortKey).localeCompare(sortVal(b, sortKey)) * (sortDir === 'asc' ? 1 : -1))

  const clearFilters = () => { setSearch(''); setRoleFilter('all'); setStatusFilter('all') }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-fg">Users</h2>
          <p className="text-sm text-fg-3">Manage admins and space admins. Operators are invited from each space's Web App section.</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={load} disabled={loading}
            className="p-2 border border-line-strong rounded-lg text-fg-3 hover:bg-surface-3 hover:text-fg-2 transition-colors disabled:opacity-50"
            title="Refresh user statuses" aria-label="Refresh user statuses">
            <svg className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
            </svg>
          </button>
          <button onClick={() => setShowInvite(true)} disabled={!senderReady}
            className="px-4 py-2 bg-primary text-primary-fg text-sm rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            title={!senderReady ? 'Configure a verified email sender in the Email tab first' : undefined}>
            + Invite user
          </button>
        </div>
      </div>

      {!senderReady && (
        <div className="px-4 py-3 rounded-lg text-sm bg-warn-bg text-warn-text border border-warn-border">
          Email sending isn't set up, so new users can't be invited yet. Configure a verified sender in the{' '}
          <span className="font-medium">Email</span> tab first.
        </div>
      )}

      {message && (
        <div className={`px-4 py-2 rounded-lg text-sm ${
          message.kind === 'ok' ? 'bg-ok-bg text-ok-text' : 'bg-err-bg text-err-text'
        }`}>
          {message.text}
        </div>
      )}

      {showInvite && (
        <form onSubmit={invite} className="bg-surface rounded-xl border border-line p-4 space-y-3">
          <h3 className="text-sm font-semibold text-fg">Invite new user</h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <input type="text" placeholder="Full name (optional)"
              value={inviteName} onChange={e => setInviteName(e.target.value)}
              className="px-3 py-2 border border-line-strong rounded text-sm" />
            <input type="email" required placeholder="user@company.com"
              value={inviteEmail} onChange={e => setInviteEmail(e.target.value)}
              className="px-3 py-2 border border-line-strong rounded text-sm" />
            <select value={inviteRole} onChange={e => setInviteRole(e.target.value)}
              className="px-3 py-2 border border-line-strong rounded text-sm bg-surface">
              <option value="space-admins">Space Admin</option>
              <option value="admins">Admin</option>
            </select>
          </div>
          <p className="text-xs text-fg-3">
            Cognito will email a temporary password. {inviteRole === 'admins'
              ? 'Admins can manage all spaces, users, and settings.'
              : 'Space Admins manage only the spaces they create; a space they are assigned to they can view and use.'}
            {' '}Operators are invited from a space's <span className="font-medium">Web App</span> tab, not here.
          </p>
          <div className="flex gap-2">
            <button type="submit" disabled={saving || !inviteEmail.includes('@')}
              className="px-4 py-2 bg-primary text-primary-fg text-sm rounded-lg hover:bg-primary-hover disabled:opacity-50">
              {saving ? 'Sending...' : 'Send invite'}
            </button>
            <button type="button" onClick={() => { setShowInvite(false); setInviteEmail(''); setInviteName(''); setInviteRole('space-admins') }}
              className="px-4 py-2 border border-line-strong text-fg-2 text-sm rounded hover:bg-surface-3">
              Cancel
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="text-sm text-fg-muted">Loading users...</div>
      ) : users.length === 0 ? (
        <div className="bg-surface rounded-xl border border-line p-8 text-center text-sm text-fg-3">
          No users yet. Click "Invite user" to add the first one.
        </div>
      ) : (
        <>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1 min-w-[220px] max-w-sm">
            <svg className="w-4 h-4 text-fg-muted absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none"
              fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
            </svg>
            <input type="text" value={search} onChange={e => setSearch(e.target.value)}
              placeholder="Search by name or email…" aria-label="Search users"
              className="w-full pl-9 pr-3 py-2 border border-line-strong rounded-lg text-sm focus:outline-none focus:ring-1 focus:ring-primary" />
          </div>
          <select value={roleFilter} onChange={e => setRoleFilter(e.target.value)}
            aria-label="Filter by role"
            className="px-3 py-2 border border-line-strong rounded-lg text-sm bg-surface text-fg-2">
            <option value="all">All roles</option>
            <option value="admins">Admin</option>
            <option value="space-admins">Space Admin</option>
            <option value="none">No role</option>
          </select>
          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
            aria-label="Filter by status"
            className="px-3 py-2 border border-line-strong rounded-lg text-sm bg-surface text-fg-2">
            <option value="all">All statuses</option>
            {statusOptions.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <span className="text-xs text-fg-muted ml-auto">{visible.length} of {users.length} users</span>
        </div>
        <div className="bg-surface rounded-xl border border-line overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-2 text-left text-xs text-fg-3">
                <SortTh label="Name" k="name" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortTh label="Email" k="email" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortTh label="Role" k="role" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortTh label="Status" k="status" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <th className="px-5 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-2">
              {visible.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-5 py-6 text-center text-xs text-fg-muted">
                    No users match your search or filters —{' '}
                    <button onClick={clearFilters} className="text-primary hover:underline">clear them</button>
                  </td>
                </tr>
              )}
              {visible.map(u => {
                const role = primaryRole(u)
                const status = statusBadge(u.status)
                const lastAdmin = isLastAdmin(u)
                return (
                  <tr key={u.user_id} className="hover:bg-surface-3">
                    <td className="px-5 py-3 text-fg">{u.name || '—'}</td>
                    <td className="px-5 py-3 text-fg-2">{u.email || u.user_id}</td>
                    <td className="px-5 py-3">
                      {role === 'admins' ? (
                        <span className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-primary-subtle text-purple-700">Admin</span>
                      ) : role === 'space-admins' ? (
                        <span className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-primary-subtle text-primary">Space Admin</span>
                      ) : role === 'operators' ? (
                        <span className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-info-bg text-info-text">Operator</span>
                      ) : (
                        <span className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-surface-3 text-fg-3">No role</span>
                      )}
                    </td>
                    <td className="px-5 py-3 align-top">
                      {u.email_verification === 'pending' ? (
                        <div className="space-y-1 max-w-[240px]">
                          <span className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-warn-bg text-warn-text">Pending email verification</span>
                          <p className="text-[11px] text-fg-3 leading-snug">AWS emailed a verification link (valid 24 hours). Once they confirm it, click <span className="font-medium text-fg-2">Send invite</span> to deliver their credentials.</p>
                          <button onClick={() => resendVerification(u.email || u.user_id)}
                            className="text-[11px] text-primary hover:underline"
                            title="Send a fresh AWS verification link (use if the previous one expired)">
                            Resend verification email
                          </button>
                        </div>
                      ) : u.status === 'FORCE_CHANGE_PASSWORD' && u.invite_sent ? (
                        <div className="space-y-1 max-w-[240px]">
                          <span className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-primary-subtle text-primary">Invite sent</span>
                          <p className="text-[11px] text-fg-3 leading-snug">Credentials emailed — waiting for their first login. They'll show as <span className="font-medium text-fg-2">Active</span> once they sign in.</p>
                        </div>
                      ) : u.email_verification === 'verified' && u.status === 'FORCE_CHANGE_PASSWORD' ? (
                        <div className="space-y-1 max-w-[240px]">
                          <span className="inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-info-bg text-info-text">Verified — ready to invite</span>
                          <p className="text-[11px] text-fg-3 leading-snug">Email verified. Click <span className="font-medium text-fg-2">Send invite</span> to deliver their credentials.</p>
                        </div>
                      ) : (
                        <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${status.cls}`}>{status.label}</span>
                      )}
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex gap-1 items-center">
                        <select value={role || ''}
                          onChange={e => { if (e.target.value && e.target.value !== role) changeRole(u.email || u.user_id, e.target.value) }}
                          disabled={lastAdmin}
                          className="px-2 py-1 text-xs border border-line-strong rounded bg-surface text-fg-2 disabled:opacity-40 disabled:cursor-not-allowed"
                          title={lastAdmin ? 'This is the only super admin — promote a second admin before changing this role' : 'Change role'}>
                          {!role && <option value="" disabled>Set role…</option>}
                          {/* Operators are space-scoped (Web App tab) — not an assignable
                              target here. The disabled option only renders for users who
                              already ARE operators, so their current role displays correctly
                              (and they can still be promoted to Space Admin / Admin). */}
                          {role === 'operators' && <option value="operators" disabled>Operator</option>}
                          <option value="space-admins">Space Admin</option>
                          <option value="admins">Admin</option>
                        </select>
                        {u.status === 'FORCE_CHANGE_PASSWORD' && (
                          <button onClick={() => resendInvite(u.email || u.user_id)}
                            disabled={u.email_verification === 'pending'}
                            className="px-2 py-1 text-xs bg-warn-bg text-warn-text rounded hover:bg-warn-hover disabled:opacity-40 disabled:cursor-not-allowed"
                            title={u.email_verification === 'pending' ? 'Waiting for the recipient to verify their email (SES sandbox)' : 'Send the welcome email with a new temp password'}>
                            {u.invite_sent ? 'Resend invite' : 'Send invite'}
                          </button>
                        )}
                        <button onClick={() => removeUser(u.email || u.user_id)}
                          disabled={lastAdmin}
                          className="px-2 py-1 text-xs bg-err-bg text-err rounded hover:bg-err-hover disabled:opacity-40 disabled:cursor-not-allowed"
                          title={lastAdmin ? 'This is the only super admin — it cannot be deleted while it is the last one' : 'Permanently delete this user'}>
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        </>
      )}
    </div>
  )
}
