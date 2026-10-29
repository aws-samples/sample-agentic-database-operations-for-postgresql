// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import {
  Plus, LayoutGrid, Search, TrendingUp, Waypoints, FileText, BookOpen,
  PanelLeftClose, PanelLeftOpen, LogOut, Pencil, Trash2, MessageSquare,
  Building2, ChevronDown, UserRound,
} from 'lucide-react'
import { APP_NAME } from '../appName'

// Left navigation rail: 32px rows, 8px radius, 14px
// regular text, 18px icons, and one lavender wash for both hover and the
// active row (the text stays neutral). Collapses to an icon-only 56px rail.
// Nav items open as panels in the workspace; chat stays primary.

// App passes icon names; the map keeps the rail's icon set in one place.
const NAV_ICONS = {
  dashboard: LayoutGrid,
  investigations: Search,
  trends: TrendingUp,
  topology: Waypoints,
  reports: FileText,
  knowledge: BookOpen,
}

const ICON = { size: 18, strokeWidth: 1.75 }

// Group sessions by recency (Today / Yesterday / Previous 7 days / Older),
// preserving the server's most-recent-first order inside each group.
function groupRecents(sessions) {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const day = 86400e3
  const groups = [
    { label: 'Today', items: [] },
    { label: 'Yesterday', items: [] },
    { label: 'Previous 7 days', items: [] },
    { label: 'Older', items: [] },
  ]
  for (const s of sessions) {
    const t = s.last_active ? new Date(s.last_active).getTime() : 0
    if (!t || t >= startOfToday) groups[0].items.push(s)
    else if (t >= startOfToday - day) groups[1].items.push(s)
    else if (t >= startOfToday - 7 * day) groups[2].items.push(s)
    else groups[3].items.push(s)
  }
  return groups.filter(g => g.items.length)
}

// Nav rows: wash on hover and when active, neutral text throughout.
const rowBase = 'w-full flex items-center gap-2 h-8 px-2 rounded-lg text-sm font-normal text-fg transition-colors'
const rowIdle = 'hover:bg-primary-subtle'
const rowActive = 'bg-primary-subtle'

