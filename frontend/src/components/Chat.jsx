// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useRef } from 'react'
import ChatMessage from './ChatMessage'
import ChatInput from './ChatInput'
import ModelPicker from './ModelPicker'
import ScopeSelector from './ScopeSelector'
import SparkleMark from './SparkleMark'
import HomeBriefs from './HomeBriefs'
import FollowUpPills from './FollowUpPills'
import { isSkillActivation, skillNameFromInput } from '../skills'
import { generateReportFollowups, generateInvestigationFollowups } from './contextFollowups'
import { LIVE_MARKER, splitLiveMarker, reportDatabaseScope, investigationDatabaseScope } from './liveHandoff'
import { modelTraceLabel, modelTraceDetail } from './modelTrace'
import { APP_NAME } from '../appName'

function DownloadIcon({ className }) {
  return <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>
}

// Render one message as a Markdown block for the full-chat export. Assistant
// messages flatten their contentBlocks: text verbatim, tool/skill activity as a
// light italic note so the transcript reflects what actually happened.
function messageToMarkdown(msg, userName) {
  const who = msg.role === 'user' ? (userName || 'You') : APP_NAME
  const time = msg.timestamp ? new Date(msg.timestamp).toLocaleString() : ''
  let body = ''
  if (msg.role === 'user') {
    body = msg.content || ''
  } else if (msg.contentBlocks?.length) {
    body = msg.contentBlocks.map(b => {
      if (b.type === 'text') return b.content
      if (b.type === 'tool_mini') return b.isSkill ? `_[skill: ${b.skillName || 'loaded'}]_` : `_[tool: ${b.name}]_`
      if (b.type === 'thinking') return `_[${b.agent}${b.count > 1 ? ` ×${b.count}` : ''}]_`
      if (b.type === 'investigation_suggestion') return `_[suggested investigation: ${b.title}]_`
      return ''
    }).filter(Boolean).join('\n\n')
  } else {
    body = msg.content || ''
  }
  return `### ${who}${time ? `  \n*${time}*` : ''}\n\n${body || '_(no text)_'}\n`
}

// Short caution shown under the chat and appended to exports. AI output can be
// wrong; anything that changes data/config should be reviewed before running.
const CHAT_DISCLAIMER = 'AI-generated guidance may be imperfect. Review all recommendations and verify any SQL or configuration changes before applying them to your databases.'

// Build a readable Markdown transcript of the whole conversation.
function buildTranscript(messages, userName, title) {
  const header = `# ${title || 'Chat transcript'}\n\n_${APP_NAME} — exported ${new Date().toLocaleString()}_\n`
  const footer = `\n---\n\n_Disclaimer: ${CHAT_DISCLAIMER}_\n`
  return header + '\n---\n\n' + messages.map(m => messageToMarkdown(m, userName)).join('\n---\n\n') + footer
}

// The deep scan's structured sections for the [REPORT CONTEXT] block, inside a fixed
// character budget so a report turn stays well under the server's per-turn prompt cap
// (MAX_PROMPT_CHARS, 100k by default). Sections are added in the order a DBA asks about
// them most; a section that does not fit whole is added with its lists cut to the first
// few entries, and anything still over is named as omitted rather than silently dropped.
// Query text in the sections was literal-masked when the scan was stored.
const REPORT_SECTIONS_BUDGET = 45000
const SECTION_ORDER = ['performance', 'maintenance', 'optimization', 'configuration', 'security',
  'activity', 'replication', 'footprint', 'overview', 'reader']

function trimLists(v, n) {
  if (Array.isArray(v)) return v.slice(0, n).map(x => trimLists(x, n))
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, trimLists(x, n)]))
  return v
}

export function reportSectionsBlock(sections, budget = REPORT_SECTIONS_BUDGET) {
  if (!sections || typeof sections !== 'object') return ''
  const names = SECTION_ORDER.filter(n => sections[n]).concat(Object.keys(sections).filter(n => !SECTION_ORDER.includes(n)))
  let used = 0
  const parts = []
  const omitted = []
  for (const n of names) {
    let text = JSON.stringify(sections[n])
    if (used + text.length > budget) text = JSON.stringify(trimLists(sections[n], 5))
    if (used + text.length > budget) { omitted.push(n); continue }
    parts.push(`  ${n}: ${text}`)
    used += text.length
  }
  if (!parts.length) return ''
  return `Report sections (structured data from the deep scan):\n${parts.join('\n')}\n` +
    (omitted.length ? `  (omitted for size: ${omitted.join(', ')})\n` : '')
}

function generateReportSuggestions(analysis) {
  const suggestions = ["Summarize the key findings from this report"]
  const findings = analysis.findings || []
  const critical = findings.filter(f => f.severity === 'CRITICAL')
  const warnings = findings.filter(f => f.severity === 'WARNING')

  if (critical.length > 0) {
    suggestions.push(`Explain the ${critical.length} critical issue${critical.length > 1 ? 's' : ''} and how to fix ${critical.length > 1 ? 'them' : 'it'}`)
    suggestions.push(`What is the impact of "${critical[0].title || critical[0].area}" being critical?`)
  }
  if (warnings.length > 0) {
    suggestions.push(`Why is "${warnings[0].title || warnings[0].area}" flagged as a warning?`)
  }
  if (analysis.metrics?.table_cache_hit_ratio) {
    suggestions.push(`Is a ${analysis.metrics.table_cache_hit_ratio}% cache hit ratio good enough?`)
  }
  suggestions.push("What should I prioritize fixing first?")
  if (analysis.recommendations?.length > 0) {
    suggestions.push("Give me step-by-step commands to implement the top recommendation")
  }
  suggestions.push("Are there any quick wins I can implement right now?")
  return suggestions.slice(0, 8)
}

// Six friendly starter prompts for the chat home (kept short + scannable).
const HOME_SUGGESTIONS = [
  "Give me a health summary of my fleet",
  "Are there any critical issues right now?",
  "What's driving database load at the moment?",
  "Find my slowest queries",
  "Show me the latest health report findings",
  "Any unused or missing indexes to act on?",
]

