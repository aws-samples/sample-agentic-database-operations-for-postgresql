// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Network map for a space, drawn from the /topology graph: the agent's VPC on the
// left, accounts › regions › VPCs › databases on the right, and the connection each
// VPC has back to the agent (peering, Lattice resource endpoint, same VPC) as a line coloured by
// its recorded status. Database boxes carry the health score as a dot and how the
// agent reaches them as a left accent bar. Plain SVG — no graph library — so it
// paints with the theme tokens and exports as a file.
//
// Kept identical between frontend/ and admin-console/ (copy, like the token layer).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, ChevronDown, ChevronRight, Download, Maximize2, Search, X, ZoomIn, ZoomOut,
} from 'lucide-react'
import { COLOR, HEALTH_COLORS } from '../theme'

// ── geometry ──────────────────────────────────────────────────────────────────
const DB_W = 204, DB_H = 56, DB_GAP = 8
const PAD = 12, GROUP_GAP = 14, ACCOUNT_GAP = 24
const VPC_HEADER = 26, REGION_HEADER = 26, ACCOUNT_HEADER = 30
const AGENT_W = 200, AGENT_H = 92, LANE = 160
const LEGEND_BAND = 44   // px kept clear at the bottom of the canvas for the legend

const REACH_COLORS = { reachable: COLOR.ok, pending: COLOR.warn, blocked: COLOR.err, unknown: COLOR.muted }
const STATUS_COLORS = { active: COLOR.ok, creating: COLOR.warn, failed: COLOR.err, orphaned: COLOR.err, unknown: COLOR.muted }
const METHOD_LABEL = { peering: 'VPC peering', lattice: 'Lattice endpoint', same_vpc: 'Same VPC', unknown: 'Unknown' }
const CONNECTION_TYPES = new Set(['peering', 'lattice', 'same_vpc'])

