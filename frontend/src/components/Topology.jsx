// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo } from 'react'
import { Network } from 'lucide-react'
import { APP_NAME } from '../appName'
import TopologyMap from './TopologyMap'

// ── SVG Icons ──
const Icons = {
  database: (
    <svg className="w-full h-full" fill="currentColor" viewBox="0 0 24 24">
      <path d="M12 3C7.58 3 4 4.79 4 7v10c0 2.21 3.58 4 8 4s8-1.79 8-4V7c0-2.21-3.58-4-8-4zm0 2c3.87 0 6 1.5 6 2s-2.13 2-6 2-6-1.5-6-2 2.13-2 6-2zm6 12c0 .5-2.13 2-6 2s-6-1.5-6-2v-2.23c1.61.78 3.72 1.23 6 1.23s4.39-.45 6-1.23V17zm0-5c0 .5-2.13 2-6 2s-6-1.5-6-2V9.77c1.61.78 3.72 1.23 6 1.23s4.39-.45 6-1.23V12z"/>
    </svg>
  ),
  orchestrator: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 001.423 1.423l1.183.394-1.183.394a2.25 2.25 0 00-1.423 1.423z" />
    </svg>
  ),
  observability: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
    </svg>
  ),
  postgres: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" />
    </svg>
  ),
  cloud: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15a4.5 4.5 0 004.5 4.5H18a3.75 3.75 0 001.332-7.257 3 3 0 00-3.758-3.848 5.25 5.25 0 00-10.233 2.33A4.502 4.502 0 002.25 15z" />
    </svg>
  ),
  globe: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418" />
    </svg>
  ),
  building: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75c.621 0 1.125.504 1.125 1.125V21" />
    </svg>
  ),
  service: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z" />
    </svg>
  ),
  bolt: (
    <svg className="w-full h-full" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
    </svg>
  ),
  chevron: (
    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
    </svg>
  ),
}

const COLORS = {
  database: '#4338ca', agent_vpc: '#ff9900', vpc: '#64748b', account: '#8b5cf6',
  region: '#14b8a6', aws_service: '#e47911', sub_agent: '#06b6d4', tool: '#6366f1',
}
const EDGE_COLORS = { peering: '#22c55e', lattice: '#4338ca', same_vpc: '#0891b2' }
const AGENT_ICONS = { orchestrator: Icons.orchestrator, investigation: Icons.orchestrator, observability: Icons.observability, postgres: Icons.postgres, docs: Icons.globe }

// ── Reusable components ──
function IconBox({ icon, color, size = 'md' }) {
  const s = size === 'lg' ? 'w-10 h-10' : size === 'sm' ? 'w-6 h-6' : 'w-8 h-8'
  const p = size === 'lg' ? 'p-2' : size === 'sm' ? 'p-1' : 'p-1.5'
  return (
    <div className={`${s} ${p} rounded-xl flex items-center justify-center text-white flex-shrink-0`}
      style={{ backgroundColor: color }}>{icon}</div>
  )
}

function Card({ icon, color, title, subtitle, desc, onClick, badge, health, size = 'md' }) {
  const healthBorder = health === 'critical' ? 'border-l-red-500' : health === 'warning' ? 'border-l-amber-500' : health === 'ok' ? 'border-l-green-500' : ''
  return (
    <button onClick={onClick}
      className={`relative flex items-center gap-3 rounded-xl border-2 transition-all hover:shadow-lg text-left
        bg-surface border-line hover:border-primary
        ${healthBorder ? `border-l-4 ${healthBorder}` : ''}
        ${size === 'sm' ? 'px-3 py-2.5' : 'px-4 py-3.5'}`}>
      <IconBox icon={icon} color={color} size={size === 'sm' ? 'sm' : 'md'} />
      <div className="flex-1 min-w-0">
        <div className={`font-semibold text-fg leading-tight truncate ${size === 'sm' ? 'text-xs' : 'text-sm'}`}>{title}</div>
        {subtitle && <div className={`text-fg-3 truncate ${size === 'sm' ? 'text-[10px]' : 'text-[11px]'}`}>{subtitle}</div>}
        {desc && <div className="text-[10px] text-fg-muted mt-0.5 truncate">{desc}</div>}
      </div>
      {badge}
      {onClick && <span className="text-fg-muted flex-shrink-0">{Icons.chevron}</span>}
    </button>
  )
}

function BackButton({ onClick }) {
  return (
    <button onClick={onClick} className="flex items-center gap-1.5 text-sm text-fg-3 hover:text-primary transition-colors mb-5">
      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" /></svg>
      Back
    </button>
  )
}

function SectionHeader({ icon, color, title, subtitle }) {
  return (
    <div className="flex items-center gap-3 mb-4">
      <IconBox icon={icon} color={color} size="lg" />
      <div>
        <h3 className="text-base font-bold text-fg">{title}</h3>
        {subtitle && <p className="text-xs text-fg-3 mt-0.5">{subtitle}</p>}
      </div>
    </div>
  )
}

