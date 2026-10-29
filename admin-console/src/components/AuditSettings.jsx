// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Settings → Audit (super admins): the vault switch and retention, then the
// account-wide audit log. While the vault is on, every privileged admin action is
// also written once to an S3 bucket under Object Lock (COMPLIANCE), where nobody,
// including the account root user, can change or delete it before it expires.
// Switching the vault off needs a typed confirmation, is itself recorded in the
// vault first, and sends a notification. Retention can be raised here, never set
// below the deploy minimum (AUDIT_RETENTION_DAYS).
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import Toggle from './Toggle'
import AuditLog from './AuditLog'

const OFF_PHRASE = 'turn off audit vault'
const RETENTION_MAX = 3653

const errText = (e, fallback) => !e ? '' : e.status === 502 ? fallback : (e.detail || e.message || fallback)
const fmtWhen = (iso) => {
  const d = new Date(iso)
  return iso && !Number.isNaN(d.getTime()) ? d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''
}

function ConfirmVaultOffDialog({ onCancel, onConfirm, busy, error }) {
  const [text, setText] = useState('')
  const inputRef = useRef(null)
  useEffect(() => { inputRef.current?.focus() }, [])
  // Esc cancels; the page behind stays put while the dialog is open (as SideSheet does).
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); onCancel() } }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = prev }
  }, [onCancel])
  const matches = text.trim().toLowerCase() === OFF_PHRASE
  // A portal to <body>: the dialog must not inherit the page's spacing utilities.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center side-sheet-backdrop p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel() }}>
      <div role="dialog" aria-modal="true" aria-labelledby="vault-off-title" aria-describedby="vault-off-desc"
        className="w-full max-w-md bg-surface border border-line rounded-xl shadow-2xl p-5">
        <h2 id="vault-off-title" className="text-base font-semibold text-fg">Turn off the audit vault?</h2>
        <p id="vault-off-desc" className="text-xs text-fg-3 mt-2 leading-relaxed">
          New admin actions will be recorded in the index only, which can be edited by anyone holding the
          application&apos;s write role. Records already in the vault stay locked until they expire. A
          notification is sent.
        </p>
        <form className="mt-4 space-y-3" onSubmit={(e) => { e.preventDefault(); if (matches && !busy) onConfirm(text.trim()) }}>
          <label className="block text-xs text-fg-2">
            Type <span className="font-mono text-fg">{OFF_PHRASE}</span> to confirm
            <input ref={inputRef} value={text} onChange={e => setText(e.target.value)} autoComplete="off" spellCheck={false}
              className="mt-1 w-full px-2 py-1.5 text-sm rounded-lg border border-line-strong bg-surface text-fg focus:outline-none focus:border-primary-line" />
          </label>
          {error && <p role="alert" className="text-xs text-err">{error}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={onCancel} className="px-3 py-1.5 text-fg-3 text-xs rounded-lg hover:bg-surface-3 transition-colors">Cancel</button>
            <button type="submit" disabled={!matches || busy}
              className="px-3 py-1.5 bg-err text-white text-xs rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
              {busy ? 'Turning off…' : 'Turn off the vault'}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}

