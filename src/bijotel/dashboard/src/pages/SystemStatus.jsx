// SystemStatus — minimal /system page. Lists every layer with its
// current runtime state. Re-uses the /layers endpoint already consumed
// by the PolicyDashboard's "Layers" section but presents it as the
// primary content of its own page (deep-link target from the sidebar).
//
// v2.5.0 adds an "Energy & Carbon" panel that pulls /energy/summary,
// surfacing the v1.9.0 layer that was operating headlessly until now.

import { useEffect, useState } from 'react'
import { Activity, RefreshCw, Leaf } from 'lucide-react'
import { api } from '../api/client.js'
import StatusBadge from '../components/StatusBadge.jsx'

function tone(status) {
  if (status === 'active') return 'ok'
  if (status === 'available') return 'info'
  return 'neutral'
}

// ─── Energy & Carbon panel (v2.5.0 surface) ────────────────────────────────

function EnergyPanel() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    api
      .energySummary()
      .then((d) => active && setData(d))
      .catch((e) => active && setErr(e.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  if (err) return null  // route may not be wired; fail silently

  return (
    <section className="mt-8 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
          <Leaf size={16} className="text-green-600" /> Energy &amp; carbon
        </h2>
        <span className="text-xs text-gray-500">
          from <code>/energy/summary</code>
        </span>
      </div>

      {loading ? (
        <div className="text-sm text-gray-500">loading…</div>
      ) : !data?.has_data ? (
        <p className="text-sm text-gray-500">
          No data in energy log yet. Run{' '}
          <code>bijotel energy backfill --db chain.db</code> to populate
          from existing chain entries, or wait for the next sealed call
          (an <code>EnergySpanProcessor</code> must be wired alongside the
          HMAC processor).
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <div className="font-mono text-xl text-gray-900">
                {data.total_calls.toLocaleString()}
              </div>
              <div className="text-xs text-gray-500">total calls</div>
            </div>
            <div>
              <div className="font-mono text-xl text-gray-900">
                {data.total_wh.toFixed(2)}
              </div>
              <div className="text-xs text-gray-500">Wh consumed</div>
            </div>
            <div>
              <div className="font-mono text-xl text-gray-900">
                {data.total_co2_grams.toFixed(2)}
              </div>
              <div className="text-xs text-gray-500">g CO₂</div>
            </div>
            <div>
              <div className="font-mono text-xl text-gray-900">
                {data.equivalent_phone_charges.toFixed(2)}
              </div>
              <div className="text-xs text-gray-500">
                ≈ phone charges
              </div>
            </div>
          </div>

          {/* Per-model breakdown */}
          {data.per_model?.length > 0 && (
            <div className="mt-5">
              <div className="text-xs font-medium text-gray-700 mb-2">
                Per model
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
                      <th className="pb-1 pr-3 font-medium">Model</th>
                      <th className="pb-1 pr-3 font-medium text-right">Calls</th>
                      <th className="pb-1 pr-3 font-medium text-right">Wh</th>
                      <th className="pb-1 font-medium text-right">g CO₂</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {[...data.per_model]
                      .sort((a, b) => b.wh - a.wh)
                      .slice(0, 10)
                      .map((m, i) => (
                        <tr key={i}>
                          <td className="py-1 pr-3 font-mono text-xs">
                            {m.model}
                          </td>
                          <td className="py-1 pr-3 text-right font-mono text-xs">
                            {m.calls}
                          </td>
                          <td className="py-1 pr-3 text-right font-mono text-xs">
                            {m.wh.toFixed(3)}
                          </td>
                          <td className="py-1 text-right font-mono text-xs">
                            {m.co2_grams.toFixed(3)}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <p className="mt-4 text-[10px] leading-snug text-gray-500">
            Estimates from per-1K-token Wh rates + regional grid intensity.
            Directional, not ISO-14064. With v2.4.0 cache-aware costing,
            cached reads count at ~10% of normal input energy when the
            instrumentor emits <code>gen_ai.usage.cache_read.input_tokens</code>.
          </p>
        </>
      )}
    </section>
  )
}

export default function SystemStatus() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)

  const load = () => {
    setLoading(true)
    api
      .layers()
      .then((d) => {
        setData(d)
        setErr(null)
      })
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false))
  }
  useEffect(() => {
    load()
  }, [])

  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Activity size={20} /> System Status
          </h1>
          <p className="text-sm text-gray-500">
            Live runtime state of every bijuterie wired in this BIJOTEL build.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          <RefreshCw size={14} /> {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {err && (
        <div className="rounded-md bg-red-50 ring-1 ring-red-200 p-3 text-sm text-red-700">
          {err}
        </div>
      )}

      {data && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
            <Counter label="Total" value={data.total} />
            <Counter label="Active" value={data.active} tone="ok" />
            <Counter label="Available" value={data.available} tone="info" />
            <Counter label="Planned" value={data.planned} tone="neutral" />
          </div>

          <div className="overflow-hidden rounded-xl ring-1 ring-gray-200 bg-white">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <Th>Bijuterie</Th>
                  <Th>Layer</Th>
                  <Th>Status</Th>
                  <Th>Note</Th>
                  <Th>Metrics</Th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {data.layers.map((layer) => (
                  <tr key={layer.id}>
                    <Td className="font-mono text-xs text-gray-500">
                      {layer.bijuterie}
                    </Td>
                    <Td className="font-medium">
                      {layer.id.replace(/_/g, ' ')}
                    </Td>
                    <Td>
                      <StatusBadge tone={tone(layer.status)} label={layer.status} />
                    </Td>
                    <Td className="text-xs text-gray-600 max-w-md">
                      {layer.note || '—'}
                    </Td>
                    <Td className="text-xs font-mono text-gray-700">
                      {layer.metrics && Object.keys(layer.metrics).length > 0
                        ? Object.entries(layer.metrics)
                            .map(
                              ([k, v]) =>
                                `${k}=${
                                  typeof v === 'object' ? JSON.stringify(v) : v
                                }`,
                            )
                            .join(' · ')
                        : '—'}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* v2.5.0 — energy + carbon surface */}
      <EnergyPanel />
    </div>
  )
}

function Counter({ label, value, tone: t }) {
  return (
    <div className="rounded-xl bg-white ring-1 ring-gray-200 p-4">
      <div className="text-xs uppercase tracking-wider text-gray-500">{label}</div>
      <div
        className={`text-2xl font-semibold mt-0.5 ${
          t === 'ok'
            ? 'text-emerald-700'
            : t === 'info'
              ? 'text-blue-700'
              : 'text-gray-900'
        }`}
      >
        {value}
      </div>
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
