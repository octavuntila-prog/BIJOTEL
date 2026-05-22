// ChainExplorer — Day 8 main page. Stats cards + paginated entries +
// detail panel + verify button.

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ShieldCheck,
  Layers,
  Recycle,
  Calendar,
  RefreshCw,
  Search,
  Download,
} from 'lucide-react'
import { api, ApiError } from '../api/client.js'
import StatusBadge from '../components/StatusBadge.jsx'
import Pagination from '../components/Pagination.jsx'
import ChainEntry from '../components/ChainEntry.jsx'

const PAGE_SIZE = 50

function StatCard({ icon: Icon, label, value, sub, tone }) {
  return (
    <div className="stat-card rounded-xl bg-white ring-1 ring-gray-200 p-5 flex items-start gap-3">
      <div
        className={`h-10 w-10 grid place-items-center rounded-lg ${
          tone === 'ok'
            ? 'bg-emerald-50 text-emerald-600'
            : tone === 'info'
              ? 'bg-blue-50 text-blue-600'
              : 'bg-gray-100 text-gray-600'
        }`}
      >
        <Icon size={20} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-xs uppercase tracking-wider text-gray-500">
          {label}
        </div>
        <div className="text-2xl font-semibold text-gray-900 mt-0.5">
          {value}
        </div>
        {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
      </div>
    </div>
  )
}

function hmacTone(v) {
  if (v === true) return 'ok'
  if (v === false) return 'error'
  return 'warn'
}

function deriveProvider(spanName) {
  if (!spanName) return '—'
  const lower = spanName.toLowerCase()
  if (lower.includes('anthropic')) return 'anthropic'
  if (lower.includes('openai') || lower.includes('gpt')) return 'openai'
  return spanName.split('.')[0] || '—'
}

function formatTs(iso) {
  if (!iso) return '—'
  try {
    return iso.replace('T', ' ').replace('Z', '')
  } catch {
    return iso
  }
}

export default function ChainExplorer() {
  const [stats, setStats] = useState(null)
  const [statsErr, setStatsErr] = useState(null)

  const [entries, setEntries] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const [selectedSeq, setSelectedSeq] = useState(null)
  const [filter, setFilter] = useState('')

  const [verify, setVerify] = useState({ open: false, busy: false, result: null })

  // ---- Initial load ----
  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [s, page] = await Promise.all([
        api.chainStats().catch((e) => {
          setStatsErr(e.message)
          return null
        }),
        api.chainList({ limit: PAGE_SIZE, offset: 0 }),
      ])
      if (s) setStats(s)
      setEntries(page.entries)
      setTotal(page.total)
      setOffset(page.entries.length)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  const loadMore = useCallback(async () => {
    setLoading(true)
    try {
      const page = await api.chainList({ limit: PAGE_SIZE, offset })
      setEntries((prev) => [...prev, ...page.entries])
      setOffset(offset + page.entries.length)
      setTotal(page.total)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [offset])

  const filtered = useMemo(() => {
    if (!filter.trim()) return entries
    const q = filter.toLowerCase()
    return entries.filter((e) =>
      [e.span_name, e.trace_id, String(e.seq)]
        .filter(Boolean)
        .some((v) => v.toLowerCase().includes(q)),
    )
  }, [entries, filter])

  const runVerify = useCallback(async (full) => {
    setVerify({ open: true, busy: true, result: null })
    try {
      const r = await api.chainVerify(full)
      setVerify({ open: true, busy: false, result: r })
    } catch (e) {
      setVerify({
        open: true,
        busy: false,
        result: { valid: false, error: e.message },
      })
    }
  }, [])

  const handleExport = useCallback(async () => {
    try {
      const { blob, filename } = await api.exportChain()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      alert(`Export failed: ${e.message}`)
    }
  }, [])

  // ---- Render ----
  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto">
      {/* Header row */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Chain Explorer</h1>
          <p className="text-sm text-gray-500">
            Browse the tamper-evident HMAC chain. Click any row for the full
            canonical body.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => reload()}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            <RefreshCw size={14} /> Refresh
          </button>
          <button
            type="button"
            onClick={handleExport}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
            title="Download a signed JSON snapshot (POST /export)"
          >
            <Download size={14} /> Export
          </button>
          <button
            type="button"
            onClick={() => runVerify(false)}
            className="inline-flex items-center gap-1.5 rounded-md bg-bijotel-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
          >
            <ShieldCheck size={14} /> Verify chain
          </button>
        </div>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <StatCard
          icon={Layers}
          label="Chain entries"
          value={stats ? stats.total_entries.toLocaleString() : '—'}
          sub={stats ? `${stats.entries_per_day.toLocaleString()} per day` : ''}
          tone="info"
        />
        <StatCard
          icon={Layers}
          label="CAS entries"
          value={stats ? stats.cas_entries.toLocaleString() : '—'}
          sub={stats ? 'Unique canonical bodies' : ''}
        />
        <StatCard
          icon={Recycle}
          label="Dedup factor"
          value={stats ? `${stats.dedup_factor.toFixed(2)}×` : '—'}
          sub={stats && stats.dedup_factor > 1 ? 'Reuse detected' : 'No dedup yet'}
          tone={stats && stats.dedup_factor > 1.1 ? 'ok' : undefined}
        />
        <StatCard
          icon={Calendar}
          label="Chain age"
          value={stats ? `${stats.age_days.toFixed(1)} d` : '—'}
          sub={stats ? `since ${formatTs(stats.first_entry)}` : ''}
        />
      </div>

      {statsErr && (
        <div className="mb-4 rounded-md bg-amber-50 ring-1 ring-amber-200 p-3 text-sm text-amber-800">
          Stats unavailable: {statsErr}
        </div>
      )}

      {/* Filter */}
      <div className="mb-3 flex items-center gap-2">
        <div className="relative flex-1 max-w-md">
          <Search
            size={14}
            className="absolute left-2.5 top-2.5 text-gray-400 pointer-events-none"
          />
          <input
            type="text"
            placeholder="Filter loaded rows by span name / trace / seq…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="w-full rounded-md border border-gray-300 bg-white pl-8 pr-3 py-1.5 text-sm placeholder:text-gray-400 focus:outline-none focus:ring-1 focus:ring-bijotel-accent"
          />
        </div>
        <span className="text-xs text-gray-500">
          {filter ? `${filtered.length} of ${entries.length} shown` : ''}
        </span>
      </div>

      {/* Entries table */}
      <div className="overflow-hidden rounded-xl ring-1 ring-gray-200 bg-white">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <Th>Seq</Th>
                <Th>Timestamp</Th>
                <Th>Span</Th>
                <Th>Provider</Th>
                <Th>Hash</Th>
                <Th>HMAC</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {filtered.length === 0 && !loading && !error && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-3 py-8 text-center text-sm text-gray-500"
                  >
                    No entries match the current filter.
                  </td>
                </tr>
              )}
              {filtered.map((e) => (
                <tr
                  key={e.seq}
                  className="hover:bg-gray-50 cursor-pointer"
                  onClick={() => setSelectedSeq(e.seq)}
                >
                  <Td className="font-mono">{e.seq}</Td>
                  <Td className="text-gray-600 whitespace-nowrap">
                    {formatTs(e.timestamp)}
                  </Td>
                  <Td>{e.span_name}</Td>
                  <Td className="text-gray-600">{deriveProvider(e.span_name)}</Td>
                  <Td className="font-mono text-xs text-gray-500">
                    {(e.canonical_hash || '').slice(0, 12)}…
                  </Td>
                  <Td>
                    <StatusBadge
                      tone={hmacTone(e.hmac_valid)}
                      label={
                        e.hmac_valid === true
                          ? 'VALID'
                          : e.hmac_valid === false
                            ? 'INVALID'
                            : 'UNKNOWN'
                      }
                    />
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {error && (
          <div className="px-4 py-3 bg-red-50 text-sm text-red-700 border-t border-red-200">
            {error}
          </div>
        )}
        <div className="px-4 pb-3">
          <Pagination
            loaded={entries.length}
            total={total}
            onLoadMore={loadMore}
            loading={loading}
          />
        </div>
      </div>

      {/* Detail side panel */}
      {selectedSeq != null && (
        <ChainEntry seq={selectedSeq} onClose={() => setSelectedSeq(null)} />
      )}

      {/* Verify overlay */}
      {verify.open && (
        <div
          className="fixed inset-0 z-40 bg-black/30 grid place-items-center p-4"
          onClick={() =>
            !verify.busy && setVerify({ open: false, busy: false, result: null })
          }
        >
          <div
            className="bg-white rounded-xl shadow-xl max-w-md w-full p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold mb-2">Chain verification</h3>
            {verify.busy && (
              <p className="text-sm text-gray-600">Running smoke check…</p>
            )}
            {!verify.busy && verify.result && (
              <>
                <div className="mb-3">
                  <StatusBadge
                    tone={verify.result.valid ? 'ok' : 'error'}
                    label={verify.result.valid ? 'VALID' : 'INVALID'}
                  />
                </div>
                <dl className="grid grid-cols-3 gap-y-1 text-sm">
                  <dt className="col-span-1 text-gray-500">Entries verified</dt>
                  <dd className="col-span-2 font-mono">
                    {verify.result.entries_verified ?? '—'}
                  </dd>
                  <dt className="col-span-1 text-gray-500">First seq</dt>
                  <dd className="col-span-2 font-mono">
                    {verify.result.first_seq ?? '—'}
                  </dd>
                  <dt className="col-span-1 text-gray-500">Last seq</dt>
                  <dd className="col-span-2 font-mono">
                    {verify.result.last_seq ?? '—'}
                  </dd>
                  {verify.result.error && (
                    <>
                      <dt className="col-span-1 text-gray-500">Error</dt>
                      <dd className="col-span-2 text-red-700">
                        {verify.result.error}
                        {verify.result.error_seq != null
                          ? ` (seq=${verify.result.error_seq})`
                          : ''}
                      </dd>
                    </>
                  )}
                </dl>
                <div className="mt-4 flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => runVerify(true)}
                    className="text-xs text-gray-600 hover:text-gray-900"
                    title="Re-run with full canonical re-verification (needs BIJOTEL_HMAC_SECRET)"
                  >
                    Full verify (slow)
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setVerify({ open: false, busy: false, result: null })
                    }
                    className="rounded-md bg-bijotel-accent px-3 py-1 text-xs font-medium text-white hover:opacity-90"
                  >
                    Close
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function Th({ children }) {
  return (
    <th className="px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-500">
      {children}
    </th>
  )
}
function Td({ children, className = '' }) {
  return <td className={`px-3 py-2 text-sm ${className}`}>{children}</td>
}
