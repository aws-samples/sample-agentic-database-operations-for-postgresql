// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// The per-database drawer under the Dashboard table: one surface with tabs for
// Members (DB clusters only), Metrics (Tier 1), Deep scans (Tier 2), Trends and
// CloudWatch metrics. Deep scans lists every completed scan with its headline data and
// report link (and compares two of them), so reports need no tab of their own. Trends
// is the curated handful of charts with scans as markers; CloudWatch metrics is every
// published series over windows up to 15 months. Row actions deep-link to a tab; the
// drawer stays on the same database while the operator switches. For a cluster a
// member picker beside the tabs chooses whose rows the other tabs read (the writer
// by default).
import { useEffect, useState } from 'react'
import Tier1Metrics from './Tier1Metrics'
import ScanHistory from './ScanHistory'
import TrendChart from './TrendChart'
import CloudWatchMetrics from './CloudWatchMetrics'
import ClusterMembers from './ClusterMembers'
import MemberPicker from './MemberPicker'

const BASE_TABS = [
  { id: 'metrics', label: 'Metrics (Tier 1)' },
  { id: 'scans', label: 'Deep scans (Tier 2)' },
  { id: 'trends', label: 'Trends' },
  { id: 'cloudwatch', label: 'CloudWatch metrics' },
]

export const isCluster = (instance) => (instance?.cluster_member_roles || []).length > 0

// The drawer's tabs for a database: Members first for a cluster, hidden otherwise.
export function drawerTabs(instance) {
  return isCluster(instance) ? [{ id: 'members', label: 'Members' }, ...BASE_TABS] : BASE_TABS
}

export default function DatabaseDrawer({ api, instance, tab, onTab, onClose, onViewReport, onCompare }) {
  const [member, setMember] = useState('writer')
  useEffect(() => { setMember('writer') }, [instance?.instance_id])
  if (!instance) return null
  const tabs = drawerTabs(instance)
  const cluster = isCluster(instance)
  const active = tabs.some(t => t.id === tab) ? tab : (cluster ? 'members' : 'metrics')
  const selector = cluster ? member : ''
  return (
    <div className="bg-surface rounded-lg border border-line shadow-sm">
      <div className="flex items-center gap-3 px-5 pt-4 pb-3 border-b border-line-2 flex-wrap">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-fg truncate">{instance.display_name}</h2>
          <p className="text-[10px] text-fg-muted">{instance.engine}{instance.region ? ` · ${instance.region}` : ''}</p>
        </div>
        <div role="tablist" aria-label="Database records" className="flex items-center gap-1 p-0.5 rounded-lg bg-surface-2 border border-line-2 ml-2">
          {tabs.map(t => (
            <button key={t.id} role="tab" aria-selected={active === t.id} onClick={() => onTab?.(t.id)}
              className={`px-3 h-7 rounded-md text-xs transition-colors ${active === t.id ? 'bg-surface text-fg font-medium shadow-sm' : 'text-fg-3 hover:text-fg'}`}>
              {t.label}
            </button>
          ))}
        </div>
        {cluster && active !== 'members' && (
          <MemberPicker api={api} instance={instance} value={member} onChange={setMember} id={`member-${instance.instance_id}`} />
        )}
        <button onClick={onClose} className="ml-auto text-xs text-fg-3 hover:text-fg-2">✕ Close</button>
      </div>
      <div className="p-5">
        {active === 'members' && (
          <ClusterMembers api={api} instance={instance} onSelectMember={(sel) => { setMember(sel); onTab?.('metrics') }} />
        )}
        {active === 'metrics' && <Tier1Metrics api={api} instance={instance} member={selector} />}
        {active === 'scans' && <ScanHistory api={api} instance={instance} member={selector} onViewReport={onViewReport} onCompare={onCompare ? (a, b) => onCompare(instance, a, b) : undefined} embedded />}
        {active === 'trends' && <TrendChart api={api} instanceId={instance.instance_id} instance={instance} member={selector} />}
        {active === 'cloudwatch' && <CloudWatchMetrics api={api} instance={instance} member={selector} />}
      </div>
    </div>
  )
}
