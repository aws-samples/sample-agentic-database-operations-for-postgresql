// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Left navigation rail for the admin console, on the same metrics as the operator
// app's rail (32px rows, 8px radius, 14px text, 18px icons, one lavender wash for
// hover and the active row; collapses to an icon-only 56px rail). The three console
// areas sit at the top; when a space or Settings is open, its sections appear as
// sub-items so the page itself needs no tab rows.
import { useState } from 'react'
import {
  Activity, Building2, Settings as SettingsIcon, PanelLeftClose, PanelLeftOpen, LogOut,
  SlidersHorizontal, BookOpen, Waypoints, Users, UserCog, Mail, ShieldCheck, LayoutGrid, Timer, Database,
  Check, Lock, ScrollText,
} from 'lucide-react'
import { APP_NAME } from '../appName'

const ICON = { size: 18, strokeWidth: 1.75 }
const SUB_ICON = { size: 15, strokeWidth: 1.75 }
const rowBase = 'w-full flex items-center gap-2 h-8 px-2 rounded-lg text-sm font-normal text-fg transition-colors'
const rowIdle = 'hover:bg-primary-subtle'
const rowActive = 'bg-primary-subtle'

const SECTION_ICONS = {
  accounts: SlidersHorizontal, databases: Database, capabilities: BookOpen, topology: Waypoints,
  users: Users, operators: UserCog, email: Mail, security: ShieldCheck, audit: ScrollText,
  cadence: Timer, collection: Database,
}