// ── Drill-downs ──
function AgentDrillDown({ node, allNodes, allEdges, onDrillDown, onBack }) {
  const tools = node.tools || []
  const meta = node.metadata || {}
  const services = allEdges.filter(e => e.source === node.id && e.type === 'uses_service')
    .map(e => allNodes.find(n => n.id === e.target)).filter(Boolean)

  // Group tools by category for postgres
  const toolGroups = useMemo(() => {
    if (tools.length < 10) return null
    const cats = { 'check_db_connectivity': 'Utility', 'run_full_analysis': 'Full Analysis',
      'get_db_overview': 'Overview', 'get_health_summary': 'Health Summary',
      'get_db_configuration': 'Configuration', 'get_security_audit': 'Configuration', 'get_setting_overrides': 'Configuration',
      'get_current_activity': 'Activity', 'get_connection_analysis': 'Activity', 'get_workload_characterization': 'Activity',
      'get_replication_status': 'Replication', 'get_wal_status': 'Replication', 'get_logical_replication': 'Replication',
      'get_storage_analysis': 'Data Footprint', 'get_checkpoint_stats': 'Data Footprint', 'get_bgwriter_stats': 'Data Footprint',
      'get_autovacuum_utilization': 'Data Footprint', 'get_connection_pooling_efficiency': 'Data Footprint',
      'get_buffer_pool_stats': 'Data Footprint', 'get_lock_analysis': 'Data Footprint',
      'get_io_statistics': 'Query Performance', 'get_query_performance': 'Query Performance', 'get_query_plan': 'Query Performance',
      'get_transaction_stats': 'Query Performance', 'get_function_performance': 'Query Performance',
      'get_wait_event_analysis': 'Query Performance', 'get_session_duration_analysis': 'Query Performance',
      'get_prepared_statement_analysis': 'Query Performance', 'get_temp_file_queries': 'Query Performance',
      'get_maintenance_status': 'Maintenance', 'get_sequence_exhaustion': 'Maintenance',
      'get_constraint_validation': 'Maintenance', 'get_materialized_view_freshness': 'Maintenance', 'get_vacuum_stats_health': 'Maintenance',
      'get_index_statistics': 'Optimization', 'get_toast_status': 'Optimization', 'get_partitioning_analysis': 'Optimization',
      'get_bloat_analysis': 'Optimization', 'get_unused_indexes': 'Optimization', 'get_duplicate_indexes': 'Optimization',
      'get_idle_connections': 'Optimization', 'get_oversized_datatypes': 'Optimization', 'get_unused_tables': 'Optimization',
      'get_fk_without_indexes': 'Optimization', 'get_trigger_analysis': 'Optimization', 'get_sequential_scan_tables': 'Optimization',
    }
    const groups = {}
    tools.forEach(t => { const c = cats[t.name] || 'Other'; if (!groups[c]) groups[c] = []; groups[c].push(t) })
    return groups
  }, [tools])

  return (
    <div>
      <BackButton onClick={onBack} />
      <SectionHeader icon={AGENT_ICONS[node.agent_type] || Icons.orchestrator} color={COLORS.sub_agent}
        title={node.label} subtitle={`AI Agent · ${meta.model || 'Claude (Amazon Bedrock)'}`} />
      <p className="text-sm text-fg-2 mb-6 ml-[52px]">{meta.description}</p>

      {/* Sub-agents (observability_agent, postgres_agent) — show as clickable agent cards */}
      {(() => {
        const subAgentTools = tools.filter(t => t.name === 'observability_agent' || t.name === 'postgres_agent' || t.name === 'docs_agent')
        const regularTools = tools.filter(t => t.name !== 'observability_agent' && t.name !== 'postgres_agent' && t.name !== 'docs_agent')
        const subAgentNodes = subAgentTools.map(t => allNodes.find(n => n.id === `agent-${t.name.replace('_agent', '')}`)).filter(Boolean)

        return (
          <>
            {subAgentNodes.length > 0 && (
              <div className="mb-6">
                <div className="text-xs font-semibold text-fg-3 uppercase tracking-wider mb-3">Sub-Agents</div>
                <div className="flex flex-wrap gap-3">
                  {subAgentNodes.map(sa => (
                    <Card key={sa.id} icon={AGENT_ICONS[sa.agent_type] || Icons.orchestrator} color={COLORS.sub_agent}
                      title={sa.label} subtitle={`${(sa.tools || []).length} tools`}
                      desc={sa.metadata?.description?.slice(0, 80)} onClick={() => onDrillDown(sa)} />
                  ))}
                </div>
              </div>
            )}

            {services.length > 0 && (
              <div className="mb-6">
                <div className="text-xs font-semibold text-fg-3 uppercase tracking-wider mb-3">Connected AWS Services</div>
                <div className="flex flex-wrap gap-2">
                  {services.map(svc => (
                    <Card key={svc.id} icon={Icons.service} color={COLORS.aws_service} title={svc.label}
                      subtitle={svc.metadata?.description} onClick={() => onDrillDown(svc)} size="sm" />
                  ))}
                </div>
              </div>
            )}

            {regularTools.length > 0 && (
              <>
                <div className="text-xs font-semibold text-fg-3 uppercase tracking-wider mb-3">Tools ({regularTools.length})</div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                  {regularTools.map((t, i) => (
                    <div key={i} className="px-3 py-2 rounded-lg border border-line-2 bg-surface-2">
                      <div className="flex items-center gap-1.5">
                        <div className="w-4 h-4 text-primary">{Icons.bolt}</div>
                        <span className="text-[11px] font-medium text-fg font-mono">{t.name}</span>
                      </div>
                      <p className="text-[10px] text-fg-3 mt-0.5 ml-[22px]">{t.description}</p>
                    </div>
                  ))}
                </div>
              </>
            )}
          </>
        )
      })()}
    </div>
  )
}

