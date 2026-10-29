// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Numbered pages over a keyset-paged records endpoint (/api/scan-metrics/{id},
// /api/scan-results). The API hands rows back newest first in chunks, each chunk
// naming the cursor (`next_before`) for the next older one, and reports the
// window's `total`. This hook turns that into the page model the tables show:
// rows are fetched in chunks as the reader moves, and a page that is not loaded
// yet is reached by walking the cursor forward until it is. Nothing is ever
// re-fetched to change page, and the API keeps its index-friendly keyset shape.
import { useCallback, useEffect, useRef, useState } from 'react'

export default function usePagedRecords(api, buildPath, deps, { pageSize = 10, chunk = 200 } = {}) {
  const [rows, setRows] = useState([])         // every row loaded so far, newest first
  const [next, setNext] = useState(null)       // cursor for the next older chunk, or null
  const [total, setTotal] = useState(null)     // window total, when known
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const gen = useRef(0)                        // drops responses from a superseded request

  const fetchChunk = useCallback(async (cursor) => {
    const d = await api.get(buildPath(cursor, chunk))
    return { got: d.rows || d.results || [], next: d.has_more ? d.next_before : null, total: typeof d.total === 'number' ? d.total : null }
  }, [api, buildPath, chunk])

  // (Re)load from the newest row. Runs whenever the window or filters change.
  // The total is read once here, so pages stay put while the reader browses;
  // rows collected after this moment appear on the next reload. When the first
  // chunk already reaches the end, the rows themselves are the total.
  const reload = useCallback(async () => {
    const my = ++gen.current
    setLoading(true); setError(null)
    try {
      const r = await fetchChunk(null)
      if (my !== gen.current) return
      setRows(r.got); setNext(r.next); setTotal(r.next ? r.total : r.got.length); setPage(1)
    } catch (e) {
      if (my !== gen.current) return
      setRows([]); setNext(null); setTotal(null); setPage(1)
      setError(e)
    }
    if (my === gen.current) setLoading(false)
  }, [fetchChunk])
  useEffect(() => { reload() }, deps) // eslint-disable-line react-hooks/exhaustive-deps

  const knownPages = Math.max(1, Math.ceil(rows.length / pageSize))
  // With a total the page count is exact; without one it is the loaded pages,
  // and the pager shows "…" while the cursor says there is more.
  const pageCount = total !== null ? Math.max(1, Math.ceil(total / pageSize)) : knownPages
  const open = total === null && next !== null

  // Show page `p`, walking the cursor until its rows are loaded. If the window
  // has shrunk since the total was read (retention), settle on the last real page.
  const goTo = useCallback(async (p) => {
    const target = Math.max(1, total !== null ? Math.min(p, pageCount) : p)
    if (rows.length >= target * pageSize) { setPage(target); return }
    if (!next) { setPage(knownPages); return }
    const my = gen.current
    setLoading(true)
    let all = rows, cursor = next
    try {
      while (all.length < target * pageSize && cursor) {
        const r = await fetchChunk(cursor)
        if (my !== gen.current) return
        all = [...all, ...r.got]; cursor = r.next
      }
      setRows(all); setNext(cursor)
      if (!cursor) setTotal(all.length)
      setPage(Math.max(1, Math.min(target, Math.ceil(all.length / pageSize))))
    } catch (e) {
      if (my !== gen.current) return
      setError(e)
    }
    if (my === gen.current) setLoading(false)
  }, [rows, next, total, pageCount, knownPages, pageSize, fetchChunk])

  // The pager's trailing "…" (no total known): one more chunk, landing on its first page.
  const more = useCallback(() => goTo(knownPages + 1), [goTo, knownPages])

  const pageRows = rows.slice((page - 1) * pageSize, page * pageSize)
  return { rows: pageRows, all: rows, page, pageCount, total, open, loading, error, goTo, more, reload }
}
