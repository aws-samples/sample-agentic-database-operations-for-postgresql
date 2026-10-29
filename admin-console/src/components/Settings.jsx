// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import UserManagement from './UserManagement'
import Operators from './Operators'
import EmailSender from './EmailSender'
import Security from './Security'
import { AuditSettingsPage } from './AuditSettings'
import AlarmSettings from './AlarmSettings'

// Settings hub — deployment administration, super-admin only: who can sign in
// (Users), who operates which space (Operators), outbound Email, database Alarms, the Security
// posture, and the admin Audit log with its locked vault. What the agent knows and can do is its own console area (Agent
// Capabilities, at the account tier for super admins and the space-admin tier for
// space admins), not a setting.
// The console's navigation rail lists the sections (hideTabs); rendered standalone
// the hub falls back to its own top tabs so the content gets the full width.

// ownHeading: the section renders its own title row, so the hub adds none above it.
export const SETTINGS_SECTIONS = [
  { id: 'users', label: 'Users', superOnly: true, ownHeading: true },
  { id: 'operators', label: 'Operators', superOnly: true, ownHeading: true },
  { id: 'email', label: 'Email', superOnly: true, ownHeading: true },
  { id: 'alarms', label: 'Alarms', superOnly: true, ownHeading: true },
  { id: 'security', label: 'Security', superOnly: true, ownHeading: true },
  { id: 'audit', label: 'Audit', superOnly: true, ownHeading: true },
]

export default function Settings({ api, initialSection = 'users', isSuperAdmin = true, section: controlled, onSectionChange, hideTabs = false }) {
  const [internal, setInternal] = useState(initialSection)
  const section = controlled ?? internal
  const setSection = (id) => { onSectionChange ? onSectionChange(id) : setInternal(id) }
  const sections = SETTINGS_SECTIONS.filter(s => isSuperAdmin || !s.superOnly)
  const current = sections.find(s => s.id === section)
  const showHeader = !hideTabs || !current?.ownHeading

  return (
    <div className="space-y-5">
      {showHeader && (
        <div>
          <h1 className="text-lg font-semibold text-fg">{hideTabs && current ? current.label : 'Settings'}</h1>
          {!isSuperAdmin && (
            <p className="text-xs text-fg-3 mt-0.5">Your space-admin tier — applies to every space you manage.</p>
          )}
        </div>
      )}

      {/* Top tabs — hidden when the navigation rail lists the sections */}
      {!hideTabs && <div className="border-b border-line">
        <div className="flex gap-0">
          {sections.map(s => (
            <button key={s.id} onClick={() => setSection(s.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                section === s.id
                  ? 'border-primary text-primary'
                  : 'border-transparent text-fg-3 hover:text-fg-2'
              }`}>{s.label}</button>
          ))}
        </div>
      </div>}

      <div>
        {isSuperAdmin && section === 'users' && <UserManagement api={api} />}
        {isSuperAdmin && section === 'operators' && <Operators api={api} />}
        {isSuperAdmin && section === 'email' && <EmailSender api={api} />}
        {isSuperAdmin && section === 'alarms' && <AlarmSettings api={api} />}
        {isSuperAdmin && section === 'security' && <Security api={api} onOpenAudit={() => setSection('audit')} />}
        {isSuperAdmin && section === 'audit' && <AuditSettingsPage api={api} />}
      </div>
    </div>
  )
}
