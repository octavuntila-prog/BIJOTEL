// RegressionView — Day 9 page 3.
//
// Top status row → middle timeline (recharts) → bottom split:
//   left = latest-run dimension breakdown
//   right = on-demand "Run Now" form with window selector
//
// History data comes from GET /regression/history (paginated, list of
// run summaries). Latest detail comes from GET /regression/latest
// (carries dimension stats + flat anomaly list).

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  Activity,
  AlertOctagon,
  Database,
  History,
  PlayCircle,
  RefreshCw,
} from 'lucide-react'
import { api, ApiError } from '../api/client.js'
import StatusBadge from '../components/StatusBadge.jsx'

// ───────────────────────── helpers ─────────────────────────

const WINDOW_PRESETS = [50, 100, 200, 500]
const RANGE_PRESETS = {
  '24h': 24 * 3600 * 1000,
  '7d': 7 * 24 * 3600 * 1000,
  '30d': 30 * 24 * 3600 * 1000,
  all: null,
}

function statusTone(status) {
  if (status === 'clean') return 'ok'
  if (status === 'anomaly') return 'error'
  return 'warn' // insufficient_data
}

function statusLabel(status) {
  if (status === 'clean') return 'CLEAN'
  if (status === 'anomaly') return 'ANOMALY'
  if (status === 'insufficient_data') return 'INSUFFICIENT DATA'
  return (status || 'unknown').toUpperCase()
}

