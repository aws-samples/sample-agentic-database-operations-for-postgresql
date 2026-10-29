// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback, useRef } from 'react'
import { useNotifications } from './Notifications'
import { memberCountLabel } from './MemberPicker'
import { engineLabel } from './DbListControls'


export default function DbInstanceCard({ instance, onScan, onMetrics, onScans, onTrend, onCloudWatch, onSelectDatabase, api, scanSubmitted }) {
  const [scanJobs, setScanJobs] = useState([])
  const [trackedJob, setTrackedJob] = useState(null) // null | 'pending' | job_id
  const { addNotification } = useNotifications()
  const pollRef = useRef(null)

  const scoreColors = {
    critical: 'bg-err-bg-2 text-err-text border-err-border',
    warning: 'bg-warn-bg-2 text-warn-text border-warn-border',
    ok: 'bg-ok-bg-2 text-ok-text border-ok-border',
  }
  const score = instance.last_health_score || 'unknown'
  const badgeClass = scoreColors[score] || 'bg-surface-3 text-fg-2 border-line-strong'

  const fetchJobs = useCallback(async () => {
    if (!api) return []
    try {
      const data = await api.get(`/api/scan/${instance.instance_id}/jobs?limit=5`)
      const jobs = data.jobs || []
      setScanJobs(jobs)
      return jobs
    } catch { return [] }
  }, [instance.instance_id])

  // Initial fetch
  useEffect(() => { fetchJobs() }, [fetchJobs])

  // When scan is submitted, start tracking and polling
  useEffect(() => {
    if (!scanSubmitted) return
    setTrackedJob('pending')

    if (pollRef.current) clearInterval(pollRef.current)
    let currentTracked = 'pending'
    let pendingChecks = 0

    pollRef.current = setInterval(async () => {
      const jobs = await fetchJobs()

      if (currentTracked === 'pending') {
        pendingChecks++
        const activeJob = jobs.find(j => ['RUNNING', 'STARTING', 'SUBMITTED', 'PENDING', 'RUNNABLE'].includes(j.status))
        if (activeJob) {
          currentTracked = activeJob.job_id
          setTrackedJob(activeJob.job_id)
        } else {
          // Check if a job completed very quickly (already SUCCEEDED/FAILED)
          const recentDone = jobs.find(j =>
            (j.status === 'SUCCEEDED' || j.status === 'FAILED') &&
            j.created_at && (Date.now() - j.created_at < 120000)
          )
          if (recentDone || pendingChecks > 10) {
            // Job completed fast or we've been waiting too long
            if (recentDone?.status === 'SUCCEEDED') {
              addNotification({ type: 'success', title: 'Scan completed', message: 'Report is ready.', database: instance.display_name })
            } else if (recentDone?.status === 'FAILED') {
              // A scan that failed before we caught it RUNNING was cleared silently; surface
              // it like the tracked-id branch so the dashboard card reports the failure.
              addNotification({ type: 'error', title: 'Scan failed', message: recentDone.status_reason || 'Check Scans for details.', database: instance.display_name })
            }
            currentTracked = null
            setTrackedJob(null)
            clearInterval(pollRef.current)
            pollRef.current = null
          }
        }
      } else if (currentTracked && currentTracked !== 'pending') {
        const tracked = jobs.find(j => j.job_id === currentTracked)
        if (tracked && tracked.status === 'SUCCEEDED') {
          addNotification({ type: 'success', title: 'Scan completed', message: 'Report is ready.', database: instance.display_name })
          currentTracked = null
          setTrackedJob(null)
          clearInterval(pollRef.current)
          pollRef.current = null
        } else if (tracked && tracked.status === 'FAILED') {
          addNotification({ type: 'error', title: 'Scan failed', message: tracked.status_reason || 'Check Scans for details.', database: instance.display_name })
          currentTracked = null
          setTrackedJob(null)
          clearInterval(pollRef.current)
          pollRef.current = null
        }
      }
    }, 3000)

    const timeout = setTimeout(() => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
      setTrackedJob(null)
    }, 300000) // 5 min max

    return () => { clearTimeout(timeout); if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null } }
  }, [scanSubmitted])

  const hasRunningJob = scanJobs.some(j => ['RUNNING', 'STARTING', 'SUBMITTED', 'PENDING', 'RUNNABLE'].includes(j.status))
  const isScanning = hasRunningJob || !!trackedJob

  return (
    <div className="bg-surface rounded-lg border border-line p-3 shadow-sm hover:shadow-md transition-all flex flex-col h-full">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
            score === 'critical' ? 'bg-err' : score === 'warning' ? 'bg-warn' : score === 'ok' ? 'bg-ok' : 'bg-line-strong'
          }`} />
          <button onClick={() => onSelectDatabase?.(instance)}
            className="text-sm font-semibold text-fg hover:text-primary transition-colors text-left truncate"
            title="Click to set as chat scope">
            {instance.display_name}
          </button>
        </div>
        <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border flex-shrink-0 ${badgeClass}`}>
          {score.toUpperCase()}
        </span>
      </div>

      {memberCountLabel(instance.cluster_member_roles) && (
        <p className="text-[10px] text-fg-muted -mt-1 mb-1">{memberCountLabel(instance.cluster_member_roles)}</p>
      )}
      <div className="text-[11px] text-fg-3 space-y-0.5 mb-2 flex-1">
        <div className="flex justify-between">
          <span>{engineLabel(instance.engine)} {instance.tags?.engine_version || ''}</span>
          <span className="font-mono">{instance.region}</span>
        </div>
        {instance.account_id && (
          <div className="text-[10px] text-fg-muted font-mono">{instance.account_id}</div>
        )}
        <div className="text-[10px] text-fg-muted">
          {instance.last_scan_at
            ? new Date(instance.last_scan_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + new Date(instance.last_scan_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : 'No scans'}
        </div>
      </div>

      <div className="flex items-center gap-1.5 mt-auto">
        <button onClick={(e) => { e.stopPropagation(); if (!isScanning) onScan?.(instance.instance_id) }}
          disabled={isScanning}
          className={`flex-1 px-2 py-1 text-[10px] font-medium rounded transition-colors ${
            isScanning ? 'bg-surface-3 text-fg-muted' : 'bg-primary text-primary-fg hover:bg-primary-hover'}`}>
          {isScanning ? 'Scanning...' : '⚡ Scan'}
        </button>
        <button onClick={(e) => { e.stopPropagation(); onSelectDatabase?.(instance) }}
          className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors" title="Chat">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
          </svg>
        </button>
        {/* One action per drawer tab, in the drawer's order: Metrics, Deep scans, Trends, CloudWatch metrics. */}
        <button onClick={(e) => { e.stopPropagation(); onMetrics?.(instance) }}
          className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors" title="Metrics (Tier 1)">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 12h4l3-8 4 16 3-8h4" />
          </svg>
        </button>
        <button onClick={(e) => { e.stopPropagation(); onScans?.(instance) }}
          className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors" title="Deep scans (Tier 2) and reports">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
        </button>
        <button onClick={(e) => { e.stopPropagation(); onTrend?.(instance) }}
          className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors" title="Trends">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
          </svg>
        </button>
        <button onClick={(e) => { e.stopPropagation(); onCloudWatch?.(instance) }}
          className="p-1 text-fg-muted hover:text-primary hover:bg-primary-subtle rounded transition-colors" title="CloudWatch metrics (every published series)">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 20V10m5 10V4m5 16v-7m5 7V8M3 20h18" />
          </svg>
        </button>
      </div>
    </div>
  )
}
