// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import Memories from './Memories'
import CapabilitiesKnowledge from './CapabilitiesKnowledge'
import CapabilitiesSkills from './CapabilitiesSkills'

// Operator "Agent Capabilities" surface. Same experience as the admin console,
// with Memory in place of Model:
//   • Knowledge — the caller's personal layer. Built-in docs are always on; the
//     account + space custom docs are inherited (toggle off for yourself); and the
//     operator can add their own personal docs (.md/.pdf). All custom docs live in
//     S3; this is the per-user layer.
//   • Skills    — built-in skills (read-only) + "custom coming soon".
//   • Memories  — what the agent has learned about the caller (unchanged).

const SUB_TABS = [
  { id: 'knowledge', label: 'Knowledge' },
  { id: 'skills', label: 'Skills' },
  { id: 'memories', label: 'Memories' },
]

export default function Knowledge({ api, spaceId }) {
  const [tab, setTab] = useState('knowledge')
  const tabs = SUB_TABS

  return (
    <div>
      {/* Sub-tab nav */}
      <div className="border-b border-line mb-6">
        <div className="flex gap-0">
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                tab === t.id
                  ? 'border-primary text-primary'
                  : 'border-transparent text-fg-3 hover:text-fg-2'
              }`}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'knowledge' && (
        spaceId ? (
          <CapabilitiesKnowledge api={api}
            customBase={`/api/spaces/${spaceId}/knowledge/user/custom`}
            disabledPath={`/api/spaces/${spaceId}/knowledge/user/disabled`}
            canWrite canToggle
            scopeLabel="Personal" inheritedLabel="Global & Space"
            subtitle="Your personal knowledge for this space. Built-in knowledge is always on; account and space documents are inherited (toggle off to disable one just for you); and you can add your own." />
        ) : (
          <p className="text-xs text-fg-muted">Select a space to manage its knowledge.</p>
        )
      )}
      {tab === 'skills' && <CapabilitiesSkills api={api} />}
      {tab === 'memories' && <Memories api={api} />}
    </div>
  )
}
