// SystemStatus — minimal /system page. Lists every layer with its
// current runtime state. Re-uses the /layers endpoint already consumed
// by the PolicyDashboard's "Layers" section but presents it as the
// primary content of its own page (deep-link target from the sidebar).

import { useEffect, useState } from 'react'
import { Activity, RefreshCw } from 'lucide-react'
import { api } from '../api/client.js'
import StatusBadge from '../components/StatusBadge.jsx'

function tone(status) {
  if (status === 'active') return 'ok'
  if (status === 'available') return 'info'
  return 'neutral'
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
