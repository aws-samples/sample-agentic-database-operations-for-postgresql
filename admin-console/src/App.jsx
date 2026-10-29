// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import useAuth from './hooks/useAuth'
import useApi from './hooks/useApi'
import useTheme from './hooks/useTheme'
import SpaceList from './components/SpaceList'
import CreateSpace from './components/CreateSpace'
import SpaceDetail, { SPACE_SECTIONS, SPACE_EXTRA_SECTIONS, SPACE_OVERVIEW_SECTION, AUTO_TAB } from './components/SpaceDetail'
import Settings, { SETTINGS_SECTIONS } from './components/Settings'
import AdminRail from './components/AdminRail'
import Monitoring, { MONITORING_SECTIONS } from './components/Monitoring'
import AgentCapabilities from './components/AgentCapabilities'

import LoginPage from './components/LoginPage'
import ThemeMenu from './components/ThemeMenu'
import { NotificationTray } from './components/Notifications'
import { ChevronRight } from 'lucide-react'

const RAIL_KEY = 'dbops-admin-rail-collapsed'

export default function App() {
  const { loading, error: authError, logout, login, needsLogin, isAuthenticated, isAdmin, isSpaceAdmin, userId, canManageSpace, getValidAccessToken, forceRefresh, challengeSession, completeNewPassword, mfaMode, mfaSecret, mfaOtpauthUri, completeMfaSetup, submitMfaCode, forgotMode, startForgotPassword, cancelForgotPassword, forgotPassword, confirmForgotPassword } = useAuth()
  const api = useApi(getValidAccessToken, forceRefresh)
  const appearance = useTheme()
  const [view, setView] = useState('list')
  const [selectedSpace, setSelectedSpace] = useState(null)
  const [spaceTab, setSpaceTab] = useState(AUTO_TAB)
  // Live done/locked state of the open space's stages, reported by SpaceDetail so the
  // rail's sub-items can mirror the in-page stepper.
  const [spaceStages, setSpaceStages] = useState(SPACE_SECTIONS)
  const [settingsSection, setSettingsSection] = useState('users')
  const [monitoringSection, setMonitoringSection] = useState('cadence')
  const [railCollapsed, setRailCollapsed] = useState(() => localStorage.getItem(RAIL_KEY) === '1')
  const toggleRail = () => setRailCollapsed(c => { localStorage.setItem(RAIL_KEY, c ? '0' : '1'); return !c })

  if (loading) return (
    <div className="flex items-center justify-center min-h-screen bg-canvas">
      <div className="flex items-center gap-3">
        <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <p className="text-fg-2">Loading...</p>
      </div>
    </div>
  )

  if (!isAuthenticated && needsLogin) return <LoginPage onLogin={login} onCompleteNewPassword={completeNewPassword} showNewPassword={!!challengeSession && !mfaMode && !forgotMode} error={authError} isAdmin mfaMode={mfaMode} mfaSecret={mfaSecret} mfaOtpauthUri={mfaOtpauthUri} onCompleteMfaSetup={completeMfaSetup} onSubmitMfaCode={submitMfaCode} forgotMode={forgotMode} onStartForgotPassword={startForgotPassword} onCancelForgotPassword={cancelForgotPassword} onForgotPassword={forgotPassword} onConfirmForgotPassword={confirmForgotPassword} />

  if (!isAdmin() && !isSpaceAdmin()) return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-canvas">
      <div className="bg-surface rounded-lg border border-line p-8 max-w-md text-center">
        <div className="w-12 h-12 mx-auto mb-4 rounded-full bg-err-bg flex items-center justify-center">
          <svg className="w-6 h-6 text-err" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m0 0v2m0-2h2m-2 0H10m4-6a4 4 0 11-8 0 4 4 0 018 0z" />
          </svg>
        </div>
        <h2 className="text-lg font-semibold text-fg mb-2">Access Denied</h2>
        <p className="text-sm text-fg-3 mb-4">
          You need to be in the <code className="bg-surface-3 px-1.5 py-0.5 rounded text-xs">admins</code> or
          <code className="bg-surface-3 px-1.5 py-0.5 rounded text-xs">space-admins</code> group to access the Admin Console.
          Contact your administrator.
        </p>
        <button onClick={logout} className="text-sm text-primary hover:underline">Sign out</button>
      </div>
    </div>
  )

  const openSpace = (space) => { setSelectedSpace(space); setSpaceStages(SPACE_SECTIONS); setSpaceTab(AUTO_TAB); setView('detail') }
  const backToList = () => { setSelectedSpace(null); setView('list') }
  const settingsSections = SETTINGS_SECTIONS.filter(s => isAdmin() || !s.superOnly)
  // A space's audit log is for its creator or a super admin (the server's rule).
  const canViewAudit = isAdmin() || (!!selectedSpace?.created_by && selectedSpace.created_by === userId())
  const userEmail = (() => { try { return JSON.parse(atob(localStorage.getItem('admin_id_token')?.split('.')[1] || '')).email || '' } catch { return '' } })()
  const crumbs = view === 'detail' && selectedSpace
    ? ['Agent Spaces', selectedSpace.name, [SPACE_OVERVIEW_SECTION, ...SPACE_SECTIONS, ...SPACE_EXTRA_SECTIONS].find(s => s.id === spaceTab)?.label]
    : view === 'settings' ? ['Settings', settingsSections.find(s => s.id === settingsSection)?.label]
      : view === 'monitoring' ? ['Monitoring', MONITORING_SECTIONS.find(s => s.id === monitoringSection)?.label]
        : view === 'capabilities' ? ['Agent Capabilities']
          : view === 'create' ? ['Agent Spaces', 'Create'] : ['Agent Spaces']

  return (
    <div className="flex h-screen bg-canvas">
      {/* Navigation rail: the console areas (Agent Spaces, Agent Capabilities, Monitoring,
          Settings), plus the sections of the open space or of Settings as sub-items, so
          pages carry no tab rows of their own. Settings is deployment administration and
          appears for super admins only; a space admin gets Agent Capabilities at their own
          tier instead. */}
      <AdminRail
        collapsed={railCollapsed} onToggleCollapse={toggleRail}
        view={view} onNavigate={(v) => { if (v === 'list') backToList(); else setView(v) }} canManage={isAdmin()}
        space={view === 'detail' ? selectedSpace : null} spaceSections={[SPACE_OVERVIEW_SECTION, ...spaceStages, ...(canViewAudit ? SPACE_EXTRA_SECTIONS : [])]} spaceSection={spaceTab} onSpaceSection={setSpaceTab}
        settingsSections={settingsSections} settingsSection={settingsSection} onSettingsSection={setSettingsSection}
        monitoringSections={MONITORING_SECTIONS} monitoringSection={monitoringSection} onMonitoringSection={setMonitoringSection}
        userEmail={userEmail} roleLabel={isAdmin() ? 'Administrator' : 'Space admin'} onSignOut={logout}
      />

      <div className="flex-1 flex flex-col min-w-0">
        {/* Slim header: where you are, plus the global controls. Same light chrome as the rail. */}
        <header className="bg-nav flex-shrink-0 h-11 flex items-center px-4 justify-between border-b border-line">
          <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-fg-3 text-xs min-w-0">
            {crumbs.filter(Boolean).map((c, i, arr) => (
              <span key={i} className="flex items-center gap-2 min-w-0">
                {i > 0 && <ChevronRight size={14} strokeWidth={1.75} className="text-fg-muted flex-shrink-0" aria-hidden />}
                {i === 0 && arr.length > 1 && c === 'Agent Spaces'
                  ? <button onClick={backToList} className="hover:text-fg transition-colors">{c}</button>
                  : <span className={i === arr.length - 1 ? 'text-fg truncate' : 'truncate'} aria-current={i === arr.length - 1 ? 'page' : undefined}>{c}</span>}
              </span>
            ))}
          </nav>
          <div className="flex items-center gap-3 text-xs">
            <NotificationTray />
            <ThemeMenu {...appearance} />
          </div>
        </header>

        {/* Content */}
        <main className="flex-1 overflow-y-auto">
          <div className="max-w-screen-2xl mx-auto px-6 lg:px-8 py-6">
            {view === 'list' && (
              <SpaceList api={api} onCreate={() => setView('create')} onOpen={openSpace} />
            )}
            {view === 'create' && (
              <CreateSpace api={api} ownerId={userId()} onCreated={(space) => { openSpace(space) }} onCancel={backToList} />
            )}
            {view === 'detail' && selectedSpace && (
              <SpaceDetail api={api} space={selectedSpace} onBack={backToList} canRename={isAdmin()} canManage={canManageSpace(selectedSpace)}
                canViewAudit={canViewAudit} isSuperAdmin={isAdmin()}
                onUpdate={(updated) => setSelectedSpace(updated)}
                tab={spaceTab} onTabChange={setSpaceTab} onStages={setSpaceStages} />
            )}
            {view === 'capabilities' && (
              <div className="space-y-5">
                <h1 className="text-lg font-semibold text-fg">Agent Capabilities</h1>
                <AgentCapabilities api={api} layer={isAdmin() ? 'account' : 'admin'} />
              </div>
            )}
            {view === 'settings' && isAdmin() && (
              <Settings api={api} isSuperAdmin section={settingsSection} onSectionChange={setSettingsSection} hideTabs />
            )}
            {view === 'monitoring' && (
              <Monitoring api={api} section={monitoringSection} onSectionChange={setMonitoringSection} hideTabs />
            )}
          </div>
        </main>
      </div>
    </div>
  )
}