function AuditVaultControls({ api, settings, onChange, onReload }) {
  const [busy, setBusy] = useState(false)
  const [switchError, setSwitchError] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const [confirmError, setConfirmError] = useState(null)
  const [retention, setRetention] = useState(String(settings.retention_days))
  const [retentionError, setRetentionError] = useState(null)
  const [savingRetention, setSavingRetention] = useState(false)
  const toggleRef = useRef(null)
  useEffect(() => { setRetention(String(settings.retention_days)) }, [settings.retention_days])

  const focusToggle = () => setTimeout(() => toggleRef.current?.querySelector('button')?.focus(), 0)
  const turnOn = async () => {
    setBusy(true); setSwitchError(null)
    try { onChange(await api.put('/api/audit/settings', { vault_enabled: true })) }
    catch (e) { setSwitchError(errText(e, 'Could not save the audit settings. Try again.')); onReload() }
    finally { setBusy(false) }
  }
  const turnOff = async (phrase) => {
    setBusy(true); setConfirmError(null)
    try {
      onChange(await api.put('/api/audit/settings', { vault_enabled: false, confirm: phrase }))
      setConfirming(false); focusToggle()
    } catch (e) {
      setConfirmError(errText(e, 'Could not turn the vault off; the vault is still on.'))
      onReload()
    } finally { setBusy(false) }
  }
  const cancelOff = () => { setConfirming(false); setConfirmError(null); focusToggle() }
  const saveRetention = async () => {
    const n = Number(retention)
    if (!Number.isInteger(n) || n < settings.floor_days || n > RETENTION_MAX) {
      setRetentionError(`Retention must be between ${settings.floor_days} and ${RETENTION_MAX} days`)
      setRetention(String(settings.retention_days))
      return
    }
    setSavingRetention(true); setRetentionError(null)
    try { onChange(await api.put('/api/audit/settings', { retention_days: n })) }
    catch (e) {
      setRetentionError(errText(e, 'Could not save the retention. Try again.'))
      setRetention(String(settings.retention_days)); onReload()
    } finally { setSavingRetention(false) }
  }

  const on = !!settings.vault_enabled
  return (
    <div className="bg-surface rounded-xl border border-line p-4 space-y-4">
      <div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-3 cursor-pointer">
            <span ref={toggleRef} className="inline-flex">
              <Toggle checked={on} disabled={busy} title={on ? 'The vault is on' : 'The vault is off'}
                onChange={(v) => { setSwitchError(null); if (v) turnOn(); else { setConfirmError(null); setConfirming(true) } }} />
            </span>
            <span className="text-sm font-medium text-fg">Write records to the locked vault</span>
          </label>
          {!on && <span className="inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium bg-warn-bg text-warn-text border border-warn-border">Off</span>}
        </div>
        <p className="text-xs text-fg-3 mt-1.5 ml-12 leading-relaxed">
          {on
            ? 'Every admin action is also written once to S3 Object Lock (COMPLIANCE): nobody, including the account root user, can change or delete it before it expires.'
            : 'Audit log: index only, not tamper-proof. New admin actions are recorded in the index alone.'}
        </p>
        {switchError && <p role="alert" className="text-xs text-err mt-1.5 ml-12">{switchError}</p>}
      </div>

      <div className="border-t border-line-2 pt-4">
        <div className="flex items-end gap-2 flex-wrap">
          <label className="text-xs text-fg-2">
            <span className="block mb-1 font-medium">Retention (days)</span>
            <input type="number" min={settings.floor_days} max={RETENTION_MAX} step={1} value={retention}
              onChange={e => { setRetention(e.target.value); setRetentionError(null) }}
              aria-describedby="audit-retention-help" aria-invalid={retentionError ? true : undefined}
              className="w-32 px-2 py-1.5 text-sm rounded-lg border border-line-strong bg-surface text-fg" />
          </label>
          <button onClick={saveRetention} disabled={savingRetention || retention === String(settings.retention_days)}
            className="px-3 py-1.5 bg-primary text-primary-fg text-xs rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50">
            {savingRetention ? 'Saving…' : 'Save'}
          </button>
          {retentionError && <p role="alert" className="text-xs text-err pb-1.5">{retentionError}</p>}
        </div>
        <p id="audit-retention-help" className="text-xs text-fg-3 mt-1.5 leading-relaxed">
          Applies to new records. Can be set from {settings.floor_days} (the deploy minimum) to {RETENTION_MAX}; going
          below {settings.floor_days} needs a redeploy with a lower AUDIT_RETENTION_DAYS. Other servers pick up a change
          within 30 seconds.
        </p>
      </div>

      <p className="text-[11px] text-fg-muted">
        {settings.configured && settings.updated_at
          ? `Last changed by ${settings.updated_by_email || 'an admin'} at ${fmtWhen(settings.updated_at)}`
          : 'Using deploy defaults'}
      </p>

      {confirming && <ConfirmVaultOffDialog onCancel={cancelOff} onConfirm={turnOff} busy={busy} error={confirmError} />}
    </div>
  )
}

export function AuditSettingsPage({ api }) {
  const [settings, setSettings] = useState(null)
  const [error, setError] = useState(null)
  const [logKey, setLogKey] = useState(0)
  const load = () => api.get('/api/audit/settings')
    .then(d => { setSettings(d); setError(null) })
    .catch(e => setError(errText(e, 'Could not load the audit settings.')))
  useEffect(() => { load() }, [api]) // eslint-disable-line react-hooks/exhaustive-deps
  // A saved change is itself an audit event, so the log below reloads to show it.
  const changed = (d) => { setSettings(d); setLogKey(k => k + 1) }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-fg">Audit</h2>
        <p className="text-sm text-fg-3">
          Every privileged admin action, newest first. While the vault is on, records are locked in S3 Object
          Lock (COMPLIANCE) for {settings ? settings.effective_retention_days : '…'} days.
        </p>
      </div>
      {error && <div role="alert" className="px-4 py-2 rounded-lg text-sm bg-err-bg text-err-text">{error}</div>}
      {settings && <AuditVaultControls api={api} settings={settings} onChange={changed} onReload={load} />}
      <AuditLog key={logKey} api={api} isSuperAdmin />
    </div>
  )
}

export default AuditSettingsPage
