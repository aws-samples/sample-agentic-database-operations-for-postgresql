// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useCallback, useEffect, createContext, useContext } from 'react'

const NotificationContext = createContext()
const STORAGE_KEY = 'dbops-notifications'

export function useNotifications() {
  return useContext(NotificationContext)
}

let idCounter = 0

function loadFromSession() {
  try {
    const spaceId = new URLSearchParams(window.location.search).get('space_id') || 'default'
    const raw = sessionStorage.getItem(`${STORAGE_KEY}-${spaceId}`)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    idCounter = Math.max(idCounter, ...parsed.map(n => n.id), 0)
    return parsed.map(n => ({ ...n, time: new Date(n.time), removing: false }))
  } catch { return [] }
}

function saveToSession(notifications) {
  try {
    const spaceId = new URLSearchParams(window.location.search).get('space_id') || 'default'
    const serializable = notifications.filter(n => !n.removing).map(n => ({
      id: n.id, type: n.type, title: n.title, message: n.message, database: n.database, time: n.time.toISOString(),
    }))
    sessionStorage.setItem(`${STORAGE_KEY}-${spaceId}`, JSON.stringify(serializable))
  } catch {}
}

export function NotificationProvider({ children }) {
  const [notifications, setNotifications] = useState(loadFromSession)

  useEffect(() => { saveToSession(notifications) }, [notifications])

  const addNotification = useCallback(({ type = 'info', title, message, database }) => {
    const id = ++idCounter
    setNotifications(prev => [{ id, type, title, message, database, time: new Date(), removing: false }, ...prev])
    return id
  }, [])

  const removeNotification = useCallback((id) => {
    setNotifications(prev => prev.map(n => n.id === id ? { ...n, removing: true } : n))
    setTimeout(() => setNotifications(prev => prev.filter(n => n.id !== id)), 300)
  }, [])

  const clearAll = useCallback(() => {
    setNotifications([])
  }, [])

  return (
    <NotificationContext.Provider value={{ addNotification, removeNotification, clearAll, notifications }}>
      {children}
    </NotificationContext.Provider>
  )
}

const typeStyles = {
  success: { dot: 'bg-ok', text: 'text-ok', bg: 'bg-ok-bg' },
  error:   { dot: 'bg-err',   text: 'text-err',     bg: 'bg-err-bg' },
  warning: { dot: 'bg-warn', text: 'text-warn-mid', bg: 'bg-warn-bg' },
  info:    { dot: 'bg-info',  text: 'text-info',   bg: 'bg-info-bg' },
}

function timeAgo(date) {
  const s = Math.floor((Date.now() - date.getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

// Inline header notification indicator with dropdown
export function NotificationTray() {
  const { notifications, removeNotification, clearAll } = useNotifications()
  const [open, setOpen] = useState(false)
  const count = notifications.length

  if (count === 0) return null

  return (
    <div className="relative">
      <button onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-2 py-1 rounded hover:bg-surface-3 transition-colors text-[10px] relative">
        <svg className="w-4 h-4 text-fg-muted" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
        </svg>
        <span className="absolute -top-0.5 -right-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-primary text-[10px] font-bold text-primary-fg">{count}</span>
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 bg-surface border border-line rounded-lg shadow-xl overflow-hidden" style={{ width: 320 }}>
            <div className="flex items-center justify-between px-3 py-2 border-b border-line-2">
              <span className="text-[11px] font-semibold text-fg-2">Notifications ({count})</span>
              <button onClick={clearAll} className="text-[10px] text-fg-muted hover:text-err transition-colors px-1.5 py-0.5 rounded">Clear all</button>
            </div>
            <div className="max-h-[300px] overflow-y-auto">
              {notifications.map(n => {
                const s = typeStyles[n.type] || typeStyles.info
                return (
                  <div key={n.id}
                    className={`flex items-start gap-2.5 px-3 py-2 border-b border-line-2 transition-all duration-300 ${
                      n.removing ? 'opacity-0 h-0 py-0 overflow-hidden' : 'opacity-100'
                    } ${s.bg}`}>
                    <span className={`w-1.5 h-1.5 rounded-full mt-1.5 flex-shrink-0 ${s.dot}`} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {n.title && <span className={`text-[11px] font-semibold ${s.text} leading-tight`}>{n.title}</span>}
                        {n.database && <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-3 text-fg-3 font-medium">🛢️ {n.database}</span>}
                      </div>
                      {n.message && <div className="text-[10px] text-fg-3 mt-0.5 leading-snug">{n.message}</div>}
                      <div className="text-[10px] text-fg-muted mt-0.5">{timeAgo(n.time)}</div>
                    </div>
                    <button onClick={() => removeNotification(n.id)}
                      className="flex-shrink-0 p-0.5 text-fg-muted hover:text-fg-2 transition-colors rounded">
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
