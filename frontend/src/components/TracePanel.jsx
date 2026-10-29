// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Compact markdown styling for reasoning rows — renders the specialist's own
// **bold** / ## headings / tables cleanly instead of showing raw markdown
// symbols. react-markdown escapes HTML (no rehype-raw), so this stays XSS-safe.
const mdReasoning = {
  table: ({ children }) => <div className="overflow-x-auto my-1"><table className="text-[10px] border-collapse">{children}</table></div>,
  th: ({ children }) => <th className="border border-line-strong px-1 py-0.5 text-left font-semibold">{children}</th>,
  td: ({ children }) => <td className="border border-line px-1 py-0.5 align-top">{children}</td>,
  h1: ({ children }) => <div className="font-semibold text-fg-2 mt-1">{children}</div>,
  h2: ({ children }) => <div className="font-semibold text-fg-2 mt-1">{children}</div>,
  h3: ({ children }) => <div className="font-semibold text-fg-2 mt-1">{children}</div>,
  p: ({ children }) => <p className="my-0.5">{children}</p>,
  ul: ({ children }) => <ul className="list-disc ml-4 my-0.5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal ml-4 my-0.5">{children}</ol>,
  li: ({ children }) => <li className="my-0">{children}</li>,
  code: ({ children }) => <code className="bg-surface-3 rounded px-1 text-[10px]">{children}</code>,
  a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer" className="text-primary underline">{children}</a>,
  strong: ({ children }) => <strong className="font-semibold text-fg-2">{children}</strong>,
}

// ── Plain-entry styling (user / skill / result / error / unpaired tool notes) ──
const eventColors = {
  user: 'border-primary-line bg-primary-subtle',
  tool: 'border-info-border bg-info-bg',
  skill: 'border-primary-line bg-primary-subtle',
  error: 'border-err-border bg-err-bg',
  result: 'border-ok-border bg-ok-bg',
}
const eventIcons = { user: '💬', tool: '🔧', skill: '🧠', error: '❌', result: '✅' }

// ── Severity (verdict) handling ───────────────────────────────────────────────
// Facts-only tools return raw JSON; the Python severity layer stamps *_status /
// health / severity fields (e.g. invalid_indexes_status: "WARNING"). We surface
// the worst one as a chip so the trace shows the VERDICT, not just "success".
const SEV_RANK = { OK: 0, INFO: 1, NOTICE: 2, WATCH: 2, WARNING: 3, CRITICAL: 4, EMERGENCY: 5 }
const SEV_CHIP = {
  OK: 'bg-ok-bg-2 text-ok-text',
  INFO: 'bg-surface-3 text-fg-2',
  NOTICE: 'bg-warn-bg-2 text-warn-text',
  WATCH: 'bg-warn-bg-2 text-warn-text',
  WARNING: 'bg-warn-bg-2 text-warn-text',
  CRITICAL: 'bg-err-bg-2 text-err-text',
  EMERGENCY: 'bg-err-bg-2 text-err-text',
}

// Best-effort: pull the worst health/severity verdict out of a tool-result blob.
// Fails soft — non-JSON or truncated content yields null (no chip), never throws.
function extractVerdict(content) {
  if (!content || typeof content !== 'string') return null
  let obj
  try { obj = JSON.parse(content) } catch { return null }
  let best = null, bestRank = -1
  const visit = (v, depth) => {
    if (depth > 6 || v == null) return
    if (Array.isArray(v)) { for (const x of v) visit(x, depth + 1); return }
    if (typeof v === 'object') {
      for (const [k, val] of Object.entries(v)) {
        if (typeof val === 'string') {
          const key = k.toLowerCase()
          if (key.endsWith('_status') || key === 'health' || key === 'severity') {
            const up = val.toUpperCase()
            if (up in SEV_RANK && SEV_RANK[up] > bestRank) { best = up; bestRank = SEV_RANK[up] }
          }
        } else visit(val, depth + 1)
      }
    }
  }
  visit(obj, 0)
  return best
}

