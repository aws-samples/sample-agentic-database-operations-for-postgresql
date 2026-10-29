// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'

// "Briefs" under the home composer: the things that
// need the operator's attention right now, each as its own bordered row with
// an icon tile and a chevron. Built from data the app already has — unhealthy
// databases from the fleet list and in-flight investigations — so it costs no
// new backend endpoint. Renders nothing when there is nothing to show.
const MAX_ROWS = 4

const ICONS = {
  alert: 'M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z',
  search: 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z',
  db: 'M12 3C7.58 3 4 4.79 4 7v10c0 2.21 3.58 4 8 4s8-1.79 8-4V7c0-2.21-3.58-4-8-4z',
}

function Icon({ d, className }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24" aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d={d} />
    </svg>
  )
}

export default function HomeBriefs({ api, spaceId, databases = [], onAsk, onOpenInvestigation, onOpenPanel }) {
  const [running, setRunning] = useState([])

  const anyRunning = running.length > 0
  useEffect(() => {
    if (!api || !spaceId) return
    let cancelled = false
    const load = () => api.get(`/api/spaces/${spaceId}/investigations`)
      .then(d => { if (!cancelled) setRunning((d?.investigations || []).filter(i => i.status === 'running')) })
      .catch(() => {})
    load()
    // While one is running, look again so it leaves this list when it finishes. (It used
    // to refresh only because `api` changed on every render.)
    const timer = anyRunning ? setInterval(load, 20000) : null
    return () => { cancelled = true; if (timer) clearInterval(timer) }
  }, [api, spaceId, anyRunning])

  const rank = { critical: 0, warning: 1 }
  const unhealthy = databases
    .filter(d => d.last_health_score === 'critical' || d.last_health_score === 'warning')
    .sort((a, b) => rank[a.last_health_score] - rank[b.last_health_score])

  const rows = [
    ...running.slice(0, 2).map(inv => ({
      key: `inv-${inv.investigation_id}`,
      tone: '',
      icon: ICONS.search,
      title: inv.title || inv.trigger || 'Investigation',
      sub: 'Investigation running',
      onClick: () => onOpenInvestigation?.(inv.investigation_id, inv.status),
    })),
    ...unhealthy.map(db => ({
      key: `db-${db.instance_id}`,
      tone: db.last_health_score === 'critical' ? 'err' : 'warn',
      icon: ICONS.alert,
      title: `${db.display_name} is ${db.last_health_score === 'critical' ? 'critical' : 'showing warnings'}`,
      sub: `${db.engine || 'database'} · ${db.region || ''}`.replace(/ · $/, ''),
      onClick: () => onAsk?.(`What's wrong with ${db.display_name} and what should I do first?`),
    })),
  ].slice(0, MAX_ROWS)

  if (rows.length === 0) return null

  return (
    <section className="w-full mt-8 text-left" aria-label="Needs attention">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h2 className="text-sm font-medium text-fg">Needs attention</h2>
        {onOpenPanel && (
          <button type="button" onClick={() => onOpenPanel('dashboard')}
            className="text-[13px] font-medium text-primary hover:underline whitespace-nowrap">
            View more in Dashboard
          </button>
        )}
      </div>
      <div className="flex flex-col gap-2">
        {rows.map(r => (
          <button key={r.key} type="button" className="brief-row" onClick={r.onClick}>
            <span className={`brief-icon ${r.tone}`}><Icon d={r.icon} /></span>
            <span className="flex-1 min-w-0">
              <span className="block text-[13px] font-medium text-fg truncate">{r.title}</span>
              {r.sub && <span className="block text-[12px] text-fg-3 truncate">{r.sub}</span>}
            </span>
            <svg className="w-4 h-4 text-fg-muted flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
            </svg>
          </button>
        ))}
      </div>
    </section>
  )
}
