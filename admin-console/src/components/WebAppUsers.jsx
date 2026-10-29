// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import { useNotifications } from './Notifications'

// `onCount` (optional) reports the number of assigned operators whenever the list is
// (re)loaded, so a parent stepper can show it and mark the Operators step done.
export default function WebAppUsers({ api, space, onCount }) {
  const { addNotification } = useNotifications()
  const [assignments, setAssignments] = useState({ users: [], groups: [] })
  const [allUsers, setAllUsers] = useState([])
  const [search, setSearch] = useState('')
  const [newEmail, setNewEmail] = useState('')
  const [showAddPanel, setShowAddPanel] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')

  const fetchAssignments = async () => {
    try {
      const data = await api.get(`/api/spaces/${space.space_id}/users`)
      setAssignments(data)
      setLoadError('')
      onCount?.((data?.users || []).length)
    } catch (e) {
      // Surface it — a swallowed 403 here once masqueraded as an empty list.
      setLoadError(e.detail || e.message || 'Failed to load assigned users')
    }
  }

  const fetchAllUsers = async () => {
    try {
      // Space-scoped listing — works for super admins AND this space's admins
      // (the global /api/cognito/users enumeration is super-admin-only).
      const users = await api.get(`/api/spaces/${space.space_id}/assignable-users`)
      setAllUsers(Array.isArray(users) ? users : [])
    } catch {}
  }

  useEffect(() => { fetchAssignments() }, [])

  const handleAdd = async (userId) => {
    setLoading(true)
    try {
      await api.post(`/api/spaces/${space.space_id}/users`, {
        principal_id: userId,
        principal_type: 'USER',
      })
      addNotification({ type: 'success', title: 'Access granted', message: `${userId} can now use this space in the operator app` })
      await fetchAssignments()
      await fetchAllUsers()
    } catch (e) {
      addNotification({ type: 'error', title: 'Grant failed', message: e.detail || 'Failed to assign' })
      alert(e.detail || 'Failed to assign')
    }
    finally { setLoading(false) }
  }

  const handleRemove = async (userId) => {
    if (!confirm('Remove access for this user?')) return
    try {
      await api.del(`/api/spaces/${space.space_id}/users/${userId}`)
      addNotification({ type: 'info', title: 'Access removed', message: `${userId} no longer has access to this space` })
      await fetchAssignments()
    } catch (e) {
      addNotification({ type: 'error', title: 'Remove failed', message: e.detail || 'Failed to remove' })
      alert(e.detail || 'Failed to remove')
    }
  }

  const handleResend = async (userId) => {
    try {
      await api.post(`/api/spaces/${space.space_id}/users/${encodeURIComponent(userId)}/resend-invite`, {})
      addNotification({ type: 'success', title: 'Invite sent', message: `Credentials emailed to ${userId} — they'll show as Active after first login` })
      await fetchAssignments()
    } catch (e) {
      addNotification({ type: 'error', title: 'Invite failed', message: e.detail || 'Failed to send invite' })
      alert(e.detail || 'Failed to send invite')
    }
  }

  const handleResendVerification = async (userId) => {
    try {
      const res = await api.post(`/api/spaces/${space.space_id}/users/${encodeURIComponent(userId)}/resend-verification`, {})
      addNotification({ type: 'success', title: 'Verification re-sent', message: res.message || `Fresh verification email sent to ${userId}` })
    } catch (e) {
      addNotification({ type: 'error', title: 'Verification failed', message: e.detail || 'Failed to resend verification' })
      alert(e.detail || 'Failed to resend verification')
    }
  }

  const handleInviteNew = async () => {
    if (!newEmail.includes('@')) return
    setLoading(true)
    try {
      // The backend's assign_space_user auto-creates the Cognito user if the
      // principal_id is an email that doesn't exist yet, and puts them in the
      // operators group. Cognito sends a welcome email.
      const res = await api.post(`/api/spaces/${space.space_id}/users`, {
        principal_id: newEmail.trim(),
        principal_type: 'USER',
      })
      if (res.pending_verification) {
        addNotification({ type: 'warning', title: 'Verification pending', message: `${newEmail}: AWS emailed a verification link — click Send invite once they confirm it` })
        alert(res.message || `Invited ${newEmail} — AWS emailed a verification link. Once they confirm it, click Send invite to deliver their credentials.`)
      } else if (res.auto_created) {
        addNotification({ type: 'success', title: 'Operator invited', message: `${newEmail} — welcome email sent with a temporary password` })
      }
      setNewEmail('')
      await fetchAssignments()
      await fetchAllUsers()
    } catch (e) {
      alert(e.detail || 'Failed to invite')
    } finally {
      setLoading(false)
    }
  }

  const openAddPanel = () => {
    setShowAddPanel(true)
    fetchAllUsers()
  }

  const assignedIds = new Set(assignments.users.map(u => u.user_id))
  const availableUsers = allUsers.filter(u =>
    !assignedIds.has(u.user_id) &&
    (!search || u.name?.toLowerCase().includes(search.toLowerCase()) || u.email?.toLowerCase().includes(search.toLowerCase()))
  )

  return (
    <div className="bg-surface rounded-lg border border-line p-6">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-sm font-semibold text-fg">User access</h2>
          <p className="text-xs text-fg-3 mt-0.5">
            Manage which Cognito users can access this space in the operator app.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => { fetchAssignments(); if (showAddPanel) fetchAllUsers() }}
            className="p-1.5 border border-line rounded-lg text-fg-muted hover:bg-surface-3 hover:text-fg-2 transition-colors"
            title="Refresh statuses" aria-label="Refresh statuses">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
            </svg>
          </button>
          {showAddPanel && (
            <button onClick={() => setShowAddPanel(false)}
              className="px-3 py-1.5 text-xs text-fg-3 border border-line rounded-lg hover:bg-surface-3">
              Cancel
            </button>
          )}
          <button onClick={openAddPanel}
            className="px-3 py-1.5 text-xs font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover transition-colors">
            Add user
          </button>
        </div>
      </div>

      {/* Add panel */}
      {showAddPanel && (
        <div className="mb-4 border border-primary-line rounded-lg p-4 bg-primary-subtle space-y-4">
          <div>
            <h3 className="text-xs font-semibold text-fg-2 mb-2">Invite a new operator by email</h3>
            <div className="flex gap-2">
              <input type="email" value={newEmail} onChange={(e) => setNewEmail(e.target.value)}
                placeholder="user@company.com"
                className="flex-1 px-3 py-1.5 text-sm border border-line-strong rounded-lg focus:outline-none focus:ring-1 focus:ring-primary" />
              <button onClick={handleInviteNew} disabled={loading || !newEmail.includes('@')}
                className="px-3 py-1.5 text-xs font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover disabled:opacity-50">
                Invite & grant access
              </button>
            </div>
            <p className="text-[11px] text-fg-3 mt-1">
              Creates a new Cognito user as an operator and grants access to this space. They receive a welcome email with a temporary password.
            </p>
          </div>

          <div className="border-t border-line pt-3">
            <h3 className="text-xs font-semibold text-fg-2 mb-2">Or grant access to an existing user</h3>
            <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name or email..."
              className="w-full px-3 py-1.5 text-sm border border-line-strong rounded-lg mb-3 focus:outline-none focus:ring-1 focus:ring-primary" />
            <div className="max-h-48 overflow-y-auto space-y-1">
              {availableUsers.map(u => (
                <div key={u.user_id} className="flex items-center justify-between px-3 py-1.5 hover:bg-surface rounded">
                  <div>
                    <span className="text-sm text-fg">{u.name || u.email}</span>
                    {u.name && <span className="text-xs text-fg-muted ml-2">{u.email}</span>}
                    {u.role && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-3 text-fg-3 ml-2">
                        {{ admins: 'Admin', 'space-admins': 'Space Admin', operators: 'Operator' }[u.role] || u.role}
                      </span>
                    )}
                  </div>
                  <button onClick={() => handleAdd(u.user_id)} disabled={loading}
                    className="text-xs text-primary hover:underline disabled:opacity-50">Grant access</button>
                </div>
              ))}
              {availableUsers.length === 0 && (
                <p className="text-xs text-fg-muted text-center py-2">
                  {allUsers.length === 0 ? 'No Cognito users found' : 'All users already have access'}
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Assigned users table */}
      {loadError && (
        <div className="mb-3 px-3 py-2 text-xs text-err-text bg-err-bg border border-err-border rounded-lg">
          Couldn't load this space's users: {loadError}
        </div>
      )}
      <table className="w-full">
        <thead className="bg-surface-2">
          <tr>
            <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider">User</th>
            <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Email</th>
            <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Status</th>
            <th className="px-3 py-2 text-right text-[10px] font-semibold text-fg-3 uppercase tracking-wider">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line-2">
          {assignments.users.length === 0 ? (
            <tr><td colSpan={4} className="px-3 py-6 text-center text-xs text-fg-muted">
              No users assigned to this space. Click "Add user" to grant access.
            </td></tr>
          ) : assignments.users.map(u => (
            <tr key={u.user_id}>
              <td className="px-3 py-2 text-sm text-fg">{u.name || '—'}</td>
              <td className="px-3 py-2 text-sm text-fg-2">{u.email || '—'}</td>
              <td className="px-3 py-2 align-top">
                {u.email_verification === 'pending' ? (
                  <div className="space-y-1 max-w-[220px]">
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-warn-bg text-warn-text">Pending email verification</span>
                    <p className="text-[10px] text-fg-3 leading-snug">AWS emailed them a verification link (valid 24h). Once they confirm it, click <span className="font-medium text-fg-2">Send invite</span>.</p>
                    <button onClick={() => handleResendVerification(u.user_id)}
                      className="text-[10px] text-primary hover:underline"
                      title="Send a fresh AWS verification link (use if the previous one expired)">
                      Resend verification email
                    </button>
                  </div>
                ) : u.status === 'FORCE_CHANGE_PASSWORD' && u.invite_sent ? (
                  <div className="space-y-1 max-w-[220px]">
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-primary-subtle text-primary">Invite sent</span>
                    <p className="text-[10px] text-fg-3 leading-snug">Credentials emailed — Active after their first login.</p>
                  </div>
                ) : u.email_verification === 'verified' && u.status === 'FORCE_CHANGE_PASSWORD' ? (
                  <div className="space-y-1 max-w-[220px]">
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-info-bg text-info-text">Verified — ready to invite</span>
                    <p className="text-[10px] text-fg-3 leading-snug">Click <span className="font-medium text-fg-2">Send invite</span> to deliver their credentials.</p>
                  </div>
                ) : (
                  <span className={`text-[10px] px-2 py-0.5 rounded-full ${
                    u.status === 'CONFIRMED' ? 'bg-ok-bg text-ok-text' : 'bg-warn-bg text-warn-text'
                  }`}>{u.status || 'unknown'}</span>
                )}
              </td>
              <td className="px-3 py-2 text-right whitespace-nowrap">
                {u.status === 'FORCE_CHANGE_PASSWORD' && (
                  <button onClick={() => handleResend(u.user_id)}
                    disabled={u.email_verification === 'pending'}
                    className="text-xs text-warn-mid hover:underline mr-3 disabled:opacity-40 disabled:cursor-not-allowed disabled:no-underline"
                    title={u.email_verification === 'pending' ? 'Waiting for the recipient to verify their email (SES sandbox)' : 'Send the welcome email with a new temp password'}>
                    {u.invite_sent ? 'Resend invite' : 'Send invite'}
                  </button>
                )}
                <button onClick={() => handleRemove(u.user_id)}
                  className="text-xs text-err hover:underline">Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