// One-line result hint for Summary mode (e.g. "12 tables"). Fails soft to a
// trimmed snippet.
function resultPreview(content) {
  if (!content || typeof content !== 'string') return ''
  let obj
  try { obj = JSON.parse(content) } catch { return content.replace(/\s+/g, ' ').trim().slice(0, 100) }
  if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
    for (const [k, v] of Object.entries(obj)) if (Array.isArray(v)) return `${v.length} ${k}`
  }
  if (Array.isArray(obj)) return `${obj.length} rows`
  return ''
}

// Strip the "↳ Agent: " / "Tool: " prefixes to recover a bare tool name.
const bareName = (label = '') =>
  label.replace(/^↳\s*[^:]*:\s*/, '').replace(/^Tool(?: call)?:\s*/, '').replace(/\s*\(\d+ args?\)$/, '').trim()

// ── Fold a flat event list into display nodes ─────────────────────────────────
// 1) pair each tool call with its result by toolUseId, so out-of-order arrival
//    does not matter even when many calls precede their results;
// 2) lift reasoning bursts and skills into their own nodes;
// 3) nest consecutive sub-agent nodes under an agent group.
function buildNodes(events) {
  const byId = new Map()
  const flat = []
  for (const e of events) {
    if (e.type === 'user') continue // shown as the interaction header
    const id = e.toolUseId || e.detail?.toolUseId
    const toolish = e.type === 'tool' || e.type === 'tool_start' || e.type === 'tool_result'
    if (toolish && id) {
      let node = byId.get(id)
      if (!node) { node = { kind: 'tool', id, time: e.time }; byId.set(id, node); flat.push(node) }
      if (e.agent) node.agent = e.agent
      if (e.type === 'tool' || e.type === 'tool_start') {
        node.name = node.name || e.detail?.name || bareName(e.label)
        if (!node.input && e.detail?.input) node.input = e.detail.input
      } else {
        node.status = e.status || 'success'
        node.content = typeof e.detail === 'string' ? e.detail : (e.detail == null ? '' : JSON.stringify(e.detail))
      }
    } else if (e.type === 'reasoning') {
      flat.push({ kind: 'reasoning', agent: e.agent, text: typeof e.detail === 'string' ? e.detail : '', time: e.time })
    } else if (e.type === 'skill') {
      flat.push({ kind: 'skill', label: e.label, detail: e.detail, time: e.time })
    } else {
      flat.push({ kind: 'plain', entry: e })
    }
  }
  // Nest consecutive sub-agent nodes (those carrying an agent) under one group.
  const nodes = []
  let group = null
  for (const n of flat) {
    const agent = (n.kind === 'tool' || n.kind === 'reasoning') ? n.agent : null
    if (agent) {
      if (!group || group.agent !== agent) { group = { kind: 'group', agent, children: [] }; nodes.push(group) }
      group.children.push(n)
    } else {
      group = null
      nodes.push(n)
    }
  }
  return nodes
}

// Count tool nodes (incl. nested) for a group/interaction header.
function countTools(nodes) {
  let n = 0
  for (const node of nodes) {
    if (node.kind === 'tool') n++
    else if (node.kind === 'group') n += node.children.filter(c => c.kind === 'tool').length
  }
  return n
}

const tsForFile = () => new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
const slugify = (s, n = 40) =>
  ((s || 'trace').replace(/^(User|Investigation):\s*/i, '').replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, n).toLowerCase() || 'trace')

function downloadTrace(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function DownloadIcon({ className }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
    </svg>
  )
}