export default function AdminRail({
  collapsed, onToggleCollapse,
  view, onNavigate, canManage,
  space, spaceSections = [], spaceSection, onSpaceSection,
  settingsSections = [], settingsSection, onSettingsSection,
  monitoringSections = [], monitoringSection, onMonitoringSection,
  userEmail, roleLabel = 'Administrator', onSignOut,
}) {
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const railW = collapsed ? 'w-14' : 'w-60'
  const initial = (userEmail || '?').slice(0, 1).toUpperCase()
  const inSpaces = view === 'list' || view === 'create' || view === 'detail'

  const NavButton = ({ active, label, Icon, onClick, title }) => (
    <button onClick={onClick} title={collapsed ? label : title} aria-current={active ? 'page' : undefined}
      className={`${rowBase} ${collapsed ? 'justify-center px-0' : ''} ${active ? rowActive : rowIdle}`}>
      <Icon {...ICON} className="flex-shrink-0 text-fg-2" />
      {!collapsed && <span className="flex-1 text-left truncate">{label}</span>}
    </button>
  )

  // Sub-items: indented under their area, with a thin guide line. Collapsed, they
  // stay reachable as a grouped stack of smaller icons.
  const SubItems = ({ items, current, onSelect, heading }) => {
    if (!items.length) return null
    // An item that carries `unlocked: false` is a gated stage; plain sections never lock.
    const locked = (s) => s.unlocked === false
    if (collapsed) return (
      <div className="my-1 py-1 border-y border-line-2 space-y-0.5" role="group" aria-label={heading || 'Sections'}>
        {items.map(s => {
          const Icon = SECTION_ICONS[s.id] || LayoutGrid
          const active = current === s.id
          return (
            <button key={s.id} onClick={() => locked(s) || onSelect(s.id)} disabled={locked(s)} title={heading ? `${heading} · ${s.label}` : s.label} aria-current={active ? 'page' : undefined}
              className={`w-full flex items-center justify-center h-7 rounded-md transition-colors ${active ? 'bg-primary-subtle text-fg' : locked(s) ? 'text-fg-muted/50 cursor-not-allowed' : 'text-fg-muted hover:bg-primary-subtle hover:text-fg-2'}`}>
              <Icon {...SUB_ICON} />
            </button>
          )
        })}
      </div>
    )
    return (
      <div className="ml-4 pl-3 border-l border-line-2 space-y-0.5 py-0.5">
        {heading && <p className="px-2 pt-1 pb-0.5 text-[11px] font-medium text-fg-3 truncate" title={heading}>{heading}</p>}
        {items.map(s => {
          const Icon = SECTION_ICONS[s.id] || LayoutGrid
          const active = current === s.id
          return (
            <button key={s.id} onClick={() => locked(s) || onSelect(s.id)} disabled={locked(s)} aria-current={active ? 'page' : undefined}
              title={locked(s) ? 'Finish the previous stage first' : undefined}
              className={`w-full flex items-center gap-2 h-7 px-2 rounded-md text-[13px] transition-colors ${active ? 'bg-primary-subtle text-fg font-medium' : locked(s) ? 'text-fg-muted cursor-not-allowed' : 'text-fg-2 hover:bg-primary-subtle'}`}>
              <Icon {...SUB_ICON} className="flex-shrink-0 text-fg-muted" />
              <span className="flex-1 text-left truncate">{s.label}</span>
              {/* Stage state, when the items carry it (a space's stages do; Settings' don't). */}
              {s.done && <Check size={13} strokeWidth={2.25} className="flex-shrink-0 text-ok" aria-label="done" />}
              {locked(s) && <Lock size={12} strokeWidth={1.75} className="flex-shrink-0 text-fg-muted" aria-label="locked" />}
            </button>
          )
        })}
      </div>
    )
  }

  return (
    <nav className={`${railW} flex-shrink-0 flex flex-col h-full bg-nav border-r border-line transition-all duration-200`} aria-label="Primary">
      {/* Brand + collapse toggle */}
      <div className={`flex-shrink-0 ${collapsed ? 'px-2 pt-2.5 pb-2 flex flex-col items-center gap-2' : 'h-11 px-3 flex items-center gap-2'}`}>
        {/* Same stacked-cylinder mark as the operator app's rail, so the two consoles
            read as one product. */}
        <button onClick={() => onNavigate('list')} className="w-7 h-7 rounded-lg bg-brand-gradient flex items-center justify-center flex-shrink-0" title="Agent Spaces">
          <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24" aria-hidden>
            <ellipse cx="12" cy="5" rx="7" ry="2.6" />
            <path strokeLinecap="round" d="M5 5v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V5" />
            <path strokeLinecap="round" d="M5 11v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6v-6" />
          </svg>
        </button>
        {!collapsed && (
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-fg truncate leading-tight" title={APP_NAME}>{APP_NAME}</p>
            <p className="text-[11px] text-fg-3 truncate leading-tight">Admin console</p>
          </div>
        )}
        <button onClick={onToggleCollapse} title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          className="w-7 h-7 flex items-center justify-center text-fg-3 hover:text-fg hover:bg-primary-subtle rounded-md transition-colors flex-shrink-0">
          {collapsed ? <PanelLeftOpen size={16} strokeWidth={1.75} /> : <PanelLeftClose size={16} strokeWidth={1.75} />}
        </button>
      </div>

      {/* Areas + contextual sections */}
      <div className="flex-1 overflow-y-auto px-2 pt-1 pb-2 space-y-0.5">
        <NavButton active={inSpaces} label="Agent Spaces" Icon={Building2} onClick={() => onNavigate('list')} />
        {view === 'detail' && space && (
          <SubItems heading={space.name} items={spaceSections} current={spaceSection} onSelect={onSpaceSection} />
        )}
        <NavButton active={view === 'capabilities'} label="Agent Capabilities" Icon={BookOpen} onClick={() => onNavigate('capabilities')}
          title={canManage ? 'Knowledge, skills and models for every space' : 'Knowledge, skills and models for the spaces you manage'} />
        <NavButton active={view === 'monitoring'} label="Monitoring" Icon={Activity} onClick={() => onNavigate('monitoring')}
          title="Collection cadence and status for every database" />
        {view === 'monitoring' && (
          <SubItems items={monitoringSections} current={monitoringSection} onSelect={onMonitoringSection} />
        )}
        {canManage && settingsSections.length > 0 && (
          <>
            <NavButton active={view === 'settings'} label="Settings" Icon={SettingsIcon} onClick={() => onNavigate('settings')}
              title="Users, Operators, Email, Security and Audit" />
            {view === 'settings' && (
              <SubItems items={settingsSections} current={settingsSection} onSelect={onSettingsSection} />
            )}
          </>
        )}
      </div>

      {/* User */}
      <div className="flex-shrink-0 px-2 py-2 border-t border-line-2">
        <div className="relative">
          <button onClick={() => setUserMenuOpen(v => !v)} title={collapsed ? userEmail : undefined}
            aria-haspopup="menu" aria-expanded={userMenuOpen}
            className={`w-full flex items-center gap-2 px-1.5 py-1.5 rounded-lg hover:bg-primary-subtle transition-colors ${collapsed ? 'justify-center px-0' : ''}`}>
            <div className="w-7 h-7 rounded-full bg-brand-gradient flex items-center justify-center flex-shrink-0 text-[11px] font-semibold text-white">{initial}</div>
            {!collapsed && (
              <div className="flex-1 min-w-0 text-left">
                <p className="text-[13px] font-medium text-fg truncate leading-tight">{userEmail || 'Signed in'}</p>
                <p className="text-[11px] text-fg-3 truncate leading-tight">{roleLabel}</p>
              </div>
            )}
          </button>
          {userMenuOpen && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setUserMenuOpen(false)} />
              <div role="menu" className="absolute bottom-full left-0 mb-1 w-full min-w-[220px] bg-surface border border-line rounded-lg shadow-lg z-50 overflow-hidden">
                <div className="px-3 py-2.5 border-b border-line-2">
                  <p className="text-sm font-medium text-fg truncate" title={userEmail}>{userEmail}</p>
                  <p className="text-[11px] text-fg-3">{roleLabel}</p>
                </div>
                <button role="menuitem" onClick={() => { setUserMenuOpen(false); onSignOut?.() }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-err hover:bg-err-hover transition-colors">
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
