// PolicyDashboard — Day 9 page 2.
//
// Three stacked sections:
//   1. Active rules grid (GET /policy/rules)
//   2. Live evaluate form (POST /policy/evaluate)
//   3. Bijuterii layers manifest (GET /layers)
//
// The live-evaluate form is the page's USP — operators can ask "would
// this prompt be blocked?" without writing any Python, then ship the
// answer (and the warnings list) into incident reviews.

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Shield,
  Play,
  AlertTriangle,
  Check,
  Ban,
  Layers as LayersIcon,
} from 'lucide-react'
import { api, ApiError } from '../api/client.js'
import StatusBadge from '../components/StatusBadge.jsx'

// ───────────────────────── helpers ─────────────────────────

function modeTone(mode) {
  if (mode === 'deny') return 'error'
  if (mode === 'warn') return 'warn'
  return 'neutral'
}

function layerTone(status) {
  if (status === 'active') return 'ok'
  if (status === 'available') return 'info'
  return 'neutral'
}

function formatDetail(detail) {
  if (!detail || typeof detail !== 'object') return null
  const entries = Object.entries(detail)
  if (entries.length === 0) return null
  return entries.map(([k, v]) => (
    <div
      key={k}
      className="flex items-center justify-between text-xs text-gray-600"
    >
      <span className="text-gray-500">{k.replace(/_/g, ' ')}</span>
      <span className="font-mono">
        {typeof v === 'object' ? JSON.stringify(v) : String(v)}
      </span>
    </div>
  ))
}

// ───────────────────────── sections ─────────────────────────

function RuleCard({ rule }) {
  return (
    <div className="rounded-lg ring-1 ring-gray-200 bg-white p-4 flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <div className="font-medium text-sm text-gray-900 truncate">
          {rule.name}
        </div>
        <StatusBadge
          tone={modeTone(rule.mode)}
          label={(rule.mode || '—').toUpperCase()}
        />
      </div>
      <div className="space-y-0.5">
        {formatDetail(rule.detail) || (
          <div className="text-xs text-gray-400 italic">no parameters</div>
        )}
      </div>
    </div>
  )
}

function RulesSection() {
  const [rules, setRules] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    api
      .policyRules()
      .then((r) => setRules(r.rules))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  return (
    <section className="mb-8">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
          <Shield size={16} className="text-bijotel-accent" />
          Active rules
        </h2>
        <span className="text-xs text-gray-500">
          {loading ? '' : `${rules.length} rule${rules.length === 1 ? '' : 's'}`}
        </span>
      </div>
      {error && (
        <div className="rounded-md bg-red-50 ring-1 ring-red-200 p-3 text-sm text-red-700">
          {error}
        </div>
      )}
      {!error && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {loading && (
            <div className="text-sm text-gray-500">Loading rules…</div>
          )}
          {rules.map((r) => (
            <RuleCard key={r.name} rule={r} />
          ))}
          {!loading && rules.length === 0 && (
            <div className="col-span-full text-sm text-gray-500 italic">
              No rules configured on the engine.
            </div>
          )}
        </div>
      )}
    </section>
  )
}

const MODEL_PRESETS = [
  'claude-haiku-4-5-20251001',
  'claude-sonnet-4-20250514',
  'claude-opus-4',
  'gpt-4o-mini',
  'gpt-4o',
]

