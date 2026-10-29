// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback } from 'react'

// Model governance tab for the two cascading layers below the account catalog
// (model-and-knowledge-governance). Cascade semantics: everything enabled at the
// layer above arrives here ENABLED BY DEFAULT; this layer may switch items off
// (disable-based, so a new account enable cascades in automatically) and set its
// own default (resolution: user pick → space default → space-admin default →
// account default).
//   • mode="space"  — the space layer: available = account-enabled minus the
//     space-admin tier's disables.
//   • mode="admin"  — the space-admin tier: available = account-enabled; the
//     disables/default apply to EVERY space this admin manages.
// Look and feel deliberately mirrors ModelCatalog (the account layer) so the
// three surfaces read as one experience.

const TIER_STYLE = {
  low: 'bg-ok-bg text-ok-text border-ok-border',
  mid: 'bg-info-bg text-info-text border-info-border',
  high: 'bg-warn-bg text-warn-text border-warn-border',
}

export default function ModelsTab({ api, space = null, mode = 'space' }) {
  const isAdminTier = mode === 'admin'
  const endpoint = isAdminTier ? '/api/admin-tier/models' : `/api/spaces/${space?.space_id}/models`
  const radioGroup = isAdminTier ? 'admin-tier-default' : 'space-default'
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [enabled, setEnabled] = useState(new Set())
  const [defaultModel, setDefaultModel] = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const d = await api.get(endpoint)
      setData(d)
      // enabled_models is the EFFECTIVE list at this layer (inherited minus disables).
      setEnabled(new Set(d.enabled_models || []))
      // The radio shows the EFFECTIVE default: this layer's own override, or the
      // one cascading down from above (space admin's, then the account's).
      setDefaultModel(d.default_model || d.inherited_default || '')
    } catch (e) { setError(e.detail || 'Failed to load models') }
    finally { setLoading(false) }
  }, [api, endpoint])

  useEffect(() => { load() }, [load])

  const toggle = (mid) => {
    const next = new Set(enabled)
    if (next.has(mid)) next.delete(mid); else next.add(mid)
    setEnabled(next)
    if (defaultModel && !next.has(defaultModel)) setDefaultModel('')
  }

  const save = async () => {
    setSaving(true); setError(null)
    try {
      // Cascade layers persist DISABLES: everything available-but-unchecked.
      const disabled = (data?.available || []).map(m => m.model_id).filter(id => !enabled.has(id))
      // Only persist a default when it differs from the inherited one — selecting
      // the inherited default keeps (or restores) the cascade, so upstream changes
      // keep flowing down.
      const defaultToStore = defaultModel === (data?.inherited_default || '') ? '' : defaultModel
      await api.put(endpoint, { disabled_models: disabled, default_model: defaultToStore })
      await load()
    } catch (e) { setError(e.detail || 'Save failed') }
    finally { setSaving(false) }
  }

  const available = data?.available || []
  const inheritedDefault = data?.inherited_default || ''
  const dirty = data && (
    defaultModel !== (data.default_model || inheritedDefault || '') ||
    available.some(m => enabled.has(m.model_id) === (data.disabled_models || []).includes(m.model_id))
  )

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-fg">
          {isAdminTier ? 'Models for your spaces' : 'Models for this space'}
        </h1>
        <p className="text-xs text-fg-3">
          {isAdminTier
            ? 'Account-enabled models arrive enabled by default and cascade into every space you manage. Uncheck one to remove it from all your spaces; each space can further narrow its own list.'
            : 'Models flowing down from the layers above arrive enabled by default. Uncheck one to remove it from this space. Operators pick from this list per message; changes take effect on the next chat/investigation.'}
        </p>
      </div>

      {error && (
        <div className="text-xs text-err-text bg-err-bg border border-err-border rounded-md px-3 py-2">{error}</div>
      )}

      {loading ? (
        <div className="text-sm text-fg-3 py-8 text-center">Loading…</div>
      ) : available.length === 0 ? (
        <div className="text-sm text-fg-3 py-8 text-center">
          {isAdminTier
            ? 'No models are enabled at the account level yet. A super admin must enable models under Settings → Agent Capabilities → Model first.'
            : 'No models are flowing down to this space yet. Models must be enabled at the account level (and not switched off at the space-admin tier) before this space can use them.'}
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
              {available.map(m => {
                const on = enabled.has(m.model_id)
                return (
                  <tr key={m.model_id} className="hover:bg-surface-3">
                    <td className="px-3 py-2">
                      <div className="text-sm text-fg">{m.display_name || m.model_id}</div>
                      <div className="text-[11px] font-mono text-fg-muted">{m.model_id}</div>
                      {m.recommended && (
                        <div className="flex gap-1 mt-1">
                          <span className="text-[10px] px-1.5 py-0.5 rounded border bg-surface-2 text-fg-2 border-line">Recommended</span>
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {m.cost_tier ? (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border ${TIER_STYLE[m.cost_tier] || 'bg-surface-2 text-fg-2 border-line'}`}>{m.cost_tier}</span>
                      ) : <span className="text-fg-2">—</span>}
                    </td>
                    <td className="px-3 py-2 text-xs text-fg-3">{on ? 'Enabled' : 'Off here'}</td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <input type="radio" name={radioGroup} checked={defaultModel === m.model_id}
                        disabled={!on}
                        onChange={() => setDefaultModel(m.model_id)}
                        title={on
                          ? (isAdminTier ? 'Default for your spaces' : 'Set as space default')
                          : 'Enable first'} />
                      {!data?.default_model && inheritedDefault === m.model_id && defaultModel === m.model_id && (
                        <span className="ml-1.5 text-[10px] px-1.5 py-0.5 rounded border bg-surface-2 text-fg-3 border-line"
                          title="Cascading from the layer above; pick another model to override it here">
                          inherited
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <input type="checkbox" checked={on} onChange={() => toggle(m.model_id)}
                        title={isAdminTier ? 'On for every space you manage' : 'On for this space'} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {!loading && available.length > 0 && (
        <div className="flex items-center gap-3">
          <button onClick={save} disabled={saving || !dirty}
            className="px-4 py-1.5 text-xs font-medium rounded-md bg-primary text-primary-fg hover:bg-primary-hover disabled:opacity-50">
            {saving ? 'Saving…' : 'Save'}
          </button>
          {!defaultModel && enabled.size > 0 && (
            <span className="text-[11px] text-fg-muted">
              {isAdminTier
                ? 'No default chosen — spaces fall back to the account default.'
                : 'No default chosen — your space admin\u2019s or the account default will be used.'}
            </span>
          )}
        </div>
      )}
      <p className="text-[11px] text-fg-muted">
        Defaults cascade: an operator's pick wins, then the space default, then the space admin's, then the
        account default. Model changes take effect on the next chat/investigation — no redeploy.
      </p>
    </div>
  )
}