const shortEngine = (e = '') => {
  if (e.startsWith('aurora-postgres')) return 'Aurora PostgreSQL'
  if (e.startsWith('aurora-mysql')) return 'Aurora MySQL'
  if (e.includes('postgres')) return 'RDS PostgreSQL'
  if (e.includes('mysql')) return 'RDS MySQL'
  return e || 'database'
}
const truncate = (s = '', max) => (s.length > max ? s.slice(0, Math.max(0, max - 1)) + '…' : s)
const timeAgo = (iso) => {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  if (!Number.isFinite(ms)) return ''
  const m = Math.round(ms / 60000)
  if (m < 2) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h} h ago`
  return `${Math.round(h / 24)} d ago`
}

function chunk(arr, size) {
  const out = []
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size))
  return out
}

// Absolute-coordinate layout: accounts stacked, regions stacked inside, VPCs
// stacked inside a region (one column, so every VPC keeps a clear left edge for
// its connection line), databases stacked inside a VPC. Databases with no VPC
// recorded land in a "No VPC recorded" pseudo-group so they are never hidden.
export function buildLayout(nodes, edges, { maxVpcCols = 1 } = {}) {
  const dbs = nodes.filter(n => n.type === 'database')
  const vpcNodes = {}
  for (const n of nodes) if (n.type === 'vpc') vpcNodes[n.metadata?.vpc_id || ''] = n
  const agentNode = nodes.find(n => n.type === 'agent_vpc') || null
  const connByVpc = {}
  for (const e of edges) {
    if (e.source === 'agent-vpc' && CONNECTION_TYPES.has(e.type) && e.target.startsWith('vpc-')) {
      connByVpc[e.target.slice(4)] = e
    }
  }

  const tree = {}
  for (const db of dbs) {
    const acc = db.account_id || 'unknown', reg = db.region || 'unknown', vpc = db.metadata?.vpc_id || ''
    if (!tree[acc]) tree[acc] = {}
    if (!tree[acc][reg]) tree[acc][reg] = {}
    if (!tree[acc][reg][vpc]) tree[acc][reg][vpc] = []
    tree[acc][reg][vpc].push(db)
  }

  const accounts = Object.keys(tree).sort().map(accId => {
    const regions = Object.keys(tree[accId]).sort().map(regId => {
      const vpcs = Object.keys(tree[accId][regId]).sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b))).map(vpcId => {
        const list = tree[accId][regId][vpcId].slice().sort((a, b) => (a.label || '').localeCompare(b.label || ''))
        const h = VPC_HEADER + PAD + list.length * DB_H + Math.max(0, list.length - 1) * DB_GAP + PAD
        return { id: vpcId ? `vpc-${vpcId}` : `novpc-${accId}-${regId}`, vpcId, node: vpcNodes[vpcId] || null,
          conn: connByVpc[vpcId] || null, w: DB_W + 2 * PAD, h, dbs: list.map(n => ({ node: n })) }
      })
      const rows = chunk(vpcs, maxVpcCols)
      const rowH = rows.map(r => Math.max(...r.map(v => v.h)))
      const rowW = rows.map(r => r.reduce((s, v) => s + v.w, 0) + (r.length - 1) * GROUP_GAP)
      const w = Math.max(...rowW) + 2 * PAD
      const h = REGION_HEADER + PAD + rowH.reduce((a, b) => a + b, 0) + (rows.length - 1) * GROUP_GAP + PAD
      return { id: `region-${regId}-${accId}`, region: regId, accountId: accId, vpcs, rows, rowH, w, h }
    })
    const innerW = Math.max(...regions.map(r => r.w))
    const h = ACCOUNT_HEADER + PAD + regions.reduce((s, r) => s + r.h, 0) + (regions.length - 1) * GROUP_GAP + PAD
    return { id: `account-${accId}`, accountId: accId, regions, w: innerW + 2 * PAD, h }
  })

  const accountW = accounts.length ? Math.max(...accounts.map(a => a.w)) : 0
  const stackH = accounts.reduce((s, a) => s + a.h, 0) + Math.max(0, accounts.length - 1) * ACCOUNT_GAP
  const height = Math.max(stackH, AGENT_H)
  const gx = AGENT_W + LANE
  let y = (height - stackH) / 2
  for (const acc of accounts) {
    acc.x = gx; acc.y = y; acc.w = accountW
    let ry = acc.y + ACCOUNT_HEADER + PAD
    for (const reg of acc.regions) {
      reg.x = acc.x + PAD; reg.y = ry; reg.w = accountW - 2 * PAD
      let vy = reg.y + REGION_HEADER + PAD
      reg.rows.forEach((row, i) => {
        let vx = reg.x + PAD
        for (const vpc of row) {
          vpc.x = vx; vpc.y = vy
          let dy = vpc.y + VPC_HEADER + PAD
          for (const db of vpc.dbs) { db.x = vpc.x + PAD; db.y = dy; db.w = DB_W; db.h = DB_H; dy += DB_H + DB_GAP }
          vx += vpc.w + GROUP_GAP
        }
        vy += reg.rowH[i] + GROUP_GAP
      })
      ry += reg.h + GROUP_GAP
    }
    y += acc.h + ACCOUNT_GAP
  }

  const agent = agentNode ? { node: agentNode, x: 0, y: (height - AGENT_H) / 2, w: AGENT_W, h: AGENT_H } : null
  const vpcBoxes = accounts.flatMap(a => a.regions.flatMap(r => r.vpcs))
  return { width: gx + accountW, height, agent, accounts, vpcBoxes, dbCount: dbs.length }
}

// ── small pieces ──────────────────────────────────────────────────────────────
const Row = ({ label, value, mono, wrap }) => (
  <div className="flex items-start justify-between gap-3 py-1 text-[11px]">
    <span className="text-fg-muted flex-shrink-0">{label}</span>
    <span className={`text-fg-2 text-right ${mono ? 'font-mono text-[10px]' : ''} ${wrap ? 'break-all' : 'truncate'}`}
      title={typeof value === 'string' ? value : undefined}>{value || '—'}</span>
  </div>
)
const Pill = ({ color, children }) => (
  <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-medium border"
    style={{ color, borderColor: color, backgroundColor: 'var(--surface-2)' }}>
    <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />{children}
  </span>
)
const Section = ({ title, children }) => (
  <div className="pt-3 mt-3 border-t border-line-2 first:border-0 first:mt-0 first:pt-0">
    <div className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted mb-1">{title}</div>
    {children}
  </div>
)

function Inspector({ node, layout, edges, nodes, onClose, onSelect, onDrillDown }) {
  const m = node.metadata || {}
  const dbNodes = nodes.filter(n => n.type === 'database')
  let title = node.label, subtitle = node.type, body = null

  if (node.type === 'database') {
    subtitle = [shortEngine(node.engine), m.engine_version && `v${m.engine_version}`, m.instance_class].filter(Boolean).join(' · ')
    const members = m.cluster_member_roles || []
    body = (
      <>
        <div className="flex flex-wrap gap-1.5 mb-1">
          <Pill color={HEALTH_COLORS[node.health] || HEALTH_COLORS.unknown}>Health: {node.health || 'unknown'}</Pill>
          <Pill color={REACH_COLORS[m.reachability] || REACH_COLORS.unknown}>{m.reachability || 'unknown'} · {METHOD_LABEL[m.reach_method] || m.reach_method || '—'}</Pill>
        </div>
        {m.reach_detail && <p className="text-[11px] text-fg-3 mb-1">{m.reach_detail}</p>}
        <Section title="Connection">
          <Row label="Endpoint" value={m.endpoint} mono wrap />
          {m.reader_endpoint && <Row label="Reader endpoint" value={m.reader_endpoint} mono wrap />}
          <Row label="Port · database" value={`${m.port || 5432} · ${m.database_name || 'postgres'}`} />
          <Row label="Multi-AZ" value={m.multi_az === 'True' ? 'Yes' : m.multi_az ? 'No' : '—'} />
        </Section>
        <Section title="Placement">
          <Row label="Account · region" value={`${node.account_id || '—'} · ${node.region || '—'}`} mono />
          <Row label="VPC" value={m.vpc_id} mono />
          <Row label="Subnets" value={(m.subnet_ids || []).length ? `${m.subnet_ids.length} subnet${m.subnet_ids.length > 1 ? 's' : ''}` : ''} />
          <Row label="Availability zones" value={(m.subnet_azs || []).join(', ')} />
          <Row label="Security groups" value={(m.security_group_ids || []).join(', ')} mono wrap />
        </Section>
        {(m.is_cluster || members.length > 0 || (m.replicas || []).length > 0 || m.replica_of) && (
          <Section title="Cluster & replication">
            {members.map(r => (
              <div key={r.instance_id} className="flex items-center justify-between gap-2 py-0.5 text-[11px]">
                <span className="font-mono text-[10px] text-fg-2 truncate" title={r.endpoint || r.instance_id}>{r.instance_id}</span>
                <span className={`px-1.5 py-px rounded text-[10px] font-medium ${r.is_writer ? 'bg-primary-subtle text-primary' : 'bg-surface-3 text-fg-3'}`}>{r.is_writer ? 'writer' : 'reader'}</span>
              </div>
            ))}
            {m.replica_of && <Row label="Replica of" value={m.replica_of} mono />}
            {(m.replicas || []).length > 0 && <Row label="Replicas" value={m.replicas.join(', ')} mono wrap />}
          </Section>
        )}
        <Section title="Monitoring">
          <Row label="Performance Insights" value={m.pi_enabled === 'True' ? `On · ${m.pi_retention || 7} d` : m.pi_enabled ? 'Off' : '—'} />
          <Row label="CloudWatch alarms" value={m.alarm_count === '' || m.alarm_count == null ? '—'
            : `${m.alarm_count}${m.alarms_in_alarm && m.alarms_in_alarm !== '0' ? ` · ${m.alarms_in_alarm} in ALARM` : ''}`} />
          {(m.alarm_names || []).length > 0 && <p className="text-[10px] text-fg-3 font-mono break-all">{m.alarm_names.join(', ')}</p>}
          <Row label="Log groups" value={m.log_groups ? String(m.log_groups.split(',').filter(Boolean).length) : '—'} />
          <Row label="Reports · open recs" value={`${m.report_count ?? 0} · ${m.rec_active ?? 0}`} />
          <Row label="Last scan" value={m.last_scan_at ? timeAgo(m.last_scan_at) : 'never'} />
          <Row label="Metadata refreshed" value={m.metadata_refreshed_at ? timeAgo(m.metadata_refreshed_at) : 'never'} />
        </Section>
      </>
    )
  } else if (node.type === 'vpc') {
    const box = layout.vpcBoxes.find(v => v.vpcId === m.vpc_id)
    const conn = box?.conn
    const cm = conn?.metadata || {}
    const inside = dbNodes.filter(d => d.metadata?.vpc_id === m.vpc_id)
    subtitle = [m.cidr, node.region, m.is_agent_vpc && 'agent VPC'].filter(Boolean).join(' · ')
    body = (
      <>
        <Section title="Connection to the agent">
          {conn ? (
            <>
              <div className="mb-1"><Pill color={STATUS_COLORS[cm.status] || STATUS_COLORS.unknown}>{METHOD_LABEL[conn.type]} · {cm.status || 'unknown'}</Pill></div>
              {cm.peering_id && <Row label="Peering" value={cm.peering_id} mono />}
              {cm.vpce_id && <Row label="Endpoint" value={cm.vpce_id} mono />}
              {cm.endpoint_service && <Row label="Endpoint service" value={cm.endpoint_service} mono wrap />}
              {cm.vpce_dns && <Row label="Endpoint DNS" value={cm.vpce_dns} mono wrap />}
            </>
          ) : <p className="text-[11px] text-fg-3">No connection recorded from the agent VPC.</p>}
        </Section>
        <Section title="Placement">
          <Row label="Account" value={node.account_id} mono />
          <Row label="Region" value={node.region} />
          <Row label="CIDR" value={m.cidr} mono />
        </Section>
        <Section title={`Databases (${inside.length})`}>
          {inside.map(d => (
            <button key={d.id} onClick={() => onSelect(d)} className="w-full flex items-center justify-between gap-2 py-1 text-[11px] text-left hover:text-primary">
              <span className="truncate text-fg-2">{d.label}</span>
              <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: HEALTH_COLORS[d.health] || HEALTH_COLORS.unknown }} />
            </button>
          ))}
        </Section>
      </>
    )
  } else if (node.type === 'agent_vpc') {
    const conns = edges.filter(e => e.source === 'agent-vpc' && CONNECTION_TYPES.has(e.type))
    const reach = edges.filter(e => e.type === 'reaches')
    const count = (arr, f) => arr.filter(f).length
    subtitle = [m.vpc_id, m.cidr, node.region].filter(Boolean).join(' · ')
    body = (
      <>
        <Section title="Connections">
          <Row label="VPC peerings" value={String(count(conns, e => e.type === 'peering'))} />
          <Row label="Lattice endpoints" value={String(count(conns, e => e.type === 'lattice'))} />
          <Row label="Same VPC" value={String(count(conns, e => e.type === 'same_vpc'))} />
        </Section>
        <Section title="Databases by reachability">
          {['reachable', 'pending', 'blocked', 'unknown'].map(k => (
            <div key={k} className="flex items-center justify-between py-0.5 text-[11px]">
              <Pill color={REACH_COLORS[k]}>{k}</Pill><span className="text-fg-2">{count(reach, e => e.metadata?.reachability === k)}</span>
            </div>
          ))}
        </Section>
      </>
    )
  } else if (node.type === 'account' || node.type === 'region') {
    const inside = node.type === 'account' ? dbNodes.filter(d => d.account_id === m.account_id) : dbNodes.filter(d => d.region === m.region)
    body = (
      <Section title={`Databases (${inside.length})`}>
        {inside.map(d => (
          <button key={d.id} onClick={() => onSelect(d)} className="w-full flex items-center justify-between gap-2 py-1 text-[11px] text-left hover:text-primary">
            <span className="truncate text-fg-2">{d.label}</span>
            <span className="text-fg-muted text-[10px]">{d.region}</span>
          </button>
        ))}
      </Section>
    )
  }

  return (
    <aside className="w-[300px] flex-shrink-0 border-l border-line bg-surface flex flex-col min-h-0" aria-label="Selected node details">
      <div className="flex items-start gap-2 px-4 py-3 border-b border-line-2">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-fg truncate" title={title}>{title}</div>
          <div className="text-[11px] text-fg-3 truncate">{subtitle}</div>
        </div>
        <button onClick={onClose} className="p-1 rounded-md text-fg-3 hover:text-fg hover:bg-surface-3" aria-label="Close details"><X className="w-4 h-4" /></button>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-3">{body}</div>
      {onDrillDown && node.type !== 'region' && (
        <div className="px-4 py-3 border-t border-line-2">
          <button onClick={() => onDrillDown(node)} className="w-full px-3 py-1.5 text-xs font-medium rounded-lg border border-primary text-primary hover:bg-primary-subtle transition-colors">
            Open details
          </button>
        </div>
      )}
    </aside>
  )
}

// ── export helpers ────────────────────────────────────────────────────────────
const SVG_NS = 'http://www.w3.org/2000/svg'
function serializeMap(svgEl, layout) {
  const clone = svgEl.cloneNode(true)
  const src = svgEl.querySelectorAll('*'), dst = clone.querySelectorAll('*')
  const props = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'opacity', 'font-family', 'font-size', 'font-weight', 'color']
  src.forEach((s, i) => {
    const cs = getComputedStyle(s)
    for (const p of props) { const v = cs.getPropertyValue(p); if (v) dst[i].style.setProperty(p, v) }
  })
  const g = clone.querySelector('[data-viewport]')
  if (g) g.setAttribute('transform', 'translate(24 24)')
  const w = Math.ceil(layout.width + 48), h = Math.ceil(layout.height + 48)
  clone.setAttribute('xmlns', SVG_NS)
  clone.setAttribute('width', String(w)); clone.setAttribute('height', String(h))
  clone.setAttribute('viewBox', `0 0 ${w} ${h}`)
  clone.removeAttribute('class'); clone.removeAttribute('style')
  const bg = document.createElementNS(SVG_NS, 'rect')
  bg.setAttribute('width', '100%'); bg.setAttribute('height', '100%')
  bg.setAttribute('fill', getComputedStyle(svgEl).getPropertyValue('--app-bg').trim() || '#ffffff')
  clone.insertBefore(bg, clone.firstChild)
  return { xml: new XMLSerializer().serializeToString(clone), w, h }
}
function download(blob, name) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a'); a.href = url; a.download = name; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function exportSvg(svgEl, layout, name) {
  const { xml } = serializeMap(svgEl, layout)
  download(new Blob([xml], { type: 'image/svg+xml;charset=utf-8' }), `${name}.svg`)
}
function exportPng(svgEl, layout, name) {
  const { xml, w, h } = serializeMap(svgEl, layout)
  const img = new Image()
  img.onload = () => {
    const canvas = document.createElement('canvas')
    canvas.width = w * 2; canvas.height = h * 2
    const ctx = canvas.getContext('2d'); ctx.scale(2, 2); ctx.drawImage(img, 0, 0)
    canvas.toBlob(b => b && download(b, `${name}.png`), 'image/png')
  }
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml)
}

// ── the map ───────────────────────────────────────────────────────────────────
export default function TopologyMap({ nodes = [], edges = [], coverage, warnings = [], onDrillDown, compact = false, height, exportName = 'topology' }) {
  const [query, setQuery] = useState('')
  const [healthFilter, setHealthFilter] = useState('all')
  const [reachFilter, setReachFilter] = useState('all')
  const [selectedId, setSelectedId] = useState(null)
  const [hover, setHover] = useState(null)
  const [showWarnings, setShowWarnings] = useState(false)
  const [view, setView] = useState({ k: 1, x: 0, y: 0 })
  const containerRef = useRef(null), svgRef = useRef(null), dragRef = useRef(null)

  const layout = useMemo(() => buildLayout(nodes, edges), [nodes, edges])
  const selected = useMemo(() => nodes.find(n => n.id === selectedId) || null, [nodes, selectedId])
  // Canvas height follows the diagram (bounded) so a small fleet is not scaled down
  // into a strip and a large one still fits on screen; the legend keeps a band at the bottom.
  const mapHeight = height || Math.min(Math.max(Math.round(layout.height * (compact ? 0.85 : 0.9)) + 80, compact ? 360 : 440), compact ? 620 : 760)

  const matches = useCallback((db) => {
    const m = db.metadata || {}
    if (healthFilter !== 'all' && (db.health || 'unknown') !== healthFilter) return false
    if (reachFilter !== 'all' && (m.reachability || 'unknown') !== reachFilter) return false
    if (!query.trim()) return true
    const q = query.trim().toLowerCase()
    return [db.label, db.engine, m.engine_version, m.vpc_id, db.account_id, db.region, m.endpoint, m.instance_class, ...(m.subnet_azs || [])]
      .some(v => String(v || '').toLowerCase().includes(q))
  }, [query, healthFilter, reachFilter])
  const filtering = query.trim() !== '' || healthFilter !== 'all' || reachFilter !== 'all'
  const matchCount = useMemo(() => nodes.filter(n => n.type === 'database' && matches(n)).length, [nodes, matches])

  // zoom / pan
  // The canvas (and so containerRef and svgRef) exists only once there is a database to
  // draw: the empty state returns early. Effects that need those elements key on this, so
  // they attach when the map first appears in a mount that began empty.
  const hasMap = layout.dbCount > 0
  const fit = useCallback(() => {
    const el = containerRef.current
    if (!el || !layout.width) return
    const cw = el.clientWidth, ch = el.clientHeight - LEGEND_BAND
    // A hidden or not-yet-laid-out container measures 0: fitting to it would park the
    // diagram off-screen. Wait for the ResizeObserver to report a real size.
    if (cw <= 0 || ch <= 0) return
    const k = Math.max(0.2, Math.min((cw - 48) / layout.width, (ch - 32) / layout.height, 1))
    setView({ k, x: (cw - layout.width * k) / 2, y: 16 + (ch - 32 - layout.height * k) / 2 })
  }, [layout])
  useEffect(() => { fit() }, [fit, selectedId])
  useEffect(() => {
    const onResize = () => fit()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [fit])
  // Refit whenever the canvas itself changes size, including from 0 to its real size (a
  // stage or tab shown after the map mounted, a sidebar toggled), which no window resize
  // event reports.
  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let last = ''
    const ro = new ResizeObserver(() => {
      const size = `${el.clientWidth}x${el.clientHeight}`
      if (size === last) return
      last = size
      fit()
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [fit, hasMap])
  useEffect(() => {
    const el = svgRef.current
    if (!el) return
    const onWheel = (e) => {
      e.preventDefault()
      const r = el.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top
      setView(v => {
        const k = Math.max(0.2, Math.min(3, v.k * Math.exp(-e.deltaY * 0.0015)))
        const s = k / v.k
        return { k, x: mx - (mx - v.x) * s, y: my - (my - v.y) * s }
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [hasMap])
  const zoomBy = (f) => setView(v => {
    const el = containerRef.current, cx = (el?.clientWidth || 0) / 2, cy = (el?.clientHeight || 0) / 2
    const k = Math.max(0.2, Math.min(3, v.k * f)), s = k / v.k
    return { k, x: cx - (cx - v.x) * s, y: cy - (cy - v.y) * s }
  })
  // Pointer capture is taken only once a drag is under way: capturing on
  // pointerdown would retarget the pointerup (and so the click) to the <svg>,
  // and node clicks would never reach the node.
  const onPointerDown = (e) => {
    if (e.button !== 0) return
    dragRef.current = { sx: e.clientX, sy: e.clientY, x: view.x, y: view.y, moved: false, id: e.pointerId }
  }
  const onPointerMove = (e) => {
    const d = dragRef.current
    if (!d) return
    const dx = e.clientX - d.sx, dy = e.clientY - d.sy
    if (!d.moved && Math.abs(dx) + Math.abs(dy) > 3) {
      d.moved = true
      try { e.currentTarget.setPointerCapture(d.id) } catch { /* not supported */ }
    }
    if (d.moved) setView(v => ({ ...v, x: d.x + dx, y: d.y + dy }))
  }
  const onPointerUp = (e) => {
    const d = dragRef.current
    dragRef.current = null
    if (d && !d.moved && e.target === svgRef.current) setSelectedId(null)
  }

  const select = (node) => setSelectedId(cur => (cur === node.id ? null : node.id))
  const agentCy = layout.agent ? layout.agent.y + layout.agent.h / 2 : 0
  const cov = coverage || {}

  if (!hasMap) {
    return (
      <div className="bg-surface rounded-xl border border-line p-10 text-center">
        <p className="text-sm text-fg-2">No databases configured yet in this space.</p>
        <p className="text-xs text-fg-muted mt-1">The map appears once a database has been configured and its network connection recorded.</p>
      </div>
    )
  }

  return (
    <div className="bg-surface rounded-xl border border-line overflow-hidden">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-line-2">
        <label className="relative flex-1 min-w-[180px] max-w-xs">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-muted" aria-hidden />
          <input value={query} onChange={e => setQuery(e.target.value)} placeholder="Find database, VPC, AZ, account…"
            className="w-full pl-8 pr-7 py-1.5 text-xs rounded-lg border border-line bg-surface text-fg placeholder:text-fg-muted focus:outline-none focus:border-primary-line" aria-label="Search the map" />
          {query && <button onClick={() => setQuery('')} className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 text-fg-muted hover:text-fg" aria-label="Clear search"><X className="w-3 h-3" /></button>}
        </label>
        <select value={healthFilter} onChange={e => setHealthFilter(e.target.value)} className="text-xs py-1.5 px-2 rounded-lg border border-line bg-surface text-fg-2" aria-label="Filter by health">
          <option value="all">All health</option><option value="critical">Critical</option><option value="warning">Warning</option><option value="ok">Healthy</option><option value="unknown">Unknown health</option>
        </select>
        <select value={reachFilter} onChange={e => setReachFilter(e.target.value)} className="text-xs py-1.5 px-2 rounded-lg border border-line bg-surface text-fg-2" aria-label="Filter by reachability">
          <option value="all">All reachability</option><option value="reachable">Reachable</option><option value="pending">Pending</option><option value="blocked">Blocked</option><option value="unknown">Unknown</option>
        </select>
        {filtering && <span className="text-[11px] text-fg-3">{matchCount} of {layout.dbCount} match</span>}
        <div className="ml-auto flex items-center gap-1">
          <button onClick={() => zoomBy(1.25)} className="p-1.5 rounded-md text-fg-3 hover:text-fg hover:bg-surface-3" aria-label="Zoom in" title="Zoom in"><ZoomIn className="w-4 h-4" /></button>
          <button onClick={() => zoomBy(0.8)} className="p-1.5 rounded-md text-fg-3 hover:text-fg hover:bg-surface-3" aria-label="Zoom out" title="Zoom out"><ZoomOut className="w-4 h-4" /></button>
          <button onClick={fit} className="p-1.5 rounded-md text-fg-3 hover:text-fg hover:bg-surface-3" aria-label="Fit to view" title="Fit to view"><Maximize2 className="w-4 h-4" /></button>
          <span className="w-px h-4 bg-line mx-1" />
          <button onClick={() => svgRef.current && exportSvg(svgRef.current, layout, exportName)} className="inline-flex items-center gap-1 px-2 py-1.5 rounded-md text-[11px] text-fg-3 hover:text-fg hover:bg-surface-3" title="Download as SVG"><Download className="w-3.5 h-3.5" />SVG</button>
          <button onClick={() => svgRef.current && exportPng(svgRef.current, layout, exportName)} className="inline-flex items-center gap-1 px-2 py-1.5 rounded-md text-[11px] text-fg-3 hover:text-fg hover:bg-surface-3" title="Download as PNG"><Download className="w-3.5 h-3.5" />PNG</button>
        </div>
      </div>

      {/* coverage + warnings */}
      {(coverage || warnings.length > 0) && (
        <div className="px-3 py-1.5 border-b border-line-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-fg-3">
          {coverage && (
            <>
              <span><span className="font-medium text-fg-2">{cov.databases ?? 0}</span> databases</span>
              {['reachable', 'pending', 'blocked', 'unknown'].filter(k => cov[k]).map(k => (
                <span key={k} className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: REACH_COLORS[k] }} />{cov[k]} {k}</span>
              ))}
              <span>metadata collected for <span className="font-medium text-fg-2">{cov.with_metadata ?? 0}</span>, refreshed for <span className="font-medium text-fg-2">{cov.refreshed ?? 0}</span></span>
            </>
          )}
          {warnings.length > 0 && (
            <button onClick={() => setShowWarnings(s => !s)} className="ml-auto inline-flex items-center gap-1 text-warn-text hover:underline" aria-expanded={showWarnings}>
              <AlertTriangle className="w-3.5 h-3.5" />{warnings.length} warning{warnings.length > 1 ? 's' : ''}
              {showWarnings ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
          )}
        </div>
      )}
      {showWarnings && warnings.length > 0 && (
        <ul className="px-3 py-2 border-b border-warn-border bg-warn-bg text-[11px] text-warn-text space-y-1 max-h-32 overflow-y-auto">
          {warnings.map((w, i) => (
            <li key={i}>
              {w.node_id ? <button onClick={() => setSelectedId(w.node_id)} className="hover:underline text-left">{w.message}</button> : w.message}
            </li>
          ))}
        </ul>
      )}

      <div className="flex" style={{ height: mapHeight }}>
        {/* canvas */}
        <div ref={containerRef} className="relative flex-1 min-w-0 overflow-hidden select-none" style={{ backgroundColor: 'var(--app-bg)' }}>
          <svg ref={svgRef} className="w-full h-full touch-none" style={{ cursor: dragRef.current ? 'grabbing' : 'grab', fontFamily: 'var(--font-sans)' }}
            onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerLeave={() => setHover(null)}
            role="img" aria-label="Network map of the agent, VPCs and databases">
            <g data-viewport transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
              {/* agent */}
              {layout.agent && (
                <g role="button" tabIndex={0} aria-label="DBOps Agent VPC" style={{ cursor: 'pointer' }}
                  onClick={() => select(layout.agent.node)} onKeyDown={e => e.key === 'Enter' && select(layout.agent.node)}>
                  <rect x={layout.agent.x} y={layout.agent.y} width={layout.agent.w} height={layout.agent.h} rx={12}
                    style={{ fill: 'var(--surface)', stroke: selectedId === 'agent-vpc' ? 'var(--primary)' : 'var(--primary-border)', strokeWidth: selectedId === 'agent-vpc' ? 2 : 1.5 }} />
                  <rect x={layout.agent.x + 12} y={layout.agent.y} width={layout.agent.w - 24} height={4} rx={2} style={{ fill: 'var(--primary)' }} />
                  <text x={layout.agent.x + 14} y={layout.agent.y + 32} style={{ fill: 'var(--text)', fontSize: 13, fontWeight: 700 }}>{layout.agent.node.label || 'DBOps Agent'}</text>
                  <text x={layout.agent.x + 14} y={layout.agent.y + 50} style={{ fill: 'var(--text-2)', fontSize: 10.5, fontFamily: 'var(--font-mono, ui-monospace, monospace)' }}>{layout.agent.node.metadata?.vpc_id}</text>
                  <text x={layout.agent.x + 14} y={layout.agent.y + 66} style={{ fill: 'var(--text-3)', fontSize: 10.5 }}>{[layout.agent.node.metadata?.cidr, layout.agent.node.region].filter(Boolean).join(' · ')}</text>
                  <text x={layout.agent.x + 14} y={layout.agent.y + 82} style={{ fill: 'var(--text-muted)', fontSize: 9.5 }}>{layout.vpcBoxes.filter(v => v.conn).length} connection{layout.vpcBoxes.filter(v => v.conn).length === 1 ? '' : 's'}</text>
                </g>
              )}

              {/* accounts › regions › vpcs › databases */}
              {layout.accounts.map(acc => (
                <g key={acc.id}>
                  <rect x={acc.x} y={acc.y} width={acc.w} height={acc.h} rx={14} style={{ fill: 'var(--surface-2)', stroke: 'var(--border-strong)', strokeWidth: 1 }} />
                  <text x={acc.x + PAD} y={acc.y + 20} style={{ fill: 'var(--text)', fontSize: 11.5, fontWeight: 700, cursor: 'pointer' }}
                    onClick={() => { const n = nodes.find(x => x.id === acc.id); if (n) select(n) }}>Account {acc.accountId}</text>
                  {acc.regions.map(reg => (
                    <g key={reg.id}>
                      <rect x={reg.x} y={reg.y} width={reg.w} height={reg.h} rx={12} style={{ fill: 'var(--surface)', stroke: 'var(--border)', strokeWidth: 1, strokeDasharray: '4 3' }} />
                      <text x={reg.x + PAD} y={reg.y + 18} style={{ fill: 'var(--text-2)', fontSize: 11, fontWeight: 600, cursor: 'pointer' }}
                        onClick={() => { const n = nodes.find(x => x.id === `region-${reg.region}`); if (n) select(n) }}>{reg.region}</text>
                      {reg.vpcs.map(vpc => {
                        const vpcSelected = vpc.node && selectedId === vpc.node.id
                        const cidr = vpc.node?.metadata?.cidr
                        return (
                          <g key={vpc.id}>
                            <rect x={vpc.x} y={vpc.y} width={vpc.w} height={vpc.h} rx={10}
                              style={{ fill: vpc.node?.metadata?.is_agent_vpc ? 'var(--primary-wash)' : 'var(--surface-2)', stroke: vpcSelected ? 'var(--primary)' : 'var(--border)', strokeWidth: vpcSelected ? 2 : 1 }} />
                            <text x={vpc.x + PAD} y={vpc.y + 17} style={{ fill: 'var(--text-2)', fontSize: 10.5, fontWeight: 600, cursor: vpc.node ? 'pointer' : 'default', fontFamily: 'var(--font-mono, ui-monospace, monospace)' }}
                              onClick={() => vpc.node && select(vpc.node)}>
                              {vpc.vpcId ? truncate(vpc.vpcId, cidr ? 14 : 26) : 'No VPC recorded'}{cidr ? `  ${cidr}` : ''}
                            </text>
                            {vpc.dbs.map(({ node: db, x, y, w, h }) => {
                              const m = db.metadata || {}
                              const isSel = selectedId === db.id
                              const dim = filtering && !matches(db)
                              const reach = REACH_COLORS[m.reachability] || REACH_COLORS.unknown
                              const badges = []
                              if (m.is_cluster && (m.cluster_member_roles || []).length) badges.push(`${m.writer_instance ? '1W' : ''}${m.reader_instances?.length ? `+${m.reader_instances.length}R` : ''}` || `${m.cluster_member_roles.length} members`)
                              if (m.alarms_in_alarm && m.alarms_in_alarm !== '0') badges.push(`${m.alarms_in_alarm} alarm`)
                              if (m.rec_active) badges.push(`${m.rec_active} rec`)
                              if (!m.engine_version) badges.push('no metadata')
                              const sub = [shortEngine(db.engine), m.engine_version && `v${m.engine_version}`, m.instance_class].filter(Boolean).join(' · ')
                              return (
                                <g key={db.id} role="button" tabIndex={0} aria-label={`${db.label}, health ${db.health || 'unknown'}, ${m.reachability || 'unknown'}`}
                                  style={{ cursor: 'pointer', opacity: dim ? 0.22 : 1, transition: 'opacity .15s' }}
                                  onClick={() => select(db)} onKeyDown={e => e.key === 'Enter' && select(db)}
                                  onMouseEnter={(e) => { const r = containerRef.current.getBoundingClientRect(); setHover({ node: db, x: e.clientX - r.left, y: e.clientY - r.top }) }}
                                  onMouseMove={(e) => { const r = containerRef.current.getBoundingClientRect(); setHover(hv => hv ? { ...hv, x: e.clientX - r.left, y: e.clientY - r.top } : hv) }}
                                  onMouseLeave={() => setHover(null)}>
                                  <rect x={x} y={y} width={w} height={h} rx={8} style={{ fill: 'var(--surface)', stroke: isSel ? 'var(--primary)' : 'var(--border-strong)', strokeWidth: isSel ? 2 : 1 }} />
                                  <rect x={x + 3} y={y + 8} width={4} height={h - 16} rx={2} style={{ fill: reach }} />
                                  <text x={x + 14} y={y + 20} style={{ fill: 'var(--text)', fontSize: 12, fontWeight: 600 }}>{truncate(db.label || '', 24)}</text>
                                  <circle cx={x + w - 12} cy={y + 14} r={4.5} style={{ fill: HEALTH_COLORS[db.health] || HEALTH_COLORS.unknown, stroke: 'var(--surface)', strokeWidth: 1.5 }} />
                                  <text x={x + 14} y={y + 35} style={{ fill: 'var(--text-3)', fontSize: 10 }}>{truncate(sub, 32)}</text>
                                  <text x={x + 14} y={y + 48} style={{ fill: 'var(--text-muted)', fontSize: 9.5 }}>{truncate(badges.join(' · ') || (m.reachability || 'unknown'), 34)}</text>
                                </g>
                              )
                            })}
                            {vpc.dbs.length === 0 && <text x={vpc.x + PAD} y={vpc.y + VPC_HEADER + PAD + 12} style={{ fill: 'var(--text-muted)', fontSize: 10 }}>No databases</text>}
                          </g>
                        )
                      })}
                    </g>
                  ))}
                </g>
              ))}
              {/* connection edges — drawn last so group fills never cover them; the
                  start points fan out along the agent box in target order */}
              {layout.agent && (() => {
                const connected = layout.vpcBoxes.filter(v => v.conn).sort((a, b) => (a.y + a.h / 2) - (b.y + b.h / 2))
                const n = connected.length
                return connected.map((vpc, i) => {
                  const status = vpc.conn.metadata?.status || 'unknown'
                  const color = STATUS_COLORS[status] || STATUS_COLORS.unknown
                  const x1 = layout.agent.x + layout.agent.w
                  const y1 = n === 1 ? agentCy : layout.agent.y + 18 + i * ((layout.agent.h - 36) / (n - 1))
                  const x2 = vpc.x, y2 = vpc.y + vpc.h / 2
                  const cx1 = x1 + (x2 - x1) * 0.45, cx2 = x2 - (x2 - x1) * 0.45
                  const t = 0.42, u = 1 - t
                  const lx = u * u * u * x1 + 3 * u * u * t * cx1 + 3 * u * t * t * cx2 + t * t * t * x2
                  const ly = u * u * u * y1 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y2
                  const label = METHOD_LABEL[vpc.conn.type] || vpc.conn.type
                  const lw = label.length * 6.2 + 16
                  const dimmed = filtering && !vpc.dbs.some(d => matches(d.node))
                  return (
                    <g key={`edge-${vpc.id}`} style={{ opacity: dimmed ? 0.2 : 1, transition: 'opacity .15s' }}>
                      <path d={`M ${x1} ${y1} C ${cx1} ${y1}, ${cx2} ${y2}, ${x2} ${y2}`} fill="none"
                        style={{ stroke: color, strokeWidth: 1.75, strokeDasharray: status === 'active' ? 'none' : '6 4' }} />
                      <circle cx={x1} cy={y1} r={3} style={{ fill: color }} />
                      <circle cx={x2} cy={y2} r={3.5} style={{ fill: color }} />
                      <g transform={`translate(${lx - lw / 2} ${ly - 10})`}>
                        <rect width={lw} height={20} rx={10} style={{ fill: 'var(--surface)', stroke: color, strokeWidth: 1 }} />
                        <text x={lw / 2} y={13.5} textAnchor="middle" style={{ fill: color, fontSize: 10.5, fontWeight: 600 }}>{label}</text>
                      </g>
                    </g>
                  )
                })
              })()}
            </g>
          </svg>

          {/* hover tooltip */}
          {hover && (
            <div className="pointer-events-none absolute z-10 px-3 py-2 rounded-lg border border-line bg-surface shadow-lg text-[11px] max-w-[260px]"
              style={{ left: Math.min(hover.x + 14, (containerRef.current?.clientWidth || 400) - 270), top: hover.y + 14 }}>
              <div className="font-semibold text-fg truncate">{hover.node.label}</div>
              <div className="text-fg-3">{shortEngine(hover.node.engine)}{hover.node.metadata?.engine_version ? ` v${hover.node.metadata.engine_version}` : ''}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                <Pill color={HEALTH_COLORS[hover.node.health] || HEALTH_COLORS.unknown}>{hover.node.health || 'unknown'}</Pill>
                <Pill color={REACH_COLORS[hover.node.metadata?.reachability] || REACH_COLORS.unknown}>{hover.node.metadata?.reachability || 'unknown'}</Pill>
              </div>
              {hover.node.metadata?.reach_detail && <div className="mt-1 text-fg-muted">{hover.node.metadata.reach_detail}</div>}
            </div>
          )}

          {/* legend */}
          <div className="absolute left-3 bottom-3 flex flex-wrap items-center gap-x-3 gap-y-1 px-2.5 py-1.5 rounded-lg border border-line bg-surface text-[10px] text-fg-3 pointer-events-none">
            <span className="font-medium text-fg-2">Health</span>
            {['ok', 'warning', 'critical', 'unknown'].map(k => <span key={k} className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: HEALTH_COLORS[k] }} />{k}</span>)}
            <span className="w-px h-3 bg-line" />
            <span className="font-medium text-fg-2">Reach</span>
            {['reachable', 'pending', 'blocked', 'unknown'].map(k => <span key={k} className="inline-flex items-center gap-1"><span className="w-1 h-3 rounded-sm" style={{ backgroundColor: REACH_COLORS[k] }} />{k}</span>)}
            <span className="w-px h-3 bg-line" />
            <span className="inline-flex items-center gap-1"><span className="w-4 h-px" style={{ backgroundColor: COLOR.ok }} />active</span>
            <span className="inline-flex items-center gap-1"><span className="w-4 border-t border-dashed" style={{ borderColor: COLOR.warn }} />creating</span>
            <span className="inline-flex items-center gap-1"><span className="w-4 border-t border-dashed" style={{ borderColor: COLOR.err }} />failed</span>
          </div>
        </div>

        {selected && (
          <Inspector node={selected} layout={layout} edges={edges} nodes={nodes}
            onClose={() => setSelectedId(null)} onSelect={(n) => setSelectedId(n.id)} onDrillDown={onDrillDown} />
        )}
      </div>
    </div>
  )
}
