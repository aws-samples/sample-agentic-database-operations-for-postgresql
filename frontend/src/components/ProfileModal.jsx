// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'

// Self-service profile editor: display name + password. Both call Cognito
// directly (user-context) via useAuth helpers passed in as props.
export default function ProfileModal({ email, currentName, onUpdateName, onChangePassword, onClose }) {
  const [name, setName] = useState(currentName || '')
  const [savingName, setSavingName] = useState(false)
  const [nameMsg, setNameMsg] = useState(null)

  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [savingPw, setSavingPw] = useState(false)
  const [pwMsg, setPwMsg] = useState(null)

  const saveName = async (e) => {
    e.preventDefault()
    const n = name.trim()
    if (!n) { setNameMsg({ ok: false, text: 'Name cannot be empty' }); return }
    setSavingName(true); setNameMsg(null)
    const res = await onUpdateName?.(n)
    setSavingName(false)
    setNameMsg(res?.ok ? { ok: true, text: 'Saved' } : { ok: false, text: res?.error || 'Failed to save' })
  }

  const savePw = async (e) => {
    e.preventDefault()
    if (newPw.length < 8) { setPwMsg({ ok: false, text: 'New password must be at least 8 characters' }); return }
    if (newPw !== confirmPw) { setPwMsg({ ok: false, text: 'New passwords do not match' }); return }
    setSavingPw(true); setPwMsg(null)
    const res = await onChangePassword?.(oldPw, newPw)
    setSavingPw(false)
    if (res?.ok) { setPwMsg({ ok: true, text: 'Password changed' }); setOldPw(''); setNewPw(''); setConfirmPw('') }
    else setPwMsg({ ok: false, text: res?.error || 'Failed to change password' })
  }

  const inputCls = 'w-full px-3 py-2 text-sm rounded-lg border border-line bg-surface text-fg focus:outline-none focus:ring-2 focus:ring-primary'

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative w-full max-w-md bg-surface rounded-2xl border border-line shadow-2xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-4 border-b border-line-2">
          <h2 className="text-base font-semibold text-fg">Profile</h2>
          <button onClick={onClose} className="p-1 text-fg-muted hover:text-fg-2 rounded-md hover:bg-surface-3">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="px-5 py-4 space-y-6">
          {/* Email (read-only) */}
          <div>
            <label className="block text-xs font-medium text-fg-3 mb-1">Email</label>
            <input value={email} readOnly disabled
              className="w-full px-3 py-2 text-sm rounded-lg border border-line bg-surface-2 text-fg-3 cursor-not-allowed" />
          </div>

          {/* Display name */}
          <form onSubmit={saveName}>
            <label className="block text-xs font-medium text-fg-3 mb-1">Display name</label>
            <div className="flex items-center gap-2">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" className={inputCls} />
              <button type="submit" disabled={savingName}
                className="px-3 py-2 text-sm font-medium rounded-lg bg-primary hover:bg-primary-hover text-primary-fg disabled:opacity-50 transition-colors flex-shrink-0">
                {savingName ? 'Saving…' : 'Save'}
              </button>
            </div>
            {nameMsg && <p className={`text-xs mt-1.5 ${nameMsg.ok ? 'text-ok' : 'text-err'}`}>{nameMsg.text}</p>}
          </form>

          {/* Change password */}
          <form onSubmit={savePw} className="border-t border-line-2 pt-5">
            <p className="text-sm font-medium text-fg mb-2">Change password</p>
            <div className="space-y-2">
              <input type="password" value={oldPw} onChange={(e) => setOldPw(e.target.value)} placeholder="Current password" autoComplete="current-password" className={inputCls} />
              <input type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} placeholder="New password (min 8 chars)" autoComplete="new-password" className={inputCls} />
              <input type="password" value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} placeholder="Confirm new password" autoComplete="new-password" className={inputCls} />
            </div>
            <button type="submit" disabled={savingPw || !oldPw || !newPw || !confirmPw}
              className="mt-2 px-3 py-2 text-sm font-medium rounded-lg border border-line-strong text-fg-2 hover:bg-surface-3 disabled:opacity-50 transition-colors">
              {savingPw ? 'Updating…' : 'Update password'}
            </button>
            {pwMsg && <p className={`text-xs mt-1.5 ${pwMsg.ok ? 'text-ok' : 'text-err'}`}>{pwMsg.text}</p>}
          </form>
        </div>
      </div>
    </div>
  )
}
