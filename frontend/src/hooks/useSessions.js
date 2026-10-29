// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback, useRef } from 'react'

// Per-user chat-session index powering the left-rail "Recents" list. Backed by
// the /api/sessions endpoints, which scope every call server-side to the
// caller's Cognito sub. Recents are non-critical: all calls fail quietly so a
// transient error never blocks the chat experience.
//
// `api` (from useApi) is a fresh object each render, so we read it through a ref
// to keep the callbacks stable and avoid a refetch loop.
export default function useSessions(api, spaceId, enabled = true) {
  const apiRef = useRef(api)
  apiRef.current = api

  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const q = spaceId ? `?space_id=${encodeURIComponent(spaceId)}` : ''
      const data = await apiRef.current.get(`/api/sessions${q}`)
      setSessions(Array.isArray(data) ? data : [])
    } catch {
      // non-critical
    } finally {
      setLoading(false)
    }
  }, [spaceId])

  // `enabled` is the caller's authenticated flag. On a first sign-in this hook mounts
  // before any token exists; a load fired then goes out unauthenticated and nothing
  // re-ran it once the token arrived, so Recents stayed empty until a hard refresh.
  useEffect(() => { if (enabled) refresh() }, [refresh, enabled])

  // Activity bump (and, on first turn, set the title) — keeps Recents current.
  const touch = useCallback(async (sessionId, { title, space_id } = {}) => {
    if (!sessionId) return
    try {
      const body = {}
      if (title !== undefined) body.title = title
      if (space_id !== undefined) body.space_id = space_id
      await apiRef.current.put(`/api/sessions/${sessionId}`, body)
      refresh()
    } catch {
      // non-critical
    }
  }, [refresh])

  const rename = useCallback(async (sessionId, title) => {
    setSessions(prev => prev.map(s => (s.session_id === sessionId ? { ...s, title } : s)))
    try {
      await apiRef.current.put(`/api/sessions/${sessionId}`, { title })
    } catch {
      refresh()
    }
  }, [refresh])

  const remove = useCallback(async (sessionId) => {
    setSessions(prev => prev.filter(s => s.session_id !== sessionId))
    try {
      await apiRef.current.del(`/api/sessions/${sessionId}`)
    } catch {
      refresh()
    }
  }, [refresh])

  // Past turns for rehydrating a reopened chat. Attaches a timestamp so the
  // message bubbles render a time label (AgentCore drops per-event times here).
  const loadMessages = useCallback(async (sessionId) => {
    try {
      const data = await apiRef.current.get(`/api/sessions/${sessionId}/messages`)
      const now = new Date().toISOString()
      return (data?.messages || []).map((m, i) => {
        const base = { ...m, timestamp: m.timestamp || now }
        // Rebuild contentBlocks for assistant turns that carried an investigation
        // suggestion (reconstructed server-side from memory) so the card renders
        // on refresh exactly as it did live. A persisted `suggestions` block
        // restores the follow-up pills the same way.
        if (m.role === 'assistant' && Array.isArray(m.blocks) && m.blocks.length) {
          base.contentBlocks = [
            ...(m.content ? [{ type: 'text', content: m.content }] : []),
            ...m.blocks
              .filter(b => b?.type === 'investigation_suggestion')
              .map((b, j) => ({
                type: 'investigation_suggestion',
                title: b.title,
                rationale: b.rationale,
                toolUseId: `rehydrated-${i}-${j}`,
                rehydrated: true,
              })),
          ]
          const sugg = m.blocks.find(b => b?.type === 'suggestions' && Array.isArray(b.items))
          if (sugg?.items?.length) { base.followups = sugg.items; base.followupsKind = sugg.kind === 'replies' ? 'replies' : 'questions' }
        }
        return base
      })
    } catch {
      return []
    }
  }, [])

  return { sessions, loading, refresh, touch, rename, remove, loadMessages }
}
