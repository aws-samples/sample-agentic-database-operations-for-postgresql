// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useRef, useCallback, useMemo } from 'react'
import { config } from '../config'

const API_URL = config.apiUrl

export default function useApi(getValidAccessToken, forceRefresh) {
  const tokenFnRef = useRef(getValidAccessToken)
  tokenFnRef.current = getValidAccessToken
  const refreshRef = useRef(forceRefresh)
  refreshRef.current = forceRefresh

  const request = useCallback(async (path, options = {}) => {
    const send = (token) => {
      const headers = { 'Content-Type': 'application/json' }
      if (token) headers['Authorization'] = `Bearer ${token}`
      return fetch(`${API_URL}${path}`, { ...options, headers })
    }
    let res = await send(tokenFnRef.current ? await tokenFnRef.current() : null)
    // The server refuses a token issued before this user's access last changed. Get a
    // fresh token once and retry; when the user was signed out (role changed, deleted)
    // the refresh fails and sends them to sign in.
    if (res.status === 401 && refreshRef.current) {
      let fresh = null
      try { fresh = await refreshRef.current() } catch {}
      if (fresh) res = await send(fresh)
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      let body = null
      try { body = await res.json(); if (body.detail) detail = body.detail } catch {}
      const err = new Error(detail); err.detail = detail; err.status = res.status; err.body = body; throw err
    }
    return res.json()
  }, [])

  const get = useCallback((path) => request(path), [request])
  const post = useCallback((path, body) => request(path, { method: 'POST', body: JSON.stringify(body) }), [request])
  const put = useCallback((path, body) => request(path, { method: 'PUT', body: JSON.stringify(body) }), [request])
  const del = useCallback((path) => request(path, { method: 'DELETE' }), [request])

  // One object for the life of the component: the callbacks above never change, so the
  // object must not either. A new literal on every render changed `api` at streaming
  // frequency, and every effect keyed on it blanked its panel and fetched again.
  return useMemo(() => ({ get, post, put, del }), [get, post, put, del])
}
