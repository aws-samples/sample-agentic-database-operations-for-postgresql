// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { APP_NAME } from '../appName'

export default function LoginPage({
  onLogin, onCompleteNewPassword, showNewPassword, error, isAdmin,
  mfaMode, mfaSecret, mfaOtpauthUri, onCompleteMfaSetup, onSubmitMfaCode,
  forgotMode, onStartForgotPassword, onCancelForgotPassword, onForgotPassword, onConfirmForgotPassword,
}) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [mfaCode, setMfaCode] = useState('')
  const [resetCode, setResetCode] = useState('')
  const [loading, setLoading] = useState(false)
  const [localError, setLocalError] = useState(null)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!email || !password) return
    setLoading(true)
    setLocalError(null)
    await onLogin(email, password)
    setLoading(false)
  }

  const handleNewPassword = async (e) => {
    e.preventDefault()
    setLocalError(null)
    if (!newPassword) return
    if (newPassword.length < 12) {
      setLocalError('Password must be at least 12 characters')
      return
    }
    if (newPassword !== confirmPassword) {
      setLocalError('Passwords do not match')
      return
    }
    setLoading(true)
    await onCompleteNewPassword(newPassword)
    setLoading(false)
  }

  const handleMfaCode = async (e) => {
    e.preventDefault()
    setLocalError(null)
    const code = mfaCode.trim()
    if (!/^\d{6}$/.test(code)) {
      setLocalError('Enter the 6-digit code from your authenticator app')
      return
    }
    setLoading(true)
    const ok = mfaMode === 'setup' ? await onCompleteMfaSetup(code) : await onSubmitMfaCode(code)
    setLoading(false)
    if (ok !== true) setMfaCode('')
  }

  // Step 1: ask Cognito to email a reset code to the address.
  const handleRequestReset = async (e) => {
    e.preventDefault()
    setLocalError(null)
    if (!email) { setLocalError('Enter your email'); return }
    setLoading(true)
    await onForgotPassword(email)
    setLoading(false)
  }

  // Step 2: redeem the emailed code with a new password.
  const handleConfirmReset = async (e) => {
    e.preventDefault()
    setLocalError(null)
    if (!resetCode.trim()) { setLocalError('Enter the code we emailed you'); return }
    if (newPassword.length < 12) { setLocalError('Password must be at least 12 characters'); return }
    if (newPassword !== confirmPassword) { setLocalError('Passwords do not match'); return }
    setLoading(true)
    const ok = await onConfirmForgotPassword(resetCode.trim(), newPassword)
    setLoading(false)
    if (ok === true) { setResetCode(''); setNewPassword(''); setConfirmPassword(''); setPassword('') }
  }

  const displayError = localError || error
  const inputCls = "w-full px-3 py-2.5 bg-surface border border-line-strong rounded-lg text-fg text-sm placeholder-fg-muted focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary transition-colors"
  const btnCls = "w-full py-2.5 px-4 bg-primary text-primary-fg font-medium rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
  const spinner = <div className="w-4 h-4 border-2 border-line border-t-white rounded-full animate-spin" />
  const codeField = (
    <input type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={6}
      value={mfaCode} onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ''))}
      placeholder="123456" autoFocus className={inputCls + " tracking-[0.5em] text-center text-lg"} />
  )

  return (
    <div className="min-h-screen bg-canvas flex items-center justify-center">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center mb-4">
            <div className="w-12 h-12 rounded-xl bg-brand-gradient flex items-center justify-center shadow-lg">
              <svg className="w-7 h-7 text-white" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" />
              </svg>
            </div>
          </div>
          <h1 className="text-2xl font-semibold text-fg">{APP_NAME}</h1>
          <p className="text-fg-3 text-sm mt-1">Agent Spaces</p>
        </div>

        {/* Card */}
        <div className="bg-surface rounded-xl border border-line p-8 shadow-lg">
          {forgotMode === 'request' ? (
            /* ── Forgot password: request a code ── */
            <>
              <div className="mb-5">
                <p className="text-sm font-medium text-fg">Reset your password</p>
                <p className="text-xs text-fg-muted mt-1">Enter your email and we'll send a reset code.</p>
              </div>
              <form onSubmit={handleRequestReset}>
                <div className="mb-6">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">Email</label>
                  <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                    placeholder="admin@example.com" autoComplete="email" autoFocus className={inputCls} />
                </div>
                {displayError && (
                  <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg"><p className="text-xs text-err">{displayError}</p></div>
                )}
                <button type="submit" disabled={loading || !email} className={btnCls}>
                  {loading ? spinner : 'Send reset code'}
                </button>
              </form>
              <button type="button" onClick={() => { setLocalError(null); onCancelForgotPassword() }}
                className="mt-4 w-full text-center text-xs text-fg-3 hover:text-fg">Back to sign in</button>
            </>
          ) : forgotMode === 'confirm' ? (
            /* ── Forgot password: enter code + new password ── */
            <>
              <div className="mb-5">
                <p className="text-sm font-medium text-fg">Enter your reset code</p>
                <p className="text-xs text-fg-muted mt-1">Check your email for a code, then choose a new password.</p>
              </div>
              <form onSubmit={handleConfirmReset}>
                <div className="mb-4">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">Reset code</label>
                  <input type="text" inputMode="numeric" autoComplete="one-time-code" value={resetCode}
                    onChange={(e) => setResetCode(e.target.value.replace(/\D/g, ''))}
                    placeholder="123456" autoFocus className={inputCls} />
                </div>
                <div className="mb-4">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">New Password</label>
                  <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="At least 12 characters" autoComplete="new-password" className={inputCls} />
                </div>
                <div className="mb-6">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">Confirm Password</label>
                  <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Re-enter your new password" autoComplete="new-password" className={inputCls} />
                </div>
                {displayError && (
                  <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg"><p className="text-xs text-err">{displayError}</p></div>
                )}
                <button type="submit" disabled={loading || !resetCode || !newPassword || !confirmPassword} className={btnCls}>
                  {loading ? spinner : 'Set new password'}
                </button>
              </form>
              <button type="button" onClick={() => { setLocalError(null); onCancelForgotPassword() }}
                className="mt-4 w-full text-center text-xs text-fg-3 hover:text-fg">Back to sign in</button>
            </>
          ) : mfaMode === 'setup' ? (
            <>
              <div className="mb-5">
                <p className="text-sm font-medium text-fg">Set up two-factor authentication</p>
                <p className="text-xs text-fg-muted mt-1">This account requires an authenticator app (TOTP). Add the key below, then enter the 6-digit code.</p>
              </div>
              {mfaOtpauthUri && (
                <div className="mb-4 flex flex-col items-center">
                  <div className="p-3 bg-surface rounded-lg">
                    <QRCodeSVG value={mfaOtpauthUri} size={168} level="M" marginSize={0} />
                  </div>
                  <p className="text-xs text-fg-muted mt-2 text-center">Scan with your authenticator app</p>
                  <a href={mfaOtpauthUri} className="inline-block mt-1 text-[11px] text-primary hover:underline">Open in authenticator app</a>
                </div>
              )}
              <div className="mb-4">
                <label className="block text-xs font-medium text-fg-muted mb-1.5">Can't scan? Enter this key manually</label>
                <div className="px-3 py-2.5 bg-surface-2 border border-line rounded-lg text-primary text-xs font-mono break-all select-all">{mfaSecret}</div>
              </div>
              <form onSubmit={handleMfaCode}>
                <div className="mb-6">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">Authenticator code</label>
                  {codeField}
                </div>
                {displayError && (
                  <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg"><p className="text-xs text-err">{displayError}</p></div>
                )}
                <button type="submit" disabled={loading || mfaCode.length !== 6} className={btnCls}>
                  {loading ? spinner : 'Verify & Sign In'}
                </button>
              </form>
            </>
          ) : mfaMode === 'challenge' ? (
            <>
              <div className="mb-5">
                <p className="text-sm font-medium text-fg">Two-factor authentication</p>
                <p className="text-xs text-fg-muted mt-1">Enter the 6-digit code from your authenticator app.</p>
              </div>
              <form onSubmit={handleMfaCode}>
                <div className="mb-6">{codeField}</div>
                {displayError && (
                  <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg"><p className="text-xs text-err">{displayError}</p></div>
                )}
                <button type="submit" disabled={loading || mfaCode.length !== 6} className={btnCls}>
                  {loading ? spinner : 'Verify'}
                </button>
              </form>
            </>
          ) : showNewPassword ? (
            /* ── New Password Form ── */
            <>
              <div className="mb-5 flex items-center gap-2">
                <div className="w-8 h-8 rounded-lg bg-warn-bg flex items-center justify-center">
                  <svg className="w-4 h-4 text-warn" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                  </svg>
                </div>
                <div>
                  <p className="text-sm font-medium text-fg">Set a new password</p>
                  <p className="text-xs text-fg-muted">Your account requires a password change</p>
                </div>
              </div>
              <form onSubmit={handleNewPassword}>
                <div className="mb-4">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">New Password</label>
                  <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="At least 12 characters" autoComplete="new-password" autoFocus className={inputCls} />
                </div>
                <div className="mb-6">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">Confirm Password</label>
                  <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Re-enter your new password" autoComplete="new-password" className={inputCls} />
                </div>
                {displayError && (
                  <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg"><p className="text-xs text-err">{displayError}</p></div>
                )}
                <button type="submit" disabled={loading || !newPassword || !confirmPassword} className={btnCls}>
                  {loading ? spinner : 'Set Password & Continue'}
                </button>
              </form>
            </>
          ) : (
            /* ── Login Form ── */
            <>
              <form onSubmit={handleSubmit}>
                <div className="mb-4">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">Email</label>
                  <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                    placeholder="admin@example.com" autoComplete="email" className={inputCls} />
                </div>
                <div className="mb-6">
                  <label className="block text-xs font-medium text-fg-muted mb-1.5">Password</label>
                  <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                    placeholder="Enter your password" autoComplete="current-password" className={inputCls} />
                </div>
                {displayError && (
                  <div className="mb-4 px-3 py-2 bg-err-bg border border-err-border rounded-lg"><p className="text-xs text-err">{displayError}</p></div>
                )}
                <button type="submit" disabled={loading || !email || !password} className={btnCls}>
                  {loading ? spinner : 'Sign In'}
                </button>
              </form>

              <button type="button" onClick={() => { setLocalError(null); onStartForgotPassword() }}
                className="mt-4 w-full text-center text-xs text-primary hover:underline">Forgot password?</button>

              <div className="mt-6 flex items-center justify-center gap-6 text-fg-3">
                <div className="flex flex-col items-center gap-1">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" />
                  </svg>
                  <span className="text-[10px]">AI-powered</span>
                </div>
                <div className="flex flex-col items-center gap-1">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5" />
                  </svg>
                  <span className="text-[10px]">Fleet monitoring</span>
                </div>
                <div className="flex flex-col items-center gap-1">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
                  </svg>
                  <span className="text-[10px]">Cross-account</span>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
