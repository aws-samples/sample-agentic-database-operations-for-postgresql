// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// One set of list controls for every database list in the product: search, the four
// filters (engine, account, region, status), clearable filter pills, and a pager. The
// operator app's Dashboard established this vocabulary; the admin console's "found, not
// yet onboarded" and "onboarded" lists use the same component so an operator who learned
// the filters in one place finds the identical ones everywhere. The two apps are separate
// builds with no shared package, so this file is kept byte-identical in
// frontend/src/components and admin-console/src/components (like ThemeMenu / useTheme).
//
// Filtering and paging are client-side on purpose: a space holds at most a few hundred
// rows, which load once and then search/filter instantly with no round trips. Server-side
// paging only pays off at thousands of rows per space; the list endpoints can grow a
// limit/cursor then without changing this component's contract.

import { useMemo, useState } from 'react'

export const ENGINE_LABELS = {
  'aurora-postgresql': 'Aurora PostgreSQL',
  'rds-postgresql': 'RDS PostgreSQL',
  'aurora-mysql': 'Aurora MySQL',
  'rds-mysql': 'RDS MySQL',
  'sqlserver': 'RDS SQL Server',
  'oracle': 'RDS Oracle',
}

// RDS names a PostgreSQL instance `postgres`; records written before discovery
// canonicalized it still carry that spelling. Fold it into RDS PostgreSQL so the
// filter offers one engine, not two names for the same one.
const ENGINE_ALIASES = { postgres: 'rds-postgresql', postgresql: 'rds-postgresql' }
export const canonicalEngine = e => ENGINE_ALIASES[(e || '').toLowerCase()] || e || ''
export const engineLabel = e => ENGINE_LABELS[canonicalEngine(e)] || e || ''

export const DEFAULT_PAGE_SIZE = 10
export const PAGE_SIZES = [10, 20, 50]

// Free-text search across the fields an operator thinks of a database by. `extraText`
// lets a list add its own searchable text (e.g. an onboarding status label).
function matchesSearch(row, q, extraText) {
  const fields = [
    row.display_name, row.endpoint, row.engine, engineLabel(row.engine).toLowerCase(), row.region, row.account_id,
    row.database_name, row.tags?.engine_version, row.raw_identifier,
    typeof extraText === 'function' ? extraText(row) : extraText,
  ]
  return fields.some(v => (v || '').toString().toLowerCase().includes(q))
}

/**
 * State + derived lists for a filterable, pageable database list.
 *
 * @param rows            the full list (already loaded)
 * @param options.statusOf  row -> status value used by the Status filter (default: last_health_score)
 * @param options.statusOptions  [{value,label}] shown in the Status select; omit to hide it
 * @param options.searchText  row -> extra searchable text
 * @param options.pageSize  initial page size (default 10)
 */
