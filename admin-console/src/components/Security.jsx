// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'

/**
 * Monthly cost budget — alert-only. Shows the current limit and month-to-date spend,
 * and lets a super admin change the limit. The budget itself is created by the deploy;
 * changing the amount here only moves the 80%/100% alert thresholds — it never blocks
 * any workload. Hidden when the deployment has no budget configured.
 */
function BudgetCard({ api }) {
  const [budget, setBudget] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(false)
  const [amount, setAmount] = useState('')
  const [saving, setSaving] = useState(false)

  const load = () => {
    setLoading(true)
    api.get('/api/settings/budget')
      .then(d => {
        setBudget(d)
        if (d?.configured) setAmount(String(d.limit_amount ?? ''))
        setError(null)
      })
      .catch(e => setError(e.detail || e.message || 'Failed to load the budget'))
      .finally(() => setLoading(false))
  }
  useEffect(load, [api])

  const save = () => {
    const n = Number(amount)
    if (!Number.isFinite(n) || n < 1 || n > 1000000) {
      setError('Enter an amount between 1 and 1,000,000 USD')
      return
    }
    setSaving(true)
    api.put('/api/settings/budget', { amount: n })
      .then(() => { setEditing(false); setError(null); load() })
      .catch(e => setError(e.detail || e.message || 'Failed to update the budget'))
      .finally(() => setSaving(false))
  }

  if (loading) return null
  if (budget && !budget.configured) return null  // no budget in this deployment

  const money = (v) => v == null ? '—' : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
  const pct = (budget?.actual_amount != null && budget.limit_amount)
    ? Math.round((budget.actual_amount / budget.limit_amount) * 100) : null
  const over = pct != null && pct >= 100

  return (
    <div className="bg-surface rounded-xl border border-line p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-fg">Monthly cost budget</h3>
          <p className="text-xs text-fg-3 mt-1 leading-relaxed">
            Alert-only. Emails the notification address at 80% and 100% of the limit; it never
            blocks any workload. Super admins can change the limit.
          </p>
        </div>
        {!editing && (
          <button onClick={() => setEditing(true)}
            className="px-3 py-1.5 bg-primary text-primary-fg text-xs rounded-lg hover:bg-primary-hover transition-colors flex-shrink-0">
            Change
          </button>
        )}
      </div>

      {error && (
        <div className="mt-3 px-3 py-1.5 rounded-lg text-xs bg-err-bg text-err-text">{error}</div>
      )}

      {!editing ? (
        <div className="mt-3 flex items-baseline gap-4">
          <div>
            <div className="text-2xl font-semibold text-fg">{money(budget?.limit_amount)}</div>
            <div className="text-[11px] text-fg-muted">limit / month</div>
          </div>
          <div>
            <div className={`text-2xl font-semibold ${over ? 'text-warn-mid' : 'text-fg-2'}`}>
              {money(budget?.actual_amount)}
            </div>
            <div className="text-[11px] text-fg-muted">
              spent this month{pct != null ? ` (${pct}%)` : ''}
            </div>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-2">
          <span className="text-sm text-fg-3">$</span>
          <input type="number" min="1" max="1000000" step="1" value={amount}
            onChange={e => setAmount(e.target.value)}
            className="w-32 px-2 py-1.5 text-sm rounded-lg border border-line-strong bg-surface text-fg"
            autoFocus />
          <span className="text-xs text-fg-muted">/ month</span>
          <button onClick={save} disabled={saving}
            className="px-3 py-1.5 bg-primary text-primary-fg text-xs rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50">
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button onClick={() => { setEditing(false); setError(null); if (budget?.configured) setAmount(String(budget.limit_amount ?? '')) }}
            className="px-3 py-1.5 text-fg-3 text-xs rounded-lg hover:bg-surface-3 transition-colors">
            Cancel
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * Per-caller rate limit — the runaway guard. Shows the per-minute ceiling each
 * authenticated caller has on the model paths and lets a super admin retune it. It is
 * fail-open and never applies to super admins; changing it only moves how generous the
 * ceiling is, so it never blocks a legitimate operator. Always present (unlike the budget)
 * because the guard is always active.
 */
function RateLimitCard({ api }) {
  const [state, setState] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)

  const load = () => {
    setLoading(true)
    api.get('/api/settings/rate-limit')
      .then(d => {
        setState(d)
        if (d?.per_minute != null) setValue(String(d.per_minute))
        setError(null)
      })
      .catch(e => setError(e.detail || e.message || 'Failed to load the rate limit'))
      .finally(() => setLoading(false))
  }
  useEffect(load, [api])

  const save = () => {
    const n = Number(value)
    if (!Number.isInteger(n) || n < 1 || n > 100000) {
      setError('Enter a whole number between 1 and 100,000')
      return
    }
    setSaving(true)
    api.put('/api/settings/rate-limit', { per_minute: n })
      .then(() => { setEditing(false); setError(null); load() })
      .catch(e => setError(e.detail || e.message || 'Failed to update the rate limit'))
      .finally(() => setSaving(false))
  }

  if (loading) return null

  return (
    <div className="bg-surface rounded-xl border border-line p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-fg">Per-caller rate limit</h3>
          <p className="text-xs text-fg-3 mt-1 leading-relaxed">
            A fairness guard on the AI paths: at most this many requests per minute per
            operator, so one runaway client cannot starve others. It never blocks super
            admins and fails open, so a generous value is safe. Keep it well above normal
            interactive pace.
          </p>
        </div>
        {!editing && (
          <button onClick={() => setEditing(true)}
            className="px-3 py-1.5 bg-primary text-primary-fg text-xs rounded-lg hover:bg-primary-hover transition-colors flex-shrink-0">
            Change
          </button>
        )}
      </div>

      {error && (
        <div className="mt-3 px-3 py-1.5 rounded-lg text-xs bg-err-bg text-err-text">{error}</div>
      )}

      {!editing ? (
        <div className="mt-3 flex items-baseline gap-4">
          <div>
            <div className="text-2xl font-semibold text-fg">{state?.per_minute ?? '—'}</div>
            <div className="text-[11px] text-fg-muted">requests / minute / caller</div>
          </div>
          {state && !state.configured && (
            <div className="text-[11px] text-fg-muted">
              deployment default ({state.default})
            </div>
          )}
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-2">
          <input type="number" min="1" max="100000" step="1" value={value}
            onChange={e => setValue(e.target.value)}
            className="w-32 px-2 py-1.5 text-sm rounded-lg border border-line-strong bg-surface text-fg"
            autoFocus />
          <span className="text-xs text-fg-muted">/ minute / caller</span>
          <button onClick={save} disabled={saving}
            className="px-3 py-1.5 bg-primary text-primary-fg text-xs rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50">
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button onClick={() => { setEditing(false); setError(null); if (state?.per_minute != null) setValue(String(state.per_minute)) }}
            className="px-3 py-1.5 text-fg-3 text-xs rounded-lg hover:bg-surface-3 transition-colors">
            Cancel
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * Guardrail alarm thresholds — alert-only. Two CloudWatch alarms watch the input
 * guardrail (ApplyGuardrail throttling, and how often the guardrail intervenes). Changing
 * a threshold only moves when the alarm notifies; it never blocks a model call. A super
 * admin retunes them here without a redeploy. Hidden when the deployment has no guardrail
 * alarms configured.
 */
function GuardrailAlarmsCard({ api }) {
  const [state, setState] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(false)
  const [throttle, setThrottle] = useState('')
  const [intervention, setIntervention] = useState('')
  const [saving, setSaving] = useState(false)

  const load = () => {
    setLoading(true)
    api.get('/api/settings/guardrail-alarms')
      .then(d => {
        setState(d)
        if (d?.configured) {
          setThrottle(String(d.throttle?.threshold ?? ''))
          setIntervention(String(d.intervention?.threshold ?? ''))
        }
        setError(null)
      })
      .catch(e => setError(e.detail || e.message || 'Failed to load the guardrail alarms'))
      .finally(() => setLoading(false))
  }
  useEffect(load, [api])

  const save = () => {
    const t = Number(throttle), i = Number(intervention)
    if (![t, i].every(n => Number.isInteger(n) && n >= 1 && n <= 100000)) {
      setError('Enter whole numbers between 1 and 100,000')
      return
    }
    setSaving(true)
    api.put('/api/settings/guardrail-alarms', { throttle_threshold: t, intervention_threshold: i })
      .then(() => { setEditing(false); setError(null); load() })
      .catch(e => setError(e.detail || e.message || 'Failed to update the guardrail alarms'))
      .finally(() => setSaving(false))
  }

  if (loading) return null
  if (state && !state.configured) return null  // no guardrail alarms in this deployment

  return (
    <div className="bg-surface rounded-xl border border-line p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-fg">Guardrail alarm thresholds</h3>
          <p className="text-xs text-fg-3 mt-1 leading-relaxed">
            Alert-only. Emails the notification address when guardrail throttling or
            interventions exceed these counts in a 5-minute window; it never blocks a model
            call. Super admins can retune them.
          </p>
        </div>
        {!editing && (
          <button onClick={() => setEditing(true)}
            className="px-3 py-1.5 bg-primary text-primary-fg text-xs rounded-lg hover:bg-primary-hover transition-colors flex-shrink-0">
            Change
          </button>
        )}
      </div>

      {error && (
        <div className="mt-3 px-3 py-1.5 rounded-lg text-xs bg-err-bg text-err-text">{error}</div>
      )}

      {!editing ? (
        <div className="mt-3 flex items-baseline gap-6">
          <div>
            <div className="text-2xl font-semibold text-fg">{state?.throttle?.threshold ?? '—'}</div>
            <div className="text-[11px] text-fg-muted">throttling / 5 min</div>
          </div>
          <div>
            <div className="text-2xl font-semibold text-fg">{state?.intervention?.threshold ?? '—'}</div>
            <div className="text-[11px] text-fg-muted">interventions / 5 min</div>
          </div>
        </div>
      ) : (
        <div className="mt-3 space-y-2">
          <label className="flex items-center gap-2">
            <input type="number" min="1" max="100000" step="1" value={throttle}
              onChange={e => setThrottle(e.target.value)}
              className="w-28 px-2 py-1.5 text-sm rounded-lg border border-line-strong bg-surface text-fg"
              autoFocus />
            <span className="text-xs text-fg-muted">throttling alerts / 5 min</span>
          </label>
          <label className="flex items-center gap-2">
            <input type="number" min="1" max="100000" step="1" value={intervention}
              onChange={e => setIntervention(e.target.value)}
              className="w-28 px-2 py-1.5 text-sm rounded-lg border border-line-strong bg-surface text-fg" />
            <span className="text-xs text-fg-muted">intervention alerts / 5 min</span>
          </label>
          <div className="flex items-center gap-2 pt-1">
            <button onClick={save} disabled={saving}
              className="px-3 py-1.5 bg-primary text-primary-fg text-xs rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50">
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button onClick={() => {
                setEditing(false); setError(null)
                if (state?.configured) {
                  setThrottle(String(state.throttle?.threshold ?? ''))
                  setIntervention(String(state.intervention?.threshold ?? ''))
                }
              }}
              className="px-3 py-1.5 text-fg-3 text-xs rounded-lg hover:bg-surface-3 transition-colors">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Admin audit vault — read-only summary. Editing lives in one place, Settings → Audit,
 * which the button opens. Hidden while the state is loading or unreadable (the posture
 * row below still reports it).
 */
function AuditVaultCard({ api, onOpenAudit }) {
  const [meta, setMeta] = useState(null)
  useEffect(() => {
    let live = true
    api.get('/api/audit/meta').then(d => { if (live) setMeta(d) }).catch(() => {})
    return () => { live = false }
  }, [api])
  if (!meta) return null
  const on = !!meta.vault_enabled
  return (
    <div className={`bg-surface rounded-xl border p-4 ${on ? 'border-line' : 'border-warn-border'}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-fg">Admin audit vault</h3>
            <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${on ? 'bg-ok-bg text-ok-text' : 'bg-warn-bg text-warn-text'}`}>
              {on ? 'On' : 'Off'}
            </span>
          </div>
          <p className="text-xs text-fg-3 mt-1 leading-relaxed">
            Records are kept in S3 Object Lock (COMPLIANCE): nobody, including the account root user, can change
            or delete a record before it expires.
          </p>
          {!on && (
            <p className="text-xs text-warn-text mt-1 leading-relaxed">
              Audit log: index only, not tamper-proof. A super admin can switch the vault on in Settings → Audit.
            </p>
          )}
        </div>
        {onOpenAudit && (
          <button onClick={onOpenAudit}
            className="px-3 py-1.5 text-xs font-medium border border-line rounded-lg text-fg-2 hover:bg-surface-3 transition-colors flex-shrink-0">
            Open audit settings
          </button>
        )}
      </div>
      <div className="mt-3 flex items-baseline gap-6">
        <div>
          <div className="text-2xl font-semibold text-fg">{meta.effective_retention_days ?? '—'}</div>
          <div className="text-[11px] text-fg-muted">days retention for new records</div>
        </div>
        {meta.floor_days != null && (
          <div>
            <div className="text-2xl font-semibold text-fg-2">{meta.floor_days}</div>
            <div className="text-[11px] text-fg-muted">days minimum (deploy floor)</div>
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * Security posture for this deployment, in three tiers:
 *   1. Recommended actions — configurable controls that are OFF and worth enabling.
 *   2. Configurable controls — the CloudFormation stack toggles with live status
 *      (read from the task environment, so they always reflect the deployed stack).
 *   3. Built-in protections — always on, provided by the architecture; shown so
 *      customers see what they get implicitly, not as choices.
 *
 * This page never changes infrastructure — configurable controls are stack
 * parameters, changed by updating the DBOps Agent stack, which the page names
 * (stack, account, region) without linking to the console.
 * The application is intentionally not granted permission to modify its own stack.
 */
export default function Security({ api, onOpenAudit }) {
  const [posture, setPosture] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    api.get('/api/security-posture')
      .then(d => { if (active) { setPosture(d); setError(null) } })
      .catch(e => { if (active) setError(e.detail || e.message || 'Failed to load security posture') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [api])

  if (loading) return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-fg">Security</h2>
      <div className="text-sm text-fg-muted">Loading security posture...</div>
    </div>
  )

  const controls = posture?.controls || []
  const configurable = controls.filter(c => c.category === 'configurable')
  const builtin = controls.filter(c => c.category !== 'configurable')
  const recommendations = configurable.filter(c => c.status !== 'enabled' && c.recommended)

  // 'partial' and 'unknown' exist so a control is never reported as Enabled on a guess.
  // The MFA row is read from the live user pool, so it can come back OPTIONAL or
  // unreadable — printing either as "Enabled" would hide a disabled auth control behind
  // a green badge.
  const badge = (c) => {
    if (c.status === 'enabled') return { label: 'Enabled', cls: 'bg-ok-bg text-ok-text' }
    if (c.status === 'partial') return { label: 'Partial', cls: 'bg-warn-bg text-warn-text' }
    if (c.status === 'unknown') return { label: 'Unknown', cls: 'bg-surface-3 text-fg-2' }
    return { label: 'Off', cls: 'bg-warn-bg text-warn-text' }
  }

  // Distinguishes a status read from the live resource from one that is a property of the
  // architecture nothing checks at runtime. Only shown for 'declared', so the default
  // reading of an unlabelled row is "this was measured".
  const declaredNote = (c) => c.basis === 'declared' ? (
    <span className="ml-1.5 text-[10px] text-fg-muted"
          title="Architectural property of this deployment. Not verified by a runtime check.">
      by design
    </span>
  ) : null

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-fg">Security</h2>
        <p className="text-sm text-fg-3">
          Live posture for this deployment. Configurable controls reflect the deployed
          CloudFormation stack parameters; changing them is a stack update, not an in-app action.
        </p>
      </div>

      <AuditVaultCard api={api} onOpenAudit={onOpenAudit} />
      <BudgetCard api={api} />
      <RateLimitCard api={api} />
      <GuardrailAlarmsCard api={api} />

      {error && (
        <div className="px-4 py-2 rounded-lg text-sm bg-err-bg text-err-text">{error}</div>
      )}

      {posture && (
        <>
          {/* 1 — Recommended actions */}
          {recommendations.length > 0 && (
            <div className="bg-warn-bg border border-warn-border rounded-xl p-4">
              <div className="flex items-center gap-2 mb-2">
                <svg className="w-4 h-4 text-warn-mid" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                </svg>
                <h3 className="text-sm font-semibold text-warn-text">
                  Recommended — currently off
                </h3>
              </div>
              <ul className="space-y-2">
                {recommendations.map(c => (
                  <li key={c.key} className="text-xs text-warn-text leading-relaxed">
                    <span className="font-semibold">{c.label}</span> — {c.recommendation}{' '}
                    {c.parameter && (
                      <code className="bg-warn-bg-2 px-1.5 py-0.5 rounded whitespace-nowrap">{c.parameter}</code>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* 2 — Configurable controls */}
          <div>
            <h3 className="text-sm font-semibold text-fg mb-2">Configurable controls</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {configurable.map(c => {
                const b = badge(c)
                const off = c.status !== 'enabled'
                return (
                  <div key={c.key}
                    className={`bg-surface rounded-xl border p-4 ${
                      off ? 'border-warn-border' : 'border-line'
                    }`}>
                    <div className="flex items-start justify-between gap-3">
                      <h4 className="text-sm font-semibold text-fg">
                        {c.label}{declaredNote(c)}
                      </h4>
                      <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium flex-shrink-0 ${b.cls}`}>
                        {b.label}
                      </span>
                    </div>
                    <p className="text-xs text-fg-3 mt-2 leading-relaxed">{c.detail}</p>
                    {c.parameter && (
                      <p className="text-[11px] text-fg-muted mt-2">
                        Stack parameter: <code className="bg-surface-3 px-1.5 py-0.5 rounded">{c.parameter}</code>
                      </p>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          {/* Manage note: where these controls are changed, and which stack this is. The
              stack, account and region are named so an operator can find it in their own
              console session; no link is offered because the console would open in whatever
              account the browser happens to be signed in to. */}
          {posture.manage && (
            <div className="bg-info-bg border border-info-border rounded-xl p-4">
              <p className="text-xs text-fg-2 leading-relaxed">{posture.manage.note}</p>
              {(posture.manage.stack_name || posture.manage.account_id) && (
                <p className="text-xs text-fg-3 mt-2 leading-relaxed">
                  This deployment
                  {posture.manage.stack_name && <> is stack <span className="font-mono text-fg-2">{posture.manage.stack_name}</span></>}
                  {posture.manage.account_id && <> in account <span className="font-mono text-fg-2">{posture.manage.account_id}</span></>}
                  {posture.manage.region && <> ({posture.manage.region})</>}
                  .
                </p>
              )}
            </div>
          )}

          {/* 3 — Built-in protections */}
          <div>
            <h3 className="text-sm font-semibold text-fg mb-1">Built-in protections</h3>
            <p className="text-xs text-fg-3 mb-2">
              Provided by the architecture rather than chosen per deployment. Rows marked
              &ldquo;by design&rdquo; are properties of the build that no runtime check confirms;
              the rest are read live, so they can report off.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {/* Built-in does not mean "cannot be off". The MFA row is read from the live
                  user pool and can legitimately come back off, partial or unknown, so the
                  badge reflects the status the API returned rather than the section it is
                  rendered in. */}
              {builtin.map(c => {
                const b = badge(c)
                const off = c.status !== 'enabled'
                return (
                  <div key={c.key}
                    className={`bg-surface rounded-xl border p-4 ${
                      off ? 'border-warn-border' : 'border-line'
                    }`}>
                    <div className="flex items-start justify-between gap-3">
                      <h4 className="text-sm font-semibold text-fg">
                        {c.label}{declaredNote(c)}
                      </h4>
                      <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium flex-shrink-0 ${b.cls}`}>
                        {b.label}
                      </span>
                    </div>
                    <p className="text-xs text-fg-3 mt-2 leading-relaxed">{c.detail}</p>
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
