// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useRef, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { COLOR, HEALTH_COLORS } from '../theme'
import { isSkillActivation, skillNameFromInput } from '../skills'
import { config } from '../config'
import { modelTraceLabel, modelTraceDetail } from './modelTrace'

// REST base, same source useApi reads, so the unload beacon hits the same backend the
// component's authenticated api.post calls do.
const API_BASE = config.apiUrl || config.localBackendUrl

const STEP_LABELS = {
  investigation_agent: { label: 'Investigation Agent', icon: '🔬' },
  observability_agent: { label: 'Observability Agent', icon: '👁️' },
  postgres_agent: { label: 'PostgreSQL Agent', icon: '🐘' },
  docs_agent: { label: 'Research Agent', icon: '📚' },
}

const SEV = {
  critical: { accent: 'border-l-red-500', badge: 'bg-err-bg text-err', text: 'text-err' },
  warning: { accent: 'border-l-amber-500', badge: 'bg-warn-bg text-warn-mid', text: 'text-warn-mid' },
  info: { accent: 'border-l-blue-500', badge: 'bg-info-bg text-info', text: 'text-info' },
}

// Caution shown at the foot of the report and appended to the download. This is
// AI-generated decision support, not a substitute for DBA review.
const REPORT_DISCLAIMER = 'This report is AI-generated and may contain errors. Treat it as decision support, not a substitute for DBA review. Verify findings and test any recommended SQL or configuration changes in a non-production environment before applying them.'

const SHORT_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
function fmtDate(d) { if (!d) return ''; const [,m,day] = d.split('-'); return `${parseInt(day)} ${SHORT_MONTHS[parseInt(m)-1]}` }