export function useDbListControls(rows, options = {}) {
  const { statusOf = r => r.last_health_score || 'unknown', statusOptions, searchText, pageSize: initialSize = DEFAULT_PAGE_SIZE } = options
  const [search, setSearch] = useState('')
  const [engineFilter, setEngineFilter] = useState('')
  const [accountFilter, setAccountFilter] = useState('')
  const [regionFilter, setRegionFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(initialSize)

  const list = Array.isArray(rows) ? rows : []
  const engines = useMemo(() => [...new Set(list.map(r => canonicalEngine(r.engine)).filter(Boolean))].sort(), [list])
  const accounts = useMemo(() => [...new Set(list.map(r => r.account_id).filter(Boolean))].sort(), [list])
  const regions = useMemo(() => [...new Set(list.map(r => r.region).filter(Boolean))].sort(), [list])

  const filtered = useMemo(() => {
    let out = list
    const q = search.trim().toLowerCase()
    if (q) out = out.filter(r => matchesSearch(r, q, searchText))
    if (engineFilter) out = out.filter(r => canonicalEngine(r.engine) === engineFilter)
    if (accountFilter) out = out.filter(r => r.account_id === accountFilter)
    if (regionFilter) out = out.filter(r => r.region === regionFilter)
    if (statusFilter) out = out.filter(r => statusOf(r) === statusFilter)
    return out
  }, [list, search, engineFilter, accountFilter, regionFilter, statusFilter, statusOf, searchText])

  // Clamp the page when the filtered list shrinks under it.
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const safePage = Math.min(page, totalPages)
  const paged = filtered.slice((safePage - 1) * pageSize, safePage * pageSize)

  const reset = fn => v => { fn(v); setPage(1) }
  const clearAll = () => { setEngineFilter(''); setAccountFilter(''); setRegionFilter(''); setStatusFilter(''); setPage(1) }
  const activeFilters = [
    engineFilter && { label: `Engine: ${engineLabel(engineFilter)}`, clear: () => reset(setEngineFilter)('') },
    accountFilter && { label: `Account: ${accountFilter}`, clear: () => reset(setAccountFilter)('') },
    regionFilter && { label: `Region: ${regionFilter}`, clear: () => reset(setRegionFilter)('') },
    statusFilter && { label: `Status: ${(statusOptions || []).find(o => o.value === statusFilter)?.label || statusFilter}`, clear: () => reset(setStatusFilter)('') },
  ].filter(Boolean)

  return {
    // state
    search, setSearch: reset(setSearch),
    engineFilter, setEngineFilter: reset(setEngineFilter),
    accountFilter, setAccountFilter: reset(setAccountFilter),
    regionFilter, setRegionFilter: reset(setRegionFilter),
    statusFilter, setStatusFilter: reset(setStatusFilter),
    statusOptions,
    page: safePage, setPage, pageSize, setPageSize: s => { setPageSize(Number(s)); setPage(1) },
    // derived
    engines, accounts, regions, filtered, paged, totalPages, activeFilters, clearAll,
    total: list.length,
  }
}

/**
 * Page over an already-nested list (the output of a replica-nesting pass: items carrying
 * `depth`, where depth 0 is a primary and depth > 0 its replicas) so a primary and its
 * replicas are never split across two pages. Groups are the unit of paging; a page may
 * therefore hold a little more than pageSize rows when a group straddles the boundary.
 */
export function pageNested(nested, page, pageSize) {
  const groups = []
  nested.forEach(item => {
    if (!item.depth || groups.length === 0) groups.push([item])
    else groups[groups.length - 1].push(item)
  })
  const totalPages = Math.max(1, Math.ceil(groups.length / pageSize))
  const safePage = Math.min(page, totalPages)
  const rows = groups.slice((safePage - 1) * pageSize, safePage * pageSize).flat()
  return { rows, totalPages, page: safePage, totalGroups: groups.length }
}

export function FilterPill({ label, onRemove }) {
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-primary-subtle text-primary border border-primary-line">
      {label}
      <button type="button" onClick={onRemove} className="hover:text-err transition-colors" aria-label={`Clear ${label}`}>×</button>
    </span>
  )
}

const selectCls = 'text-[11px] border border-line rounded-lg px-2 py-1.5 bg-surface text-fg-2 focus:outline-none focus:ring-1 focus:ring-primary'

/**
 * The toolbar: search box + selects + pills + result count. Pass the object returned by
 * useDbListControls as `c`. `right` renders extra controls (refresh, view toggle) on the
 * right of the selects; `title` is the list heading shown left of the search box.
 */