function formatRelative(iso) {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const ms = Date.now() - then
  if (ms < 60_000) return 'just now'
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`
  return `${Math.floor(ms / 86_400_000)}d ago`
}

function fmtFloat(n, digits = 3) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return Number(n).toFixed(digits)
}

// ───────────────────────── timeline chart ─────────────────────────

function TimelineChart({ runs, range }) {
  const data = useMemo(() => {
    const cutoff = RANGE_PRESETS[range]
    const limit = cutoff ? Date.now() - cutoff : 0
    const filtered = runs
      .filter((r) => {
        const t = new Date(r.timestamp).getTime()
        return Number.isFinite(t) && t >= limit
      })
      // Reverse to chronological order for charting
      .slice()
      .reverse()
    return filtered.map((r) => ({
      ts: new Date(r.timestamp).getTime(),
      tsLabel: r.timestamp.replace('T', ' ').replace('Z', ''),
      anomalies: r.total_anomalies,
      status: r.status,
    }))
  }, [runs, range])

  if (data.length === 0) {
    return (
      <div className="h-64 grid place-items-center text-sm text-gray-500 bg-gray-50 rounded-lg">
        No runs in the selected range. Click <span className="font-medium mx-1">Run Now</span> to start.
      </div>
    )
  }

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer>
        <AreaChart data={data} margin={{ top: 8, right: 16, left: -8, bottom: 0 }}>
          <defs>
            <linearGradient id="anomGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ef4444" stopOpacity={0.5} />
              <stop offset="100%" stopColor="#ef4444" stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis
            dataKey="ts"
            type="number"
            domain={['dataMin', 'dataMax']}
            scale="time"
            tickFormatter={(t) => {
              const d = new Date(t)
              return d.toISOString().slice(5, 16).replace('T', ' ')
            }}
            tick={{ fill: '#6b7280', fontSize: 11 }}
          />
          <YAxis
            allowDecimals={false}
            tick={{ fill: '#6b7280', fontSize: 11 }}
            label={{
              value: 'anomalies',
              angle: -90,
              position: 'insideLeft',
              style: { fill: '#6b7280', fontSize: 11 },
            }}
          />
          <Tooltip
            contentStyle={{
              fontSize: 12,
              border: '1px solid #e5e7eb',
              borderRadius: 8,
            }}
            labelFormatter={(t) => new Date(t).toISOString().replace('T', ' ').slice(0, 19) + 'Z'}
            formatter={(v) => [v, 'anomalies']}
          />
          <Area
            type="monotone"
            dataKey="anomalies"
            stroke="#ef4444"
            fill="url(#anomGrad)"
            strokeWidth={2}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

// ───────────────────────── dimension breakdown ─────────────────────────

function DimensionTable({ run }) {
  if (!run) return null
  const dims = run.dimensions || {}
  const rows = Object.entries(dims)
  if (rows.length === 0) {
    return (
      <div className="text-sm text-gray-500 italic">
        No dimension data on the latest run.
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-lg ring-1 ring-gray-200 bg-white">
      <table className="min-w-full divide-y divide-gray-200">
        <thead className="bg-gray-50">
          <tr>
            <Th>Dimension</Th>
            <Th className="text-right">Mean</Th>
            <Th className="text-right">Std dev</Th>
            <Th className="text-right">Samples</Th>
            <Th className="text-right">Anomalies</Th>
            <Th>Status</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map(([dim, stats]) => (
            <tr key={dim}>
              <Td className="font-mono text-xs">{dim}</Td>
              <Td className="text-right font-mono">{fmtFloat(stats.baseline_mean, 2)}</Td>
              <Td className="text-right font-mono">{fmtFloat(stats.baseline_std, 2)}</Td>
              <Td className="text-right font-mono">{stats.samples}</Td>
              <Td className="text-right font-mono">{stats.anomalies}</Td>
              <Td>
                <StatusBadge tone={statusTone(stats.status)} label={statusLabel(stats.status)} />
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Th({ children, className = '' }) {
  return (
    <th
      className={`px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-500 ${className}`}
    >
      {children}
    </th>
  )
}
function Td({ children, className = '' }) {
  return <td className={`px-3 py-2 text-sm ${className}`}>{children}</td>
}

// ───────────────────────── status cards ─────────────────────────

function StatusCard({ icon: Icon, label, value, sub, tone }) {
  return (
    <div className="rounded-xl bg-white ring-1 ring-gray-200 p-5 flex items-start gap-3">
      <div
        className={`h-10 w-10 grid place-items-center rounded-lg ${
          tone === 'ok'
            ? 'bg-emerald-50 text-emerald-600'
            : tone === 'error'
              ? 'bg-red-50 text-red-600'
              : tone === 'warn'
                ? 'bg-amber-50 text-amber-600'
                : 'bg-gray-100 text-gray-600'
        }`}
      >
        <Icon size={20} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-xs uppercase tracking-wider text-gray-500">{label}</div>
        <div className="text-2xl font-semibold text-gray-900 mt-0.5">{value}</div>
        {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
      </div>
    </div>
  )
}

// ───────────────────────── page ─────────────────────────

export default function RegressionView() {
  const [latest, setLatest] = useState(null)
  const [latestErr, setLatestErr] = useState(null)
  const [history, setHistory] = useState({ runs: [], total_runs: 0 })
  const [historyErr, setHistoryErr] = useState(null)
  const [range, setRange] = useState('7d')

  const [runWindow, setRunWindow] = useState(100)
  const [runZ, setRunZ] = useState(3.0)
  const [running, setRunning] = useState(false)
  const [runErr, setRunErr] = useState(null)

  const reload = useCallback(async () => {
    setLatestErr(null)
    setHistoryErr(null)
    const [latestRes, histRes] = await Promise.allSettled([
      api.regressionLatest(),
      api.regressionHistory({ limit: 100 }),
    ])
    if (latestRes.status === 'fulfilled') {
      setLatest(latestRes.value)
    } else {
      // 404 is the no-runs-yet case — not an error, just empty state.
      const msg = latestRes.reason?.message || String(latestRes.reason)
      if (latestRes.reason?.status !== 404) {
        setLatestErr(msg)
      } else {
        setLatest(null)
      }
    }
    if (histRes.status === 'fulfilled') {
      setHistory(histRes.value)
    } else {
      setHistoryErr(histRes.reason?.message || String(histRes.reason))
    }
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  const runNow = useCallback(async () => {
    setRunning(true)
    setRunErr(null)
    try {
      await api.regressionRun({ window: runWindow, z_threshold: runZ })
      await reload()
    } catch (e) {
      setRunErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }, [runWindow, runZ, reload])

  const lastAnomaly = useMemo(() => {
    const hit = history.runs.find((r) => r.total_anomalies > 0)
    return hit ? hit.timestamp : null
  }, [history])

  const currentTone = statusTone(latest?.status)
  const currentLabel = latest ? statusLabel(latest.status) : 'NO RUNS YET'

  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Regression Monitor
          </h1>
          <p className="text-sm text-gray-500">
            z-score + IQR drift detection over input_tokens / output_tokens / cost.
          </p>
        </div>
        <button
          type="button"
          onClick={reload}
          className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {/* Status row */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
        <StatusCard
          icon={Activity}
          label="Current status"
          value={
            <StatusBadge tone={currentTone} label={currentLabel} />
          }
          sub={latest ? `window=${latest.window} · z=${latest.z_threshold}` : ''}
          tone={currentTone}
        />
        <StatusCard
          icon={History}
          label="Total runs"
          value={history.total_runs.toLocaleString()}
          sub={history.runs[0] ? `latest ${formatRelative(history.runs[0].timestamp)}` : 'no runs yet'}
          tone="ok"
        />
        <StatusCard
          icon={AlertOctagon}
          label="Last anomaly"
          value={lastAnomaly ? formatRelative(lastAnomaly) : 'none'}
          sub={lastAnomaly ? lastAnomaly.replace('T', ' ').replace('Z', '') : 'clean across all runs'}
          tone={lastAnomaly ? 'warn' : 'ok'}
        />
      </div>

      {(latestErr || historyErr) && (
        <div className="mb-4 rounded-md bg-amber-50 ring-1 ring-amber-200 p-3 text-sm text-amber-800">
          {[latestErr, historyErr].filter(Boolean).join(' · ')}
        </div>
      )}

      {/* Timeline */}
      <section className="mb-8">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-base font-semibold text-gray-900">
            Anomaly timeline
          </h2>
          <div className="inline-flex rounded-md ring-1 ring-gray-200 bg-white text-xs">
            {Object.keys(RANGE_PRESETS).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => setRange(k)}
                className={`px-2.5 py-1 ${
                  range === k
                    ? 'bg-bijotel-accent text-white'
                    : 'text-gray-600 hover:bg-gray-50'
                } first:rounded-l-md last:rounded-r-md`}
              >
                {k}
              </button>
            ))}
          </div>
        </div>
        <div className="rounded-xl ring-1 ring-gray-200 bg-white p-4">
          <TimelineChart runs={history.runs} range={range} />
        </div>
      </section>

      {/* Bottom split: detail | run-now */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <h2 className="text-base font-semibold text-gray-900 mb-3 flex items-center gap-2">
            <Database size={16} className="text-bijotel-accent" />
            Latest run · dimension breakdown
          </h2>
          {latest ? (
            <>
              <DimensionTable run={latest} />
              {latest.details && latest.details.length > 0 && (
                <details className="mt-3">
                  <summary className="text-xs text-gray-600 cursor-pointer hover:text-gray-900">
                    {latest.details.length} anomaly detail
                    {latest.details.length === 1 ? '' : 's'}
                  </summary>
                  <div className="mt-2 max-h-64 overflow-y-auto rounded-lg ring-1 ring-gray-200 bg-white">
                    <table className="min-w-full text-xs">
                      <thead className="bg-gray-50 sticky top-0">
                        <tr>
                          <Th>seq</Th>
                          <Th>dimension</Th>
                          <Th className="text-right">value</Th>
                          <Th className="text-right">z</Th>
                          <Th>method</Th>
                          <Th>severity</Th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {latest.details.map((a, idx) => (
                          <tr key={`${a.seq}-${a.dimension}-${idx}`}>
                            <Td className="font-mono">{a.seq}</Td>
                            <Td className="font-mono">{a.dimension}</Td>
                            <Td className="text-right font-mono">{fmtFloat(a.value, 2)}</Td>
                            <Td className="text-right font-mono">{fmtFloat(a.z_score, 2)}</Td>
                            <Td>{a.method_triggered}</Td>
                            <Td>
                              <StatusBadge
                                tone={a.severity === 'anomaly' ? 'error' : 'warn'}
                                label={a.severity}
                              />
                            </Td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              )}
            </>
          ) : (
            <div className="rounded-xl bg-white ring-1 ring-gray-200 p-6 text-sm text-gray-500">
              No regression run persisted yet. Use <strong>Run Now</strong> →
            </div>
          )}
        </div>

        <div>
          <h2 className="text-base font-semibold text-gray-900 mb-3 flex items-center gap-2">
            <PlayCircle size={16} className="text-bijotel-accent" />
            Run on demand
          </h2>
          <div className="rounded-xl ring-1 ring-gray-200 bg-white p-4">
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Baseline window
            </label>
            <div className="inline-flex rounded-md ring-1 ring-gray-200 bg-white text-xs mb-3">
              {WINDOW_PRESETS.map((w) => (
                <button
                  key={w}
                  type="button"
                  onClick={() => setRunWindow(w)}
                  className={`px-2.5 py-1 ${
                    runWindow === w
                      ? 'bg-bijotel-accent text-white'
                      : 'text-gray-600 hover:bg-gray-50'
                  } first:rounded-l-md last:rounded-r-md`}
                >
                  {w}
                </button>
              ))}
            </div>

            <label className="block text-xs font-medium text-gray-700 mb-1">
              z-threshold
            </label>
            <input
              type="number"
              min="0.5"
              max="10"
              step="0.1"
              value={runZ}
              onChange={(e) => setRunZ(Number(e.target.value))}
              className="w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-bijotel-accent mb-3"
            />

            <button
              type="button"
              onClick={runNow}
              disabled={running}
              className="w-full inline-flex justify-center items-center gap-1.5 rounded-md bg-bijotel-accent px-3 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <PlayCircle size={14} />
              {running ? 'Running…' : 'Run Now'}
            </button>

            {runErr && (
              <div className="mt-3 rounded-md bg-red-50 ring-1 ring-red-200 p-2 text-xs text-red-700">
                {runErr}
              </div>
            )}

            <p className="mt-3 text-[11px] text-gray-500 leading-snug">
              Persists into <code className="font-mono">regression_runs</code>{' '}
              and refreshes the timeline + latest panel.
            </p>
          </div>
        </div>
      </section>
    </div>
  )
}
