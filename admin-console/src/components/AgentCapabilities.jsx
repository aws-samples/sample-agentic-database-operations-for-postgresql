// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import CapabilitiesKnowledge from './CapabilitiesKnowledge'
import CapabilitiesSkills from './CapabilitiesSkills'
import ModelCatalog from './ModelCatalog'
import ModelsTab from './ModelsTab'

// Unified "Agent Capabilities" surface for the admin console. The same experience
// is used at three cascading layers (account → space admin → space → operator):
//   • account  — account-global custom knowledge, built-in skills, the model catalog
//   • admin    — the SPACE-ADMIN tier: what cascades into every space this space
//                admin manages (their added docs + their disables of inherited items)
//   • space    — space custom knowledge (+ disable inherited), built-in skills, the
//                space model list/default (cascade: inherited-enabled minus disables)
// Sub-tabs: Knowledge | Skills | Model. The operator app reuses Knowledge + Skills
// and swaps Model for Memory.

const SUB_TABS = [
  { id: 'knowledge', label: 'Knowledge' },
  { id: 'skills', label: 'Skills' },
  { id: 'model', label: 'Model' },
]

export default function AgentCapabilities({ api, layer = 'account', space = null }) {
  const [tab, setTab] = useState('knowledge')
  const isSpace = layer === 'space'
  const isAdminTier = layer === 'admin'
  const sid = space?.space_id

  const tabs = SUB_TABS

  return (
    <div className="space-y-4">
      {layer === 'account' && (
        <p className="text-xs text-fg-3">
          What the agent knows and can do across every space. Built-in knowledge and skills are always on and
          read-only; add account-global custom knowledge and choose which models your teams may use.
        </p>
      )}
      {isAdminTier && (
        <p className="text-xs text-fg-3">
          Your space-admin tier: what cascades into every space you manage. Account-level knowledge and models
          arrive enabled by default — switch items off for your spaces, or add your own knowledge documents that
          flow down into them. Each space can further adjust its own layer.
        </p>
      )}

      {/* Sub-tab nav */}
      <div className="border-b border-line">
        <div className="flex gap-0">
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                tab === t.id ? 'border-primary text-primary'
                             : 'border-transparent text-fg-3 hover:text-fg-2'
              }`}>{t.label}</button>
          ))}
        </div>
      </div>

      {tab === 'knowledge' && (
        isSpace ? (
          <CapabilitiesKnowledge api={api}
            customBase={`/api/spaces/${sid}/knowledge/custom`}
            disabledPath={`/api/spaces/${sid}/knowledge/disabled`}
            canWrite canToggle
            scopeLabel="This Space" inheritedLabel="Inherited"
            subtitle="Documents this space adds, plus documents flowing down from the account and your space admins (toggle off to disable one for this space). Built-in knowledge is always on." />
        ) : isAdminTier ? (
          <CapabilitiesKnowledge api={api}
            customBase="/api/admin-tier/knowledge/custom"
            disabledPath="/api/admin-tier/knowledge/disabled"
            canWrite canToggle
            scopeLabel="My Spaces" inheritedLabel="Global"
            subtitle="Documents you add here cascade into every space you manage. Account-global documents arrive enabled — toggle one off to remove it from all your spaces. Built-in knowledge is always on." />
        ) : (
          <CapabilitiesKnowledge api={api}
            customBase="/api/knowledge/custom"
            canWrite
            scopeLabel="Global"
            subtitle="Account-global custom documents inherited by every space. Built-in knowledge is always on and read-only." />
        )
      )}

      {tab === 'skills' && <CapabilitiesSkills api={api} />}

      {tab === 'model' && (
        isSpace ? <ModelsTab api={api} space={space} />
          : isAdminTier ? <ModelsTab api={api} mode="admin" />
          : <ModelCatalog api={api} />
      )}

    </div>
  )
}
