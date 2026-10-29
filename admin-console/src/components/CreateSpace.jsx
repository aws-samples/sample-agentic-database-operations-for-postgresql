// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { config } from '../config'
import { Check, Copy } from 'lucide-react'
import { ownerKey } from './SpaceDetail'

// `ownerId`: the caller's Cognito sub. A space is owned by its creator and the access
// role is keyed on the owner (see ownerKey), so the role name shown here is the real one.
export default function CreateSpace({ api, onCreated, onCancel, ownerId = '' }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  // "View policy" expander — lazily fetches the exact policy the backend attaches.
  const [showPolicy, setShowPolicy] = useState(false)
  const [policy, setPolicy] = useState(null)
  const [policyLoading, setPolicyLoading] = useState(false)
  const [copied, setCopied] = useState(false)

  const addTag = () => setTags([...tags, { key: '', value: '' }])
  const removeTag = (i) => setTags(tags.filter((_, idx) => idx !== i))
  const updateTag = (i, field, val) => {
    const updated = [...tags]
    updated[i][field] = val
    setTags(updated)
  }

  const togglePolicy = async () => {
    const next = !showPolicy
    setShowPolicy(next)
    if (next && !policy && !policyLoading) {
      setPolicyLoading(true)
      try {
        setPolicy(await api.get('/api/agent-role-policy'))
      } catch {
        setPolicy({ error: 'Could not load the policy. It will still be created automatically on deploy.' })
      } finally {
        setPolicyLoading(false)
      }
    }
  }

  const copyPolicy = () => {
    if (!policy) return
    navigator.clipboard?.writeText(JSON.stringify(policy.permissions_policy, null, 2))
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!name.trim()) { setError('Name is required'); return }
    setSubmitting(true)
    setError(null)
    try {
      const tagObj = {}
      tags.forEach(t => { if (t.key.trim()) tagObj[t.key.trim()] = t.value.trim() })
      // The Agent Space role is always auto-created (scoped, least-privilege).
      // Web-app access is configured later from the space's "Configure access".
      const space = await api.post('/api/spaces', {
        name: name.trim(),
        description: description.trim(),
        iam_role_arn: '',
        tags: tagObj,
      })
      onCreated(space)
    } catch (err) {
      setError(err.detail || err.message || 'Failed to create space')
    } finally { setSubmitting(false) }
  }

  return (
    <div>
      <h1 className="text-xl font-bold text-fg mb-1">Create an Agent Space</h1>
      <p className="text-sm text-fg-3 mb-6">
        Agent Spaces control what your agent can access and do. They set limits for infrastructure
        visibility, capabilities, and permissions. Create your Agent Space and connect it to your
        primary AWS account.
      </p>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Agent Space details */}
        <div className="bg-surface rounded-xl border border-line p-6">
          <h2 className="text-sm font-semibold text-fg mb-4">Agent Space details</h2>
          <div className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-fg-2 mb-1">Agent Space Name *</label>
              <input type="text" value={name} onChange={(e) => setName(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-line-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                placeholder="e.g., prod-us, staging, team-payments" />
            </div>
            <div>
              <label className="block text-xs font-medium text-fg-2 mb-1">Description (optional)</label>
              <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2}
                className="w-full px-3 py-2 text-sm border border-line-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                placeholder="Describe the purpose of this Agent Space" />
            </div>
          </div>
        </div>

        {/* Agent Space IAM role — auto-created, with a transparency expander */}
        <div className="bg-surface rounded-xl border border-line p-6">
          <h2 className="text-sm font-semibold text-fg mb-1">AWS resource access</h2>
          <p className="text-xs text-fg-3 mb-4">
            A least-privilege IAM role lets your Agent Spaces read database metrics and configuration
            in this account. There is one role per admin, not per space.
          </p>
          <div className="flex items-start gap-3 p-3 border border-line rounded-xl bg-surface-2">
            <svg className="w-4 h-4 text-ok mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <div className="text-xs text-fg-2">
              Your role is named <code className="bg-surface-3 px-1.5 py-0.5 rounded">{`${config.resourcePrefix}-agent-${ownerKey(ownerId)}`}</code> with
              read-only access to RDS, CloudWatch, Performance Insights, CloudTrail, and Secrets Manager. It is created
              with your first space and reused for every later space you create.
            </div>
          </div>
          <button type="button" onClick={togglePolicy}
            className="mt-3 text-xs text-primary hover:underline">
            {showPolicy ? 'Hide policy' : 'View the permissions this role will have'}
          </button>
          {showPolicy && (
            <div className="mt-2">
              {policyLoading ? (
                <div className="text-xs text-fg-muted">Loading policy…</div>
              ) : policy?.error ? (
                <div className="text-xs text-warn-mid">{policy.error}</div>
              ) : policy ? (
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-semibold text-fg-muted uppercase tracking-wider">Permissions policy</span>
                    <button type="button" onClick={copyPolicy} className="inline-flex items-center gap-1 text-[10px] text-primary hover:underline">
                      {copied ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy</>}
                    </button>
                  </div>
                  <pre className="bg-surface-code text-ok rounded p-3 text-[10px] overflow-x-auto max-h-64">{JSON.stringify(policy.permissions_policy, null, 2)}</pre>
                  <p className="text-[10px] text-fg-muted mt-1">
                    Trust: assumable only within this AWS account (<code className="bg-surface-3 px-1 rounded">sts:AssumeRole</code> from account root).
                  </p>
                </div>
              ) : null}
            </div>
          )}
        </div>

        {/* Tags */}
        <div className="bg-surface rounded-xl border border-line p-6">
          <h2 className="text-sm font-semibold text-fg mb-1">Include AWS tags — optional</h2>
          <p className="text-xs text-fg-3 mb-4">
            Tags help you organize, track, and manage your Agent Space instances.
          </p>
          {tags.length > 0 && (
            <div className="space-y-2 mb-3">
              {tags.map((tag, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input type="text" value={tag.key} onChange={(e) => updateTag(i, 'key', e.target.value)}
                    className="flex-1 px-3 py-1.5 text-sm border border-line-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                    placeholder="Key" />
                  <input type="text" value={tag.value} onChange={(e) => updateTag(i, 'value', e.target.value)}
                    className="flex-1 px-3 py-1.5 text-sm border border-line-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                    placeholder="Value" />
                  <button type="button" onClick={() => removeTag(i)}
                    className="text-fg-muted hover:text-err text-sm">✕</button>
                </div>
              ))}
            </div>
          )}
          <button type="button" onClick={addTag}
            className="px-3 py-1.5 text-xs font-medium text-primary border border-primary rounded-lg hover:bg-primary-subtle transition-colors">
            Add new tag
          </button>
          <p className="text-[10px] text-fg-muted mt-1">You can add up to 50 tags.</p>
        </div>

        {error && <div className="bg-err-bg border border-err-border rounded-lg p-3 text-sm text-err-text">{error}</div>}

        {/* Actions */}
        <div className="flex items-center gap-3 justify-end">
          <button type="button" onClick={onCancel}
            className="px-5 py-2 text-sm text-fg-2 hover:text-fg transition-colors">
            Cancel
          </button>
          <button type="submit" disabled={submitting}
            className="px-5 py-2 bg-primary hover:bg-primary-hover text-primary-fg text-sm font-medium rounded-lg transition-colors disabled:opacity-50">
            {submitting ? 'Creating...' : 'Create'}
          </button>
        </div>
      </form>
    </div>
  )
}
