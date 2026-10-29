// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback, useRef } from 'react'
import useAuth from './hooks/useAuth'
import useStreaming from './hooks/useStreaming'
import useApi from './hooks/useApi'
import useTheme from './hooks/useTheme'
import useSessions from './hooks/useSessions'
import Dashboard from './components/Dashboard'
import Investigations from './components/Investigations'
import Topology from './components/Topology'
import Trends from './components/Trends'
import Knowledge from './components/Knowledge'
import Chat from './components/Chat'
import ReportPanel from './components/ReportPanel'
import ReportComparison from './components/ReportComparison'
import TracePanel from './components/TracePanel'
import LeftRail from './components/LeftRail'
import ChatLauncher from './components/ChatLauncher'
import { PanelLeftClose } from 'lucide-react'
import { NotificationTray } from './components/Notifications'
import LoginPage from './components/LoginPage'
import ThemeMenu from './components/ThemeMenu'
import ProfileModal from './components/ProfileModal'
import { config } from './config'

const DEPLOYED_BACKEND_URL = config.deployedBackendUrl

// Shared empty conversation for sessions with nothing loaded/streamed yet. A stable
// reference so an unknown session renders without allocating per render. Never mutated —
// every setter returns new arrays/objects. `activeInvestigation` is set while an
// investigation started from that chat is still running (see the hand-off below).
const EMPTY_CONVERSATION = { messages: [], currentMessage: null, pendingInterrupt: null, activeInvestigation: null }
const INVESTIGATION_POLL_MS = 10000

// The assistant turn appended to a chat when the investigation it started finishes:
// a line of text (so exports and plain rendering have something to say) plus a
// hand-off block the chat renders with an "Open investigation" action and pills.
function buildHandoffMessage(inv) {
  const status = inv.status === 'completed' ? 'complete' : inv.status
  const title = inv.title || 'Investigation'
  const findings = Array.isArray(inv.findings) ? inv.findings.length : 0
  const firstLine = (inv.summary || '').split('\n').map(s => s.trim()).find(s => s && !s.startsWith('#')) || ''
  const excerpt = firstLine.length > 220 ? `${firstLine.slice(0, 219).trimEnd()}…` : firstLine
  const facts = inv.status === 'completed'
    ? [inv.severity ? `${inv.severity} severity` : null, `${findings} finding${findings === 1 ? '' : 's'}`].filter(Boolean).join(', ')
    : ''
  const content = `Investigation ${status}: **${title}**${facts ? ` — ${facts}` : ''}.${excerpt ? ` ${excerpt}` : ''}`
  return {
    role: 'assistant',
    timestamp: new Date().toISOString(),
    content,
    contentBlocks: [
      { type: 'text', content },
      { type: 'investigation_handoff', investigation: {
        investigation_id: inv.investigation_id, title, status: inv.status, severity: inv.severity || null,
        findings, completed_at: inv.completed_at || null,
      } },
    ],
  }
}
const BACKEND_URL = DEPLOYED_BACKEND_URL || config.localBackendUrl

// Read space_id from URL params
const getSpaceId = () => new URLSearchParams(window.location.search).get('space_id') || ''

// Workspace nav items — each opens as a panel on the right; chat stays primary.
const NAV_ITEMS = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
  { id: 'investigations', label: 'Investigations', icon: 'investigations' },
  { id: 'trends', label: 'Trends', icon: 'trends' },
  { id: 'topology', label: 'Topology', icon: 'topology' },
]
// Reports live with their database: the Dashboard drawer's Deep scans tab lists every
// scan with its report and can compare two of them (the 'compare' panel below).
const PANEL_LABELS = {
  dashboard: 'Dashboard', investigations: 'Investigations', trends: 'Trends',
  topology: 'Topology', report: 'Report', compare: 'Compare scans', knowledge: 'Agent Capabilities',
}
const CHAT_WIDTH_KEY = 'dbops-chat-width'
const CHAT_MIN_WIDTH = 360
const CHAT_MAX_WIDTH = 720
const CHAT_DEFAULT_WIDTH = 420