function DatabaseDrillDown({ node, onBack, allNodes = [] }) {
  const [activeTab, setActiveTab] = useState('database')
  const m = node.metadata || {}
  const memberRoles = m.cluster_member_roles || []
  const REACH = { reachable: 'bg-ok-bg-2 text-ok-text border-ok-border', pending: 'bg-warn-bg-2 text-warn-text border-warn-border', blocked: 'bg-err-bg-2 text-err-text border-err-border', unknown: 'bg-surface-3 text-fg-2 border-line' }
  const nw = m.network_method || 'direct'
  const logExports = m.log_exports ? m.log_exports.split(',').filter(Boolean) : []
  const logGroups = m.log_groups ? m.log_groups.split(',').filter(Boolean) : []
  const clusterMembers = m.cluster_members ? m.cluster_members.split(',').filter(Boolean) : []
  const alarmCount = parseInt(m.alarm_count || '0', 10)

  const StatusDot = ({ ok }) => (
    <span className={`inline-block w-1.5 h-1.5 rounded-full ${ok ? 'bg-ok' : 'bg-line-strong'}`} />
  )

  return (
    <div>
      <BackButton onClick={onBack} />

      {/* Header with health badge */}
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl flex items-center justify-center text-white flex-shrink-0" style={{ backgroundColor: COLORS.database }}>
            <div className="w-5 h-5">{Icons.database}</div>
          </div>
          <div>
            <h2 className="text-base font-bold text-fg">{node.label}</h2>
            <div className="flex items-center gap-2 mt-0.5 text-[10px] text-fg-3">
              <span>{node.engine}</span>
              {m.engine_version && <span>v{m.engine_version}</span>}
              <span>·</span>
              <span>{node.region}</span>
              {m.instance_class && <><span>·</span><span>{m.instance_class}</span></>}
            </div>
          </div>
        </div>
        <span className={`px-2.5 py-1 rounded-lg text-xs font-semibold border ${
          node.health === 'ok' ? 'bg-ok-bg-2 text-ok-text border-ok-border' :
          node.health === 'warning' ? 'bg-warn-bg-2 text-warn-text border-warn-border' :
          node.health === 'critical' ? 'bg-err-bg-2 text-err-text border-err-border' :
          'bg-surface-3 text-fg-2 border-line'
        }`}>{(node.health || 'unknown').toUpperCase()}</span>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-3 border-b border-line pb-2">
        {[
          { id: 'database', label: 'Database',
            svg: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" /></svg> },
          { id: 'agent', label: 'Agent Resources',
            svg: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" /></svg> },
        ].map(tab => (
          <button key={tab.id} onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg transition-colors ${
              activeTab === tab.id
                ? 'bg-primary-subtle text-primary font-semibold'
                : 'text-fg-3 hover:bg-surface-3'
            }`}>{tab.svg} {tab.label}</button>
        ))}
      </div>

      {activeTab === 'database' && (<>
      <div className="bg-surface rounded-xl border border-line overflow-hidden">

        {/* Connection & Network */}
        <div className="px-5 py-4 border-b border-line-2">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-5 h-5 rounded bg-info-bg-2 text-info flex items-center justify-center text-[10px]">🔌</span>
            <span className="text-xs font-bold text-fg-2">Connection</span>
          </div>
          <div className="ml-7 grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-1.5 text-[11px]">
            <div><span className="text-fg-muted">Endpoint</span><p className="font-mono text-fg-2 truncate text-[10px]" title={m.endpoint}>{m.endpoint || '—'}</p></div>
            <div><span className="text-fg-muted">Port</span><p className="text-fg-2">{m.port || 5432}</p></div>
            <div><span className="text-fg-muted">Database</span><p className="text-fg-2">{m.database_name || 'postgres'}</p></div>
            <div><span className="text-fg-muted">Network</span><p className="text-fg-2">{nw}</p></div>
            {m.reader_endpoint && <div className="col-span-2"><span className="text-fg-muted">Reader endpoint</span><p className="font-mono text-fg-2 truncate text-[10px]" title={m.reader_endpoint}>{m.reader_endpoint}</p></div>}
            <div className="col-span-2 sm:col-span-4 flex items-center gap-2 pt-1">
              <span className={`px-2 py-0.5 rounded-md text-[10px] font-semibold border ${REACH[m.reachability] || REACH.unknown}`}>{(m.reachability || 'unknown').toUpperCase()}</span>
              <span className="text-fg-3">{m.reach_detail || 'Reachability not recorded'}</span>
            </div>
          </div>
        </div>

        {/* Placement */}
        <div className="px-5 py-4 border-b border-line-2">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-5 h-5 rounded bg-primary-subtle text-primary flex items-center justify-center"><Network className="w-3 h-3" aria-hidden /></span>
            <span className="text-xs font-bold text-fg-2">Placement</span>
          </div>
          <div className="ml-7 flex flex-wrap gap-x-6 gap-y-1.5 text-[11px]">
            <div><span className="text-fg-muted">Availability zones</span><p className="text-fg-2">{(m.subnet_azs || []).join(', ') || '—'}</p></div>
            <div><span className="text-fg-muted">Subnets</span><p className="font-mono text-fg-2 text-[10px]">{(m.subnet_ids || []).join(', ') || '—'}</p></div>
            <div><span className="text-fg-muted">Security groups</span><p className="font-mono text-fg-2 text-[10px]">{(m.security_group_ids || []).join(', ') || '—'}</p></div>
            {m.replica_of && <div><span className="text-fg-muted">Replica of</span><p className="font-mono text-fg-2 text-[10px]">{m.replica_of}</p></div>}
            {(m.replicas || []).length > 0 && <div><span className="text-fg-muted">Replicas</span><p className="font-mono text-fg-2 text-[10px]">{m.replicas.join(', ')}</p></div>}
            <div><span className="text-fg-muted">Metadata refreshed</span><p className="text-fg-2">{m.metadata_refreshed_at ? new Date(m.metadata_refreshed_at).toLocaleString() : 'never'}</p></div>
          </div>
        </div>

        {/* Infrastructure */}
        <div className="px-5 py-4 border-b border-line-2">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-5 h-5 rounded bg-primary-subtle text-primary flex items-center justify-center text-[10px]">🏗️</span>
            <span className="text-xs font-bold text-fg-2">Infrastructure</span>
          </div>
          <div className="ml-7 flex flex-wrap gap-x-6 gap-y-1.5 text-[11px]">
            <div><span className="text-fg-muted">Account</span><p className="font-mono text-fg-2 text-[10px]">{node.account_id}</p></div>
            <div><span className="text-fg-muted">VPC</span><p className="font-mono text-fg-2 text-[10px]">{m.vpc_id || '—'}</p></div>
            <div><span className="text-fg-muted">Parameter Group</span><p className="text-fg-2">{m.parameter_group || '—'}</p></div>
            {m.multi_az === 'True' && <div><span className="text-fg-muted">Multi-AZ</span><p className="text-ok font-medium">Enabled</p></div>}
          </div>
        </div>

        {/* Monitoring — visual status row */}
        <div className="px-5 py-4 border-b border-line-2">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-5 h-5 rounded bg-cat-cyan-bg text-cat-cyan flex items-center justify-center text-[10px]">📊</span>
            <span className="text-xs font-bold text-fg-2">Monitoring</span>
          </div>
          <div className="ml-7 space-y-2">
            <div className="flex items-center gap-6 text-[11px]">
              <div className="flex items-center gap-1.5">
                <StatusDot ok={m.pi_enabled === 'True'} />
                <span className="text-fg-2">Performance Insights</span>
                {m.pi_enabled === 'True' && (
                  <>
                    <span className="text-[10px] text-fg-muted">{m.pi_retention || 7}d retention</span>
                    <a href={`https://${node.region}.console.aws.amazon.com/rds/home?region=${node.region}#performance-insights-v20206:/resourceId/${node.label}`}
                      target="_blank" rel="noopener noreferrer"
                      className="text-[10px] text-primary hover:underline inline-flex items-center gap-0.5">
                      Dashboard
                      <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" /></svg>
                    </a>
                  </>
                )}
              </div>
              <div className="flex items-center gap-1.5">
                <StatusDot ok={alarmCount > 0} />
                <span className="text-fg-2">CloudWatch Alarms</span>
                {alarmCount > 0 && <span className="text-[10px] text-fg-muted">{alarmCount} active</span>}
              </div>
            </div>
            {logGroups.length > 0 && (
              <div>
                <span className="text-[10px] text-fg-muted block mb-1">Log Groups</span>
                <div className="flex flex-col gap-1">
                  {logGroups.map(l => (
                    <a key={l} href={`https://${node.region}.console.aws.amazon.com/cloudwatch/home?region=${node.region}#logsV2:log-groups/log-group/${encodeURIComponent(l)}`}
                      target="_blank" rel="noopener noreferrer"
                      className="px-2 py-1 text-[10px] rounded-md bg-surface-2 text-primary font-mono truncate border border-line-2 hover:border-primary-line transition-colors inline-flex items-center gap-1" title={l}>
                      {l}
                      <svg className="w-2.5 h-2.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" /></svg>
                    </a>
                  ))}
                </div>
              </div>
            )}
            {logGroups.length === 0 && logExports.length > 0 && (
              <div>
                <span className="text-[10px] text-fg-muted block mb-1">Log Exports</span>
                <div className="flex flex-wrap gap-1">
                  {logExports.map(l => (
                    <span key={l} className="px-1.5 py-0.5 text-[10px] rounded bg-surface-2 text-fg-2 border border-line-2">{l}</span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Security & Storage — compact row */}
        <div className="px-5 py-4 flex flex-wrap gap-8">
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className="w-5 h-5 rounded bg-err-bg-2 text-err flex items-center justify-center text-[10px]">🔐</span>
              <span className="text-xs font-bold text-fg-2">Security</span>
            </div>
            <div className="ml-7 flex items-center gap-4 text-[11px]">
              <div className="flex items-center gap-1.5">
                <StatusDot ok={m.storage_encrypted === 'True'} />
                <span className="text-fg-2">Encryption</span>
              </div>
              <div className="flex items-center gap-1.5">
                <StatusDot ok={m.deletion_protection === 'True'} />
                <span className="text-fg-2">Deletion Protection</span>
              </div>
            </div>
            {m.kms_key_id && (
              <div className="ml-7 mt-1">
                <a href={`https://${node.region}.console.aws.amazon.com/kms/home?region=${node.region}#/kms/keys`}
                  target="_blank" rel="noopener noreferrer"
                  className="text-[10px] font-mono text-primary hover:underline break-all">{m.kms_key_id}</a>
              </div>
            )}
          </div>
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className="w-5 h-5 rounded bg-warn-bg-2 text-warn-mid flex items-center justify-center text-[10px]">💾</span>
              <span className="text-xs font-bold text-fg-2">Storage & Backup</span>
            </div>
            <div className="ml-7 flex items-center gap-4 text-[11px]">
              {m.storage_type && <div><span className="text-fg-muted">Type</span><p className="text-fg-2">{m.storage_type}</p></div>}
              {m.allocated_storage && m.allocated_storage !== '0' && <div><span className="text-fg-muted">Size</span><p className="text-fg-2">{m.allocated_storage} GB</p></div>}
              {m.backup_retention && m.backup_retention !== '0' && <div><span className="text-fg-muted">Backup</span><p className="text-fg-2">{m.backup_retention} days</p></div>}
            </div>
          </div>
        </div>
      </div>

      {clusterMembers.length > 0 && (
        <div className="mt-3 bg-surface rounded-xl border border-line px-5 py-4">
          <div className="flex items-center gap-2 mb-2">
            <span className="w-5 h-5 rounded bg-ok-bg-2 text-ok flex items-center justify-center text-[10px]">🔗</span>
            <span className="text-xs font-bold text-fg-2">Cluster Members ({clusterMembers.length})</span>
          </div>
          <div className="ml-7 flex flex-wrap gap-1.5">
            {clusterMembers.map(cm => {
              const role = memberRoles.find(r => r.instance_id === cm)
              return (
                <span key={cm} className="inline-flex items-center gap-1.5 px-2 py-1 text-[10px] rounded-md bg-surface-2 text-fg-2 font-mono border border-line-2" title={role?.endpoint || cm}>
                  {cm}
                  {role && <span className={`px-1 rounded font-sans font-medium ${role.is_writer ? 'bg-primary-subtle text-primary' : 'bg-surface-3 text-fg-3'}`}>{role.is_writer ? 'writer' : 'reader'}</span>}
                </span>
              )
            })}
          </div>
        </div>
      )}
      </>)}

      {/* Agent Tab */}
      {activeTab === 'agent' && (
        <AgentResourceCards node={node} m={m} nw={nw} logGroups={logGroups} logExports={logExports} allNodes={allNodes} />
      )}
    </div>
  )
}

function AgentResourceCards({ node, m, nw, logGroups, logExports, allNodes = [] }) {
  const [expanded, setExpanded] = useState(null)
  const agentByType = Object.fromEntries(allNodes.filter(n => n.type === 'sub_agent').map(n => [n.agent_type, n]))
  const toolCount = (t) => (agentByType[t]?.tools || []).length
  const modelName = agentByType.orchestrator?.metadata?.model || 'Claude (Amazon Bedrock)'
  const region = node.region || 'us-east-1'
  const dbId = node.label || ''
  const consoleBase = `https://${region}.console.aws.amazon.com`
  const alarmCount = parseInt(m.alarm_count || '0', 10)

  const ExtLink = ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer"
      className="text-[10px] text-primary hover:underline inline-flex items-center gap-0.5">
      {children}
      <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" /></svg>
    </a>
  )

  const SvcIcon = ({ type }) => {
    const cls = "w-5 h-5 text-white"
    if (type === 'network') return <svg className={cls} fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244" /></svg>
    if (type === 'cloudwatch') return <svg className={cls} fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" /></svg>
    if (type === 'pi') return <svg className={cls} fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" /></svg>
    if (type === 'logs') return <svg className={cls} fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" /></svg>
    if (type === 'cloudtrail') return <svg className={cls} fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
    if (type === 'bedrock') return <svg className={cls} fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" /></svg>
    return null
  }

  const services = [
    { id: 'network', color: COLORS.database, label: 'Network & Credentials',
      subtitle: `${nw === 'lattice' ? 'Lattice endpoint' : nw === 'peering' ? 'VPC Peering' : 'Direct'} · ${m.credentials_configured ? 'Credentials configured' : 'No credentials'}` },
    { id: 'cloudwatch', color: '#e47911', label: 'CloudWatch Metrics',
      subtitle: 'CPU, IOPS, connections, storage, network' },
    { id: 'pi', color: '#8b5cf6', label: 'Performance Insights',
      subtitle: m.pi_enabled === 'True' ? `Enabled · ${m.pi_retention || 7}d retention` : 'Disabled' },
    { id: 'logs', color: '#06b6d4', label: 'CloudWatch Logs',
      subtitle: logGroups.length > 0 ? `${logGroups.length} log group${logGroups.length !== 1 ? 's' : ''}` : logExports.length > 0 ? `${logExports.length} export${logExports.length !== 1 ? 's' : ''}` : 'No exports' },
    { id: 'cloudtrail', color: '#22c55e', label: 'CloudTrail',
      subtitle: `RDS API events · ${region}` },
    { id: 'bedrock', color: '#6366f1', label: 'Amazon Bedrock',
      subtitle: 'Claude · Chat, Investigations, Learning' },
  ]

  const details = {
    network: () => (
      <div className="space-y-2 text-[11px]">
        <div className="flex items-center gap-4">
          <div><span className="text-fg-muted">Method</span><p className="text-fg-2 font-medium">{nw === 'lattice' ? 'Lattice endpoint' : nw === 'peering' ? 'VPC Peering' : 'Direct'}</p></div>
          <div><span className="text-fg-muted">Protocol</span><p className="text-fg-2">PostgreSQL (read-only, 30s timeout)</p></div>
          {/* The credential itself (a Secrets Manager secret) is managed in the admin
              console; the operator app only learns whether one is on file. */}
          <div><span className="text-fg-muted">Credentials</span><p className="text-fg-2">{m.credentials_configured ? 'Secrets Manager secret on file' : 'None configured'}</p></div>
        </div>
      </div>
    ),
    cloudwatch: () => (
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-[11px]">
          <span className="text-fg-muted">Namespace: AWS/RDS</span>
          <ExtLink href={`${consoleBase}/cloudwatch/home?region=${region}#metricsV2?graph=~()&namespace=AWS/RDS`}>Open Dashboard</ExtLink>
        </div>
        <div>
          <span className="text-[10px] text-fg-muted block mb-1">Metrics monitored by the agent</span>
          <div className="flex flex-wrap gap-1">
            {['CPUUtilization', 'DatabaseConnections', 'FreeableMemory', 'ReadIOPS', 'WriteIOPS', 'ReadLatency', 'WriteLatency', 'FreeStorageSpace', 'NetworkReceiveThroughput', 'NetworkTransmitThroughput'].map(metric => (
              <span key={metric} className="px-1.5 py-0.5 text-[10px] rounded bg-surface-2 text-fg-3 border border-line-2">{metric}</span>
            ))}
          </div>
        </div>
        {alarmCount > 0 && <div className="text-[11px]"><span className="text-fg-muted">Alarms:</span> <span className="text-fg-2">{alarmCount} configured</span></div>}
      </div>
    ),
    pi: () => (
      <div className="space-y-2 text-[11px]">
        {m.pi_enabled === 'True' ? (
          <>
            <div className="flex items-center gap-4">
              <div><span className="text-fg-muted">Status</span><p className="text-ok font-medium">Enabled</p></div>
              <div><span className="text-fg-muted">Retention</span><p className="text-fg-2">{m.pi_retention || 7} days</p></div>
            </div>
            <div>
              <span className="text-[10px] text-fg-muted block mb-1">Agent capabilities when PI is enabled</span>
              <div className="flex flex-wrap gap-1">
                {['DB Load (AAS)', 'Top SQL by load', 'Top wait events', 'Wait event breakdown'].map(cap => (
                  <span key={cap} className="px-1.5 py-0.5 text-[10px] rounded bg-primary-subtle text-primary border border-primary-line">{cap}</span>
                ))}
              </div>
            </div>
            <ExtLink href={`${consoleBase}/rds/home?region=${region}#performance-insights-v20206:/resourceId/${dbId}`}>Open PI Dashboard</ExtLink>
          </>
        ) : (
          <div>
            <p className="text-warn font-medium">Disabled</p>
            <p className="text-[10px] text-fg-muted mt-1">DB load, top SQL, and wait event analysis are unavailable. Enable Performance Insights in the RDS console to unlock these capabilities.</p>
            <ExtLink href={`${consoleBase}/rds/home?region=${region}#database:id=${dbId};is-cluster=true`}>Open RDS Console</ExtLink>
          </div>
        )}
      </div>
    ),
    logs: () => (
      <div className="space-y-2 text-[11px]">
        {logGroups.length > 0 ? (
          <div>
            <span className="text-[10px] text-fg-muted block mb-1">Log Groups</span>
            <div className="flex flex-col gap-1">
              {logGroups.map(lg => (
                <ExtLink key={lg} href={`${consoleBase}/cloudwatch/home?region=${region}#logsV2:log-groups/log-group/${encodeURIComponent(lg)}`}>{lg}</ExtLink>
              ))}
            </div>
          </div>
        ) : logExports.length > 0 ? (
          <div>
            <span className="text-[10px] text-fg-muted block mb-1">Configured Exports</span>
            <div className="flex flex-wrap gap-1">
              {logExports.map(l => (
                <span key={l} className="px-1.5 py-0.5 text-[10px] rounded bg-surface-2 text-fg-3 border border-line-2">{l}</span>
              ))}
            </div>
          </div>
        ) : (
          <div>
            <p className="text-warn font-medium">No log exports configured</p>
            <p className="text-[10px] text-fg-muted mt-1">Error logs and log queries are unavailable. Enable log exports in the RDS console.</p>
            <ExtLink href={`${consoleBase}/rds/home?region=${region}#database:id=${dbId}`}>Open RDS Console</ExtLink>
          </div>
        )}
        <div>
          <span className="text-[10px] text-fg-muted block mb-1">Agent capabilities</span>
          <div className="flex flex-wrap gap-1">
            {['Error log search', 'Slow query logs', 'Logs Insights queries'].map(cap => (
              <span key={cap} className="px-1.5 py-0.5 text-[10px] rounded bg-cat-cyan-bg text-cat-cyan border border-cat-cyan-border">{cap}</span>
            ))}
          </div>
        </div>
      </div>
    ),
    cloudtrail: () => (
      <div className="space-y-2 text-[11px]">
        <div className="flex items-center gap-4">
          <div><span className="text-fg-muted">Source</span><p className="text-fg-2">rds.amazonaws.com</p></div>
          <div><span className="text-fg-muted">Region</span><p className="text-fg-2">{region}</p></div>
        </div>
        <div>
          <span className="text-[10px] text-fg-muted block mb-1">Events tracked by the agent</span>
          <div className="flex flex-wrap gap-1">
            {['Parameter changes', 'Reboots', 'Failovers', 'Scaling events', 'Snapshot operations', 'Security group changes'].map(ev => (
              <span key={ev} className="px-1.5 py-0.5 text-[10px] rounded bg-ok-bg text-ok border border-ok-border">{ev}</span>
            ))}
          </div>
        </div>
        <ExtLink href={`${consoleBase}/cloudtrailv2/home?region=${region}#/events?EventSource=rds.amazonaws.com`}>View CloudTrail Events</ExtLink>
      </div>
    ),
    bedrock: () => (
      <div className="space-y-2 text-[11px]">
        <div className="flex items-center gap-4">
          <div><span className="text-fg-muted">Model</span><p className="text-fg-2">{modelName}</p></div>
          <div><span className="text-fg-muted">Provider</span><p className="text-fg-2">Amazon Bedrock</p></div>
        </div>
        <div>
          <span className="text-[10px] text-fg-muted block mb-1">Used by</span>
          <div className="flex flex-wrap gap-1">
            {['Chat Orchestrator', 'Investigation Agent', 'Learning Agent', 'Research Agent', 'Observability Agent', 'PostgreSQL Agent'].map(a => (
              <span key={a} className="px-1.5 py-0.5 text-[10px] rounded bg-primary-subtle text-primary border border-primary-line">{a}</span>
            ))}
          </div>
        </div>
      </div>
    ),
  }

  return (
    <div className="mt-3 bg-surface rounded-xl border border-line overflow-hidden">
      <div className="px-5 py-3 border-b border-line-2">
        <div className="flex items-center gap-2">
          <span className="w-5 h-5 rounded bg-primary-subtle text-primary flex items-center justify-center text-[10px]">🤖</span>
          <span className="text-xs font-bold text-fg-2">Agent Connectivity & Resources</span>
          <span className="text-[10px] text-fg-muted">Click a service to see details</span>
        </div>
      </div>

      {/* Reports & Recommendations summary */}
      <div className="px-5 py-2 border-b border-line-2 flex items-center gap-5 text-[10px]">
        <span className="text-fg-3">📄 {m.report_count || 0} scan reports{m.last_scan_at ? ` · last ${m.last_scan_at.split('T')[0]}` : ''}</span>
        <span className={m.rec_active > 0 ? 'text-warn' : 'text-fg-3'}>💡 {m.rec_active > 0 ? `${m.rec_active} active recommendations` : `${m.rec_total || 0} recommendations`}</span>
      </div>

      {/* Agents */}
      <div className="px-5 pt-4 pb-2">
        <div className="text-[10px] font-bold text-fg-muted uppercase tracking-wider mb-2">Agents that analyze this database</div>
        <div className="flex flex-wrap gap-2">
          {[
            { label: 'Orchestrator', desc: toolCount('orchestrator') ? `Chat routing · ${toolCount('orchestrator')} tools` : 'Chat routing', color: '#0891b2', icon: <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" /></svg> },
            { label: 'Investigation', desc: toolCount('investigation') ? `Root cause analysis · ${toolCount('investigation')} tools` : 'Root cause analysis', color: '#0891b2', icon: <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" /></svg> },
            { label: 'Learning', desc: 'Trend recommendations', color: '#0891b2', icon: <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" /></svg> },
            { label: 'Observability', desc: toolCount('observability') ? `${toolCount('observability')} monitoring tools` : 'CloudWatch, PI, CloudTrail', color: '#06b6d4', icon: <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" /></svg> },
            { label: 'PostgreSQL', desc: toolCount('postgres') ? `${toolCount('postgres')} analysis tools` : 'Analysis tools', color: '#06b6d4', icon: <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375" /></svg> },
            { label: 'Research', desc: toolCount('docs') ? `Doc verification · ${toolCount('docs')} tools` : 'Doc verification', color: '#06b6d4', icon: <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418" /></svg> },
          ].map(a => (
            <div key={a.label} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg border border-line bg-surface">
              <div className="w-5 h-5 rounded-md flex items-center justify-center flex-shrink-0" style={{ backgroundColor: a.color }}>{a.icon}</div>
              <div>
                <div className="text-[10px] font-semibold text-fg leading-tight">{a.label}</div>
                <div className="text-[10px] text-fg-3">{a.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* AWS Services */}
      <div className="px-5 pb-4 pt-2">
        <div className="text-[10px] font-bold text-fg-muted uppercase tracking-wider mb-2">Connected AWS Services</div>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {services.map(svc => {
            const isActive = expanded === svc.id
            return (
              <button key={svc.id} onClick={() => setExpanded(isActive ? null : svc.id)}
                className={`relative flex flex-col items-center gap-2 rounded-xl border-2 px-4 py-4 text-center transition-all hover:shadow-lg hover:-translate-y-0.5 ${
                  isActive
                    ? 'border-primary bg-primary-subtle shadow-md'
                    : 'border-line hover:border-primary-line bg-surface'
                }`}>
                <div className="w-10 h-10 rounded-xl flex items-center justify-center shadow-sm" style={{ backgroundColor: svc.color }}>
                  <SvcIcon type={svc.id} />
                </div>
                <div>
                  <div className="text-[11px] font-bold text-fg leading-tight">{svc.label}</div>
                  <div className="text-[10px] text-fg-3 mt-0.5">{svc.subtitle}</div>
                </div>
                {isActive && (
                  <div className="absolute -bottom-1.5 left-1/2 -translate-x-1/2 w-3 h-3 rotate-45 border-b-2 border-r-2 border-primary bg-surface-2" />
                )}
              </button>
            )
          })}
        </div>

        {/* Expanded detail */}
        {expanded && details[expanded] && (
          <div className="mt-4 p-4 rounded-xl border border-line bg-surface-2">
            {details[expanded]()}
          </div>
        )}
      </div>
    </div>
  )
}

function RegionDrillDown({ regionName, accountId, allNodes, allEdges, onDrillDown, onBack }) {
  const vpcs = allNodes.filter(n => n.type === 'vpc' && n.region === regionName && n.account_id === accountId)
  const dbs = allNodes.filter(n => n.type === 'database' && n.region === regionName && n.account_id === accountId)
  return (
    <div>
      <BackButton onClick={onBack} />
      <SectionHeader icon={Icons.globe} color={COLORS.region}
        title={regionName} subtitle={`Account ${accountId} · ${vpcs.length} VPCs · ${dbs.length} databases`} />
      <div className="space-y-4 mt-2">
        {vpcs.map(vpc => {
          const vpcDbs = dbs.filter(d => d.metadata?.vpc_id === vpc.metadata?.vpc_id)
          const netEdge = allEdges.find(e => (e.type === 'peering' || e.type === 'lattice' || e.type === 'same_vpc') && e.target === vpc.id)
          return (
            <div key={vpc.id} className="border border-line rounded-xl p-4 bg-surface-2">
              <div className="flex items-center gap-2 mb-3">
                <Card icon={Icons.cloud} color={COLORS.vpc} title={vpc.metadata?.vpc_id}
                  subtitle={vpc.metadata?.cidr || 'VPC'} onClick={() => onDrillDown(vpc)} size="sm"
                  badge={netEdge && <span className="px-2 py-0.5 rounded-full text-[10px] font-medium flex-shrink-0"
                    style={{ backgroundColor: `${EDGE_COLORS[netEdge.type]}15`, color: EDGE_COLORS[netEdge.type], border: `1px solid ${EDGE_COLORS[netEdge.type]}30` }}>
                    {netEdge.type === 'peering' ? 'Peering' : netEdge.type === 'same_vpc' ? 'Same VPC' : 'Lattice endpoint'}</span>} />
              </div>
              <div className="flex flex-wrap gap-2 ml-9">
                {vpcDbs.map(db => (
                  <Card key={db.id} icon={Icons.database} color={COLORS.database} title={db.label}
                    subtitle={db.engine} onClick={() => onDrillDown(db)} size="sm" health={db.health} />
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function AccountDrillDown({ node, allNodes, allEdges, onDrillDown, onBack }) {
  const accountId = node.metadata?.account_id || ''
  const dbs = allNodes.filter(n => n.type === 'database' && n.account_id === accountId)
  const regions = [...new Set(dbs.map(d => d.region).filter(Boolean))]
  return (
    <div>
      <BackButton onClick={onBack} />
      <SectionHeader icon={Icons.building} color={COLORS.account}
        title={`Account ${accountId}`} subtitle={`${regions.length} regions · ${dbs.length} databases`} />
      <div className="space-y-3 mt-2">
        {regions.map(r => {
          const rDbs = dbs.filter(d => d.region === r)
          const rVpcs = allNodes.filter(n => n.type === 'vpc' && n.region === r && n.account_id === accountId)
          const health = { ok: rDbs.filter(d => d.health === 'ok').length, warning: rDbs.filter(d => d.health === 'warning').length, critical: rDbs.filter(d => d.health === 'critical').length }
          const healthSummary = [health.critical && `${health.critical} critical`, health.warning && `${health.warning} warning`, health.ok && `${health.ok} healthy`].filter(Boolean).join(' · ')
          return (
            <Card key={r} icon={Icons.globe} color={COLORS.region} title={r}
              subtitle={`${rVpcs.length} VPCs · ${rDbs.length} databases`}
              desc={healthSummary || undefined}
              onClick={() => onDrillDown({ _type: 'region', region: r, account_id: accountId })} />
          )
        })}
      </div>
    </div>
  )
}

function VpcDrillDown({ node, allNodes, allEdges, onDrillDown, onBack }) {
  const m = node.metadata || {}
  const dbs = allNodes.filter(n => n.type === 'database' && n.metadata?.vpc_id === m.vpc_id)
  const netEdge = allEdges.find(e => (e.type === 'peering' || e.type === 'lattice' || e.type === 'same_vpc') && e.target === node.id)
  return (
    <div>
      <BackButton onClick={onBack} />
      <div className="flex items-center gap-3 mb-6">
        <IconBox icon={Icons.cloud} color={COLORS.vpc} size="lg" />
        <div>
          <h2 className="text-base font-bold text-fg">{m.vpc_id}</h2>
          <p className="text-xs text-fg-3">VPC {m.cidr ? `· ${m.cidr}` : ''} · {node.region} · {dbs.length} databases</p>
        </div>
        {netEdge && <span className="px-3 py-1 rounded-full text-[10px] font-medium"
          style={{ backgroundColor: `${EDGE_COLORS[netEdge.type]}15`, color: EDGE_COLORS[netEdge.type], border: `1px solid ${EDGE_COLORS[netEdge.type]}30` }}>
          {netEdge.type === 'peering' ? 'VPC Peering' : netEdge.type === 'same_vpc' ? 'Same VPC' : 'Lattice endpoint'}</span>}
      </div>
      <div className="flex flex-wrap gap-3">
        {dbs.map(db => (
          <Card key={db.id} icon={Icons.database} color={COLORS.database} title={db.label}
            subtitle={db.engine} onClick={() => onDrillDown(db)} health={db.health} />
        ))}
        {dbs.length === 0 && <p className="text-sm text-fg-muted">No databases in this VPC</p>}
      </div>
    </div>
  )
}

// ── Main component ──
export default function Topology({ api, spaceId }) {
  const [topology, setTopology] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [drillStack, setDrillStack] = useState([])

  const fetchTopology = async () => {
    if (!spaceId) return
    try { setLoading(true); const d = await api.get(`/api/spaces/${spaceId}/topology`); setTopology(d); setError(null) }
    catch (e) { setError(e.detail || 'Failed to load topology') }
    finally { setLoading(false) }
  }
  // No "Refresh metadata" here: re-collecting metadata is a manage-level action, and the
  // operator app is operators-only (use, never manage). Such a button would only ever 403
  // for operators, so metadata refresh lives in the admin console; the map loads on mount.
  useEffect(() => { fetchTopology() }, [spaceId])

  const allNodes = topology?.nodes || []
  const allEdges = topology?.edges || []
  const stats = topology?.stats || {}
  const agents = useMemo(() => allNodes.filter(n => n.type === 'sub_agent'), [allNodes])
  const accounts = useMemo(() => allNodes.filter(n => n.type === 'account'), [allNodes])
  const coverage = topology?.coverage || null

  const drillInto = (node) => setDrillStack(prev => [...prev, node])
  const drillBack = () => setDrillStack(prev => prev.slice(0, -1))
  const current = drillStack[drillStack.length - 1] || null

  if (loading) return <div className="flex items-center justify-center py-20"><div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" /></div>
  if (error) return <div className="text-center py-20"><p className="text-sm text-err mb-2">{error}</p><button onClick={fetchTopology} className="text-sm text-primary hover:underline">Retry</button></div>

  // Drill-down view
  if (current) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold text-fg">Topology</h2>
        </div>
        <div className="bg-surface rounded-xl border border-line p-6 min-h-[400px]">
          {current.type === 'sub_agent' && <AgentDrillDown node={current} allNodes={allNodes} allEdges={allEdges} onDrillDown={drillInto} onBack={drillBack} />}
          {current.type === 'database' && <DatabaseDrillDown node={current} onBack={drillBack} allNodes={allNodes} />}
          {current.type === 'vpc' && <VpcDrillDown node={current} allNodes={allNodes} allEdges={allEdges} onDrillDown={drillInto} onBack={drillBack} />}
          {current.type === 'account' && <AccountDrillDown node={current} allNodes={allNodes} allEdges={allEdges} onDrillDown={drillInto} onBack={drillBack} />}
          {current._type === 'region' && <RegionDrillDown regionName={current.region} accountId={current.account_id} allNodes={allNodes} allEdges={allEdges} onDrillDown={drillInto} onBack={drillBack} />}
          {current.type === 'aws_service' && (
            <div><BackButton onClick={drillBack} />
              <SectionHeader icon={Icons.service} color={COLORS.aws_service} title={current.label} subtitle="AWS Service" />
              {current.metadata?.description && <p className="text-sm text-fg-2 ml-[52px]">{current.metadata.description}</p>}
            </div>
          )}
        </div>
      </div>
    )
  }

  // Overview
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-fg">Topology</h2>
          <p className="text-sm text-fg-3">
            {stats.databases || 0} databases · {stats.vpcs || 0} VPCs · {stats.accounts || 0} accounts · {stats.regions || 0} regions
            {coverage && (coverage.blocked || coverage.pending) ? <span className="text-warn-text"> · {[coverage.blocked && `${coverage.blocked} blocked`, coverage.pending && `${coverage.pending} pending`].filter(Boolean).join(', ')}</span> : null}
          </p>
        </div>
      </div>

      {topology?.generated_at && <div className="text-xs text-fg-3">Last generated {new Date(topology.generated_at).toLocaleString()}</div>}

      {/* Network map */}
      {/* Keyed on the generation time: a newly generated map mounts fresh. */}
      <TopologyMap key={topology?.generated_at || 'none'} nodes={allNodes} edges={allEdges} coverage={coverage} warnings={topology?.warnings || []}
        onDrillDown={drillInto} exportName={`topology-${spaceId || 'space'}`} />

      {/* Agent Platform */}
      <div className="bg-surface rounded-xl border border-line p-6">
        <SectionHeader icon={Icons.database} color="#ff9900"
          title={APP_NAME} subtitle="Multi-agent platform with supervisor and domain-specific agents." />

        {/* Supervisor Agents */}
        <div className="mb-5">
          <div className="text-[10px] font-bold text-fg-muted uppercase tracking-wider mb-2.5">Supervisor Agents</div>
          <p className="text-[11px] text-fg-3 mb-3">Entry points that coordinate investigations and chat. They delegate to domain agents.</p>
          <div className="flex flex-wrap gap-3">
            {agents.filter(a => a.agent_type === 'orchestrator' || a.agent_type === 'investigation').map(a => (
              <Card key={a.id} icon={AGENT_ICONS[a.agent_type] || Icons.orchestrator} color="#0891b2"
                title={a.label} subtitle={`${(a.tools || []).length} tools`}
                desc={a.metadata?.description?.slice(0, 70)} onClick={() => drillInto(a)} />
            ))}
          </div>
        </div>

        {/* Domain Agents */}
        <div>
          <div className="text-[10px] font-bold text-fg-muted uppercase tracking-wider mb-2.5">Domain Agents</div>
          <p className="text-[11px] text-fg-3 mb-3">Specialists that perform the actual analysis. Shared by both supervisor agents.</p>
          <div className="flex flex-wrap gap-3">
            {agents.filter(a => a.agent_type !== 'orchestrator' && a.agent_type !== 'investigation').map(a => (
              <Card key={a.id} icon={AGENT_ICONS[a.agent_type] || Icons.orchestrator} color={COLORS.sub_agent}
                title={a.label} subtitle={`${(a.tools || []).length} tools`}
                desc={a.metadata?.description?.slice(0, 70)} onClick={() => drillInto(a)} />
            ))}
          </div>
        </div>
      </div>

      {/* Infrastructure */}
      <div className="bg-surface rounded-xl border border-line p-6">
        <SectionHeader icon={Icons.building} color={COLORS.account}
          title="Infrastructure" subtitle="AWS accounts, regions, VPCs, and databases. Click to drill down." />
        <div className="space-y-5">
          {accounts.map(account => {
            const accountId = account.metadata?.account_id || ''
            const accountDbs = allNodes.filter(n => n.type === 'database' && n.account_id === accountId)
            const regions = [...new Set(accountDbs.map(d => d.region).filter(Boolean))]
            return (
              <div key={account.id} className="border border-line rounded-xl p-5 bg-surface-2">
                <div className="mb-4">
                  <Card icon={Icons.building} color={COLORS.account} title={`Account ${accountId}`}
                    subtitle={`${regions.length} regions · ${accountDbs.length} databases`} onClick={() => drillInto(account)} />
                </div>
                <div className="ml-5 space-y-4">
                  {regions.map(r => {
                    const rDbs = accountDbs.filter(d => d.region === r)
                    const rVpcs = allNodes.filter(n => n.type === 'vpc' && n.region === r && n.account_id === accountId)
                    const health = { ok: rDbs.filter(d => d.health === 'ok').length, w: rDbs.filter(d => d.health === 'warning').length, c: rDbs.filter(d => d.health === 'critical').length }
                    return (
                      <div key={r} className="border border-line rounded-lg p-4 bg-surface-2">
                        <div className="flex items-center gap-2 mb-3">
                          <Card icon={Icons.globe} color={COLORS.region} title={r}
                            subtitle={`${rVpcs.length} VPCs · ${rDbs.length} DBs`}
                            desc={[health.c && `${health.c} critical`, health.w && `${health.w} warning`, health.ok && `${health.ok} healthy`].filter(Boolean).join(' · ') || undefined}
                            onClick={() => drillInto({ _type: 'region', region: r, account_id: accountId })} size="sm" />
                        </div>
                        <div className="flex flex-wrap gap-2 ml-9">
                          {rDbs.map(db => (
                            <Card key={db.id} icon={Icons.database} color={COLORS.database} title={db.label}
                              subtitle={db.engine} onClick={() => drillInto(db)} size="sm" health={db.health} />
                          ))}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-5 text-[10px] text-fg-3">
        {[['database', 'Database', COLORS.database], ['sub_agent', 'AI Agent', COLORS.sub_agent], ['account', 'Account', COLORS.account], ['region', 'Region', COLORS.region], ['vpc', 'VPC', COLORS.vpc]].map(([, label, color]) => (
          <div key={label} className="flex items-center gap-1.5"><span className="w-3 h-3 rounded" style={{ backgroundColor: color }} />{label}</div>
        ))}
        <span className="text-fg-muted">|</span>
        <div className="flex items-center gap-1.5"><span className="w-4 h-0.5 rounded" style={{ backgroundColor: EDGE_COLORS.peering }} />VPC Peering</div>
        <div className="flex items-center gap-1.5"><span className="w-4 h-0.5 rounded" style={{ backgroundColor: EDGE_COLORS.lattice }} />Lattice endpoint</div>
      </div>
    </div>
  )
}