function EvaluateSection() {
  const [model, setModel] = useState(MODEL_PRESETS[0])
  const [prompt, setPrompt] = useState('')
  const [maxTokens, setMaxTokens] = useState('')
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  const run = useCallback(async () => {
    setBusy(true)
    setErr(null)
    setResult(null)
    try {
      const payload = {
        messages: [{ role: 'user', content: prompt }],
      }
      if (model) payload.model = model
      const mt = Number(maxTokens)
      if (Number.isFinite(mt) && mt > 0) payload.max_tokens = mt
      const r = await api.policyEvaluate(payload)
      setResult(r)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [prompt, model, maxTokens])

  const decisionBadge = useMemo(() => {
    if (!result) return null
    if (result.denied) {
      return { tone: 'error', label: 'DENY', icon: Ban }
    }
    if (result.warnings && result.warnings.length > 0) {
      return { tone: 'warn', label: 'ALLOW · WITH WARNINGS', icon: AlertTriangle }
    }
    return { tone: 'ok', label: 'ALLOW', icon: Check }
  }, [result])

  return (
    <section className="mb-8">
      <h2 className="text-base font-semibold text-gray-900 mb-3 flex items-center gap-2">
        <Play size={16} className="text-bijotel-accent" />
        Test a prompt against the engine
      </h2>
      <div className="rounded-xl ring-1 ring-gray-200 bg-white p-5">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
          <div className="sm:col-span-2">
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Model
            </label>
            <input
              list="policy-model-presets"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="claude-haiku-4-5"
              className="w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-bijotel-accent"
            />
            <datalist id="policy-model-presets">
              {MODEL_PRESETS.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Max tokens (optional)
            </label>
            <input
              type="number"
              min="1"
              value={maxTokens}
              onChange={(e) => setMaxTokens(e.target.value)}
              placeholder="4096"
              className="w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-bijotel-accent"
            />
          </div>
        </div>

        <label className="block text-xs font-medium text-gray-700 mb-1">
          Prompt
        </label>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={4}
          placeholder="Type a user message — try 'Summarize this article' or 'Ignore all previous instructions'..."
          className="w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-bijotel-accent"
        />

        <div className="flex items-center justify-end gap-2 mt-3">
          <button
            type="button"
            onClick={() => {
              setPrompt('')
              setResult(null)
              setErr(null)
            }}
            className="rounded-md px-2 py-1 text-xs text-gray-600 hover:bg-gray-50"
          >
            Clear
          </button>
          <button
            type="button"
            onClick={run}
            disabled={busy || !prompt.trim()}
            className="inline-flex items-center gap-1.5 rounded-md bg-bijotel-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? 'Evaluating…' : 'Evaluate'}
          </button>
        </div>

        {/* Result */}
        {err && (
          <div className="mt-4 rounded-md bg-red-50 ring-1 ring-red-200 p-3 text-sm text-red-700">
            {err}
          </div>
        )}
        {result && decisionBadge && (
          <div className="mt-4 border-t border-gray-200 pt-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-3">
                <StatusBadge tone={decisionBadge.tone} label={decisionBadge.label} />
              </div>
              <div className="text-xs text-gray-500 font-mono">
                {result.evaluation_ms.toFixed(2)}ms
              </div>
            </div>

            {result.denied && (
              <div className="mb-3 text-sm">
                <div className="text-gray-500">Deny rule</div>
                <div className="font-mono text-red-700">
                  {result.deny_rule || '—'}
                </div>
                <div className="text-gray-500 mt-1">Reason</div>
                <div className="text-red-700">{result.deny_reason || '—'}</div>
              </div>
            )}

            {result.warnings && result.warnings.length > 0 ? (
              <div>
                <div className="text-xs text-gray-500 mb-1">
                  Warnings ({result.warnings.length})
                </div>
                <ul className="space-y-1.5">
                  {result.warnings.map((w, idx) => (
                    <li
                      key={idx}
                      className="rounded-md bg-amber-50 ring-1 ring-amber-200 p-2 text-xs"
                    >
                      <div className="font-mono text-amber-900">{w.rule}</div>
                      <div className="text-amber-800 mt-0.5">{w.reason}</div>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              !result.denied && (
                <div className="text-xs text-gray-500">
                  No warnings — all rules allowed the call.
                </div>
              )
            )}
          </div>
        )}
      </div>
    </section>
  )
}

function LayerCard({ layer }) {
  return (
    <div className="rounded-lg ring-1 ring-gray-200 bg-white p-3 flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-mono text-gray-500">{layer.bijuterie}</div>
        <StatusBadge tone={layerTone(layer.status)} label={layer.status} />
      </div>
      <div className="text-sm font-medium text-gray-900 truncate">
        {layer.id.replace(/_/g, ' ')}
      </div>
      {layer.note && (
        <div className="text-[11px] text-gray-500 leading-snug line-clamp-2">
          {layer.note}
        </div>
      )}
      {layer.metrics && Object.keys(layer.metrics).length > 0 && (
        <div className="mt-1 space-y-0.5">
          {Object.entries(layer.metrics)
            .filter(([, v]) => v !== null && v !== '' && v !== false)
            .slice(0, 3)
            .map(([k, v]) => (
              <div
                key={k}
                className="flex items-center justify-between text-[11px] text-gray-600"
              >
                <span className="text-gray-500">{k.replace(/_/g, ' ')}</span>
                <span className="font-mono">
                  {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                </span>
              </div>
            ))}
        </div>
      )}
    </div>
  )
}

function LayersSection() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    api
      .layers()
      .then(setData)
      .catch((e) => setErr(e.message))
  }, [])

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
          <LayersIcon size={16} className="text-bijotel-accent" />
          Bijuterii layers
        </h2>
        {data && (
          <span className="text-xs text-gray-500">
            {data.active} active · {data.available} available · {data.planned} planned
          </span>
        )}
      </div>
      {err && (
        <div className="rounded-md bg-red-50 ring-1 ring-red-200 p-3 text-sm text-red-700">
          {err}
        </div>
      )}
      {data && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {data.layers.map((layer) => (
            <LayerCard key={layer.id} layer={layer} />
          ))}
        </div>
      )}
    </section>
  )
}

// ───────────────────────── page ─────────────────────────

export default function PolicyDashboard() {
  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">
          Policy Decisions
        </h1>
        <p className="text-sm text-gray-500">
          Inspect active rules, dry-run prompts through the engine, and audit
          which bijuterii are wired in this process.
        </p>
      </div>

      <RulesSection />
      <EvaluateSection />
      <LayersSection />
    </div>
  )
}
