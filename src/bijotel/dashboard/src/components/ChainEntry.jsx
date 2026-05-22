// ChainEntry — slide-in detail panel for a single chain row.
//
// Lazy-fetches /chain/{seq} on open, caches per seq so reopening is instant.
// Collapsible sections for canonical body, request messages, response.

import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, X, Copy } from 'lucide-react'
import { api } from '../api/client.js'
import StatusBadge from './StatusBadge.jsx'

const _cache = new Map()

function hmacTone(valid) {
  if (valid === true) return 'ok'
  if (valid === false) return 'error'
  return 'warn'
}

function shortHash(h) {
  if (!h || typeof h !== 'string') return '—'
  return h.length > 16 ? `${h.slice(0, 10)}…${h.slice(-6)}` : h
}

function Section({ title, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border-t border-gray-200">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 py-2 px-1 text-sm font-medium text-gray-700 hover:bg-gray-50"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {title}
      </button>
      {open && <div className="pb-3 pl-5">{children}</div>}
    </div>
  )
}

function CopyButton({ text, label = 'Copy' }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(
          () => {
            setDone(true)
            setTimeout(() => setDone(false), 1200)
          },
          () => {},
        )
      }}
      className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700"
    >
      <Copy size={12} />
      {done ? 'Copied' : label}
    </button>
  )
}

function JsonView({ value }) {
  // Simple pretty-print. Tree view is overkill for v1.2.0.
  const text =
    typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  return (
    <div className="relative">
      <pre className="bg-gray-50 rounded-md p-3 text-xs overflow-x-auto max-h-80 font-mono leading-snug">
        {text}
      </pre>
      <div className="absolute top-2 right-2">
        <CopyButton text={text} />
      </div>
    </div>
  )
}

function extractMessages(body) {
  // Best-effort extraction. The canonical body carries OTel GenAI conv.
  // We look for attributes.gen_ai.prompt and gen_ai.completion if present.
  const attrs = (body && body.attributes) || {}
  const prompt = attrs['gen_ai.prompt'] || attrs.prompt
  const completion = attrs['gen_ai.completion'] || attrs.completion
  return { prompt, completion, model: attrs['gen_ai.request.model'] }
}

export default function ChainEntry({ seq, onClose }) {
  const [data, setData] = useState(_cache.get(seq) || null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(!_cache.has(seq))

  useEffect(() => {
    if (_cache.has(seq)) {
      setData(_cache.get(seq))
      setErr(null)
      setLoading(false)
      return
    }
    let active = true
    setLoading(true)
    api
      .chainDetail(seq)
      .then((d) => {
        if (!active) return
        _cache.set(seq, d)
        setData(d)
        setErr(null)
      })
      .catch((e) => {
        if (active) setErr(e.message || String(e))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [seq])

  const messages = data ? extractMessages(data.canonical_body) : {}

  return (
    <aside className="fixed inset-y-0 right-0 z-30 w-full sm:w-[28rem] max-w-full bg-white shadow-xl border-l border-gray-200 flex flex-col">
      <div className="flex items-center justify-between p-4 border-b border-gray-200">
        <div>
          <div className="text-xs uppercase tracking-wider text-gray-500">
            Chain entry
          </div>
          <div className="text-lg font-semibold">#{seq}</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="p-1.5 rounded-md hover:bg-gray-100 text-gray-500"
          aria-label="Close detail"
        >
          <X size={18} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 text-sm">
        {loading && <div className="text-gray-500">Loading…</div>}
        {err && (
          <div className="rounded-md bg-red-50 ring-1 ring-red-200 p-3 text-red-700">
            {err}
          </div>
        )}
        {data && (
          <>
            <dl className="grid grid-cols-3 gap-y-2 mb-3">
              <dt className="col-span-1 text-gray-500">Timestamp</dt>
              <dd className="col-span-2 font-mono text-xs">
                {data.timestamp}
              </dd>

              <dt className="col-span-1 text-gray-500">Span name</dt>
              <dd className="col-span-2">{data.span_name}</dd>

              <dt className="col-span-1 text-gray-500">Span kind</dt>
              <dd className="col-span-2">{data.span_kind || '—'}</dd>

              <dt className="col-span-1 text-gray-500">Model</dt>
              <dd className="col-span-2 font-mono text-xs">
                {messages.model || '—'}
              </dd>

              <dt className="col-span-1 text-gray-500">HMAC</dt>
              <dd className="col-span-2">
                <StatusBadge
                  tone={hmacTone(data.hmac_valid)}
                  label={
                    data.hmac_valid === true
                      ? 'VALID'
                      : data.hmac_valid === false
                        ? 'INVALID'
                        : 'UNKNOWN'
                  }
                />
              </dd>

              <dt className="col-span-1 text-gray-500">Trace</dt>
              <dd
                className="col-span-2 font-mono text-xs truncate"
                title={data.trace_id}
              >
                {shortHash(data.trace_id)}
              </dd>

              <dt className="col-span-1 text-gray-500">Span</dt>
              <dd
                className="col-span-2 font-mono text-xs truncate"
                title={data.span_id}
              >
                {shortHash(data.span_id)}
              </dd>

              <dt className="col-span-1 text-gray-500">Canonical</dt>
              <dd className="col-span-2 font-mono text-xs flex items-center gap-2">
                <span title={data.canonical_hash}>
                  {shortHash(data.canonical_hash)}
                </span>
                <CopyButton text={data.canonical_hash} />
              </dd>

              <dt className="col-span-1 text-gray-500">Prev</dt>
              <dd
                className="col-span-2 font-mono text-xs truncate"
                title={data.prev_hash}
              >
                {shortHash(data.prev_hash)}
              </dd>

              <dt className="col-span-1 text-gray-500">HMAC hash</dt>
              <dd
                className="col-span-2 font-mono text-xs truncate"
                title={data.hmac_hash}
              >
                {shortHash(data.hmac_hash)}
              </dd>

              <dt className="col-span-1 text-gray-500">CAS ref</dt>
              <dd
                className="col-span-2 font-mono text-xs truncate"
                title={data.cas_ref || ''}
              >
                {shortHash(data.cas_ref)}
              </dd>
            </dl>

            <Section title="Canonical body (JSON)" defaultOpen={false}>
              <JsonView value={data.canonical_body} />
            </Section>

            {messages.prompt !== undefined && (
              <Section title="Input prompt" defaultOpen={false}>
                <JsonView value={messages.prompt} />
              </Section>
            )}

            {messages.completion !== undefined && (
              <Section title="Output completion" defaultOpen={false}>
                <JsonView value={messages.completion} />
              </Section>
            )}
          </>
        )}
      </div>
    </aside>
  )
}
