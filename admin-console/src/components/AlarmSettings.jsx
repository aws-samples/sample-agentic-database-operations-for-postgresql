// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Settings → Alarms (super admins): email alarms on the collected metrics. One switch
// for all alarms, one per alarm, where the emails go, a test email, and the alarms
// firing now. Each switch saves at once; the CloudWatch alarms follow in the
// background, so the counts below refresh on their own for a short while after a save.
import { useCallback, useEffect, useRef, useState } from 'react'
import Toggle from './Toggle'

const errText = (e, fallback) => !e ? '' : (e.detail || e.message || fallback)
const fmtWhen = (iso) => {
  const d = new Date(iso)
  return iso && !Number.isNaN(d.getTime())
    ? d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : ''
}
const SCOPE = { writer: 'Writer', readers: 'Readers', database: 'Database' }
// Refetch after a save: the API returns at once and syncs the alarms in the background.
const FOLLOW_UP_MS = [4000, 12000, 30000]

function SeverityPill({ severity }) {
  const cls = severity === 'critical'
    ? 'bg-err-bg text-err-text border-err-border'
    : 'bg-warn-bg text-warn-text border-warn-border'
  return <span className={`inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium border ${cls}`}>
    {severity === 'critical' ? 'Critical' : 'Warning'}
  </span>
}

function Delivery({ api, delivery }) {
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState(null)
  const send = async () => {
    setSending(true); setResult(null)
    try {
      const r = await api.post('/api/settings/alarms/test', {})
      setResult({ ok: true, text: `Test email sent to ${r.recipient} ${r.sent_via === 'ses' ? 'as HTML (SES)' : 'as plain text (notification topic)'}.` })
    } catch (e) {
      setResult({ ok: false, text: errText(e, 'Could not send the test email.') })
    } finally { setSending(false) }
  }
  const d = delivery || {}
  const html = d.channel === 'ses'
  return (
    <div className="bg-surface rounded-xl border border-line p-4 space-y-2">
      <h3 className="text-sm font-semibold text-fg">Where alarm emails go</h3>
      {d.recipient
        ? <p className="text-sm text-fg-2">
            To <span className="font-medium text-fg">{d.recipient}</span>
            {html ? <> as formatted HTML, from <span className="font-medium text-fg">{d.sender}</span>.</>
              : d.channel === 'sns' ? ' as plain text through the notification topic.' : '.'}
          </p>
        : <p className="text-sm text-warn-text">No notification address is set for this deployment.</p>}
      {d.reason && <p className="text-xs text-fg-3 leading-relaxed">{d.reason}</p>}
      <p className="text-xs text-fg-3 leading-relaxed">
        The address is the deployment&apos;s notification email (NOTIFICATION_EMAIL). Formatted emails need a
        verified sender under Settings › Email.
      </p>
      <div className="flex items-center gap-3 pt-1">
        <button onClick={send} disabled={sending || !d.recipient}
          className="px-3 py-1.5 bg-surface-3 text-fg text-xs rounded-lg hover:bg-surface-active transition-colors disabled:opacity-50">
          {sending ? 'Sending…' : 'Send a test email'}
        </button>
        {result && <p role={result.ok ? 'status' : 'alert'} className={`text-xs ${result.ok ? 'text-ok-text' : 'text-err'}`}>{result.text}</p>}
      </div>
    </div>
  )
}