function Chevron({ open, className = 'w-2.5 h-2.5' }) {
  return (
    <svg className={`${className} text-fg-muted transition-transform ${open ? 'rotate-90' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
    </svg>
  )
}

function VerdictChip({ sev }) {
  if (!sev) return null
  return <span className={`text-[10px] font-bold px-1 rounded ${SEV_CHIP[sev] || SEV_CHIP.INFO}`}>{sev}</span>
}

// ── One paired tool call+result ───────────────────────────────────────────────
function ToolNode({ node, mode }) {
  const [open, setOpen] = useState(false)
  const verdict = node.content ? extractVerdict(node.content) : null
  const argCount = node.input ? Object.keys(node.input).length : 0
  const failed = node.status === 'error'
  const running = !node.status
  const hasDetail = !!(node.input || node.content)

  const statusEl = verdict
    ? <VerdictChip sev={verdict} />
    : running
      ? <span className="text-[10px] text-fg-muted animate-pulse">running…</span>
      : <span className={`text-[10px] ${failed ? 'text-err' : 'text-ok'}`}>{failed ? '✕ error' : '✓'}</span>

  if (mode === 'summary') {
    const preview = node.content ? resultPreview(node.content) : ''
    return (
      <div className="flex items-center gap-1.5 py-0.5 text-[11px]">
        <span className="text-fg-muted">{node.agent ? '·' : '🔧'}</span>
        <span className="font-mono text-fg-2">{node.name || 'tool'}</span>
        {statusEl}
        {preview && <span className="text-[10px] text-fg-muted truncate">— {preview}</span>}
      </div>
    )
  }

  return (
    <div className="border-l-2 border-info-border bg-info-bg rounded-r px-2 py-1 text-[11px]">
      <div className={`flex items-center justify-between gap-2 ${hasDetail ? 'cursor-pointer' : ''}`} onClick={() => hasDetail && setOpen(!open)}>
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[10px]">{node.agent ? '↳' : '🔧'}</span>
          <span className="font-mono font-medium text-fg-2 truncate">{node.name || 'tool'}</span>
          {argCount > 0 && <span className="text-[10px] text-fg-muted flex-shrink-0">({argCount} arg{argCount > 1 ? 's' : ''})</span>}
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {statusEl}
          {hasDetail && <Chevron open={open} />}
        </div>
      </div>
      {open && hasDetail && (
        <div className="mt-1 space-y-1">
          {argCount > 0 && (
            <pre className="text-[10px] text-fg-2 bg-surface-2 rounded p-1.5 overflow-x-auto whitespace-pre-wrap break-all">
              {JSON.stringify(node.input, null, 2)}
            </pre>
          )}
          {node.content && (
            <pre className="text-[10px] text-fg-2 bg-surface-2 rounded p-1.5 overflow-x-auto max-h-[240px] overflow-y-auto whitespace-pre-wrap break-all">
              {node.content.length > 4000 ? node.content.slice(0, 4000) + '\n…(truncated)' : node.content}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

// ── One specialist reasoning burst (the "why" behind a specialist's work) ─────
// Renders the narration as markdown (bold/headings/tables) in a bounded,
// scrollable box — the full text is always present (no preview truncation).
function ReasoningNode({ node }) {
  const text = (node.text || '').trim()
  if (!text) return null
  return (
    <div className="border-l-2 border-primary-line bg-primary-subtle rounded-r px-2 py-1">
      <div className="flex gap-1.5">
        <span className="text-[10px] mt-0.5 flex-shrink-0">💭</span>
        <div className="text-[11px] text-fg-2 max-h-[220px] overflow-y-auto min-w-0 break-words">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdReasoning}>{text}</ReactMarkdown>
        </div>
      </div>
    </div>
  )
}

function SkillNode({ node }) {
  return (
    <div className="border-l-2 border-primary-line bg-primary-subtle rounded-r px-2 py-1 text-[11px]">
      <span className="mr-1">🧠</span>
      <span className="font-medium text-primary">{node.label}</span>
    </div>
  )
}

function PlainNode({ entry }) {
  const [open, setOpen] = useState(false)
  const color = eventColors[entry.type] || 'border-line bg-surface'
  const icon = eventIcons[entry.type] || '•'
  const hasDetail = entry.detail != null && entry.detail !== ''
  return (
    <div className={`border-l-2 ${color} rounded-r px-2 py-1 text-[11px]`}>
      <div className={`flex items-center justify-between ${hasDetail ? 'cursor-pointer' : ''}`} onClick={() => hasDetail && setOpen(!open)}>
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[10px]">{icon}</span>
          <span className="font-medium text-fg-2 truncate">{entry.label}</span>
        </div>
        {hasDetail && <Chevron open={open} />}
      </div>
      {open && hasDetail && (
        <pre className="mt-1 text-[10px] text-fg-2 bg-surface-2 rounded p-2 overflow-x-auto max-h-[300px] overflow-y-auto whitespace-pre-wrap break-all">
          {typeof entry.detail === 'string' ? entry.detail : JSON.stringify(entry.detail, null, 2)}
        </pre>
      )}
    </div>
  )
}

// ── A sub-agent's nested run ──────────────────────────────────────────────────
function AgentGroup({ node, mode }) {
  const [open, setOpen] = useState(true)
  const tools = node.children.filter(c => c.kind === 'tool').length
  return (
    <div className="border border-cat-cyan-border bg-cat-cyan-bg rounded">
      <div className="flex items-center justify-between px-2 py-1 cursor-pointer" onClick={() => setOpen(!open)}>
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[10px]">↳</span>
          <span className="text-[11px] font-semibold text-cat-cyan truncate">{node.agent}</span>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-[10px] text-fg-muted">{tools} tool{tools !== 1 ? 's' : ''}</span>
          <Chevron open={open} />
        </div>
      </div>
      {open && (
        <div className="px-2 pb-1.5 pl-3 space-y-1">
          {node.children.map((c, i) => <NodeView key={i} node={c} mode={mode} />)}
        </div>
      )}
    </div>
  )
}

function NodeView({ node, mode }) {
  if (node.kind === 'group') return <AgentGroup node={node} mode={mode} />
  if (node.kind === 'tool') return <ToolNode node={node} mode={mode} />
  if (node.kind === 'reasoning') return <ReasoningNode node={node} mode={mode} />
  if (node.kind === 'skill') return <SkillNode node={node} />
  return <PlainNode entry={node.entry} />
}

function InteractionGroup({ interaction, defaultExpanded, mode }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const nodes = buildNodes(interaction.events)
  const toolCount = countTools(nodes)
  const skillCount = nodes.filter(n => n.kind === 'skill').length
  const hasError = interaction.events.some(e => e.type === 'error')

  return (
    <div className="border border-line rounded-lg overflow-hidden">
      <button onClick={() => setExpanded(!expanded)}
        className={`w-full px-3 py-1.5 flex items-center justify-between text-left transition-colors ${
          expanded ? 'bg-surface-active' : 'bg-surface-3 hover:bg-surface-active'
        }`}>
        <div className="flex items-center gap-2 min-w-0">
          <Chevron open={expanded} className="w-3 h-3 flex-shrink-0" />
          <span className="text-[11px] font-semibold text-fg truncate">{interaction.label}</span>
          {hasError && <span className="text-[10px] bg-err-bg-2 text-err rounded px-1">error</span>}
        </div>
        <div className="flex items-center gap-3 flex-shrink-0">
          {toolCount > 0 && <span className="text-[10px] text-primary font-medium">{toolCount} tool{toolCount !== 1 ? 's' : ''}</span>}
          {skillCount > 0 && <span className="text-[10px] text-primary font-medium">{skillCount} skill{skillCount > 1 ? 's' : ''}</span>}
          <span className="text-[10px] text-fg-3">{interaction.timestamp}</span>
          <span role="button" tabIndex={0} title="Download this interaction's trace"
            onClick={(e) => { e.stopPropagation(); downloadTrace(interaction, `agent-trace-${slugify(interaction.label)}-${tsForFile()}.json`) }}
            className="text-fg-muted hover:text-primary cursor-pointer">
            <DownloadIcon className="w-3 h-3" />
          </span>
        </div>
      </button>
      {expanded && (
        <div className="px-2 py-1.5 space-y-1 bg-surface">
          {nodes.length === 0
            ? <div className="text-[10px] text-fg-muted px-1 py-1">No steps recorded.</div>
            : nodes.map((n, i) => <NodeView key={i} node={n} mode={mode} />)}
        </div>
      )}
    </div>
  )
}

export default function TracePanel({ traces, onClear }) {
  const [expanded, setExpanded] = useState(false)
  const [mode, setMode] = useState('timeline') // 'timeline' | 'summary'

  // Group traces into interactions (split on 'user' type)
  const interactions = []
  let current = null
  for (const trace of traces) {
    if (trace.type === 'user') {
      if (current) interactions.push(current)
      current = { label: trace.label, timestamp: trace.time, events: [trace] }
    } else if (current) {
      current.events.push(trace)
    }
  }
  if (current) interactions.push(current)

  const reversed = [...interactions].reverse() // newest on top

  return (
    <div className={`border-t border-line bg-surface transition-all duration-200 flex-shrink-0 ${expanded ? 'h-[320px]' : 'h-8'}`}>
      {/* A div with a button role rather than a <button>: the mode toggle and
          download/clear controls nest inside it, and interactive content is not
          allowed inside a real button. */}
      <div role="button" tabIndex={0} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded) } }}
        className="w-full h-8 px-4 flex items-center justify-between bg-nav hover:bg-surface-3 transition-colors cursor-pointer select-none">
        <div className="flex items-center gap-2">
          <svg className="w-3.5 h-3.5 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
          </svg>
          <span className="text-xs font-medium text-fg-2">Agent Trace</span>
          {interactions.length > 0 && (
            <span className="text-[10px] bg-surface-active text-fg-2 rounded-full px-1.5 py-0.5">{interactions.length} interactions</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {expanded && interactions.length > 0 && (
            <span className="flex items-center rounded-md overflow-hidden border border-line" onClick={(e) => e.stopPropagation()}>
              {['timeline', 'summary'].map(m => (
                <button key={m} onClick={(e) => { e.stopPropagation(); setMode(m) }}
                  className={`px-2 py-0.5 text-[10px] font-medium capitalize transition-colors ${
                    mode === m ? 'bg-primary text-primary-fg' : 'text-fg-3 hover:text-fg'
                  }`}>{m}</button>
              ))}
            </span>
          )}
          {interactions.length > 0 && (
            <button onClick={(e) => { e.stopPropagation(); downloadTrace({ exported_at: new Date().toISOString(), interaction_count: interactions.length, interactions }, `agent-trace-all-${tsForFile()}.json`) }}
              title="Download all traces"
              className="text-fg-muted hover:text-fg-2 transition-colors">
              <DownloadIcon className="w-3.5 h-3.5" />
            </button>
          )}
          {expanded && interactions.length > 0 && (
            <button onClick={(e) => { e.stopPropagation(); onClear?.() }} title="Clear traces"
              className="text-fg-muted hover:text-fg-2 transition-colors">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          )}
          <svg className={`w-3.5 h-3.5 text-fg-muted transition-transform ${expanded ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
          </svg>
        </div>
      </div>
      {expanded && (
        <div className="h-[calc(100%-2rem)] overflow-y-auto px-3 py-2 space-y-1.5">
          {reversed.length === 0 ? (
            <div className="text-xs text-fg-muted text-center py-4">No trace events yet. Send a message to start.</div>
          ) : (
            reversed.map((interaction, i) => (
              <InteractionGroup key={i} interaction={interaction} defaultExpanded={i === 0} mode={mode} />
            ))
          )}
        </div>
      )}
    </div>
  )
}
