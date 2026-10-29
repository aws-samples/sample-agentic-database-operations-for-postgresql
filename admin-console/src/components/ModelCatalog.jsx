// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback } from 'react'

// Account-level model catalog (model-and-knowledge-governance). Lists Bedrock
// models discovered live in the account, annotated with enablement + metadata.
// "Discovered != enabled": a model does nothing until an admin enables it here,
// after which spaces can add it and operators can pick it. Admin-only (the whole
// admin console is gated to the `admins` group).

const TIER_STYLE = {
  low: 'bg-ok-bg text-ok-text border-ok-border',
  mid: 'bg-info-bg text-info-text border-info-border',
  high: 'bg-warn-bg text-warn-text border-warn-border',
}

export default function ModelCatalog({ api }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try { setData(await api.get('/api/models/catalog')) }
    catch (e) { setError(e.detail || 'Failed to load model catalog') }
    finally { setLoading(false) }
  }, [api])

  useEffect(() => { load() }, [load])

  const toggle = async (m) => {
    setBusy(m.model_id); setError(null)
    try {
      await api.put('/api/models/catalog', { model_id: m.model_id, enabled: !m.enabled })
      await load()
    } catch (e) { setError(e.detail || 'Update failed') }
    finally { setBusy(null) }
  }

  const setDefault = async (modelId) => {
    setBusy(modelId); setError(null)
    try { await api.put('/api/models/catalog', { account_default: modelId }); await load() }
    catch (e) { setError(e.detail || 'Update failed') }
    finally { setBusy(null) }
  }

  const refresh = async () => {
    setRefreshing(true); setError(null)
    try { await api.post('/api/models/catalog/refresh', {}); await load() }
    catch (e) { setError(e.detail || 'Refresh failed') }
    finally { setRefreshing(false) }
  }

  const models = data?.models || []
  const accountDefault = data?.account_default || ''
  // "deployment" means nothing is stored and this is the AgentModelId the stack was
  // deployed with. Worth distinguishing: an admin who sees the model they want already
  // selected will not click it, so without this the account default stays unset.
  const defaultInherited = data?.account_default_source === 'deployment'

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-fg">Model Catalog</h1>
          <p className="text-xs text-fg-3">
            The latest Claude Haiku, Sonnet and Opus available in this account and region, each as a global
            and a regional inference profile. Enable the ones your teams may use — nothing is on by default.
          </p>
        </div>
        <button onClick={refresh} disabled={refreshing}
          className="px-3 py-1.5 text-xs font-medium rounded-md border border-line-strong text-fg-2 hover:bg-surface-3 disabled:opacity-50">
          {refreshing ? 'Refreshing…' : 'Refresh models'}
        </button>
      </div>

      {data?.stale && (
        <div className="text-xs text-warn-text bg-warn-bg border border-warn-border rounded-md px-3 py-2">
          Showing cached discovery — the last Bedrock refresh did not complete. Try "Refresh models".
        </div>
      )}
      {error && (
        <div className="text-xs text-err-text bg-err-bg border border-err-border rounded-md px-3 py-2">{error}</div>
      )}

      {loading ? (
        <div className="text-sm text-fg-3 py-8 text-center">Loading…</div>
      ) : models.length === 0 ? (
        <div className="text-sm text-fg-3 py-8 text-center">
          No Claude Haiku, Sonnet or Opus inference profile found in this account/region. Confirm Bedrock model access, then Refresh.
        </div>
      ) : (
        <div className="bg-surface rounded-lg border border-line overflow-hidden">
          <table className="w-full">
            <thead className="bg-surface-2">
              <tr>
                <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Model</th>
                <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Tier</th>
                <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Status</th>
                <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Default</th>
                <th className="px-3 py-2 text-right text-[10px] font-semibold text-fg-3 uppercase">Enabled</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-2">
              {models.map(m => (
                <tr key={m.model_id} className="hover:bg-surface-3">
                  <td className="px-3 py-2">
                    <div className="text-sm text-fg">{m.display_name || m.model_id}</div>
                    <div className="text-[11px] font-mono text-fg-muted">{m.model_id}</div>
                    <div className="flex gap-1 mt-1">
                      {m.scope === 'global' && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border bg-surface-2 text-fg-2 border-line"
                          title="Global cross-Region inference: Bedrock may serve the request from any supported Region.">Global</span>
                      )}
                      {m.scope === 'regional' && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border bg-surface-2 text-fg-2 border-line"
                          title="Geographic inference: requests stay within this geography.">Regional{m.geo ? ` · ${m.geo.toUpperCase()}` : ''}</span>
                      )}
                      {m.recommended && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border bg-surface-2 text-fg-2 border-line">Recommended</span>
                      )}
                      {!m.discovered && m.superseded && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border bg-warn-bg text-warn-text border-warn-border"
                          title={m.note || ''}>Superseded</span>
                      )}
                      {!m.discovered && !m.superseded && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border bg-err-bg text-err border-err-border">No longer in Bedrock</span>
                      )}
                      {m.permitted === false && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border bg-err-bg text-err border-err-border">Not permitted by IAM</span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    {m.cost_tier ? (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded border ${TIER_STYLE[m.cost_tier] || 'bg-surface-2 text-fg-2 border-line'}`}>{m.cost_tier}</span>
                    ) : <span className="text-fg-2">—</span>}
                  </td>
                  <td className="px-3 py-2 text-xs text-fg-3">{m.enabled ? 'Enabled' : 'Available'}</td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <input type="radio" name="account-default" checked={accountDefault === m.model_id}
                      disabled={!m.enabled || busy === m.model_id}
                      onChange={() => setDefault(m.model_id)}
                      title={m.enabled ? 'Set as account default' : 'Enable first'} />
                    {defaultInherited && accountDefault === m.model_id && (
                      <span className="ml-1.5 text-[10px] px-1.5 py-0.5 rounded border bg-surface-2 text-fg-3 border-line"
                        title="Inherited from the AgentModelId stack parameter. Nothing is saved yet — click to make it the stored account default.">
                        from deployment
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => toggle(m)} disabled={busy === m.model_id}
                      className={`px-2.5 py-1 text-xs font-medium rounded-md border disabled:opacity-50 ${
                        m.enabled ? 'border-primary text-primary hover:bg-primary-subtle'
                                  : 'border-line-strong text-fg-2 hover:bg-surface-3'
                      }`}>
                      {busy === m.model_id ? '…' : (m.enabled ? 'Disable' : 'Enable')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-[11px] text-fg-muted">
        Defaults cascade: an operator's pick wins, then the space default, then the space admin's, then the
        account default. Model changes take effect on the next chat/investigation — no redeploy.
      </p>
    </div>
  )
}