export default function AlarmSettings({ api }) {
  const [view, setView] = useState(null)
  const [error, setError] = useState(null)
  const [saveError, setSaveError] = useState(null)
  const [pending, setPending] = useState({})   // id or '__all' -> saving
  const timers = useRef([])

  const load = useCallback(() => api.get('/api/settings/alarms')
    .then(v => { setView(v); setError(null) })
    .catch(e => setError(errText(e, 'Could not load the alarm settings.'))), [api])
  useEffect(() => { load(); return () => timers.current.forEach(clearTimeout) }, [load])

  const followUp = () => {
    timers.current.forEach(clearTimeout)
    timers.current = FOLLOW_UP_MS.map(ms => setTimeout(load, ms))
  }
  const save = async (key, body, optimistic) => {
    setPending(p => ({ ...p, [key]: true })); setSaveError(null)
    setView(optimistic)
    try { setView(await api.put('/api/settings/alarms', body)); followUp() }
    catch (e) { setSaveError(errText(e, 'Could not save. Nothing changed.')); load() }
    finally { setPending(p => { const n = { ...p }; delete n[key]; return n }) }
  }
  const setAll = (on) => save('__all', { enabled: on }, { ...view, enabled: on })
  const setOne = (id, on) => save(id, { alarms: { [id]: on } },
    { ...view, alarms: view.alarms.map(a => a.id === id ? { ...a, enabled: on } : a) })

  if (error) return <div role="alert" className="px-4 py-2 rounded-lg text-sm bg-err-bg text-err-text">{error}</div>
  if (!view) return <p className="text-sm text-fg-3">Loading…</p>
  const st = view.status
  const onCount = view.alarms.filter(a => a.enabled).length
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-fg">Alarms</h2>
        <p className="text-sm text-fg-3">
          Email alerts when a monitored database crosses a threshold, from its Tier 1 metrics. Alarms only notify;
          they never change a database. Thresholds are the same ones the deep-scan findings use.
        </p>
      </div>
      {saveError && <div role="alert" className="px-4 py-2 rounded-lg text-sm bg-err-bg text-err-text">{saveError}</div>}

      <div className="bg-surface rounded-xl border border-line p-4 space-y-3">
        <label className="flex items-center gap-3 cursor-pointer">
          <Toggle checked={view.enabled} disabled={!!pending.__all} onChange={setAll}
            title={view.enabled ? 'Alarms are on' : 'Alarms are off'} />
          <span className="text-sm font-medium text-fg">Email alarms</span>
          {!view.enabled && <span className="inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium bg-warn-bg text-warn-text border border-warn-border">Off</span>}
        </label>
        <p className="text-xs text-fg-3 ml-12 leading-relaxed">
          {view.enabled
            ? `${onCount} of ${view.alarms.length} alarms are on for every monitored database. A database with Tier 1 paused has none.`
            : 'No alarms exist and no alarm emails are sent. Switching this on recreates them for every monitored database.'}
        </p>
        {st
          ? <p className="text-xs text-fg-2 ml-12">
              {st.alarm_count} CloudWatch alarm{st.alarm_count === 1 ? '' : 's'} across {st.databases_monitored} monitored
              database{st.databases_monitored === 1 ? '' : 's'}.
            </p>
          : view.status_error && <p className="text-xs text-warn-text ml-12">{view.status_error}</p>}
        {st && st.firing.length > 0 && (
          <div className="ml-12 border-t border-line-2 pt-3">
            <p className="text-xs font-medium text-err-text mb-1.5">Firing now ({st.firing.length})</p>
            <ul className="space-y-1">
              {st.firing.map(f => (
                <li key={`${f.alarm_id}-${f.instance_id}`} className="text-xs text-fg-2">
                  <span className="font-medium text-fg">{f.database}</span> · {f.label}
                  {f.since && <span className="text-fg-3"> · since {fmtWhen(f.since)}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <Delivery api={api} delivery={view.delivery} />

      <div className="bg-surface rounded-xl border border-line overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-fg-3 border-b border-line">
              <th className="px-4 py-2 font-medium w-16">On</th>
              <th className="px-4 py-2 font-medium">Alarm</th>
              <th className="px-4 py-2 font-medium">Fires when</th>
              <th className="px-4 py-2 font-medium w-24">Measured on</th>
            </tr>
          </thead>
          <tbody>
            {view.alarms.map(a => (
              <tr key={a.id} className={`border-b border-line-2 last:border-0 align-top ${view.enabled ? '' : 'opacity-60'}`}>
                <td className="px-4 py-3">
                  <Toggle checked={a.enabled} disabled={!view.enabled || !!pending[a.id]} size="sm"
                    onChange={(on) => setOne(a.id, on)}
                    title={!view.enabled ? 'Email alarms are off' : a.enabled ? 'On' : 'Off'} />
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-fg">{a.label}</span>
                    <SeverityPill severity={a.severity} />
                    {!a.default && <span className="text-[11px] text-fg-muted">off by default</span>}
                  </div>
                  <p className="text-xs text-fg-3 mt-1 leading-relaxed">{a.meaning}</p>
                </td>
                <td className="px-4 py-3 text-xs text-fg-2">{a.threshold}</td>
                <td className="px-4 py-3 text-xs text-fg-2">{SCOPE[a.scope] || a.scope}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-[11px] text-fg-muted">
        {view.configured && view.updated_at ? `Last changed ${fmtWhen(view.updated_at)}` : 'Using the defaults'}
      </p>
    </div>
  )
}