const ts = () => {
  const d = new Date()
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
}



export default function Chat({ accessToken, sessionId, onNewSession, streamMessage, respondToInterrupt, isStreaming, getValidAccessToken, backendUrl, api, onTrace, reportContext, databaseScope, allDatabases, onScopeChange, onRefreshDatabases, investigationContext, onSetInvestigationContext, onClearInvestigationContext, userName, conversation, setSessionMessages, setSessionCurrentMessage, setSessionPendingInterrupt, commitCurrentMessage: commitSessionCurrentMessage, onSessionActivity, onStop, onStartInvestigation, onOpenInvestigation, onOpenPanel, roleFocus = '', onRoleFocusChange }) {
  // Conversation state lives in the App, keyed by sessionId (see App.jsx), so a
  // stream keeps updating this chat even after the user switches away and this
  // component unmounts. These wrappers bind the App's session-keyed setters to
  // THIS chat's sessionId; because handleSend closes over that id, its stream
  // writes land in the right session no matter which chat is on screen.
  const messages = conversation?.messages || []
  const currentMessage = conversation?.currentMessage || null
  const pendingInterrupt = conversation?.pendingInterrupt || null
  // An investigation this chat started and is waiting on (App clears it and appends
  // the hand-off turn when it finishes). The composer pauses meanwhile.
  const investigating = conversation?.activeInvestigation || null
  const setMessages = (u) => setSessionMessages(sessionId, u)
  const setCurrentMessage = (u) => setSessionCurrentMessage(sessionId, u)
  const setPendingInterrupt = (u) => setSessionPendingInterrupt(sessionId, u)
  const commitCurrentMessage = () => commitSessionCurrentMessage(sessionId)
  // Role focus only applies when the effective scope is exactly ONE Aurora
  // cluster with readers — an RDS instance (or a mixed/multi-DB scope) has no
  // writer/reader facet, so a stale focus must never ride into the prompt or
  // the request payload. ScopeSelector resets the control; this is the
  // consumer-side guard.
  const focusScope = databaseScope?.length > 0 ? databaseScope : (allDatabases || [])
  const activeRoleFocus = (focusScope.length === 1 && focusScope[0]?.reader_endpoint) ? roleFocus : ''
  const [respondedInterrupts, setRespondedInterrupts] = useState(new Map())
  // 'discussion' = grounded in the open report/investigation; 'agent' = live database agent.
  const [chatMode, setChatMode] = useState('discussion')
  const [selectedModel, setSelectedModel] = useState('')  // operator's per-message model pick
  const spaceId = new URLSearchParams(window.location.search).get('space_id') || ''

  const reportLabel = reportContext?.meta?.report_name || reportContext?.analysis?.report_name || ''
  const greeting = (() => { const h = new Date().getHours(); return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening' })()
  const isHome = !reportContext?.analysis && !investigationContext

  const messagesEndRef = useRef(null)
  const chatContainerRef = useRef(null)

  const addTrace = (entry) => onTrace?.({ ...entry, time: ts() })

  // A report OR an investigation in context opens in grounded Discussion mode; plain chat
  // (no context) is the live agent. Keyed on the PRESENCE of a context, not its object
  // identity, so a context refresh (same record, new object) does not knock the user out
  // of a Live Agent toggle mid-chat.
  useEffect(() => {
    setChatMode(reportContext?.analysis || investigationContext ? 'discussion' : 'agent')
  }, [!!reportContext?.analysis, !!investigationContext])

  useEffect(() => {
    const { scrollTop, scrollHeight, clientHeight } = chatContainerRef.current || {}
    if (!chatContainerRef.current || scrollHeight - scrollTop - clientHeight < 100) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, currentMessage])

  // Investigations spawned from this chat, which turn a suggestion card into a
  // link with a live status badge (running -> completed). Refetch on window focus
  // so returning from the investigation panel reflects its latest status.
  const [linkedInvestigations, setLinkedInvestigations] = useState([])
  useEffect(() => {
    if (!api || !sessionId || !spaceId) return
    let cancelled = false
    const fetchLinked = () => {
      api.get(`/api/sessions/${sessionId}/investigations?space_id=${encodeURIComponent(spaceId)}`)
        .then(d => { if (!cancelled) setLinkedInvestigations(d?.investigations || []) })
        .catch(() => {})
    }
    fetchLinked()
    window.addEventListener('focus', fetchLinked)
    return () => { cancelled = true; window.removeEventListener('focus', fetchLinked) }
  }, [api, sessionId, spaceId])

  // `opts.investigationContext` lets a hand-off pill discuss an investigation in the
  // same click that puts it in context (the prop only updates on the next render).
  const handleSend = async (message, opts = {}) => {
    const activeInvestigationContext = opts.investigationContext || investigationContext
    // Notify the host so it can create/refresh this chat's Recents entry
    // (title on the first turn, activity bump thereafter).
    const isFirstMessage = messages.length === 0
    onSessionActivity?.(message, isFirstMessage)
    addTrace({ type: 'user', label: `User: ${message.slice(0, 60)}${message.length > 60 ? '...' : ''}`, detail: message })
    setMessages(prev => [...prev, { role: 'user', content: message, timestamp: new Date().toISOString() }])
    const assistantMessage = { role: 'assistant', content: '', contentBlocks: [], timestamp: new Date().toISOString() }
    setCurrentMessage(assistantMessage)

    // If in report discussion mode, prepend analysis as context
    let prompt = message
    // Database ids the backend should scope this turn to (null = no scope block, as for
    // report/investigation-context turns; [] = every configured database in the space).
    let scopeDatabaseIds = null
    // The "Ask the live agent" pill re-sends a report question to the LIVE agent, scoped to
    // the report's own database. forceLiveAgent skips the report-context branch for this one
    // turn; opts.scopeDatabases pins the scope (the report's single database).
    const liveAgent = opts.forceLiveAgent === true
    // A hand-off pill (handleHandoffPick) supplies opts.investigationContext to discuss the
    // investigation in THIS chat — even a plain live chat (chatMode 'agent') where it just
    // ran inline and was never put in context — so an explicitly supplied context grounds
    // the turn exactly as the investigation banner's Discussion toggle would. Without this
    // the pill's turn fell through to the live agent ungrounded ("I have no investigation
    // report to summarize"). Otherwise ground only when the Discussion toggle is on.
    const investigationDiscussion = !liveAgent && !!activeInvestigationContext
      && (!!opts.investigationContext || chatMode === 'discussion')
    if (!liveAgent && chatMode === 'discussion' && reportContext?.analysis) {
      const ctx = reportContext.analysis
      const reportInfo = `[REPORT CONTEXT] The user is viewing a health report for "${ctx.display_name || reportContext.meta?.instance_id}" from ${ctx.report_date || reportContext.meta?.date}.\n` +
        `Health Score: ${ctx.health_score}\n` +
        `Summary: ${ctx.summary}\n` +
        `Findings: ${JSON.stringify(ctx.findings || [])}\n` +
        `Metrics: ${JSON.stringify(ctx.metrics || {})}\n` +
        `Recommendations: ${JSON.stringify(ctx.recommendations || [])}\n` +
        reportSectionsBlock(ctx.sections) +
        `Answer ONLY from the report content above. This is a report discussion with no ` +
        `live database connection: do NOT call any live database tool, and do NOT suggest ` +
        `or offer to start an investigation. If the user asks something the report does ` +
        `not cover or asks for the current/live state, say plainly that this chat is about ` +
        `the saved report and that a live check needs the database opened in a new chat ` +
        `(or this report closed first).\n` +
        `When — and only when — your answer tells the user that a live or current check is ` +
        `needed, end your reply with the exact marker ${LIVE_MARKER} on its own line, with ` +
        `nothing after it. Never use the marker for an answer the report fully covers.\n` +
        `[END REPORT CONTEXT]\n\nUser question about this report: ${message}`
      prompt = reportInfo
      addTrace({ type: 'tool', label: 'Report context injected', detail: { report_date: ctx.report_date, health_score: ctx.health_score, findings_count: (ctx.findings || []).length } })
    } else if (investigationDiscussion) {
      // Investigation context — "Chat about this" from an investigation, or a hand-off pill
      const inv = activeInvestigationContext
      const invInfo = `[INVESTIGATION CONTEXT] The user is viewing investigation "${inv.title || inv.trigger}".\n` +
        `Status: ${inv.status}\n` +
        `Findings: ${JSON.stringify(inv.findings || [])}\n` +
        `Summary: ${inv.summary || 'N/A'}\n` +
        `Timeline: ${(inv.timeline || []).map(t => `${t.step}: ${t.summary || t.status}`).join('; ')}\n` +
        `Answer ONLY from this investigation's content above. This is an investigation ` +
        `discussion with no live database connection: do NOT call any live database tool, ` +
        `and do NOT suggest or offer to start another investigation. If the user asks ` +
        `something this investigation does not cover or asks for the current/live state, ` +
        `say plainly that this chat is about the saved investigation and that a live check ` +
        `needs the database opened in a new chat.\n` +
        `When — and only when — your answer tells the user that a live or current check is ` +
        `needed, end your reply with the exact marker ${LIVE_MARKER} on its own line, with ` +
        `nothing after it. Never use the marker for an answer the investigation fully covers.\n` +
        `[END INVESTIGATION CONTEXT]\n\nUser follow-up question: ${message}`
      prompt = invInfo
      addTrace({ type: 'tool', label: 'Investigation context injected', detail: { investigation: inv.title, findings: (inv.findings || []).length } })
    } else {
      // Structured scope: send the selected databases by id (an empty list means
      // "all configured databases in the space") and let the backend build the
      // [DATABASE SCOPE] block, including the role-focus paragraph. The backend
      // assembles this block from the registry after verifying each id is in the
      // space, so the credential pointer for operators never has to be in the browser.
      // A live-agent hand-off pins the scope to the report's own database (opts.scopeDatabases);
      // otherwise the picker's selection, else all configured databases.
      const activeScope = opts.scopeDatabases !== undefined ? opts.scopeDatabases : databaseScope
      const effectiveScope = activeScope?.length > 0 ? activeScope : (allDatabases || [])
      if (effectiveScope.length > 0) {
        scopeDatabaseIds = activeScope?.length > 0 ? activeScope.map(d => d.instance_id) : []
        const scopeLabel = activeScope?.length > 0
          ? `Selected: ${activeScope.map(d => d.display_name).join(', ')}`
          : `All databases (${effectiveScope.length})`
        const focusedInstance = activeRoleFocus.startsWith('instance:') ? activeRoleFocus.slice('instance:'.length) : ''
        const focusLabel = focusedInstance ? `reader ${focusedInstance}` : activeRoleFocus === 'reader' ? 'readers' : activeRoleFocus === 'writer' ? 'writer' : ''
        addTrace({ type: 'tool', label: `Scope: ${scopeLabel}${focusLabel ? ` · focus: ${focusLabel}` : ''}`, detail: { databases: effectiveScope.map(d => d.display_name), role_focus: activeRoleFocus || 'entire cluster' } })
      }
    }
    // Report/investigation discussion stays inside its context, so its follow-up pills
    // come from the report/investigation the client already holds — grounded in its own
    // findings, never a live check or a "start investigation" (the model's generic
    // followups are suppressed server-side for these turns). null in ordinary chat, where
    // the server's suggestions apply.
    const contextFollowups = liveAgent
      ? null
      : (chatMode === 'discussion' && reportContext?.analysis)
        ? generateReportFollowups(reportContext.analysis)
        : (investigationDiscussion
          ? generateInvestigationFollowups(activeInvestigationContext)
          : null)

    const orderedBlocks = []
    let currentTextBuffer = ''
    let completed = false
    // Set when a report-mode answer ends with the live-data marker: the committed message
    // carries liveDataNeeded + the question, and a "Ask the live agent" pill is shown.
    let liveNeeded = false
    const liveQuestion = message
    // The live-data marker mechanism applies to BOTH context discussions (report and
    // investigation): each is grounded with no live connection, so an answer that needs
    // live data ends with the marker, which the client strips and turns into a hand-off
    // button. inInvestigationMode mirrors the else-if branch above (report branch not taken).
    const inReportMode = !liveAgent && chatMode === 'discussion' && !!reportContext?.analysis
    const inInvestigationMode = investigationDiscussion && !inReportMode
    const inContextMode = inReportMode || inInvestigationMode
    // Follow-up questions arrive as a `suggestions` event, normally AFTER
    // `result`; hold them here and attach to whichever side of the commit they
    // land on.
    let pendingFollowups = null
    const attachFollowups = (items, kind) => setMessages(prev => {
      const copy = [...prev]
      const last = copy[copy.length - 1]
      if (last?.role === 'assistant') copy[copy.length - 1] = { ...last, followups: items, followupsKind: kind }
      return copy
    })
    // Advisory destructive-SQL cautions arrive on a `caution` event, like suggestions,
    // normally just after `result`; hold them and attach to the committed assistant message.
    let pendingCautions = null
    const attachCautions = (items) => setMessages(prev => {
      const copy = [...prev]
      const last = copy[copy.length - 1]
      if (last?.role === 'assistant') copy[copy.length - 1] = { ...last, cautions: items }
      return copy
    })

    // Specialist narration (sub_text) streams as many small deltas. Buffer per
    // agent and flush ONE "reasoning" trace entry per burst (before that agent's
    // next tool call, and when control returns to the parent) so the trace shows
    // WHY a tool ran without drowning in token-level entries.
    const reasoningBuf = {}
    const flushReasoning = (agent) => {
      const t = (reasoningBuf[agent] || '').trim()
      if (t) addTrace({ type: 'reasoning', agent, label: `${agent} reasoning`, detail: t })
      reasoningBuf[agent] = ''
    }
    const flushAllReasoning = () => Object.keys(reasoningBuf).forEach(flushReasoning)

    try {
      await streamMessage(backendUrl, prompt, accessToken, sessionId, (event) => {
        if (event.type === 'text') {
          currentTextBuffer += event.content
          // Remove any thinking indicators when text starts flowing
          while (orderedBlocks.length > 0 && orderedBlocks[orderedBlocks.length - 1].type === 'thinking') {
            orderedBlocks.pop()
          }
          // In report mode, strip the live-data marker (and any trailing partial) from the
          // shown text; it never reaches the screen. The marker still drives the pill.
          let displayBuffer = currentTextBuffer
          if (inContextMode) {
            const { text, needed } = splitLiveMarker(currentTextBuffer)
            displayBuffer = text
            if (needed) liveNeeded = true
          }
          const lastBlock = orderedBlocks[orderedBlocks.length - 1]
          if (lastBlock?.type === 'text') lastBlock.content = displayBuffer
          else orderedBlocks.push({ type: 'text', content: displayBuffer })
          setCurrentMessage(prev => ({ ...prev, contentBlocks: [...orderedBlocks] }))
        } else if (event.type === 'tool_start') {
          currentTextBuffer = ''
          const subAgentLabels = {
            'postgres_agent': 'PostgreSQL Agent',
            'observability_agent': 'Observability Agent',
          }
          if (subAgentLabels[event.name]) {
            // Aggregate multiple calls to the same agent into one thinking block
            const existingThinking = orderedBlocks.find(b => b.type === 'thinking' && b.agentName === event.name)
            if (existingThinking) {
              existingThinking.count = (existingThinking.count || 1) + 1
              existingThinking.toolUseIds = [...(existingThinking.toolUseIds || [existingThinking.toolUseId]), event.toolUseId]
            } else {
              orderedBlocks.push({ type: 'thinking', agent: subAgentLabels[event.name], agentName: event.name, toolUseId: event.toolUseId, toolUseIds: [event.toolUseId], count: 1 })
            }
          } else if (event.name !== 'recommend_investigation') {
            // recommend_investigation isn't a real "tool call" to show — it
            // renders as a suggestion pill on the 'tool' event instead.
            orderedBlocks.push({ type: 'tool_mini', name: event.name, toolUseId: event.toolUseId })
          }
          setCurrentMessage(prev => ({ ...prev, contentBlocks: [...orderedBlocks] }))
          // Skill activation + investigation suggestions carry no name/args yet
          // at start — trace them on the 'tool' event (which has the input).
          if (!isSkillActivation(event.name) && event.name !== 'recommend_investigation') {
            addTrace({ type: 'tool_start', toolUseId: event.toolUseId, label: `Tool call: ${event.name}`, detail: { toolUseId: event.toolUseId, name: event.name } })
          }
        } else if (event.type === 'tool') {
          if (isSkillActivation(event.name)) {
            // Agent loaded a built-in skill — upgrade its mini tag and record a
            // distinct "skill" trace entry instead of a generic tool call.
            const skillName = skillNameFromInput(event.input) || event.name
            const blk = orderedBlocks.find(b => b.type === 'tool_mini' && b.toolUseId === event.toolUseId)
            if (blk) {
              blk.isSkill = true
              blk.skillName = skillName
              setCurrentMessage(prev => ({ ...prev, contentBlocks: [...orderedBlocks] }))
            }
            addTrace({ type: 'skill', label: `Skill loaded: ${skillName}`, detail: { name: event.name, input: event.input } })
          } else if (event.name === 'recommend_investigation') {
            // Orchestrator judged this incident-shaped — surface a "Start
            // investigation" pill (the user decides; chat stays lightweight).
            const title = event.input?.title || 'Investigation'
            const rationale = event.input?.rationale || ''
            // Capture THIS turn's database scope on the suggestion so the card can
            // pre-fill the database it will investigate (what the user was discussing),
            // visible and changeable before they start — rather than silently using
            // whatever the picker happens to hold.
            const scopeDbs = (databaseScope?.length > 0 ? databaseScope : (allDatabases || []))
              .map(d => ({ instance_id: d.instance_id, display_name: d.display_name }))
            orderedBlocks.push({ type: 'investigation_suggestion', title, rationale, toolUseId: event.toolUseId, scopeDbs })
            setCurrentMessage(prev => ({ ...prev, contentBlocks: [...orderedBlocks] }))
            addTrace({ type: 'tool', label: `Suggested investigation: ${title}`, detail: event.input })
          } else {
            // Update the mini tag or thinking block — don't add a new block
            addTrace({ type: 'tool', toolUseId: event.toolUseId, label: `Tool: ${event.name} (${Object.keys(event.input || {}).length} args)`, detail: { name: event.name, input: event.input } })
          }
        } else if (event.type === 'tool_result') {
          // Decrement thinking block count, remove when all done
          const thinkingIdx = orderedBlocks.findIndex(b =>
            b.type === 'thinking' && b.toolUseIds?.includes(event.toolUseId)
          )
          if (thinkingIdx !== -1) {
            const block = orderedBlocks[thinkingIdx]
            block.completed = (block.completed || 0) + 1
            if (block.completed >= block.count) {
              orderedBlocks.splice(thinkingIdx, 1)
            }
          }
          setCurrentMessage(prev => ({ ...prev, contentBlocks: [...orderedBlocks] }))
          // A tool returned — control is back with the caller, so flush any
          // trailing specialist reasoning before recording the result.
          flushAllReasoning()
          addTrace({ type: 'tool_result', toolUseId: event.toolUseId, status: event.status, label: `Result${event.name ? ` — ${event.name}` : ''}: ${event.status} (${(event.content || '').length} chars)`, detail: event.content })
        } else if (event.type === 'sub_tool') {
          // Update thinking indicator with current sub-agent tool name
          const thinkingBlock = orderedBlocks.find(b => b.type === 'thinking')
          if (thinkingBlock) {
            thinkingBlock.currentTool = event.name
            setCurrentMessage(prev => ({ ...prev, contentBlocks: [...orderedBlocks] }))
          }
          // Flush the reasoning that led up to THIS tool call, so it renders
          // just above the call in the trace.
          flushReasoning(event.agent)
          addTrace({ type: 'tool', toolUseId: event.toolUseId, agent: event.agent, label: `↳ ${event.agent}: ${event.name} (${Object.keys(event.input || {}).length} args)`, detail: { agent: event.agent, name: event.name, input: event.input } })
        } else if (event.type === 'sub_tool_result') {
          // Output of a sub-agent (specialist) tool — surface it so the trace shows
          // each nested tool's returned values, not just its inputs.
          addTrace({ type: 'tool_result', toolUseId: event.toolUseId, agent: event.agent, status: event.status, label: `↳ ${event.agent}: ${event.name ? `${event.name} → ` : ''}result (${event.status}, ${(event.content || '').length} chars)`, detail: event.content })
        } else if (event.type === 'sub_text') {
          // Specialist narration — buffer it; flushed as one "reasoning" entry
          // per burst (see flushReasoning). This is the "why" behind the specialist's work.
          if (event.content) reasoningBuf[event.agent] = (reasoningBuf[event.agent] || '') + event.content
        } else if (event.type === 'interrupt') {
          setPendingInterrupt(event)
          orderedBlocks.push({ type: 'interrupt', interrupt: event })
          setCurrentMessage(prev => ({ ...prev, contentBlocks: [...orderedBlocks] }))
          addTrace({ type: 'tool', label: `Interrupt: ${event.name}`, detail: event })
        } else if (event.type === 'result') {
          completed = true
          flushAllReasoning()
          // In report/investigation context, attach the context-grounded followups; in
          // ordinary chat, attach whatever the server suggested.
          const commitFollowups = contextFollowups
            ? { items: contextFollowups, kind: 'questions' }
            : pendingFollowups
          // Strip the live-data marker from the committed text; flag the message so a
          // "Ask the live agent" pill can offer to re-run the SAME question live.
          const marker = inContextMode ? splitLiveMarker(currentTextBuffer) : { text: currentTextBuffer, needed: false }
          const liveTurn = inContextMode && (liveNeeded || marker.needed)
          setMessages(prev => [...prev, {
            ...assistantMessage, contentBlocks: [...orderedBlocks], content: marker.text,
            ...(commitFollowups ? { followups: commitFollowups.items, followupsKind: commitFollowups.kind } : {}),
            ...(pendingCautions ? { cautions: pendingCautions } : {}),
            ...(liveTurn ? { liveDataNeeded: true, liveQuestion } : {}),
          }])
          setCurrentMessage(null)
          addTrace({ type: 'result', label: 'Response complete', detail: marker.text })
        } else if (event.type === 'suggestions' && !contextFollowups) {
          // Report/investigation discussion ignores the server's generic suggestions and
          // uses its own report-grounded followups instead (attached on result), so this
          // branch only runs in ordinary chat.
          const items = (event.items || []).map(s => String(s).trim()).filter(Boolean).slice(0, 4)
          // `kind` says what the pills are: follow-up questions the operator might ask
          // next, or — after a clarifying turn — replies to the agent's question.
          const kind = event.kind === 'replies' ? 'replies' : 'questions'
          if (items.length) {
            if (completed) attachFollowups(items, kind)
            else pendingFollowups = { items, kind }
            addTrace({ type: 'tool', label: `${kind === 'replies' ? 'Replies' : 'Follow-ups'} suggested (${items.length})`, detail: items })
          }
        } else if (event.type === 'caution') {
          // Advisory only: destructive SQL the answer presents as runnable. Attached beside
          // the message like followups; it annotates, never blocks or edits the answer.
          const items = (event.items || []).filter(c => c && c.message)
          if (items.length) {
            if (completed) attachCautions(items)
            else pendingCautions = items
            addTrace({ type: 'tool', label: `Caution: destructive SQL (${items.length})`, detail: items })
          }
        } else if (event.type === 'model') {
          // The resolved model actually used for this turn (not necessarily the picked
          // one — the cascade may fall through a disabled default). One per turn.
          addTrace({ type: 'model', label: modelTraceLabel(event), detail: modelTraceDetail(event) })
        } else if (event.type === 'error') {
          // A mid-stream execution error (e.g. the model is in the catalog but not granted
          // Bedrock access, or a tool threw). Without committing a message the turn would
          // end with `completed` false and no visible reply — the chat would look like it
          // simply did not answer. Terminate the turn and commit a visible failed message,
          // keeping any partial content above it. Mirrors the rejected-fetch catch path.
          completed = true
          flushAllReasoning()
          addTrace({ type: 'error', label: `Error: ${event.error}`, detail: event.error })
          const failNote = `The agent couldn't finish: ${event.error || 'unknown error'}. Try again or pick another model.`
          const failText = currentTextBuffer ? `${currentTextBuffer}\n\n${failNote}` : failNote
          setMessages(prev => [...prev, {
            ...assistantMessage, contentBlocks: [...orderedBlocks], content: failText, failed: true,
          }])
          setCurrentMessage(null)
        }
      }, getValidAccessToken, { modelId: selectedModel, spaceId, roleFocus: activeRoleFocus, databaseIds: scopeDatabaseIds, userText: message })
      if (!completed) {
        // Stream ended without a final 'result' (user pressed Stop) — keep
        // whatever streamed so far so the partial answer isn't lost.
        if (currentTextBuffer || orderedBlocks.length > 0) {
          setMessages(prev => [...prev, { ...assistantMessage, contentBlocks: [...orderedBlocks], content: currentTextBuffer }])
        }
        setCurrentMessage(null)
      }
    } catch (err) {
      setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${err.message}`, timestamp: new Date().toISOString() }])
      setCurrentMessage(null)
      addTrace({ type: 'error', label: `Error: ${err.message}`, detail: err.message })
    }
  }

  const handleInterruptResponse = async (interruptId, type) => {
    setRespondedInterrupts(prev => new Map(prev).set(interruptId, type))
    addTrace({ type: 'user', label: `Interrupt response: ${type}` })
    let textBuffer = ''
    try {
      await respondToInterrupt(backendUrl, interruptId, type === 'approved' ? 'approve' : 'reject', accessToken, sessionId, (event) => {
        if (event.type === 'text') {
          textBuffer += event.content
          setCurrentMessage(prev => {
            const blocks = [...(prev?.contentBlocks || [])]
            const last = blocks[blocks.length - 1]
            if (last?.type === 'text') blocks[blocks.length - 1] = { ...last, content: textBuffer }
            else blocks.push({ type: 'text', content: textBuffer })
            return { ...prev, contentBlocks: blocks }
          })
        } else if (event.type === 'result') {
          commitCurrentMessage()
          setPendingInterrupt(null)
        }
      }, getValidAccessToken, { modelId: selectedModel, spaceId, roleFocus: activeRoleFocus })
    } catch { setPendingInterrupt(null) }
  }

  // The record's OWN database(s) a live hand-off is scoped to: the report's single database,
  // or the investigation's database(s). Never the picker or the whole space.
  const reportHandoffDb = reportContext?.analysis ? reportDatabaseScope(reportContext, allDatabases) : null
  const liveHandoffDbs = reportContext?.analysis
    ? (reportHandoffDb ? [reportHandoffDb] : [])
    : investigationDatabaseScope(investigationContext, allDatabases)
  const liveHandoffLabel = liveHandoffDbs.length === 1
    ? liveHandoffDbs[0].display_name
    : liveHandoffDbs.length > 1 ? `these ${liveHandoffDbs.length} databases` : 'this database'
  // Switch a report OR investigation discussion to the live agent, pinning the scope to the
  // record's own database(s) so the live turn covers exactly what was being discussed. The
  // banner and its toggle stay (the user can switch back to Discussion); the context is NOT
  // cleared, so both contexts behave identically.
  const goLive = () => {
    setChatMode('agent')
    onScopeChange?.(liveHandoffDbs)
  }
  // "Ask the live agent about <db>" button under an answer: go live (as above) and re-send
  // the SAME question against the record's database(s).
  const askLiveAgent = async (question) => {
    goLive()
    await handleSend(question, { forceLiveAgent: true, scopeDatabases: liveHandoffDbs })
  }

  const handleDownloadChat = () => {
    if (messages.length === 0) return
    const title = reportLabel
      ? `Chat — ${reportLabel}`
      : (investigationContext?.title || investigationContext?.trigger)
        ? `Chat — ${investigationContext.title || investigationContext.trigger}`
        : 'Chat transcript'
    const md = buildTranscript(messages, userName, title)
    const blob = new Blob([md], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `chat-${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  const isEmpty = messages.length === 0 && !currentMessage
  const suggestions = (reportContext?.analysis && chatMode === 'discussion')
    ? generateReportSuggestions(reportContext.analysis).slice(0, 6)
    : HOME_SUGGESTIONS
  const lastMessage = messages[messages.length - 1]
  // The most recent thing the user actually asked. Used as the fallback question for the
  // live-agent handoff when the model did not emit an explicit liveQuestion marker.
  const lastUserText = [...messages].reverse().find((m) => m.role === 'user')?.content || ''
  const followups = (!currentMessage && lastMessage?.role === 'assistant') ? lastMessage.followups : null
  const cautions = (!currentMessage && lastMessage?.role === 'assistant') ? lastMessage.cautions : null

  // The database-scope picker is offered only in a free live chat: either plain chat with no
  // context, or a report/investigation discussion toggled to Live Agent (where the user may
  // widen the pinned scope). It is hidden in grounded Discussion mode, where the context, not
  // the picker, supplies the scope.
  const scopeApplies = !((reportContext?.analysis || investigationContext) && chatMode === 'discussion')
  // Report and investigation discussion stay INSIDE their context: no live run can be
  // launched from here (rule-safe design). "Start investigation" is therefore never
  // offered on a context turn — the model is also told not to suggest it (see the
  // prompt text) — so a report/investigation chat cannot spawn a live run. In ordinary
  // chat the suggestion stays available.
  const inContext = !scopeApplies
  const startInvestigation = inContext ? undefined : onStartInvestigation
  const composerBusy = isStreaming || pendingInterrupt !== null || !!investigating

  // Pills on the hand-off turn: put the finished investigation in context (the same
  // banner "Chat about this" shows) and ask about it in one click.
  const HANDOFF_QUESTIONS = {
    'Summarize the investigation here': 'Summarize this investigation: the key findings, the root cause, and the recommended actions.',
    'What should I fix first?': 'Based on this investigation, what should I fix first, and how?',
  }
  const handleHandoffPick = async (inv, label) => {
    const question = HANDOFF_QUESTIONS[label]
    if (!question || !inv?.investigation_id) return
    let full = inv
    try { full = await api.get(`/api/spaces/${spaceId}/investigations/${inv.investigation_id}`) } catch {}
    onSetInvestigationContext?.(full)
    await handleSend(question, { investigationContext: full })
  }
  const scopeControl = scopeApplies ? (
    <ScopeSelector api={api} databases={allDatabases || []} selected={databaseScope || []} onChange={onScopeChange}
      onRefresh={onRefreshDatabases} roleFocus={roleFocus} onRoleFocusChange={onRoleFocusChange} />
  ) : null
  const modelPicker = <ModelPicker api={api} getValidAccessToken={getValidAccessToken} accessToken={accessToken} value={selectedModel} onChange={setSelectedModel} />

  // Discussion / Live Agent toggle, shared by the report and investigation banners so both
  // context chats offer the identical switch. Discussion is grounded in the record; Live
  // Agent runs the live database agent, scoped to the record's own database(s).
  const modeToggle = (discussionLabel) => (
    <div className="flex items-center gap-1 mt-1.5">
      <button onClick={() => setChatMode('discussion')}
        className={`flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium transition-colors ${
          chatMode === 'discussion' ? 'bg-primary text-primary-fg' : 'bg-surface text-fg-3 hover:text-fg-2 border border-line'
        }`}>
        <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
        {discussionLabel}
      </button>
      <button onClick={goLive}
        className={`flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium transition-colors ${
          chatMode === 'agent' ? 'bg-primary text-primary-fg' : 'bg-surface text-fg-3 hover:text-fg-2 border border-line'
        }`}>
        <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
        </svg>
        Live Agent
      </button>
    </div>
  )

  return (
    <div className="flex flex-col h-full">
      {/* Report context banner with mode toggle */}
      {reportContext?.analysis && (
        <div className="flex-shrink-0 px-3 py-1.5 bg-primary-subtle border-b border-primary-line">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 min-w-0">
              <svg className="w-3.5 h-3.5 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              <span className="text-[11px] text-primary font-medium truncate">
                {reportLabel}
              </span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-bold ${
                reportContext.analysis.health_score === 'critical' ? 'bg-err-bg-2 text-err' :
                reportContext.analysis.health_score === 'warning' ? 'bg-warn-bg-2 text-warn-mid' :
                'bg-ok-bg-2 text-ok'
              }`}>{(reportContext.analysis.health_score || '').toUpperCase()}</span>
            </div>
          </div>
          {modeToggle('Report Discussion')}
        </div>
      )}

      {/* Investigation context banner */}
      {investigationContext && !reportContext?.analysis && (
        <div className="flex-shrink-0 px-3 py-1.5 bg-warn-bg border-b border-warn-border">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 min-w-0">
              <svg className="w-3.5 h-3.5 text-warn flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              <span className="text-[11px] text-warn-mid font-medium truncate">
                Investigation: {investigationContext.title || investigationContext.trigger}
              </span>
              {investigationContext.severity && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-bold ${
                  investigationContext.severity === 'critical' ? 'bg-err-bg-2 text-err' :
                  investigationContext.severity === 'warning' ? 'bg-warn-bg-2 text-warn-mid' :
                  'bg-info-bg-2 text-info'
                }`}>{investigationContext.severity.toUpperCase()}</span>
              )}
            </div>
            <button onClick={() => onClearInvestigationContext?.()}
              className="text-[10px] text-fg-muted hover:text-fg-2">✕</button>
          </div>
          {modeToggle('Investigation Discussion')}
        </div>
      )}

      {isEmpty ? (
        /* Home / empty state — hero: sparkle + gradient greeting,
           starter pills, the composer, then the "briefs" that need attention.
           `safe center` keeps the composer reachable when the briefs overflow. */
        <div className="flex-1 overflow-y-auto">
          <div className="min-h-full flex flex-col items-center px-4 py-10" style={{ justifyContent: 'safe center' }}>
            <div className="w-full max-w-[720px] flex flex-col items-center">
              {isHome ? (
                <div className="flex flex-wrap items-center justify-center gap-x-3 gap-y-2 text-center mb-8">
                  <SparkleMark className="w-8 h-8 flex-shrink-0" />
                  <h1 className="greeting-gradient text-[28px] sm:text-[32px]">
                    {greeting}{userName ? `, ${userName}` : ''}! How can I help?
                  </h1>
                </div>
              ) : (
                <p className="text-fg-2 text-sm text-center mb-6 font-medium">
                  {reportContext?.analysis && chatMode === 'discussion'
                    ? `Discussing report: ${reportLabel}`
                    : 'Ask about database health, run queries, or get optimization tips'}
                </p>
              )}

              {/* Starter prompts — neutral pills */}
              <div className="suggestion-pills flex flex-wrap justify-center gap-2 mb-6">
                {suggestions.map((q, i) => (
                  <button key={i} type="button" onClick={() => handleSend(q)} disabled={composerBusy} className="suggestion-pill neutral">
                    {q}
                  </button>
                ))}
              </div>

              <div className="w-full">
                <ChatInput onSend={handleSend} disabled={composerBusy} large scopeControl={scopeControl} modelPicker={modelPicker} />
              </div>

              {isHome && (
                <HomeBriefs api={api} spaceId={spaceId} databases={allDatabases}
                  onAsk={handleSend} onOpenInvestigation={onOpenInvestigation} onOpenPanel={onOpenPanel} />
              )}
            </div>
          </div>
        </div>
      ) : (
        <>
          {/* Conversation toolbar — export the whole chat as Markdown. */}
          <div className="flex-shrink-0 flex items-center justify-end px-3 py-1 border-b border-line-2">
            <button onClick={handleDownloadChat} disabled={messages.length === 0}
              title="Download the entire conversation as Markdown"
              className="flex items-center gap-1.5 px-2 py-1 text-[11px] font-medium text-fg-3 hover:text-primary rounded-md hover:bg-surface-3 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
              <DownloadIcon className="w-3.5 h-3.5" />
              Download chat
            </button>
          </div>
          <div ref={chatContainerRef} className="flex-1 overflow-y-auto px-4 py-4">
            {/* Messages sit in the same 720px column as the composer. */}
            <div className="w-full max-w-[720px] mx-auto space-y-4">
              {messages.map((msg, i) => (
                <ChatMessage key={i} message={msg} isStreaming={false}
                  onApprove={(id) => handleInterruptResponse(id, 'approved')}
                  onReject={(id) => handleInterruptResponse(id, 'rejected')}
                  onStartInvestigation={startInvestigation}
                  onOpenInvestigation={onOpenInvestigation}
                  databases={allDatabases}
                  onHandoffPick={handleHandoffPick} handoffDisabled={composerBusy}
                  linkedInvestigations={linkedInvestigations}
                  respondedInterrupts={respondedInterrupts} userName={userName} />
              ))}
              {currentMessage && (
                <ChatMessage message={currentMessage} isStreaming={isStreaming}
                  onApprove={(id) => handleInterruptResponse(id, 'approved')}
                  onReject={(id) => handleInterruptResponse(id, 'rejected')}
                  onStartInvestigation={startInvestigation}
                  onOpenInvestigation={onOpenInvestigation}
                  databases={allDatabases}
                  onHandoffPick={handleHandoffPick} handoffDisabled={composerBusy}
                  linkedInvestigations={linkedInvestigations}
                  respondedInterrupts={respondedInterrupts} userName={userName} />
              )}
              {isStreaming && currentMessage && (!currentMessage.contentBlocks || currentMessage.contentBlocks.length === 0) && (
                <div className="flex items-center gap-2 px-3 py-2 text-xs text-fg-3">
                  <div className="flex gap-1">
                    <div className="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style={{ animationDelay: '0ms' }} />
                    <div className="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style={{ animationDelay: '150ms' }} />
                    <div className="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                  <span>Agent is thinking...</span>
                </div>
              )}
              {/* "Ask the live agent" — in EITHER context discussion (report or
                  investigation), offered after any assistant answer. Grounded discussion
                  works off a stored report/record, so the live agent is always a valid next
                  step; relying on the model to flag liveDataNeeded missed common asks (e.g.
                  "start a new investigation") where the marker was never emitted. Re-runs the
                  user's question against the live agent, scoped to the record's own
                  database(s). Prefers the model's explicit liveQuestion when present, else the
                  last thing the user asked. Same primary affordance in both places. */}
              {!isStreaming && !investigating && inContext
                && lastMessage?.role === 'assistant' && (
                <div className="flex justify-start">
                  <button type="button" disabled={composerBusy}
                    onClick={() => askLiveAgent(lastMessage.liveQuestion || lastUserText)}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-primary-fg text-[12px] font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    title="Ask the live agent this question against the record's database">
                    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
                    </svg>
                    Ask the live agent about {liveHandoffLabel}
                  </button>
                </div>
              )}
              {/* Advisory caution under the latest answer when it presents a destructive
                  statement as runnable. Annotates only; the answer itself is unchanged. */}
              {cautions && !isStreaming && (
                <div className="mt-3 space-y-2">
                  {cautions.map((c, i) => (
                    <div key={i} className="flex items-start gap-2 rounded-md border border-warn-border bg-warn-bg px-3 py-2 text-sm text-warn-text">
                      <span aria-hidden="true" className="mt-0.5">⚠</span>
                      <span><span className="sr-only">Caution: </span>{c.message}</span>
                    </div>
                  ))}
                </div>
              )}
              {/* Follow-up questions under the latest answer (none while an
                  investigation this chat started is still running). */}
              {followups && !isStreaming && !investigating && (
                <FollowUpPills items={followups} kind={lastMessage.followupsKind} onPick={handleSend} disabled={composerBusy} />
              )}
              <div ref={messagesEndRef} />
            </div>
          </div>
          <div className="flex-shrink-0 px-4 pb-1">
            <div className="w-full max-w-[720px] mx-auto">
              {isStreaming && (
                <div className="flex items-center justify-between gap-2 px-1 pb-2">
                  <span className="text-xs text-fg-3">Agent is working…</span>
                  <button onClick={() => onStop?.()} title="Stop generating"
                    className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-fg-2 border border-line-strong rounded-md hover:bg-surface-3 transition-colors">
                    <span className="w-2.5 h-2.5 rounded-[2px] bg-err" />
                    Stop
                  </button>
                </div>
              )}
              {investigating && !isStreaming && (
                <div className="flex items-center justify-between gap-2 px-1 pb-2" role="status">
                  <span className="flex items-center gap-2 text-xs text-fg-3 min-w-0">
                    <span className="w-3 h-3 border-2 border-primary border-t-transparent rounded-full animate-spin flex-shrink-0" aria-hidden />
                    <span className="truncate">Investigation in progress — {investigating.title}. This chat resumes when it finishes; other chats stay open.</span>
                  </span>
                  <button onClick={() => onOpenInvestigation?.(investigating.investigation_id, 'running')}
                    className="flex-shrink-0 px-2.5 py-1 text-xs font-medium text-primary border border-primary-line rounded-md hover:bg-primary-subtle transition-colors">
                    Open investigation
                  </button>
                </div>
              )}
              <ChatInput onSend={handleSend} disabled={composerBusy} scopeControl={scopeControl} modelPicker={modelPicker}
                busyLabel={investigating && !isStreaming ? 'Waiting for the investigation to finish…' : undefined} />
            </div>
          </div>
        </>
      )}

      {/* Standing caution — AI output can be imperfect; review before acting.
          One quiet muted line. */}
      <p className="chat-footer flex-shrink-0">{CHAT_DISCLAIMER}</p>
    </div>
  )
}
