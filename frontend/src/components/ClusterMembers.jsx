// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Members tab of a DB cluster's drawer: the cluster, reader and instance endpoints
// (information, with a copy button), and one row per member with its role NOW (the
// Tier 1 collector re-reads the roles from RDS on every run, so a failover shows
// within one interval), AZ, endpoint, health of its latest scan, replica lag and the
// freshness of its Tier 1 collection. An instance id opens the Metrics tab on that
// member. The admin console keeps an identical copy (admin-console/src/components).
import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { useLiveMembers } from './MemberPicker'

const th = 'py-2 px-2 text-left text-[10px] uppercase tracking-wider text-fg-3 font-semibold whitespace-nowrap'
const td = 'py-1.5 px-2 text-[11px] whitespace-nowrap text-fg-2'
const DOT = { fresh: 'bg-ok', stale: 'bg-warn', none: 'bg-fg-muted', paused: 'bg-fg-muted', skipped: 'bg-fg-muted' }
const HEALTH = {
  critical: 'bg-err-bg text-err-text border-err-border',
  warning: 'bg-warn-bg text-warn-text border-warn-border',
  ok: 'bg-ok-bg text-ok-text border-ok-border',
}

const timeAgo = (iso) => {
  if (!iso) return '—'
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 2) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} d ago`
}
const lag = (v) => (v === null || v === undefined) ? '—' : Number(v) >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`

function HealthBadge({ health }) {
  if (!health) return <span className="text-fg-muted">—</span>
  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${HEALTH[health] || 'bg-surface-2 text-fg-3 border-line'}`}>
      {health.toUpperCase()}
    </span>
  )
}

function CopyRow({ label, value }) {
  const [done, setDone] = useState(false)
  if (!value) return null
  const copy = () => navigator.clipboard?.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1500) }).catch(() => {})
  return (
    <div className="flex items-center gap-2 py-0.5 min-w-0">
      <span className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold w-28 flex-shrink-0">{label}</span>
      <code className="text-[11px] font-mono text-fg-2 truncate" title={value}>{value}</code>
      <button onClick={copy} aria-label={`Copy ${label} endpoint`} title="Copy"
        className="p-0.5 rounded text-fg-muted hover:text-primary flex-shrink-0">
        {done ? <Check size={12} aria-hidden /> : <Copy size={12} aria-hidden />}
      </button>
    </div>
  )
}

export default function ClusterMembers({ api, instance, onSelectMember }) {
  // Refreshed while open (every 30 s while the tab is visible, and on focus): a failover
  // shows here within a poll of Tier 1 recording it. A refresh keeps the rows on screen.
  const { data, error, loading } = useLiveMembers(api, instance?.instance_id)

  if (loading && !data) return <div className="text-sm text-fg-muted py-4 text-center">Loading members...</div>
  if (error && !data) return <p className="text-xs text-err py-3">{error}</p>
  const members = data?.members || []
  const ep = data?.endpoints || {}
  return (
    <div className="space-y-4">
      <section aria-labelledby="endpoints-title" className="bg-surface-2 rounded-lg border border-line-2 px-3 py-2">
        <h3 id="endpoints-title" className="text-[10px] uppercase tracking-wider text-fg-3 font-semibold mb-1">Endpoints</h3>
        <CopyRow label="Cluster (writer)" value={ep.cluster} />
        <CopyRow label="Reader" value={ep.reader} />
        {Object.entries(ep.instance || {}).map(([mid, host]) => <CopyRow key={mid} label={mid} value={host} />)}
      </section>
      {data && data.store_configured === false && (
        <p className="text-[11px] text-fg-muted">The metrics store is not configured for this deployment, so collection status is not shown.</p>
      )}
      {data && data.store_error && (
        <p className="text-[11px] text-warn-text">Collection status could not be read just now ({data.store_error}); showing members without live status.</p>
      )}
      {members.length === 0 ? (
        <div className="text-sm text-fg-muted py-4 text-center">No members recorded for this database.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full">
            <caption className="sr-only">Members of {instance?.display_name}</caption>
            <thead><tr className="border-b border-line">
              <th scope="col" className={th}>Role</th>
              <th scope="col" className={th}>Instance</th>
              <th scope="col" className={th}>AZ</th>
              <th scope="col" className={th}>Endpoint</th>
              <th scope="col" className={th}>Health</th>
              <th scope="col" className={th}>Replica lag</th>
              <th scope="col" className={th}>Last collection</th>
            </tr></thead>
            <tbody>
              {members.map(m => (
                <tr key={m.instance_id} className="border-b border-line-2 hover:bg-surface-3">
                  <td className={td}>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${m.role === 'writer' ? 'bg-primary-subtle text-primary border-primary-line' : 'bg-surface-2 text-fg-3 border-line'}`}>
                      {m.role}
                    </span>
                  </td>
                  <td className={td}>
                    <button onClick={() => onSelectMember?.(`instance:${m.instance_id}`)} title="Open this member's metrics"
                      className="font-mono text-fg hover:text-primary hover:underline">{m.instance_id}</button>
                  </td>
                  <td className={td}>{m.availability_zone || '—'}</td>
                  <td className={`${td} font-mono max-w-[260px] truncate`} title={m.endpoint}>{m.endpoint || '—'}</td>
                  <td className={td}><HealthBadge health={m.health} /></td>
                  <td className={`${td} tabular-nums`}>{m.role === 'reader' ? lag(m.replica_lag_ms) : '—'}</td>
                  <td className={td}>
                    {m.tier1_state ? (
                      <span className={`inline-flex items-center gap-1.5 ${m.tier1_state === 'stale' ? 'text-warn-text' : 'text-fg-3'}`}
                        title={m.skip_reason || m.tier1_state}>
                        <span className={`w-1.5 h-1.5 rounded-full ${DOT[m.tier1_state] || 'bg-fg-muted'}`} aria-hidden />
                        {m.tier1_state === 'skipped' ? 'Skipped' : m.tier1_state === 'paused' ? 'Paused' : timeAgo(m.last_tier1_at)}
                      </span>
                    ) : <span className="text-fg-muted">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {members.some(m => m.skip_reason) && (
        <p className="text-[11px] text-fg-muted">
          {members.filter(m => m.skip_reason).map(m => `${m.instance_id}: ${m.skip_reason.replace(/^skipped: /, '')}`).join(' · ')}
        </p>
      )}
      {members.some(m => m.reader_scan_outcome && m.reader_scan_outcome !== 'reached') && (
        <p className="text-[11px] text-fg-muted">
          Last reader scan: {members.filter(m => m.reader_scan_outcome && m.reader_scan_outcome !== 'reached')
            .map(m => `${m.instance_id}: ${m.reader_scan_outcome.replace(/^skipped: /, '')}`).join(' · ')}
        </p>
      )}
      {error && (
        <p className="text-[10px] text-warn-text" role="status">Could not refresh the members ({error}); showing the last answer.</p>
      )}
      {data?.members_changed_at && (
        <p className="text-[10px] text-fg-muted">Members last changed {timeAgo(data.members_changed_at)}</p>
      )}
    </div>
  )
}