function MiniHealthHeatmap({ api, spaceId, databaseIds, databaseNames }) {
  const [trends, setTrends] = useState(null)
  useEffect(() => {
    if (!spaceId || !databaseIds?.length) return
    api.get(`/api/spaces/${spaceId}/trends?days=30`).then(setTrends).catch(() => {})
  }, [spaceId])

  const scoped = useMemo(() => {
    if (!trends?.databases) return null
    const dbs = trends.databases.filter(db =>
      databaseIds.includes(db.instance_id) || databaseNames?.includes(db.display_name)
    )
    if (!dbs.length || !dbs.some(db => db.data_points?.length)) return null
    const dates = trends.dates || []
    return { dbs, dates }
  }, [trends, databaseIds, databaseNames])

  if (!scoped) return null

  return (
    <div className="bg-surface rounded-lg border border-line p-4">
      <div className="flex items-center gap-2 mb-3">
        <svg className="w-4 h-4 text-primary" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
        </svg>
        <h3 className="text-sm font-bold text-fg">Health Context</h3>
        <span className="text-[10px] text-fg-muted">Last 30 days</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[10px]">
          <thead>
            <tr>
              <th className="text-left py-1 pr-3 text-fg-3 font-medium sticky left-0 bg-surface z-10 min-w-[120px]">Database</th>
              {scoped.dates.length > 1 ? scoped.dates.map(d => (
                <th key={d} className="px-0.5 py-1 text-fg-muted font-normal whitespace-nowrap">{fmtDate(d)}</th>
              )) : <th className="text-left py-1 px-2 text-fg-muted font-normal">Health</th>}
            </tr>
          </thead>
          <tbody>
            {scoped.dbs.map(db => {
              const lookup = {}
              db.data_points?.forEach(p => { lookup[p.date] = p.health_score })
              return (
                <tr key={db.instance_id}>
                  <td className="py-0.5 pr-3 text-fg-2 font-medium sticky left-0 bg-surface z-10 truncate max-w-[120px]" title={db.display_name}>{db.display_name}</td>
                  {scoped.dates.map(d => (
                    <td key={d} className="px-0.5 py-0.5">
                      <div className="w-full h-4 rounded-sm" style={{ backgroundColor: lookup[d] ? (HEALTH_COLORS[lookup[d]] || HEALTH_COLORS.unknown) : COLOR.surface3, minWidth: 12 }}
                        title={`${db.display_name} · ${d} · ${(lookup[d] || 'no data').toUpperCase()}`} />
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="flex items-center gap-3 mt-2 text-[10px] text-fg-muted">
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm" style={{ backgroundColor: HEALTH_COLORS.ok }} />Healthy</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm" style={{ backgroundColor: HEALTH_COLORS.warning }} />Warning</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm" style={{ backgroundColor: HEALTH_COLORS.critical }} />Critical</span>
      </div>
    </div>
  )
}

function formatDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function getDuration(s, e) {
  if (!s) return ''
  const diff = (e ? new Date(e) : new Date()) - new Date(s)
  return diff < 60000 ? `${Math.round(diff / 1000)}s` : `${Math.floor(diff / 60000)}m ${Math.round((diff % 60000) / 1000)}s`
}

const SectionIcons = {
  timeline: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>,
  findings: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" /></svg>,
  rootCause: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" /></svg>,
  actions: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M11.35 3.836c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m8.9-4.414c.376.023.75.05 1.124.08 1.131.094 1.976 1.057 1.976 2.192V16.5A2.25 2.25 0 0118 18.75h-2.25m-7.5-10.5H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V18.75m-7.5-10.5h6.375c.621 0 1.125.504 1.125 1.125v9.375m-8.25-3l1.5 1.5 3-3.75" /></svg>,
}

function SectionCard({ icon, title, badge, children, collapsible = false, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="bg-surface rounded-lg border border-line">
      <div
        className={`flex items-center justify-between p-4 ${collapsible ? 'cursor-pointer select-none' : ''}`}
        onClick={collapsible ? () => setOpen(o => !o) : undefined}
      >
        <h2 className="text-sm font-bold text-fg flex items-center gap-2">
          {icon && <span className="w-6 h-6 rounded-md bg-primary-subtle text-primary flex items-center justify-center">{icon}</span>}
          {title}
        </h2>
        <div className="flex items-center gap-2">
          {badge}
          {collapsible && (
            <svg className={`w-4 h-4 text-fg-muted transition-transform ${open ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          )}
        </div>
      </div>
      {(!collapsible || open) && <div className="px-4 pb-4">{children}</div>}
    </div>
  )
}

// Live progress of the runs this browser tab is streaming, keyed by investigation id.
// The stream callbacks below write here (as well as into the mounted view's state), so
// a view that is unmounted (the user switched to another investigation) and mounted
// again while the run is still going picks its timeline and skills straight back up
// instead of showing nothing until the saved record arrives. Cleared when a run ends.
const liveRuns = new Map()
const liveListeners = new Set()
function publishLive(id, patch) {
  const cur = liveRuns.get(id) || { timeline: [], skills: [] }
  const next = { ...cur }
  for (const k of Object.keys(patch)) next[k] = typeof patch[k] === 'function' ? patch[k](cur[k]) : patch[k]
  liveRuns.set(id, next)
  liveListeners.forEach(fn => fn(id, next))
}
function clearLive(id) { liveRuns.delete(id) }

export default function InvestigationDetail({ api, spaceId, investigation: initialData, onBack, onChatAbout, onTrace, accessToken, getValidAccessToken, streamMessage, isSessionStreaming, stopStreaming, backendUrl, databases }) {
  const [investigation, setInvestigation] = useState(initialData)
  const invId = initialData?.investigation_id
  const [liveTimeline, setLiveTimelineState] = useState(() => liveRuns.get(invId)?.timeline || [])
  const [streamStarted, setStreamStarted] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [loadedSkills, setLoadedSkillsState] = useState(() => liveRuns.get(invId)?.skills || [])
  // Every live update goes to the shared store too, so a remount can restore it.
  const setLiveTimeline = (u) => { setLiveTimelineState(u); publishLive(invId, { timeline: u }) }
  const setLoadedSkills = (u) => { setLoadedSkillsState(u); publishLive(invId, { skills: u }) }
  useEffect(() => {
    const fn = (id, run) => { if (id === invId) { setLiveTimelineState(run.timeline); setLoadedSkillsState(run.skills) } }
    liveListeners.add(fn)
    return () => liveListeners.delete(fn)
  }, [invId])
  const fullTextRef = useRef('')
  const gotResultRef = useRef(false)
  // Finalized exactly once, as success or failure. A result event is success; an error
  // event, a rejected stream, or a stream that closes without a result is a failure.
  const finalizedRef = useRef(false)
  // True only while THIS tab is actively driving an unfinished stream — the one window
  // responsible for saving the result. The unload beacon reads it so only the real driver
  // finalizes an abandoned run on close/reload, and never an already-finished one.
  const drivingRef = useRef(false)
  const loadedSkillsRef = useRef([])

  const fetchDetail = async () => {
    try {
      const data = await api.get(`/api/spaces/${spaceId}/investigations/${investigation.investigation_id}`)
      setInvestigation(data)
    } catch (e) { console.error('Failed to fetch:', e) }
  }

  // Finalize the run exactly once. `outcome` is 'completed' (a real result event) or
  // 'failed' (an error event, a rejected stream, or a stream that ended without a
  // result). A failure keeps the partial text as the detail and is saved AS a failure —
  // never posted to /complete as a finished diagnosis.
  const finalizeInvestigation = (outcome, seenAgents, toolCounts, failureDetail = '') => {
    if (finalizedRef.current) return
    finalizedRef.current = true
    drivingRef.current = false  // the run has a terminal outcome; the unload beacon must not re-fail it
    const failed = outcome === 'failed'
    // Wait a tick to ensure all pending text events have been processed
    setTimeout(() => {
      setStreaming(false)
      // A "completed" outcome that produced no streamed agent text never actually
      // generated a diagnosis. This happens when two investigations are launched from
      // one turn and compete for the single active-investigation slot: the run that is
      // not driven to a full stream ends here empty, and storing it as "completed" is
      // exactly the silent blank report (status completed, 0 findings) the user saw.
      // Treat an empty completion as a failure so it surfaces as retryable, not blank.
      const hasText = !!(fullTextRef.current && fullTextRef.current.trim())
      const effectiveFailed = failed || (outcome === 'completed' && !hasText)
      const effectiveOutcome = effectiveFailed ? 'failed' : 'completed'
      const stepStatus = effectiveFailed ? 'failed' : 'completed'
      setLiveTimeline(prev => prev.map(t => ({
        ...t, status: stepStatus,
        tools: toolCounts[t.step] ? { ...toolCounts[t.step] } : t.tools,
      })))
      const finalTimeline = ['investigation_agent', ...seenAgents].map(step => ({
        step, label: STEP_LABELS[step]?.label || step,
        status: stepStatus, tools: Object.entries(toolCounts[step] || {}).map(([name, count]) => ({ name, count })),
        ...(step === 'investigation_agent' && loadedSkillsRef.current.length ? { skills: [...loadedSkillsRef.current] } : {}),
      }))
      const summary = effectiveFailed
        ? (fullTextRef.current || failureDetail || 'The investigation did not produce any results. Please retry.')
        : fullTextRef.current
      api.post(`/api/spaces/${spaceId}/investigations/${investigation.investigation_id}/complete`, {
        status: effectiveOutcome,
        summary,
        timeline: finalTimeline,
        ...(effectiveFailed ? { failure_detail: failureDetail || 'The agent stream ended without producing any findings.' } : {}),
      }).then(() => { clearLive(investigation.investigation_id); return fetchDetail() })
        .catch(e => console.error('Save failed:', e))
    }, 100) // 100ms delay to ensure all text events are processed
  }
  const completeInvestigation = (seenAgents, toolCounts) =>
    finalizeInvestigation('completed', seenAgents, toolCounts)

  // Stream through AgentCore — same path as chat. Exactly one run per investigation
  // per browser tab, and only from a FULL record:
  //  - `inv-<id>` already streaming in this tab means another mount of this view
  //    started it (the user switched away and back while it ran). That run continues
  //    in the background and saves its result; this mount just polls for it. Starting
  //    a second stream here re-ran the whole investigation and posted a second,
  //    conflicting completion.
  //  - A hollow record (a chat card's "Open investigation" passes only id + status)
  //    has no `trigger`, so a stream from it sent the literal prompt "undefined" with
  //    no time range or databases and produced an empty report. Wait for fetchDetail
  //    to load the record first; the effect re-runs when `trigger` arrives.
  const streamingElsewhere = !!isSessionStreaming?.(`inv-${investigation?.investigation_id}`)
  const sawBackgroundRun = useRef(false)
  // A running record the SERVER has already claimed (run_claim stamped by the first
  // stream) must not be re-streamed on reopen (reload or a second tab): the backend
  // refuses it, and re-streaming would duplicate the whole agent run. We only stream a
  // running record this tab is already driving, or one with no claim yet (the fresh run
  // this tab is about to start). Otherwise we just watch its live state and poll.
  const claimedElsewhere = investigation?.status === 'running'
    && !!investigation?.run_claim && !streamStarted && !streamingElsewhere
  useEffect(() => {
    if (investigation?.status !== 'running' || streamStarted) return
    if (streamingElsewhere) { sawBackgroundRun.current = true; setStreaming(true); return }
    if (claimedElsewhere) {
      // Another window owns this run. Show it as running and poll for its result; never
      // start a second stream (the server would refuse it anyway).
      setStreaming(true)
      let tries = 0
      const t = setInterval(async () => {
        tries += 1
        try {
          const data = await api.get(`/api/spaces/${spaceId}/investigations/${investigation.investigation_id}`)
          if (data?.status !== 'running') { clearInterval(t); setInvestigation(data); setLiveTimelineState([]) }
          else if (tries >= 40) clearInterval(t)  // ~10 min, matching the server sweep
        } catch { if (tries >= 40) clearInterval(t) }
      }, 15000)
      return () => clearInterval(t)
    }
    if (sawBackgroundRun.current) {
      // The background run just ended; its completion is being saved. Re-read the
      // record (a few tries, it lands a moment after the stream closes) and never
      // start a stream of our own from this mount.
      setStreaming(false)
      let tries = 0
      const t = setInterval(async () => {
        tries += 1
        try {
          const data = await api.get(`/api/spaces/${spaceId}/investigations/${investigation.investigation_id}`)
          if (data?.status !== 'running' || tries >= 5) { clearInterval(t); setInvestigation(data); if (data?.status !== 'running') setLiveTimelineState([]) }
        } catch { if (tries >= 5) clearInterval(t) }
      }, 1500)
      return () => clearInterval(t)
    }
    if (!investigation?.trigger) return
    setStreamStarted(true)
    setStreaming(true)
    drivingRef.current = true  // this tab now owns the run; the unload beacon fires if it dies mid-run
    loadedSkillsRef.current = []
    setLoadedSkills([])

    const ts = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    const seenAgents = []
    const toolCounts = {}
    let currentAgent = null

    const timeRange = investigation.time_range || 'Last 2 hours'
    const dbNames = investigation.database_names || []
    const dbIds = investigation.database_ids || []

    // The database scope is built by the backend from the investigation record's own
    // database ids (sent as `database_ids`), so the credential pointer never has to be
    // present in the browser. /api/databases does not return it to operators.
    const prompt = `[INVESTIGATION:${investigation.investigation_id}]\n[TIME RANGE: ${timeRange}. Check metrics and events for this period.]\n${investigation.trigger}`

    onTrace?.({ type: 'user', label: `Investigation: ${investigation.title || investigation.trigger}`, time: ts() })
    setLiveTimeline([{ step: 'investigation_agent', label: 'Investigation Agent', status: 'running', tools: {} }])

    // Specialist narration (sub_text) arrives as many small deltas. Buffer per
    // agent and emit ONE "reasoning" trace entry per burst — flushed before that
    // agent's next tool call and when control returns to the caller — so the
    // trace carries the "why" without token-level spam.
    const reasoningBuf = {}
    const flushReasoning = (agent) => {
      const t = (reasoningBuf[agent] || '').trim()
      if (t) onTrace?.({ type: 'reasoning', agent, label: `${agent} reasoning`, detail: t, time: ts() })
      reasoningBuf[agent] = ''
    }
    const flushAllReasoning = () => Object.keys(reasoningBuf).forEach(flushReasoning)

    streamMessage(backendUrl, prompt, accessToken, `inv-${investigation.investigation_id}`, (event) => {
      if (event.type === 'model') {
        onTrace?.({ type: 'model', label: modelTraceLabel(event), detail: modelTraceDetail(event), time: ts() })
      } else if (event.type === 'text') {
        fullTextRef.current += event.content
      } else if (event.type === 'tool_start') {
        // Skill activation carries no name yet at start — handle it on the
        // 'tool' event (which has the input naming the skill).
        if (!isSkillActivation(event.name)) {
          onTrace?.({ type: 'tool_start', toolUseId: event.toolUseId, label: `Tool call: ${event.name}`, detail: { toolUseId: event.toolUseId, name: event.name }, time: ts() })
        }
        if (event.name === 'observability_agent' || event.name === 'postgres_agent' || event.name === 'docs_agent') {
          if (!seenAgents.includes(event.name)) {
            seenAgents.push(event.name)
            toolCounts[event.name] = {}
            setLiveTimeline(prev => [...prev, { step: event.name, label: STEP_LABELS[event.name]?.label || event.name, status: 'running', tools: {} }])
          }
          currentAgent = event.name
        } else if (!isSkillActivation(event.name)) {
          // Track investigation agent's own tool calls (get_report_analysis, current_time, etc.)
          if (!toolCounts['investigation_agent']) toolCounts['investigation_agent'] = {}
          toolCounts['investigation_agent'][event.name] = (toolCounts['investigation_agent'][event.name] || 0) + 1
          setLiveTimeline(prev => prev.map(t =>
            t.step === 'investigation_agent' ? { ...t, tools: { ...toolCounts['investigation_agent'] } } : t
          ))
        }
      } else if (event.type === 'tool') {
        if (isSkillActivation(event.name)) {
          // Investigation agent loaded a built-in skill — surface it distinctly
          // (violet badge in the timeline + a "skill" trace entry).
          const skillName = skillNameFromInput(event.input) || event.name
          if (!loadedSkillsRef.current.includes(skillName)) {
            loadedSkillsRef.current = [...loadedSkillsRef.current, skillName]
            setLoadedSkills(loadedSkillsRef.current)
          }
          onTrace?.({ type: 'skill', label: `Skill loaded: ${skillName}`, detail: { name: event.name, input: event.input }, time: ts() })
        } else {
          onTrace?.({ type: 'tool', toolUseId: event.toolUseId, label: `Tool: ${event.name}`, detail: { name: event.name, input: event.input }, time: ts() })
        }
      } else if (event.type === 'tool_result') {
        // Control returned to the caller — flush any trailing specialist reasoning.
        flushAllReasoning()
        onTrace?.({ type: 'tool_result', toolUseId: event.toolUseId, status: event.status, label: `Result${event.name ? ` — ${event.name}` : ''}: ${event.status}`, detail: event.content, time: ts() })
      } else if (event.type === 'sub_tool') {
        // Flush the reasoning that led up to THIS tool call so it renders above it.
        flushReasoning(event.agent)
        onTrace?.({ type: 'tool', toolUseId: event.toolUseId, agent: event.agent, label: `↳ ${event.agent}: ${event.name}`, detail: { agent: event.agent, name: event.name, input: event.input }, time: ts() })
        if (event.agent?.includes('Observability')) currentAgent = 'observability_agent'
        else if (event.agent?.includes('PostgreSQL')) currentAgent = 'postgres_agent'
        else if (event.agent?.includes('Research')) currentAgent = 'docs_agent'
        if (currentAgent && toolCounts[currentAgent]) {
          toolCounts[currentAgent][event.name] = (toolCounts[currentAgent][event.name] || 0) + 1
          setLiveTimeline(prev => prev.map(t =>
            t.step === currentAgent ? { ...t, tools: { ...toolCounts[currentAgent] } } : t
          ))
        }
      } else if (event.type === 'sub_tool_result') {
        // Output of a sub-agent (specialist) tool — mirrors tool_result but ↳-labelled
        // so testers can verify each nested tool's returned values against the spec.
        onTrace?.({ type: 'tool_result', toolUseId: event.toolUseId, agent: event.agent, status: event.status, label: `↳ ${event.agent}: ${event.name ? `${event.name} → ` : ''}result (${event.status})`, detail: event.content, time: ts() })
      } else if (event.type === 'sub_text') {
        // Specialist narration — buffer; flushed as one "reasoning" entry per burst.
        if (event.content) reasoningBuf[event.agent] = (reasoningBuf[event.agent] || '') + event.content
      } else if (event.type === 'result') {
        gotResultRef.current = true
        flushAllReasoning()
        onTrace?.({ type: 'result', label: 'Investigation complete', time: ts() })
        finalizeInvestigation('completed', seenAgents, toolCounts)
      } else if (event.type === 'error') {
        // A terminal failure event. Record the run AS failed, keeping whatever partial
        // text streamed so far as the detail — never as a completed diagnosis.
        flushAllReasoning()
        onTrace?.({ type: 'error', label: `Error: ${event.error}`, time: ts() })
        finalizeInvestigation('failed', seenAgents, toolCounts, String(event.error || 'The agent reported an error.'))
      }
    }, getValidAccessToken, { spaceId, databaseIds: dbIds }).then(() => {
      // The stream closed. A result event already finalized it as completed; otherwise
      // it ended WITHOUT a result, which is a failure, not a success.
      if (!gotResultRef.current) {
        console.warn('Stream ended without result event — marking investigation failed')
        finalizeInvestigation('failed', seenAgents, toolCounts, 'The agent stream ended without a result.')
      }
    }).catch(err => {
      console.error('Investigation stream failed:', err)
      // A rejected stream must not leave the investigation stuck "running" forever (only
      // the 10-min cleanup sweep would rescue it), nor be saved as a completed result.
      // Finalize as a failure with an honest detail.
      finalizeInvestigation('failed', seenAgents, toolCounts,
        `The agent stream failed: ${err?.message || err}`)
    })
  }, [investigation?.status, investigation?.trigger, streamStarted, streamingElsewhere, claimedElsewhere])

  // Cancel a running investigation. Stopping the DB record alone was not enough: the
  // client stream kept running, burning a full agent run after "Cancel", and when it
  // finally produced a result it POSTed /complete (the server's idempotency guard turned
  // that into a no-op, but the work was wasted and the agent never actually stopped). So
  // we abort THIS tab's stream first, block the stream's own finalize (finalizedRef) so a
  // late result/error/close cannot re-finalize, drop the live spinner, then mark the
  // record cancelled. A run owned by another tab has no controller here — the abort is a
  // no-op and the server /cancel + /complete guard still make the outcome cancelled.
  const [cancelling, setCancelling] = useState(false)
  const cancelInvestigation = async () => {
    setCancelling(true)
    finalizedRef.current = true
    drivingRef.current = false
    try { stopStreaming?.(`inv-${investigation.investigation_id}`) } catch {}
    setStreaming(false)
    clearLive(investigation.investigation_id)
    try {
      await api.post(`/api/spaces/${spaceId}/investigations/${investigation.investigation_id}/cancel`)
    } catch (e) {
      console.error('Cancel failed:', e)
    } finally {
      setCancelling(false)
      fetchDetail()
    }
  }

  // Retry a failed run. Re-opens the record server-side (status back to
  // running, claim cleared), then lets the stream effect start a fresh run: reset the
  // one-shot guards so this tab streams again.
  const [retrying, setRetrying] = useState(false)
  const retryInvestigation = async () => {
    setRetrying(true)
    try {
      await api.post(`/api/spaces/${spaceId}/investigations/${investigation.investigation_id}/retry`, {})
      finalizedRef.current = false
      gotResultRef.current = false
      fullTextRef.current = ''
      sawBackgroundRun.current = false
      setStreamStarted(false)
      clearLive(investigation.investigation_id)
      setLiveTimelineState([])
      await fetchDetail()
    } catch (e) {
      console.error('Retry failed:', e)
    } finally {
      setRetrying(false)
    }
  }

  // Best-effort "I'm leaving" beacon. When the tab DRIVING this run goes away (close or
  // reload), finalize the run as failed right away instead of leaving it spinning until
  // the server's stale sweep (~10-15 min). Only the live driver fires it (drivingRef), and
  // never for a run already finalized/cancelled. A browser crash, a killed tab, or an
  // expired token means no beacon — the server sweep remains the backstop, so this only
  // ever makes the common case (deliberate close/reload) resolve faster; it cannot strand
  // or wrongly fail a healthy run. keepalive lets the request outlive the page, mirroring
  // the logout token-revoke beacon. NOTE: this does not restore the in-browser live state
  // (pills/timeline) or continue the run server-side — that is the durable-background-run
  // follow-up; here we only ensure an abandoned run lands on a retryable "failed".
  useEffect(() => {
    const onPageHide = (e) => {
      // A page going into the back/forward cache may be restored and keep streaming — do
      // not fail it. Only a real teardown (not persisted) and only the active driver.
      if (e.persisted || !drivingRef.current) return
      const token = localStorage.getItem('id_token')
      if (!token) return
      try {
        fetch(`${API_BASE}/api/spaces/${spaceId}/investigations/${invId}/complete`, {
          method: 'POST',
          keepalive: true,
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
          body: JSON.stringify({
            status: 'failed',
            failure_detail: 'The investigation was interrupted because its browser tab was closed before it finished. Use Retry to run it again.',
          }),
        }).catch(() => {})
      } catch { /* best effort — the sweep is the backstop */ }
    }
    window.addEventListener('pagehide', onPageHide)
    return () => window.removeEventListener('pagehide', onPageHide)
  }, [spaceId, invId])

  useEffect(() => { fetchDetail() }, [])

  const timeline = liveTimeline.length > 0 ? liveTimeline : (investigation?.timeline || [])
  const findings = investigation?.findings || []
  const sections = investigation?.sections || {}
  const status = streaming ? 'running' : (investigation?.status || 'running')

  const handleCopy = () => {
    const text = investigation?.summary || sections?.findings_text || ''
    navigator.clipboard.writeText(text)
  }
  const handleDownload = () => {
    const text = (investigation?.summary || '') + `\n\n---\n\n_Disclaimer: ${REPORT_DISCLAIMER}_\n`
    const blob = new Blob([text], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url
    a.download = `investigation-${investigation.investigation_id?.slice(0, 8)}-${new Date().toISOString().slice(0, 10)}.md`
    a.click(); URL.revokeObjectURL(url)
  }

  const sevCounts = { critical: findings.filter(f => f.severity === 'critical').length,
    warning: findings.filter(f => f.severity === 'warning').length,
    info: findings.filter(f => f.severity === 'info').length }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div>
        <button onClick={onBack} className="flex items-center gap-1.5 text-sm text-fg-3 hover:text-primary transition-colors mb-3">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" /></svg>
          Back to Investigations
        </button>
        {/* min-w-0 on the title column lets a long title wrap/truncate inside its own
            space instead of pushing on the actions; flex-shrink-0 + whitespace-nowrap on
            the actions column keeps "Chat about this" and the status badge on one line
            each, so a long title can never squeeze the button text onto two lines and
            overlap the badge. */}
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold text-fg">{investigation?.title || investigation?.trigger}</h1>
            <p className="text-sm text-fg-3 mt-1">
              Started {formatDate(investigation?.started_at)}
              {investigation?.completed_at && ` · Duration: ${getDuration(investigation?.started_at, investigation?.completed_at)}`}
            </p>
          </div>
          <div className="flex items-center gap-2 flex-shrink-0 whitespace-nowrap">
            {onChatAbout && status === 'completed' && (
              <button onClick={() => onChatAbout(investigation)}
                className="px-3 py-1.5 text-xs font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle transition-colors flex items-center gap-1.5 whitespace-nowrap">
                💬 Chat about this
              </button>
            )}
            {status === 'running' && (
              <button onClick={cancelInvestigation} disabled={cancelling}
                className="px-3 py-1.5 text-xs font-medium text-err border border-err-border rounded-lg hover:bg-err-hover transition-colors disabled:opacity-50">
                {cancelling ? 'Cancelling…' : 'Cancel'}</button>
            )}
            {status === 'failed' && (
              <button onClick={retryInvestigation} disabled={retrying}
                className="px-3 py-1.5 text-xs font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle transition-colors disabled:opacity-50 flex items-center gap-1.5">
                {retrying ? 'Retrying…' : '↻ Retry'}
              </button>
            )}
            <span className={`px-3 py-1 rounded-full text-xs font-semibold border ${
              status === 'running' ? 'bg-info-bg-2 text-info-text border-info-border' :
              status === 'completed' ? 'bg-ok-bg-2 text-ok-text border-ok-border' :
              status === 'failed' ? 'bg-err-bg-2 text-err-text border-err-border' :
              'bg-surface-3 text-fg-2 border-line'
            }`}>
              {status === 'running' && <span className="inline-block w-1.5 h-1.5 bg-info rounded-full mr-1.5 animate-pulse" />}
              {status.charAt(0).toUpperCase() + status.slice(1)}
            </span>
          </div>
        </div>
      </div>

      {/* 1. Timeline */}
      <SectionCard icon={SectionIcons.timeline} title="Timeline" badge={
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-fg-3 px-2 py-0.5 rounded-full border border-line">
            🕐 {investigation?.time_range || 'Last 2 hours'}
          </span>
          {(investigation?.database_ids?.length > 0) && (
            <span className="text-[10px] text-fg-3 px-2 py-0.5 rounded-full border border-line">
              🛢️ {investigation.database_ids.length} database{investigation.database_ids.length !== 1 ? 's' : ''}
            </span>
          )}
        </div>
      }>
        {timeline.length === 0 && status === 'running' && (
          <div className="flex items-center gap-3 py-2">
            <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="text-sm text-fg-3">Investigation Agent is starting...</span>
          </div>
        )}

        {investigation?.database_names?.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 mb-3">
            <span className="text-[10px] text-fg-3">Databases:</span>
            {investigation.database_names.map(name => (
              <span key={name} className="px-2 py-0.5 text-[10px] rounded-full bg-primary-subtle text-primary border border-primary-line">
                {name}
              </span>
            ))}
          </div>
        )}

        {timeline.length > 0 && (() => {
          const invEntry = timeline.find(t => t.step === 'investigation_agent')
          const subEntries = timeline.filter(t => t.step !== 'investigation_agent')
          const invRunning = invEntry?.status === 'running'

          return (
            <div className="border border-line rounded-xl overflow-hidden">
              {/* Investigation Agent header */}
              <div className="flex items-center gap-3 px-4 py-3 bg-surface-2">
                <div className={`w-7 h-7 rounded-full flex items-center justify-center text-sm flex-shrink-0 ${
                  invRunning ? 'bg-info-bg-2' : 'bg-ok-bg-2'
                }`}>
                  {invRunning
                    ? <div className="w-3.5 h-3.5 border-2 border-info-border border-t-transparent rounded-full animate-spin" />
                    : <svg className="w-3.5 h-3.5 text-ok" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" /></svg>}
                </div>
                <div>
                  <span className="text-sm font-bold text-fg">Investigation Agent</span>
                  {invRunning
                    ? <span className="text-[10px] text-info animate-pulse ml-2">Working...</span>
                    : <span className="text-[10px] text-ok ml-2">Completed</span>}
                  {(() => {
                    const invTools = Array.isArray(invEntry?.tools) ? invEntry.tools : Object.entries(invEntry?.tools || {}).map(([name, count]) => ({ name, count }))
                    return invTools.length > 0 ? (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {invTools.map((t, ti) => (
                          <span key={ti} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-surface-3 text-fg-3 border border-line">
                            ⚡ {t.name}{t.count > 1 ? ` ×${t.count}` : ''}
                          </span>
                        ))}
                      </div>
                    ) : null
                  })()}
                  {(() => {
                    const invSkills = (invEntry?.skills?.length ? invEntry.skills : loadedSkills) || []
                    return invSkills.length > 0 ? (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {invSkills.map((s, si) => (
                          <span key={si} className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-primary-subtle text-primary border border-primary-line">
                            🧠 Skill: {s}
                          </span>
                        ))}
                      </div>
                    ) : null
                  })()}
                </div>
              </div>

              {/* Sub-agents */}
              <div className="ml-6 border-l-2 border-line">
                {subEntries.map((entry, i) => {
                  const info = STEP_LABELS[entry.step] || { label: entry.label || entry.step, icon: '🔍' }
                  const isRunning = entry.status === 'running'
                  const tools = Array.isArray(entry.tools) ? entry.tools : Object.entries(entry.tools || {}).map(([name, count]) => ({ name, count }))

                  return (
                    <div key={i} className="ml-4 my-2 border border-line-2 rounded-lg overflow-hidden">
                      <div className="flex items-center gap-2.5 px-3 py-2.5 bg-surface">
                        <div className={`w-5 h-5 rounded-full flex items-center justify-center text-xs flex-shrink-0 ${
                          isRunning ? 'bg-info-bg-2' : entry.status === 'completed' ? 'bg-ok-bg-2' : 'bg-err-bg-2'
                        }`}>
                          {isRunning ? <div className="w-2.5 h-2.5 border-2 border-info-border border-t-transparent rounded-full animate-spin" />
                            : entry.status === 'completed' ? <svg className="w-2.5 h-2.5 text-ok" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" /></svg>
                            : <svg className="w-2.5 h-2.5 text-err" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>}
                        </div>
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-semibold text-fg">{info.label}</span>
                            {isRunning && <span className="text-[10px] text-info animate-pulse">Working...</span>}
                            {!isRunning && <span className="text-[10px] text-ok">Done</span>}
                          </div>
                          {tools.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1">
                              {tools.map((t, ti) => (
                                <span key={ti} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-surface-3 text-fg-3 border border-line">
                                  ⚡ {t.name}{t.count > 1 ? ` ×${t.count}` : ''}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>

              {status === 'completed' && (
                <div className="px-4 py-2.5 border-t border-line bg-ok-bg">
                  <p className="text-[11px] text-fg-2">
                    Investigation complete
                    {findings.length > 0 && ` — ${sevCounts.critical > 0 ? `${sevCounts.critical} critical, ` : ''}${sevCounts.warning > 0 ? `${sevCounts.warning} warning, ` : ''}${sevCounts.info > 0 ? `${sevCounts.info} info` : ''} findings`}
                  </p>
                </div>
              )}
            </div>
          )
        })()}
      </SectionCard>

      {/* Health Context Heatmap */}
      {status === 'completed' && investigation?.database_ids?.length > 0 && (
        <MiniHealthHeatmap api={api} spaceId={spaceId} databaseIds={investigation.database_ids} databaseNames={investigation.database_names} />
      )}

      {/* 2. Findings */}
      {(findings.length > 0 || sections.findings_text) && (
        <SectionCard icon={SectionIcons.findings} title="Findings" collapsible badge={
          <div className="flex items-center gap-2 text-[10px]">
            {sevCounts.critical > 0 && <span className="px-2 py-0.5 rounded-md bg-err-bg text-err font-semibold">{sevCounts.critical} critical</span>}
            {sevCounts.warning > 0 && <span className="px-2 py-0.5 rounded-md bg-warn-bg text-warn-mid font-semibold">{sevCounts.warning} warning</span>}
            {sevCounts.info > 0 && <span className="px-2 py-0.5 rounded-md bg-info-bg text-info font-semibold">{sevCounts.info} info</span>}
          </div>
        }>
          {findings.length > 0 ? (() => {
            const sevOrder = { critical: 0, warning: 1, info: 2 }
            const dbNames = investigation?.database_names || []

            const enriched = findings.map(f => {
              if (f.database) return f
              if (dbNames.length > 0) {
                const match = dbNames.find(name => f.title?.toLowerCase().includes(name.toLowerCase()))
                if (match) return { ...f, database: match }
              }
              return f
            })

            const sorted = [...enriched].sort((a, b) => (sevOrder[a.severity] ?? 3) - (sevOrder[b.severity] ?? 3))
            const hasDbGroups = sorted.some(f => f.database)

            const FindingRow = ({ f, i }) => {
              const s = SEV[f.severity] || SEV.info
              return (
                <div key={i} className={`border-l-2 ${s.accent} pl-3 py-1.5`}>
                  <div className="flex items-baseline gap-2">
                    <span className={`text-[10px] font-bold uppercase tracking-wide ${s.text} flex-shrink-0`}>{f.severity}</span>
                    <span className="text-[11px] text-fg-2 leading-snug">{f.title}</span>
                  </div>
                </div>
              )
            }

            if (hasDbGroups || dbNames.length > 1) {
              const groups = {}
              sorted.forEach(f => {
                const db = f.database || 'General'
                if (!groups[db]) groups[db] = []
                groups[db].push(f)
              })
              return (
                <div className="space-y-4">
                  {Object.entries(groups).map(([db, dbFindings]) => (
                    <div key={db}>
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-[10px] font-semibold text-fg-3 tracking-wide">{db}</span>
                        <div className="flex-1 border-t border-line-2" />
                        <span className="text-[10px] text-fg-muted">{dbFindings.length}</span>
                      </div>
                      <div className="space-y-0.5">
                        {dbFindings.map((f, i) => <FindingRow key={i} f={f} i={i} />)}
                      </div>
                    </div>
                  ))}
                </div>
              )
            }
            return (
              <div className="space-y-0.5">
                {sorted.map((f, i) => <FindingRow key={i} f={f} i={i} />)}
              </div>
            )
          })() : sections.findings_text ? (
            <div className="prose prose-sm max-w-none text-fg-2 text-[12px] leading-relaxed">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{sections.findings_text}</ReactMarkdown>
            </div>
          ) : null}
        </SectionCard>
      )}

      {/* 3. Root Cause */}
      {sections.root_cause && (
        <SectionCard icon={SectionIcons.rootCause} title="Root Cause Analysis" collapsible>
          <div className="prose prose-sm max-w-none text-fg-2 text-[12px] leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{sections.root_cause}</ReactMarkdown>
          </div>
        </SectionCard>
      )}

      {/* 4. Recommended Actions */}
      {sections.recommendations && (
        <SectionCard icon={SectionIcons.actions} title="Recommended Actions" collapsible>
          <div className="prose prose-sm max-w-none text-fg-2 text-[12px] leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{sections.recommendations}</ReactMarkdown>
          </div>
        </SectionCard>
      )}

      {/* 5. Summary */}
      {sections.executive_summary && (
        <SectionCard icon={SectionIcons.rootCause} title="Summary">
          <div className="prose prose-sm max-w-none text-fg-2 text-[12px] leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{sections.executive_summary}</ReactMarkdown>
          </div>
        </SectionCard>
      )}

      {/* Download bar */}
      {investigation?.summary && (
        <div className="flex items-center justify-between rounded-lg border border-line bg-surface px-4 py-3">
          <span className="text-xs text-fg-3">Full investigation report</span>
          <div className="flex items-center gap-2">
            <button onClick={handleCopy} className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] text-fg-2 border border-line rounded-md hover:bg-surface-3 transition-colors">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
              Copy
            </button>
            <button onClick={handleDownload} className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] text-fg-2 border border-line rounded-md hover:bg-surface-3 transition-colors">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>
              Download
            </button>
          </div>
        </div>
      )}

      {/* Caution footer — AI-generated decision support, not a DBA substitute. */}
      {(status === 'completed' || investigation?.summary) && (
        <div className="flex items-start gap-2 rounded-lg border border-warn-border bg-warn-bg px-4 py-2.5">
          <svg className="w-4 h-4 text-warn flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
          </svg>
          <p className="text-[11px] leading-relaxed font-medium text-warn-text">
            {REPORT_DISCLAIMER}
          </p>
        </div>
      )}
    </div>
  )
}