export default function App() {
  const { accessToken, loading, error: authError, logout, login, needsLogin, isAuthenticated, getValidAccessToken, getValidIdToken, refreshToken, forceRefresh, challengeSession, completeNewPassword, updateName, changePassword, mfaMode, mfaSecret, mfaOtpauthUri, completeMfaSetup, submitMfaCode, forgotMode, startForgotPassword, cancelForgotPassword, forgotPassword, confirmForgotPassword } = useAuth()
  const { streamMessage, respondToInterrupt, isSessionStreaming, stopStreaming, streamingSessions } = useStreaming()
  const api = useApi(getValidIdToken, async () => (await forceRefresh()).idToken)
  const appearance = useTheme()

  // Workspace panel/tab state: several panels can be open at once.
  const [openTabs, setOpenTabs] = useState([])      // ordered list of open panel ids
  const [activeTabId, setActiveTabId] = useState(null)
  const [railCollapsed, setRailCollapsed] = useState(false)

  const [sessionId, setSessionId] = useState(() => crypto.randomUUID())
  const [spaceId, setSpaceId] = useState(getSpaceId)
  const sessions = useSessions(api, spaceId, isAuthenticated)
  // Per-session conversation state (messages, the in-progress assistant turn, and
  // any pending interrupt), OWNED BY THE APP and keyed by sessionId. The <Chat>
  // component is keyed by sessionId and unmounts when you switch chats, but a
  // stream started in a chat keeps running at this level (useStreaming) and keeps
  // writing here via session-keyed setters. Holding the conversation here (not in
  // Chat's local state) is what lets a backgrounded chat's answer keep streaming
  // in and be there when you switch back, instead of being dropped into a dead
  // component.
  const [conversations, setConversations] = useState({})

  // Apply a patch to one session's conversation. Each patch value is either a new
  // value or a functional updater (prev => next), mirroring React's setState so
  // Chat's existing `setMessages(prev => ...)` call sites work unchanged.
  const updateConversation = useCallback((sid, patch) => {
    if (!sid) return
    setConversations(prev => {
      const cur = prev[sid] || EMPTY_CONVERSATION
      const next = { ...cur }
      for (const k of Object.keys(patch)) {
        const v = patch[k]
        next[k] = typeof v === 'function' ? v(cur[k]) : v
      }
      return { ...prev, [sid]: next }
    })
  }, [])
  const setSessionMessages = useCallback((sid, u) => updateConversation(sid, { messages: u }), [updateConversation])
  const setSessionCurrentMessage = useCallback((sid, u) => updateConversation(sid, { currentMessage: u }), [updateConversation])
  const setSessionPendingInterrupt = useCallback((sid, u) => updateConversation(sid, { pendingInterrupt: u }), [updateConversation])
  // Atomically commit the in-progress assistant turn into the message list (append
  // currentMessage, then clear it) in ONE update. Kept as a dedicated op so callers
  // never have to append and clear across two setters, which on a shared atom would
  // mean calling a setter from inside another setter's updater.
  const commitSessionCurrentMessage = useCallback((sid) => {
    setConversations(prev => {
      const cur = prev[sid]
      if (!cur || !cur.currentMessage) return prev
      return { ...prev, [sid]: { ...cur, messages: [...cur.messages, cur.currentMessage], currentMessage: null } }
    })
  }, [])
  const [showProfile, setShowProfile] = useState(false)
  const [spaceName, setSpaceName] = useState('')
  const [activeReport, setActiveReport] = useState(null)
  // { instance, scan1, scan2 } — two deep scans of one database picked in the drawer.
  const [activeCompare, setActiveCompare] = useState(null)
  const [activeReportAnalysis, setActiveReportAnalysis] = useState(null)
  // Chat pane geometry: the width persists in
  // localStorage; the collapsed state is per browser session — the chat folds
  // away the first time a panel opens, and comes back via the launcher, a
  // Recents click, a new chat, or a "chat about this" hand-off.
  const [chatWidth, setChatWidth] = useState(() => {
    try {
      const n = Number(localStorage.getItem(CHAT_WIDTH_KEY))
      return n >= CHAT_MIN_WIDTH && n <= CHAT_MAX_WIDTH ? n : CHAT_DEFAULT_WIDTH
    } catch { return CHAT_DEFAULT_WIDTH }
  })
  const [chatCollapsed, setChatCollapsed] = useState(false)
  // Sessions holding a reply (or an investigation hand-off) the operator has not
  // looked at yet: a dot on the chat's row in the rail, and on the launcher when it
  // is the current chat folded away. Cleared when that chat is brought into view.
  const [unreadSessions, setUnreadSessions] = useState({})
  const markUnread = useCallback((sid) => { if (sid) setUnreadSessions(prev => (prev[sid] ? prev : { ...prev, [sid]: true })) }, [])
  const markRead = useCallback((sid) => setUnreadSessions(prev => {
    if (!prev[sid]) return prev
    const next = { ...prev }
    delete next[sid]
    return next
  }), [])
  const [chatResizing, setChatResizing] = useState(false)
  const expandChat = () => { setChatCollapsed(false); markRead(sessionId) }
  const [traces, setTraces] = useState([])
  const [databases, setDatabases] = useState([])
  const [selectedDatabases, setSelectedDatabases] = useState([])
  const [roleFocus, setRoleFocus] = useState('')  // Aurora focus: '' | 'writer' | 'reader'
  const [accessDenied, setAccessDenied] = useState(false)
  const [availableSpaces, setAvailableSpaces] = useState(null)
  const [investigationContext, setInvestigationContext] = useState(null) // for chat integration
  const [pendingInvestigation, setPendingInvestigation] = useState(null) // chat→investigation hand-off

  // Resolve space access on load. The operator app has one role, so access is purely
  // space membership (plus the space's creator), as reported by /api/my-spaces.
  //
  // Cognito bakes group membership into a token at login. A space granted during this
  // session (or one the user just created) is therefore invisible until the token is
  // re-minted. So when the list comes back empty, or the requested space is missing,
  // refresh the token ONCE and re-ask before concluding "no access" — this turns a
  // sign-out/sign-in into a transparent retry.
  useEffect(() => {
    if (!isAuthenticated) return
    let cancelled = false
    const resolve = async (retried) => {
      let r
      try { r = await api.get('/api/my-spaces') } catch { if (!cancelled) setAccessDenied(true); return }
      if (cancelled) return
      const spaces = r.spaces || []
      const wanted = spaceId ? spaces.some(s => s.space_id === spaceId) : spaces.length > 0
      if (!wanted && !retried) {
        try { await refreshToken(); return resolve(true) } catch { /* fall through to denied */ }
      }
      setAvailableSpaces(spaces)
      if (spaceId) {
        if (!wanted) setAccessDenied(true)
        const match = spaces.find(s => s.space_id === spaceId)
        if (match) setSpaceName(match.name)
      } else if (spaces.length === 1) {
        window.location.href = `${window.location.origin}?space_id=${spaces[0].space_id}`
      } else if (spaces.length === 0) {
        setAccessDenied(true)
      }
    }
    resolve(false)
    return () => { cancelled = true }
  }, [isAuthenticated])

  // Fetch the configured databases for the scope selector, from the operator app's own
  // listing: gated by space membership alone and shaped the same for every operator
  // (no role-dependent fields), so nothing here depends on who the caller also is.
  // Scoped on the SERVER by membership (member_space_ids): a database shared into this
  // space from another space carries the other space in its legacy tags.space_id, so
  // filtering on that tag client-side would hide every shared database from this space.
  const fetchDatabases = async () => {
    if (!spaceId) return
    try {
      const data = await api.get(`/api/my-spaces/${encodeURIComponent(spaceId)}/databases`)
      const configured = data.filter(d => d.status === 'configured')
      setDatabases(configured)
      setSelectedDatabases(prev => {
        if (configured.length === 1 && prev.length === 0) return [configured[0]]
        return prev
      })
    } catch {}
  }
  // Only after /api/my-spaces has confirmed this space. Two reasons: asking earlier
  // produces a spurious "operator databases listing denied" audit line on every load;
  // and keying the fetch on confirmed spaces means it waits for a token and re-runs once
  // access resolves, so the scope selector loads databases without needing a hard refresh.
  // Access resolution only completes once authenticated, so keying on it covers both.
  useEffect(() => {
    if (availableSpaces && spaceId && availableSpaces.some(s => s.space_id === spaceId)) fetchDatabases()
  }, [spaceId, availableSpaces])

  // Resize the chat pane (chat is on the LEFT, so dragging right widens it).
  // The width is written to localStorage on release, not on every move.
  const handleDragStart = (e) => {
    e.preventDefault()
    const startX = e.clientX
    const startWidth = chatWidth
    let latest = startWidth
    setChatResizing(true)
    const onMove = (ev) => {
      latest = Math.max(CHAT_MIN_WIDTH, Math.min(CHAT_MAX_WIDTH, startWidth + (ev.clientX - startX)))
      setChatWidth(latest)
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      setChatResizing(false)
      try { localStorage.setItem(CHAT_WIDTH_KEY, String(latest)) } catch {}
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  const handleNewSession = () => {
    const id = crypto.randomUUID()
    setConversations(prev => ({ ...prev, [id]: { ...EMPTY_CONVERSATION } }))
    setSessionId(id)
    expandChat()
  }

  // Deleting a chat from the rail removes it from the list and the server. If it is the
  // chat currently open, the window would otherwise keep showing, and streaming into,
  // a deleted conversation, so stop its stream, drop its in-memory state,
  // and open a fresh chat in its place.
  const handleDeleteSession = (id) => {
    sessions.remove(id)
    if (id === sessionId) {
      stopStreaming(id)
      handleNewSession()
    }
    setConversations(prev => {
      if (!prev[id]) return prev
      const next = { ...prev }
      delete next[id]
      return next
    })
  }

  // Reopen a recent chat. If we already hold this conversation in memory (it was
  // streamed or loaded earlier this browser session), switch straight to it —
  // reloading would clobber an in-progress stream or a just-arrived answer. Only
  // load past turns from the server (AgentCore memory) for a session we have
  // never held.
  const openSession = async (id) => {
    expandChat()
    markRead(id)
    if (id === sessionId) return
    if (conversations[id]) { setSessionId(id); return }
    const msgs = await sessions.loadMessages(id)
    setConversations(prev => (
      prev[id] ? prev : { ...prev, [id]: { ...EMPTY_CONVERSATION, messages: msgs || [] } }
    ))
    setSessionId(id)
  }

  // A "chat about this" hand-off from a panel must surface the chat.
  useEffect(() => { if (investigationContext) expandChat() }, [investigationContext])

  // A reply that finishes while its chat is not in view — another chat is open, or
  // the chat is folded away behind a panel — marks that chat unread. Investigation
  // streams (`inv-<id>`) belong to the Investigations panel, not to a chat.
  const chatVisible = !chatCollapsed || openTabs.length === 0
  const prevStreaming = useRef({})
  useEffect(() => {
    for (const sid of Object.keys(prevStreaming.current)) {
      if (!streamingSessions[sid] && !sid.startsWith('inv-') && (sid !== sessionId || !chatVisible)) markUnread(sid)
    }
    prevStreaming.current = streamingSessions
  }, [streamingSessions]) // eslint-disable-line react-hooks/exhaustive-deps

  // Investigation started from a chat: that chat waits (composer paused) until the
  // investigation finishes, then gets a hand-off turn — the outcome in one line, an
  // "Open investigation" action and pills to discuss it here. One list call covers
  // every waiting chat; a record that vanished (deleted) simply releases its chat.
  const watchedSessions = Object.keys(conversations).filter(sid => conversations[sid]?.activeInvestigation)
  const watchedKey = watchedSessions.join('|')
  useEffect(() => {
    if (!watchedKey || !spaceId) return
    let cancelled = false
    const poll = async () => {
      let list
      try { list = (await api.get(`/api/spaces/${spaceId}/investigations`))?.investigations || [] } catch { return }
      if (cancelled) return
      for (const sid of watchedKey.split('|')) {
        const active = conversations[sid]?.activeInvestigation
        if (!active) continue
        const inv = list.find(i => i.investigation_id === active.investigation_id)
        if (inv && inv.status === 'running') continue
        updateConversation(sid, { activeInvestigation: null })
        if (inv) {
          setSessionMessages(sid, prev => [...prev, buildHandoffMessage(inv)])
          if (sid !== sessionId || !chatVisible) markUnread(sid)
        }
      }
    }
    poll()
    const timer = setInterval(poll, INVESTIGATION_POLL_MS)
    return () => { cancelled = true; clearInterval(timer) }
  }, [watchedKey, spaceId, sessionId, chatVisible]) // eslint-disable-line react-hooks/exhaustive-deps

  // Track the chat in Recents: set a title on the first turn of a brand-new
  // session, otherwise just bump last_active.
  const handleSessionActivity = (message, isFirst) => {
    const known = sessions.sessions.some(s => s.session_id === sessionId)
    sessions.touch(sessionId, {
      space_id: spaceId || undefined,
      ...(isFirst && !known ? { title: (message || '').trim().slice(0, 80) } : {}),
    })
  }

  // Panel/tab management
  const openPanel = (id) => {
    // The first panel to open folds the chat away (it comes back via the
    // launcher); opening further panels leaves the user's choice alone.
    if (openTabs.length === 0) setChatCollapsed(true)
    setOpenTabs(prev => (prev.includes(id) ? prev : [...prev, id]))
    setActiveTabId(id)
  }
  const closePanel = (id) => {
    const remaining = openTabs.filter(t => t !== id)
    setOpenTabs(remaining)
    setActiveTabId(cur => (cur === id ? (remaining[remaining.length - 1] || null) : cur))
    if (id === 'report') { setActiveReport(null); setActiveReportAnalysis(null) }
    if (id === 'compare') setActiveCompare(null)
    if (remaining.length === 0) expandChat()
  }
  // "Chat" on a database row: scope the chat to that one database and bring the
  // chat back into view (it folds away while panels are open), so the next
  // message is about that database rather than silently widening the scope.
  const chatAboutDatabase = (db) => {
    // Accept one database (a row's "Chat") or many (the Dashboard's "Add to Scope" over
    // a multi-select). All of them go into the scope so the next turn covers every one
    // selected, not just the last.
    setSelectedDatabases(Array.isArray(db) ? db : [db])
    setChatCollapsed(false)
  }
  const openReport = (r) => { setActiveReport(r); setInvestigationContext(null); openPanel('report') }
  const openCompare = (instance, scan1, scan2) => { setActiveCompare({ instance, scan1, scan2 }); openPanel('compare') }

  // The chat holds ONE context at a time. Putting an investigation in context replaces any
  // open report (which would otherwise silently win the chat's precedence and hide the
  // investigation banner), so "most recent wins" matches what the operator just clicked.
  const putInvestigationInContext = (inv) => {
    setInvestigationContext(inv)
    if (inv && openTabs.includes('report')) closePanel('report')  // clears activeReport + analysis
  }

  // Chat → investigation hand-off: create the investigation (carrying the current
  // DB scope) and open it in the Investigations panel. Triggered by the "Start
  // investigation" pill the orchestrator can surface in chat.
  const handleStartInvestigation = async (suggestion, overrideDbs) => {
    if (!spaceId) return null
    // The suggestion card's database dropdown wins when the user picks one: the
    // investigation runs on exactly what they confirmed, not the current picker.
    // Otherwise target the record's OWN database(s) whenever a context is active, never
    // the current picker or the whole space. In report context, the report's instance; in
    // investigation context, the record's database_ids. (The pill is suppressed in
    // context, so that branch is a defensive guard — it must not widen to the wrong DBs.)
    let dbs
    if (overrideDbs?.length) {
      dbs = overrideDbs
    } else if (investigationContext?.database_ids?.length) {
      const byId = new Set(investigationContext.database_ids)
      dbs = (databases || []).filter(d => byId.has(d.instance_id))
    } else if (activeReport?.instance_id) {
      dbs = (databases || []).filter(d => d.instance_id === activeReport.instance_id)
    } else {
      dbs = selectedDatabases.length ? selectedDatabases : databases
    }
    if (!dbs.length) return null  // a context record whose ids have not loaded: do not widen
    try {
      const inv = await api.post(`/api/spaces/${spaceId}/investigations`, {
        prompt: suggestion.rationale ? `${suggestion.title} — ${suggestion.rationale}` : suggestion.title,
        database_ids: dbs.map(d => d.instance_id),
        database_names: dbs.map(d => d.display_name),
        time_range: 'Last 2 hours',
        source_session_id: sessionId,  // link this investigation back to the chat
      })
      // The chat that asked now waits for the outcome (see the hand-off poll above).
      // The Investigations panel must open: its detail view is what runs the
      // investigation, and it streams on regardless of which panel is in front.
      if (inv?.investigation_id) {
        updateConversation(sessionId, { activeInvestigation: {
          investigation_id: inv.investigation_id, title: inv.title || suggestion.title, started_at: inv.started_at || new Date().toISOString(),
        } })
      }
      setPendingInvestigation(inv)
      openPanel('investigations')
      return inv  // let the chat card show the link + status immediately
    } catch (e) {
      console.error('Failed to start investigation from chat:', e)
      return null
    }
  }

  // Open a specific investigation (from a chat suggestion-card link). A minimal
  // record is enough — InvestigationDetail fetches the full record by id; passing
  // the known status avoids re-streaming an already-completed one.
  const openInvestigationById = (investigationId, status) => {
    if (!investigationId) return
    setPendingInvestigation({ investigation_id: investigationId, status: status || 'completed' })
    openPanel('investigations')
  }

  // Render the component for a given panel id (props mirror the previous tab wiring)
  const renderPanel = (id) => {
    switch (id) {
      case 'dashboard':
        return <Dashboard api={api} spaceId={spaceId} onViewReport={openReport} onCompareScans={openCompare}
          onSelectDatabase={chatAboutDatabase} />
      case 'investigations':
        return <Investigations api={api} spaceId={spaceId} databases={databases}
          accessToken={accessToken} getValidAccessToken={getValidAccessToken}
          streamMessage={streamMessage} isSessionStreaming={isSessionStreaming}
          stopStreaming={stopStreaming} backendUrl={BACKEND_URL}
          onChatAbout={putInvestigationInContext}
          openInvestigation={pendingInvestigation}
          onOpenInvestigationConsumed={() => setPendingInvestigation(null)}
          onTrace={(entry) => setTraces(prev => [...prev, entry].slice(-1000))} />
      case 'trends':
        return <Trends api={api} spaceId={spaceId} />
      case 'topology':
        return <Topology api={api} spaceId={spaceId} />
      case 'compare':
        return activeCompare
          ? <ReportComparison api={api} scan1={activeCompare.scan1} scan2={activeCompare.scan2}
              instanceName={activeCompare.instance?.display_name} onClose={() => closePanel('compare')} onViewReport={openReport} />
          : null
      case 'knowledge':
        return <Knowledge api={api} spaceId={spaceId} />
      case 'report':
        return activeReport
          ? <ReportPanel api={api} reportMeta={activeReport} onClose={() => closePanel('report')} onAnalysisLoaded={(a) => setActiveReportAnalysis(a)} />
          : null
      default:
        return null
    }
  }

  if (loading) return (
    <div className="flex items-center justify-center min-h-screen bg-canvas">
      <div className="flex items-center gap-3">
        <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <p className="text-fg-2">Loading...</p>
      </div>
    </div>
  )

  if (!isAuthenticated && needsLogin) return <LoginPage onLogin={login} onCompleteNewPassword={completeNewPassword} showNewPassword={!!challengeSession && !mfaMode && !forgotMode} error={authError} mfaMode={mfaMode} mfaSecret={mfaSecret} mfaOtpauthUri={mfaOtpauthUri} onCompleteMfaSetup={completeMfaSetup} onSubmitMfaCode={submitMfaCode} forgotMode={forgotMode} onStartForgotPassword={startForgotPassword} onCancelForgotPassword={cancelForgotPassword} onForgotPassword={forgotPassword} onConfirmForgotPassword={confirmForgotPassword} />

  // Denied: say WHO is signed in and WHAT they can open. The admin console and the
  // operator app share one browser, so the commonest cause is opening a space the
  // signed-in account (often an admin) is simply not an operator of, while another
  // account is. Naming who and what makes that clear rather than reading as a role problem.
  if (accessDenied) {
    const deniedEmail = (() => { try { return JSON.parse(atob(localStorage.getItem('id_token')?.split('.')[1] || '')).email || '' } catch { return '' } })()
    const others = availableSpaces || []
    return (
      <div className="flex items-center justify-center min-h-screen bg-canvas">
        <div className="text-center max-w-md px-6">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-err-bg flex items-center justify-center">
            <svg className="w-8 h-8 text-err" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m0 0v2m0-2h2m-2 0H10m4-6a4 4 0 11-8 0 4 4 0 018 0z" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold text-fg mb-2">{spaceId ? 'Not an operator of this space' : 'No spaces to open'}</h2>
          <p className="text-fg-2 text-sm mb-1">
            {deniedEmail ? <>You are signed in as <span className="font-medium text-fg">{deniedEmail}</span>.</> : 'You are signed in.'}
          </p>
          <p className="text-fg-3 text-sm mb-4">
            {spaceId
              ? 'Only users added as operators of a space can open it here; admin roles do not count. Ask a space admin to add this account under the space\'s Operators step, or sign out and sign in with an account that is an operator of it.'
              : 'This account is not an operator of any space yet. Ask a space admin to add it under the space\'s Operators step.'}
          </p>
          {others.length > 0 && (
            <div className="mb-4 text-left bg-surface rounded-lg border border-line p-3">
              <p className="text-xs text-fg-3 mb-2">This account can open:</p>
              <ul className="space-y-1">
                {others.map(s => (
                  <li key={s.space_id}>
                    <a href={`${window.location.origin}?space_id=${s.space_id}`} className="text-sm text-primary hover:underline">{s.name || s.space_id}</a>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <button onClick={logout} className="px-4 py-2 text-sm text-fg-2 border border-line-strong rounded-lg hover:bg-surface-3">
            Sign out and switch account
          </button>
        </div>
      </div>
    )
  }

  if (availableSpaces && !spaceId) return (
    <div className="flex items-center justify-center min-h-screen bg-canvas">
      <div className="bg-surface rounded-xl border border-line p-8 max-w-md w-full">
        <h2 className="text-lg font-semibold text-fg mb-2">Select a Space</h2>
        <p className="text-fg-2 text-sm mb-6">Choose which space to access.</p>
        <div className="space-y-2">
          {availableSpaces.map(s => (
            <a key={s.space_id}
              href={`${window.location.origin}?space_id=${s.space_id}`}
              className="block px-4 py-3 rounded-lg border border-line text-fg hover:bg-surface-3 hover:border-primary transition-all">
              <div className="font-medium">{s.name}</div>
              <div className="text-xs text-fg-3 mt-0.5">{s.space_id}</div>
            </a>
          ))}
        </div>
        <button onClick={logout} className="mt-6 w-full px-4 py-2 text-sm text-fg-2 border border-line rounded-lg hover:bg-surface-3">
          Sign out
        </button>
      </div>
    </div>
  )

  // User identity (from id token) for the rail + greeting
  const claims = (() => { try { return JSON.parse(atob(localStorage.getItem('id_token')?.split('.')[1] || '')) } catch { return {} } })()
  const userEmail = claims.email || ''
  const userName = claims.given_name || (claims.name ? claims.name.split(' ')[0] : '') || (userEmail ? userEmail.split('@')[0] : '') || 'there'
  const displayName = claims.name || claims.given_name || (userEmail ? userEmail.split('@')[0] : 'User')
  const currentChatTitle = sessions.sessions.find(s => s.session_id === sessionId)?.title

  const hasPanels = openTabs.length > 0

  return (
    <div className="flex h-screen bg-canvas">
      {/* ── Left navigation rail ── */}
      <LeftRail
        collapsed={railCollapsed}
        onToggleCollapse={() => setRailCollapsed(v => !v)}
        navItems={NAV_ITEMS}
        activeId={activeTabId}
        openIds={openTabs}
        onSelect={openPanel}
        onNewChat={handleNewSession}
        recents={sessions.sessions}
        activeSessionId={sessionId}
        unreadSessionIds={unreadSessions}
        onOpenSession={openSession}
        onRenameSession={sessions.rename}
        onDeleteSession={handleDeleteSession}
        spaceName={spaceName}
        availableSpaces={availableSpaces || []}
        currentSpaceId={spaceId}
        userName={displayName}
        userEmail={userEmail}
        onOpenProfile={() => setShowProfile(true)}
        onSignOut={logout}
      />

      {/* ── Workspace ── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar: panel tabs (left) + global controls (right) */}
        {/* Light chrome: same surface as the rail, hairline
            border, neutral text — no dark bar. */}
        <header className="bg-nav border-b border-line flex-shrink-0 h-11 flex items-stretch">
          {/* Chat column header — mirrors the chat slot's width so it folds
              away with the pane. Holds the collapse control when a panel is open. */}
          <div className={`chat-head-slot ${chatResizing ? 'chat-head-slot--resizing' : ''} ${hasPanels ? 'chat-head-slot--fold border-r border-line' : 'flex-1 min-w-0'}`}
            style={hasPanels ? { width: chatCollapsed ? 0 : chatWidth } : undefined} aria-hidden={hasPanels && chatCollapsed}>
            <div className="h-full flex items-center gap-2 px-3 min-w-0" style={hasPanels ? { width: chatWidth } : undefined}>
              <svg className="w-3.5 h-3.5 text-fg-muted flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.7 9.7 0 01-4-.85L3 21l1.85-4A8.96 8.96 0 013 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
              <span className="text-xs text-fg-2 truncate">{currentChatTitle || 'New chat'}</span>
              {hasPanels ? (
                <button onClick={() => setChatCollapsed(true)} title="Collapse chat" aria-label="Collapse chat"
                  className="ml-auto w-7 h-7 flex items-center justify-center rounded-md text-fg-3 hover:text-fg hover:bg-primary-subtle transition-colors flex-shrink-0">
                  <PanelLeftClose size={16} strokeWidth={1.75} />
                </button>
              ) : (
                <div className="ml-auto flex items-center gap-3 flex-shrink-0">
                  <NotificationTray />
                  <ThemeMenu {...appearance} />
                </div>
              )}
            </div>
          </div>

          {/* Right-pane header — open tabs (inverted tabs) + global controls */}
          {hasPanels && (
            <div className="flex-1 min-w-0 flex items-center justify-between gap-2 pl-2 pr-3">
              <div role="tablist" aria-label="Open panels" className="tabs-inverted min-w-0 overflow-x-auto">
                {openTabs.map(id => (
                  <button key={id} role="tab" aria-selected={activeTabId === id} onClick={() => setActiveTabId(id)}
                    className="tab group flex items-center gap-1.5 flex-shrink-0">
                    {PANEL_LABELS[id] || id}
                    <span role="button" tabIndex={0} aria-label={`Close ${PANEL_LABELS[id] || id}`}
                      onClick={(e) => { e.stopPropagation(); closePanel(id) }}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); closePanel(id) } }}
                      className="opacity-50 group-hover:opacity-100 hover:text-err ml-0.5">✕</span>
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-3 flex-shrink-0">
                <NotificationTray />
                <ThemeMenu {...appearance} />
              </div>
            </div>
          )}
        </header>

        {/* Work area: chat (primary, left) + panel host (right). `relative` anchors
            the chat launcher to the bottom-left of this area. */}
        <div className="relative flex-1 flex min-h-0 overflow-hidden">
          {/* Chat slot — always mounted so a stream keeps running while it is folded
              away. The slot animates its width (0 <-> chatWidth); the pane inside
              slides out to the left. With no panel open the slot simply fills. */}
          <div
            className={`chat-slot ${hasPanels ? '' : 'chat-slot--fill'} ${hasPanels && chatCollapsed ? 'chat-slot--closed' : ''} ${chatResizing ? 'chat-slot--resizing' : ''}`}
            style={hasPanels ? { width: chatCollapsed ? 0 : chatWidth } : undefined}
            aria-hidden={hasPanels && chatCollapsed}>
          <section
            className={`chat-pane min-h-0 ${hasPanels ? 'border-r border-line' : ''}`}
            style={hasPanels ? { width: chatWidth } : undefined}>
            <div className="flex-1 min-h-0">
              <Chat key={sessionId} accessToken={accessToken} sessionId={sessionId}
                onNewSession={handleNewSession} streamMessage={streamMessage}
                conversation={conversations[sessionId] || EMPTY_CONVERSATION}
                setSessionMessages={setSessionMessages}
                setSessionCurrentMessage={setSessionCurrentMessage}
                setSessionPendingInterrupt={setSessionPendingInterrupt}
                commitCurrentMessage={commitSessionCurrentMessage}
                onSessionActivity={handleSessionActivity}
                onStop={() => stopStreaming(sessionId)}
                respondToInterrupt={respondToInterrupt} isStreaming={isSessionStreaming(sessionId)}
                getValidAccessToken={getValidAccessToken} backendUrl={BACKEND_URL}
                api={api}
                roleFocus={roleFocus}
                onTrace={(entry) => setTraces(prev => [...prev, entry].slice(-1000))}
                reportContext={activeReport ? { meta: activeReport, analysis: activeReportAnalysis } : null}
                databaseScope={selectedDatabases}
                allDatabases={databases}
                onScopeChange={setSelectedDatabases}
                onRefreshDatabases={fetchDatabases}
                onRoleFocusChange={setRoleFocus}
                investigationContext={investigationContext}
                onSetInvestigationContext={putInvestigationInContext}
                onClearInvestigationContext={() => setInvestigationContext(null)}
                onStartInvestigation={handleStartInvestigation}
                onOpenInvestigation={openInvestigationById}
                onOpenPanel={openPanel}
                userName={userName} />
            </div>
          </section>
          </div>

          {/* Drag handle (only when a panel is open and the chat is showing) */}
          {hasPanels && !chatCollapsed && (
            <div onMouseDown={handleDragStart} role="separator" aria-orientation="vertical" aria-label="Resize chat"
              className="w-1 flex-shrink-0 cursor-col-resize bg-surface-active hover:bg-primary active:bg-primary transition-colors" />
          )}

          {/* Launcher — brings the folded chat back */}
          {hasPanels && chatCollapsed && <ChatLauncher onClick={expandChat} unread={!!unreadSessions[sessionId]} />}

          {/* Panel content area — all open panels rendered, inactive ones hidden (state preserved) */}
          {hasPanels && (
            <div className="flex-1 min-w-0 overflow-hidden bg-canvas">
              {openTabs.map(id => (
                <div key={id} className={`h-full ${id === activeTabId ? '' : 'hidden'}`}>
                  {id === 'report' ? (
                    <div className="h-full overflow-hidden">{renderPanel(id)}</div>
                  ) : (
                    <div className="h-full overflow-y-auto">
                      <div className="max-w-6xl mx-auto px-6 py-6">{renderPanel(id)}</div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── Persistent agent trace (stays no matter what's open) ── */}
        <TracePanel traces={traces} onClear={() => setTraces([])} />
      </div>

      {showProfile && (
        <ProfileModal
          email={userEmail}
          currentName={claims.name || ''}
          onUpdateName={updateName}
          onChangePassword={changePassword}
          onClose={() => setShowProfile(false)}
        />
      )}
    </div>
  )
}
