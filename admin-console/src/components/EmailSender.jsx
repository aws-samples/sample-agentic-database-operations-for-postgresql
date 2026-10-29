// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback } from 'react'

/**
 * Email sender (Amazon SES) settings.
 *
 * The Cognito user pool sends invitation emails (temporary passwords). By default
 * it uses Cognito's built-in sender, which has poor deliverability and gives no
 * delivery visibility. Here an admin points the pool at a verified SES identity so
 * invitations for the *second user onward* are delivered reliably and branded.
 *
 * New-user invites are blocked by the backend until this sender is configured and
 * verified (the Users tab surfaces that). The first/bootstrap admin is created by
 * CloudFormation while the pool is still on the built-in sender, so it never
 * depends on this being set.
 */
function Pill({ cls, children }) {
  return <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>{children}</span>
}

export default function EmailSender({ api }) {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [fromAddress, setFromAddress] = useState('')
  const [fromName, setFromName] = useState('')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState(null)

  const flash = (kind, text) => {
    setMessage({ kind, text })
    setTimeout(() => setMessage(null), 4500)
  }

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const s = await api.get('/api/email-sender')
      setStatus(s)
      if (s.from_address) setFromAddress(s.from_address)
      if (s.from_name) setFromName(s.from_name)
    } catch (e) {
      flash('err', e.detail || 'Failed to load email sender')
    } finally {
      setLoading(false)
    }
  }, [api])

  useEffect(() => { load() }, [load])

  const save = async (e) => {
    e.preventDefault()
    if (!fromAddress.includes('@')) return
    setSaving(true)
    try {
      const s = await api.put('/api/email-sender', {
        from_address: fromAddress.trim(),
        from_name: fromName.trim(),
      })
      setStatus(s)
      flash('ok', s.identity_verified
        ? 'Sender saved and verified — invitations will now be sent from it.'
        : 'Sender saved. AWS emailed a verification link to this address — click it, then Refresh.')
    } catch (e) {
      flash('err', e.detail || 'Failed to save sender')
    } finally {
      setSaving(false)
    }
  }

  const resendVerification = async () => {
    try {
      const s = await api.post('/api/email-sender/verify', {})
      setStatus(s)
      flash('ok', 'Verification email sent. Click the link in it, then Refresh.')
    } catch (e) {
      flash('err', e.detail || 'Failed to send verification')
    }
  }

  const statePill = () => {
    if (!status || !status.configured) return <Pill cls="bg-surface-3 text-fg-2">Not configured</Pill>
    if (status.ready) return <Pill cls="bg-ok-bg text-ok-text">Verified &amp; active</Pill>
    if (status.identity_exists && !status.identity_verified) return <Pill cls="bg-warn-bg text-warn-text">Pending verification</Pill>
    if (!status.sending_enabled) return <Pill cls="bg-err-bg text-err-text">SES sending disabled</Pill>
    if (status.sender_verified && !status.pool_active) return <Pill cls="bg-warn-bg text-warn-text">Verified &mdash; not applied</Pill>
    return <Pill cls="bg-warn-bg text-warn-text">Not ready</Pill>
  }

  const pending = status && status.configured && status.identity_exists && !status.identity_verified

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-fg">Email sender</h2>
          <p className="text-sm text-fg-3">
            The address invitation emails are sent from. Uses Amazon SES for reliable delivery.
          </p>
        </div>
        <button onClick={load}
          className="px-3 py-1.5 text-sm text-fg-2 border border-line-strong rounded-lg hover:bg-surface-3">
          Refresh
        </button>
      </div>

      {message && (
        <div className={`px-4 py-2 rounded-lg text-sm ${
          message.kind === 'ok' ? 'bg-ok-bg text-ok-text' : 'bg-err-bg text-err-text'
        }`}>
          {message.text}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-fg-muted">Loading…</div>
      ) : (
        <>
          {/* Current status */}
          <div className="bg-surface rounded-xl border border-line p-4">
            <div className="flex items-center justify-between">
              <div className="text-sm">
                <div className="text-fg-3">Current sender</div>
                <div className="text-fg font-medium">
                  {status?.from_address
                    ? (status.from_name ? `${status.from_name} <${status.from_address}>` : status.from_address)
                    : 'Cognito default sender (unbranded, low deliverability)'}
                </div>
              </div>
              {statePill()}
            </div>

            {pending && (
              <div className="mt-3 flex items-center gap-3 text-xs text-warn-text bg-warn-bg rounded-lg px-3 py-2">
                <span>Waiting on verification. AWS sent a confirmation email to {status.from_address}.</span>
                <button onClick={resendVerification}
                  className="px-2 py-1 rounded bg-warn-bg-2 hover:bg-warn-hover font-medium">
                  Resend verification
                </button>
              </div>
            )}

            {status?.apply_error && (
              <div className="mt-3 text-xs text-err-text bg-err-bg rounded-lg px-3 py-2">
                <strong>Couldn't apply the sender to the user pool.</strong> {status.apply_error}
              </div>
            )}

            {status?.sender_verified && !status?.pool_active && !status?.apply_error && (
              <div className="mt-3 text-xs text-warn-text bg-warn-bg rounded-lg px-3 py-2">
                Verified in SES &mdash; applying to the user pool. Click Refresh to confirm it's active.
              </div>
            )}

            {status?.configured && status?.ready && status?.sandbox && (
              <div className="mt-3 text-xs text-warn-text bg-warn-bg rounded-lg px-3 py-2">
                <strong>SES is in the sandbox.</strong> Invitations to addresses that aren't verified in SES
                won't be delivered. Request production access in the SES console to email arbitrary recipients.
              </div>
            )}
          </div>

          {/* Configure form */}
          <form onSubmit={save} className="bg-surface rounded-xl border border-line p-4 space-y-3">
            <h3 className="text-sm font-semibold text-fg">Configure sender</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-fg-3 mb-1">Display name (optional)</label>
                <input type="text" placeholder="DBOps Agent"
                  value={fromName} onChange={e => setFromName(e.target.value)}
                  className="w-full px-3 py-2 border border-line-strong rounded text-sm" />
              </div>
              <div>
                <label className="block text-xs text-fg-3 mb-1">From address</label>
                <input type="email" required placeholder="no-reply@yourdomain.com"
                  value={fromAddress} onChange={e => setFromAddress(e.target.value)}
                  className="w-full px-3 py-2 border border-line-strong rounded text-sm" />
              </div>
            </div>
            <p className="text-xs text-fg-3">
              Saving verifies the address in Amazon SES (AWS emails a confirmation link). Once verified,
              the user pool switches to sending invitations from it. You can also verify a whole domain in
              the SES console instead of a single address.
            </p>
            <button type="submit" disabled={saving || !fromAddress.includes('@')}
              className="px-4 py-2 bg-primary text-primary-fg text-sm rounded-lg hover:bg-primary-hover disabled:opacity-50">
              {saving ? 'Saving…' : 'Save sender'}
            </button>
          </form>
        </>
      )}
    </div>
  )
}