export default function LeftRail({
  collapsed,
  onToggleCollapse,
  navItems = [],
  activeId,
  openIds = [],
  onSelect,
  onNewChat,
  recents = [],
  activeSessionId,
  unreadSessionIds = {},
  onOpenSession,
  onRenameSession,
  onDeleteSession,
  spaceName,
  availableSpaces = [],
  currentSpaceId,
  userName,
  userEmail,
  onOpenProfile,
  onSignOut,
}) {
  const railW = collapsed ? 'w-14' : 'w-60'
  const [editingId, setEditingId] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [spaceMenuOpen, setSpaceMenuOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)

  // One "Recents" row: open on click, rename inline (pencil), delete (trash).
  const RecentItem = ({ session }) => {
    const id = session.session_id
    const active = id === activeSessionId
    const unread = !!unreadSessionIds[id]
    const title = session.title || 'Untitled chat'
    if (editingId === id) {
      return (
        <input
          autoFocus
          value={editValue}
          onChange={(e) => setEditValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { onRenameSession?.(id, editValue.trim() || title); setEditingId(null) }
            else if (e.key === 'Escape') setEditingId(null)
          }}
          onBlur={() => setEditingId(null)}
          aria-label="Chat title"
          className="w-full h-8 px-2 text-sm rounded-lg border border-primary-line bg-surface text-fg focus:outline-none"
        />
      )
    }
    return (
      <div className={`group relative flex items-center rounded-lg ${active ? rowActive : rowIdle}`}>
        <button onClick={() => onOpenSession?.(id)} title={title}
          className={`flex-1 min-w-0 flex items-center gap-2 text-left h-8 px-2 text-sm ${active ? 'text-fg font-medium' : 'text-fg-2'}`}>
          <MessageSquare size={15} strokeWidth={1.75} className="flex-shrink-0 text-fg-muted" />
          <span className={`truncate ${unread ? 'font-medium text-fg' : ''}`}>{title}</span>
          {/* A reply arrived here while another chat was in view. */}
          {unread && <span className="w-1.5 h-1.5 rounded-full bg-primary-light flex-shrink-0 group-hover:hidden" aria-label="Unread reply" title="Unread reply" />}
        </button>
        <div className="hidden group-hover:flex items-center gap-0.5 pr-1 flex-shrink-0">
          <button onClick={() => { setEditValue(title); setEditingId(id) }} title="Rename"
            className="p-1 text-fg-muted hover:text-fg-2 rounded">
            <Pencil size={13} strokeWidth={1.75} />
          </button>
          <button onClick={() => { if (window.confirm('Delete this chat?')) onDeleteSession?.(id) }} title="Delete"
            className="p-1 text-fg-muted hover:text-err rounded">
            <Trash2 size={13} strokeWidth={1.75} />
          </button>
        </div>
      </div>
    )
  }

  const NavButton = ({ id, label, icon, disabled, badge }) => {
    const active = activeId === id
    const open = openIds.includes(id)
    const Icon = NAV_ICONS[icon || id] || LayoutGrid
    return (
      <button
        onClick={() => !disabled && onSelect?.(id)}
        disabled={disabled}
        title={collapsed ? label : undefined}
        aria-current={active ? 'page' : undefined}
        className={`${rowBase} ${collapsed ? 'justify-center px-0' : ''} ${
          active ? rowActive : disabled ? 'text-fg-muted cursor-not-allowed' : rowIdle
        }`}
      >
        <Icon {...ICON} className={`flex-shrink-0 ${disabled ? 'text-fg-muted' : 'text-fg-2'}`} />
        {!collapsed && <span className="flex-1 text-left truncate">{label}</span>}
        {!collapsed && open && !active && <span className="w-1.5 h-1.5 rounded-full bg-primary-light flex-shrink-0" aria-label="Open" />}
        {!collapsed && badge && (
          <span className="text-[10px] font-semibold uppercase tracking-wide bg-surface-active text-fg-3 rounded px-1.5 py-0.5 flex-shrink-0">{badge}</span>
        )}
      </button>
    )
  }

  const groups = groupRecents(recents)
  const initial = (userName || '?').slice(0, 1).toUpperCase()

  return (
    <nav className={`${railW} flex-shrink-0 flex flex-col h-full bg-nav border-r border-line transition-all duration-200`} aria-label="Primary">
      {/* Brand + collapse toggle (toggle sits under the icon when collapsed) */}
      <div className={`flex-shrink-0 ${collapsed ? 'px-2 pt-2.5 pb-2 flex flex-col items-center gap-2' : 'h-11 px-3 flex items-center gap-2'}`}>
        <div className="w-7 h-7 rounded-lg bg-brand-gradient flex items-center justify-center flex-shrink-0">
          <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24" aria-hidden>
            <ellipse cx="12" cy="5" rx="7" ry="2.6" />
            <path strokeLinecap="round" d="M5 5v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V5" />
            <path strokeLinecap="round" d="M5 11v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6v-6" />
          </svg>
        </div>
        {!collapsed && <span className="flex-1 text-sm font-medium text-fg truncate">{APP_NAME}</span>}
        <button onClick={onToggleCollapse} title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          className="w-7 h-7 flex items-center justify-center text-fg-3 hover:text-fg hover:bg-primary-subtle rounded-md transition-colors flex-shrink-0">
          {collapsed ? <PanelLeftOpen size={16} strokeWidth={1.75} /> : <PanelLeftClose size={16} strokeWidth={1.75} />}
        </button>
      </div>

      {/* Primary actions + workspace nav */}
      <div className="flex-1 overflow-y-auto px-2 pt-1 pb-2">
        <div className="space-y-0.5">
          <button onClick={onNewChat} title={collapsed ? 'New chat' : undefined}
            className={`${rowBase} ${rowIdle} ${collapsed ? 'justify-center px-0' : ''}`}>
            <Plus {...ICON} className="flex-shrink-0 text-fg-2" />
            {!collapsed && <span className="flex-1 text-left">New chat</span>}
          </button>
          {navItems.map(item => <NavButton key={item.id} {...item} />)}
        </div>

        {!collapsed && groups.map(g => (
          <div key={g.label} className="pt-4">
            <p className="px-2 pb-1 text-[11px] font-medium text-fg-3">{g.label}</p>
            <div className="space-y-0.5">
              {g.items.map(s => <RecentItem key={s.session_id} session={s} />)}
            </div>
          </div>
        ))}
      </div>

      {/* Bottom: Agent Capabilities + space selector + user */}
      <div className="flex-shrink-0 px-2 py-2 border-t border-line-2 space-y-0.5">
        <NavButton id="knowledge" label="Agent Capabilities" icon="knowledge" />

        {/* Agent Space selector */}
        {availableSpaces && availableSpaces.length > 0 && (
          <div className="relative">
            <button onClick={() => !collapsed && setSpaceMenuOpen(v => !v)}
              title={collapsed ? (spaceName || 'Space') : undefined}
              aria-haspopup="menu" aria-expanded={spaceMenuOpen}
              className={`${rowBase} ${rowIdle} ${collapsed ? 'justify-center px-0' : ''}`}>
              <Building2 {...ICON} className="flex-shrink-0 text-fg-2" />
              {!collapsed && <span className="flex-1 text-left truncate">{spaceName || 'Select space'}</span>}
              {!collapsed && <ChevronDown size={14} strokeWidth={1.75} className={`flex-shrink-0 text-fg-muted transition-transform ${spaceMenuOpen ? 'rotate-180' : ''}`} />}
            </button>
            {!collapsed && spaceMenuOpen && (
              <div role="menu" className="absolute bottom-full left-0 mb-1 w-full bg-surface border border-line rounded-lg shadow-lg z-50 py-1 max-h-64 overflow-y-auto">
                {availableSpaces.map(s => (
                  <a key={s.space_id} role="menuitem" href={`${window.location.origin}?space_id=${s.space_id}`}
                    className={`block px-3 py-2 text-[13px] transition-colors truncate ${
                      s.space_id === currentSpaceId ? 'text-fg bg-primary-subtle font-medium' : 'text-fg-2 hover:bg-surface-3'
                    }`}>
                    {s.name}
                  </a>
                ))}
              </div>
            )}
          </div>
        )}

        {/* User — click the avatar for the profile menu */}
        <div className="relative">
          <button onClick={() => setUserMenuOpen(v => !v)}
            title={collapsed ? userName : undefined}
            aria-haspopup="menu" aria-expanded={userMenuOpen}
            className={`w-full flex items-center gap-2 px-1.5 py-1.5 rounded-lg hover:bg-primary-subtle transition-colors ${collapsed ? 'justify-center px-0' : ''}`}>
            <div className="w-7 h-7 rounded-full bg-brand-gradient flex items-center justify-center flex-shrink-0 text-[11px] font-semibold text-white">
              {initial}
            </div>
            {!collapsed && (
              <div className="flex-1 min-w-0 text-left">
                <p className="text-[13px] font-medium text-fg truncate leading-tight">{userName || 'User'}</p>
                <p className="text-[11px] text-fg-3 truncate leading-tight">{userEmail}</p>
              </div>
            )}
          </button>

          {userMenuOpen && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setUserMenuOpen(false)} />
              <div role="menu" className="absolute bottom-full left-0 mb-1 w-full min-w-[220px] bg-surface border border-line rounded-lg shadow-lg z-50 overflow-hidden">
                <div className="flex items-center gap-2.5 px-3 py-2.5 border-b border-line-2">
                  <div className="w-9 h-9 rounded-full bg-brand-gradient flex items-center justify-center flex-shrink-0 text-sm font-semibold text-white">
                    {initial}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-fg truncate" title={userName}>{userName || 'User'}</p>
                    <p className="text-[11px] text-fg-3 truncate" title={userEmail}>{userEmail}</p>
                  </div>
                </div>
                <button role="menuitem" onClick={() => { setUserMenuOpen(false); onOpenProfile?.() }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-fg-2 hover:bg-surface-3 transition-colors">
                  <UserRound size={16} strokeWidth={1.75} className="text-fg-muted" />
                  Edit profile
                </button>
                <button role="menuitem" onClick={() => { setUserMenuOpen(false); onSignOut?.() }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-err hover:bg-err-hover transition-colors border-t border-line-2">
                  <LogOut size={16} strokeWidth={1.75} />
                  Sign out
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </nav>
  )
}