export function DbListToolbar({ c, title, placeholder = 'Search databases...', right, noun = 'database' }) {
  const plural = n => `${n} ${noun}${n !== 1 ? 's' : ''}`
  return (
    <div>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-1 min-w-[12rem]">
          {title && <h3 className="text-xs font-semibold text-fg-2 whitespace-nowrap">{title}</h3>}
          <div className="relative flex-1 max-w-xs">
            <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input type="search" placeholder={placeholder} value={c.search} aria-label={placeholder}
              onChange={e => c.setSearch(e.target.value)}
              className="w-full pl-8 pr-3 py-1.5 text-xs border border-line rounded-lg bg-surface text-fg focus:outline-none focus:ring-1 focus:ring-primary focus:border-primary" />
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {c.engines.length > 1 && (
            <select value={c.engineFilter} onChange={e => c.setEngineFilter(e.target.value)} className={selectCls} aria-label="Filter by engine">
              <option value="">All Engines</option>
              {c.engines.map(e => <option key={e} value={e}>{engineLabel(e)}</option>)}
            </select>
          )}
          {c.accounts.length > 1 && (
            <select value={c.accountFilter} onChange={e => c.setAccountFilter(e.target.value)} className={selectCls} aria-label="Filter by account">
              <option value="">All Accounts</option>
              {c.accounts.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
          )}
          {c.regions.length > 1 && (
            <select value={c.regionFilter} onChange={e => c.setRegionFilter(e.target.value)} className={selectCls} aria-label="Filter by region">
              <option value="">All Regions</option>
              {c.regions.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          )}
          {c.statusOptions && c.statusOptions.length > 0 && (
            <select value={c.statusFilter} onChange={e => c.setStatusFilter(e.target.value)} className={selectCls} aria-label="Filter by status">
              <option value="">All Status</option>
              {c.statusOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          )}
          {right}
        </div>
      </div>
      {c.activeFilters.length > 0 && (
        <div className="flex items-center gap-1.5 mt-2 flex-wrap">
          {c.activeFilters.map((f, i) => <FilterPill key={i} label={f.label} onRemove={f.clear} />)}
          <button type="button" onClick={c.clearAll} className="text-[10px] text-fg-muted hover:text-fg-2 ml-1">Clear all</button>
        </div>
      )}
      <div className="mt-2 text-[10px] text-fg-muted">
        {c.filtered.length === c.total ? plural(c.total) : `${c.filtered.length} of ${plural(c.total)}`}
      </div>
    </div>
  )
}

/**
 * The pager row. Renders nothing when everything fits on one page. When a list pages by
 * replica group (pageNested), pass `count`/`totalPages`/`page` from that result so the
 * pager counts groups, not flat rows.
 */
export function DbListPager({ c, count, totalPages: tp, page: pg }) {
  const total = count ?? c.filtered.length
  const totalPages = tp ?? c.totalPages
  const page = pg ?? c.page
  if (total <= c.pageSize) return null
  const pages = Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
    if (totalPages <= 7) return i + 1
    if (page <= 4) return i + 1
    if (page >= totalPages - 3) return totalPages - 6 + i
    return page - 3 + i
  })
  return (
    <div className="px-3 py-2 border-t border-line-2 flex items-center justify-between flex-wrap gap-2">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-fg-3">Rows per page:</span>
        <select value={c.pageSize} onChange={e => c.setPageSize(e.target.value)} aria-label="Rows per page"
          className="text-[11px] border border-line rounded px-1.5 py-0.5 bg-surface text-fg-2">
          {PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <span className="text-[11px] text-fg-muted ml-2">
          {(page - 1) * c.pageSize + 1}–{Math.min(page * c.pageSize, total)} of {total}
        </span>
      </div>
      <div className="flex items-center gap-1">
        <button type="button" onClick={() => c.setPage(Math.max(1, page - 1))} disabled={page === 1}
          className="px-2 py-1 text-[11px] rounded border border-line text-fg-2 hover:bg-surface-3 disabled:opacity-30 disabled:cursor-not-allowed">Prev</button>
        {pages.map(p => (
          <button key={p} type="button" onClick={() => c.setPage(p)} aria-current={page === p ? 'page' : undefined}
            className={`w-7 h-7 text-[11px] rounded ${page === p ? 'bg-primary text-primary-fg' : 'text-fg-2 hover:bg-surface-3'}`}>{p}</button>
        ))}
        <button type="button" onClick={() => c.setPage(Math.min(totalPages, page + 1))} disabled={page === totalPages}
          className="px-2 py-1 text-[11px] rounded border border-line text-fg-2 hover:bg-surface-3 disabled:opacity-30 disabled:cursor-not-allowed">Next</button>
      </div>
    </div>
  )
}
