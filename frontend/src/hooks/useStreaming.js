// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useCallback } from 'react'

export default function useStreaming() {
  // Streaming state is tracked PER SESSION rather than as one global flag, so a
  // long-running stream (a chat turn, or an investigation under session
  // `inv-<id>`) only blocks its OWN composer. Other chats — including a newly
  // opened one — stay usable and can stream in parallel.
  const [streamingSessions, setStreamingSessions] = useState({})
  // One AbortController per in-flight session, so Stop cancels the right stream.
  const controllersRef = useRef({})

  const isSessionStreaming = useCallback(
    (sessionId) => !!streamingSessions[sessionId],
    [streamingSessions]
  )

  const markStreaming = (sessionId, on) => {
    setStreamingSessions(prev => {
      if (on) return { ...prev, [sessionId]: true }
      if (!(sessionId in prev)) return prev
      const next = { ...prev }
      delete next[sessionId]
      return next
    })
  }

  const streamRequest = async (endpoint, body, headers, onEvent, signal) => {
    const response = await fetch(endpoint, { method: 'POST', headers, body: JSON.stringify(body), signal })
    if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`)

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    const emitLine = (line) => {
      if (!line.trim()) return
      try { onEvent(JSON.parse(line)) }
      catch (err) { console.error('Failed to parse event:', line, err) }
    }

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          // Flush the final line. A stream whose last event is not newline-terminated
          // (a short refusal, or a response that did not end in '\n') leaves its one
          // line sitting in `buffer`; without this the user sees nothing.
          const tail = buffer + decoder.decode()
          buffer = ''
          if (tail) emitLine(tail)
          break
        }

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) emitLine(line)
      }
    } catch (err) {
      // User pressed Stop — end gracefully rather than surfacing an error.
      if (err?.name === 'AbortError') {
        try { await reader.cancel() } catch {}
        return
      }
      throw err
    }
  }

  // Cancel the in-flight response for a specific session (the partial answer so
  // far is kept). Aborting one session never affects another's stream.
  const stopStreaming = (sessionId) => {
    try { controllersRef.current[sessionId]?.abort() } catch {}
  }

  const streamMessage = async (endpoint, message, accessToken, sessionId, onEvent, getValidAccessToken, opts = {}) => {
    markStreaming(sessionId, true)
    const controller = new AbortController()
    controllersRef.current[sessionId] = controller
    try {
      const validToken = getValidAccessToken ? await getValidAccessToken() : accessToken
      // For deployed AgentCore, endpoint already includes /invocations
      // For local, we need to append it
      const url = endpoint.includes('/invocations') ? endpoint : `${endpoint}/invocations`
      // Per-message model selection + space (model governance). space_id lets the
      // backend resolve the per-space default/allowlist for chat (the scope text
      // block carries databases but not the space id).
      const body = { prompt: message }
      // The user's own typed question, separate from any app-assembled context wrapper in
      // `prompt`, so the backend input guardrail screens what the user wrote and not our
      // own report/investigation instructions (which otherwise read as a prompt attack).
      if (opts.userText) body.user_text = opts.userText
      if (opts.modelId) body.model_id = opts.modelId
      if (opts.spaceId) body.space_id = opts.spaceId
      if (opts.roleFocus) body.role_focus = opts.roleFocus  // Aurora writer/reader focus
      // Structured scope: the backend builds the [DATABASE SCOPE] block from these ids
      // ([] = all configured databases of the space). Omitted (null) = no scope block.
      if (Array.isArray(opts.databaseIds)) body.database_ids = opts.databaseIds
      await streamRequest(
        url,
        body,
        {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${validToken}`,
          'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': sessionId
        },
        onEvent,
        controller.signal
      )
    } finally {
      // Only clear if a newer stream for this session has not replaced us.
      if (controllersRef.current[sessionId] === controller) delete controllersRef.current[sessionId]
      markStreaming(sessionId, false)
    }
  }

  const respondToInterrupt = async (endpoint, interruptId, response, accessToken, sessionId, onEvent, getValidAccessToken, opts = {}) => {
    markStreaming(sessionId, true)
    const controller = new AbortController()
    controllersRef.current[sessionId] = controller
    try {
      const validToken = getValidAccessToken ? await getValidAccessToken() : accessToken
      const url = endpoint.includes('/invocations') ? endpoint : `${endpoint}/invocations`
      // Carry the same model/space as the original turn so the interrupt resumes
      // the SAME cached agent (the agent cache is keyed by resolved model).
      const body = { prompt: response }
      if (opts.modelId) body.model_id = opts.modelId
      if (opts.spaceId) body.space_id = opts.spaceId
      if (opts.roleFocus) body.role_focus = opts.roleFocus  // keep the same focus on resume
      await streamRequest(
        url,
        body,
        {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${validToken}`,
          'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': sessionId,
          'X-Amzn-Bedrock-AgentCore-Runtime-Custom-Interrupt-Id': interruptId
        },
        onEvent,
        controller.signal
      )
    } finally {
      if (controllersRef.current[sessionId] === controller) delete controllersRef.current[sessionId]
      markStreaming(sessionId, false)
    }
  }

  // `streamingSessions` is exposed so the app can notice a stream ENDING in a chat
  // that is not in view (it marks that chat unread); `isSessionStreaming` covers
  // the per-composer question.
  return { streamMessage, respondToInterrupt, isSessionStreaming, stopStreaming, streamingSessions }
}
