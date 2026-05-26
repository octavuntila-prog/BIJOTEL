// /archive — Chain segmentation + continuity (v2.5.0, surfacing v2.2.0 + v2.3.0 REST API).
//
// Two flows on one page:
//   1. Plan + execute an archive (dry-run preview first, then commit).
//   2. Verify continuity across a list of segment DB paths.
//
// The "Active chain" panel pulls /chain/stats so the operator sees what
// they're about to operate on.

import { useEffect, useState } from 'react'
import {
  Archive as ArchiveIcon,
  Play,
  Eye,
  Link as LinkIcon,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  PlusCircle,
  Trash2,
} from 'lucide-react'
import { api, ApiError } from '../api/client.js'

function fmtBytes(n) {
  if (!n && n !== 0) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function trimHash(h, n = 16) {
  if (!h) return '—'
  return h.length > n ? `${h.slice(0, n)}…` : h
}

// ─────────────────────────── Archive section ───────────────────────────

function ArchiveSection({ stats, refetchStats }) {
  const [beforeIso, setBeforeIso] = useState('')
  const [beforeSeq, setBeforeSeq] = useState('')
  const [filterMode, setFilterMode] = useState('date')
  const [outputPath, setOutputPath] = useState('')
  const [signKeyPath, setSignKeyPath] = useState('')
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [success, setSuccess] = useState(null)

  function buildPayload(dry_run) {
    const payload = { output_path: outputPath, dry_run }
    if (filterMode === 'date' && beforeIso) {
      payload.before_iso = beforeIso
    } else if (filterMode === 'seq' && beforeSeq) {
      payload.before_seq = parseInt(beforeSeq, 10)
    }
    if (signKeyPath) payload.sign_key_path = signKeyPath
    return payload
  }

  async function run(dry_run) {
    setBusy(true)
    setErr(null)
    if (!dry_run) setSuccess(null)
    try {
      const payload = buildPayload(dry_run)
      const result = await api.archive(payload)
      if (dry_run) {
        setPreview(result)
        setSuccess(null)
      } else {
        setPreview(null)
        setSuccess(result)
        refetchStats()
      }
    } catch (e) {
      setErr(e.message || 'archive request failed')
    } finally {
      setBusy(false)
    }
  }

  const filterValid =
    (filterMode === 'date' && beforeIso) ||
    (filterMode === 'seq' && beforeSeq)
  const canSubmit = !!outputPath && filterValid && !busy

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
      <h2 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
        <ArchiveIcon size={16} /> Create archive
      </h2>
      <p className="mt-1 text-xs text-gray-600">
        Peel oldest entries off into a separate SQLite. Dry-run first;
        the source DB is untouched until you commit.
      </p>

      <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label className="block text-xs font-medium text-gray-700 mb-1">
            Filter
          </label>
          <div className="flex gap-2 mb-2">
            <button
              type="button"
              onClick={() => setFilterMode('date')}
              className={`flex-1 rounded-md border px-3 py-1.5 text-xs ${
                filterMode === 'date'
                  ? 'border-bijotel-accent bg-bijotel-accent/10 text-bijotel-accent'
                  : 'border-gray-300 text-gray-600 hover:bg-gray-50'
              }`}
            >
              Before date
            </button>
            <button
              type="button"
              onClick={() => setFilterMode('seq')}
              className={`flex-1 rounded-md border px-3 py-1.5 text-xs ${
                filterMode === 'seq'
                  ? 'border-bijotel-accent bg-bijotel-accent/10 text-bijotel-accent'
                  : 'border-gray-300 text-gray-600 hover:bg-gray-50'
              }`}
            >
              Before seq
            </button>
          </div>
          {filterMode === 'date' ? (
            <input
              type="date"
              value={beforeIso}
              onChange={(e) => setBeforeIso(e.target.value)}
              className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono"
            />
          ) : (
            <input
              type="number"
              min="1"
              placeholder="e.g. 3000"
              value={beforeSeq}
              onChange={(e) => setBeforeSeq(e.target.value)}
              className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono"
            />
          )}
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-700 mb-1">
            Output path (server-side)
          </label>
          <input
            type="text"
            placeholder="/data/archives/chain_2026-05.db"
            value={outputPath}
            onChange={(e) => setOutputPath(e.target.value)}
            className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono"
          />
        </div>

        <div className="md:col-span-2">
          <label className="block text-xs font-medium text-gray-700 mb-1">
            Ed25519 private key path (optional — emits signed JSON sidecar)
          </label>
          <input
            type="text"
            placeholder="/data/keys/bijotel_private.pem"
            value={signKeyPath}
            onChange={(e) => setSignKeyPath(e.target.value)}
            className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono"
          />
        </div>
      </div>

      <div className="mt-4 flex gap-2">
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => run(true)}
          className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          <Eye size={14} /> Preview (dry-run)
        </button>
        <button
          type="button"
          disabled={!canSubmit || !preview}
          onClick={() => run(false)}
          className="inline-flex items-center gap-1.5 rounded-md bg-bijotel-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
          title={!preview ? 'Run a dry-run first' : 'Commit the archive'}
        >
          <Play size={14} /> Archive now
        </button>
      </div>

      {err && (
        <div className="mt-4 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800 flex items-start gap-2">
          <XCircle size={16} className="mt-0.5 shrink-0" /> {err}
        </div>
      )}

      {preview && (
        <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-4">
          <div className="flex items-start gap-2">
            <AlertTriangle size={16} className="text-amber-600 mt-0.5" />
            <div className="flex-1 text-sm">
              <div className="font-medium text-amber-900">
                Dry-run preview — no changes made.
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <dt className="text-amber-800">Entries to archive:</dt>
                <dd className="font-mono">{preview.archived_count}</dd>
                <dt className="text-amber-800">Window:</dt>
                <dd className="font-mono">
                  seq {preview.first_seq} → {preview.last_seq}
                </dd>
                <dt className="text-amber-800">Source remaining:</dt>
                <dd className="font-mono">
                  {preview.main_remaining_count} entries
                </dd>
                <dt className="text-amber-800">Boundary OK:</dt>
                <dd className="font-mono">
                  {preview.boundary_next_prev_hash &&
                  preview.boundary_next_prev_hash === preview.last_hmac_hash
                    ? '✓ next.prev_hash matches last archived hmac'
                    : preview.boundary_next_prev_hash
                    ? '⚠ next.prev_hash does not match!'
                    : '— (no entries remaining after window)'}
                </dd>
              </dl>
            </div>
          </div>
        </div>
      )}

      {success && (
        <div className="mt-4 rounded-md border border-green-300 bg-green-50 p-4">
          <div className="flex items-start gap-2">
            <CheckCircle2 size={16} className="text-green-600 mt-0.5" />
            <div className="flex-1 text-sm">
              <div className="font-medium text-green-900">
                Archive complete — source DB still verifies VALID.
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <dt className="text-green-800">Archived:</dt>
                <dd className="font-mono">
                  {success.archived_count} entries (seq {success.first_seq}–
                  {success.last_seq})
                </dd>
                <dt className="text-green-800">Archive path:</dt>
                <dd className="font-mono">{success.archive_path}</dd>
                {success.segment_json_path && (
                  <>
                    <dt className="text-green-800">Signed sidecar:</dt>
                    <dd className="font-mono">{success.segment_json_path}</dd>
                  </>
                )}
                <dt className="text-green-800">Source remaining:</dt>
                <dd className="font-mono">
                  {success.main_remaining_count} entries
                </dd>
              </dl>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────── Continuity section ───────────────────────────

function ContinuitySection() {
  const [paths, setPaths] = useState([''])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [result, setResult] = useState(null)

  function update(i, v) {
    const next = [...paths]
    next[i] = v
    setPaths(next)
  }

  async function run() {
    setBusy(true)
    setErr(null)
    setResult(null)
    try {
      const cleaned = paths.map((p) => p.trim()).filter(Boolean)
      if (!cleaned.length) {
        throw new Error('add at least one chain DB path')
      }
      const r = await api.verifyContinuity(cleaned)
      setResult(r)
    } catch (e) {
      setErr(e.message || 'continuity verification failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
      <h2 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
        <LinkIcon size={16} /> Verify continuity across segments
      </h2>
      <p className="mt-1 text-xs text-gray-600">
        Walk an ordered list of chain DBs and confirm{' '}
        <code>archive_N.last_hmac == archive_N+1.first_prev_hash</code> for
        every adjacent pair. Pass the segments oldest-first.
      </p>

      <div className="mt-4 space-y-2">
        {paths.map((p, i) => (
          <div key={i} className="flex gap-2">
            <input
              type="text"
              placeholder={
                i === 0
                  ? '/data/archives/chain_2026-04.db (oldest segment)'
                  : i === paths.length - 1
                  ? '/data/chain.db (current live chain)'
                  : `/data/archives/chain_${i}.db`
              }
              value={p}
              onChange={(e) => update(i, e.target.value)}
              className="flex-1 rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono"
            />
            <button
              type="button"
              onClick={() => setPaths(paths.filter((_, k) => k !== i))}
              disabled={paths.length === 1}
              className="rounded-md border border-gray-300 px-2 text-gray-500 hover:bg-gray-50 disabled:opacity-40"
              aria-label="Remove path"
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={() => setPaths([...paths, ''])}
          className="inline-flex items-center gap-1.5 rounded-md border border-dashed border-gray-300 px-3 py-1 text-xs text-gray-600 hover:bg-gray-50"
        >
          <PlusCircle size={12} /> Add segment
        </button>
      </div>

      <div className="mt-4">
        <button
          type="button"
          onClick={run}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md bg-bijotel-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          <LinkIcon size={14} /> Verify continuity
        </button>
      </div>

      {err && (
        <div className="mt-4 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800">
          {err}
        </div>
      )}

      {result && (
        <div className="mt-4">
          <div
            className={`rounded-md border p-3 text-sm font-medium flex items-center gap-2 ${
              result.valid
                ? 'border-green-300 bg-green-50 text-green-900'
                : 'border-red-300 bg-red-50 text-red-900'
            }`}
          >
            {result.valid ? (
              <CheckCircle2 size={16} />
            ) : (
              <XCircle size={16} />
            )}
            {result.valid
              ? `CONTINUOUS — ${result.segments.length} segments linked`
              : 'BROKEN — at least one boundary mismatch'}
          </div>

          {/* Segment list */}
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
                  <th className="pb-1 pr-3 font-medium">Segment</th>
                  <th className="pb-1 pr-3 font-medium">Seq range</th>
                  <th className="pb-1 pr-3 font-medium">Entries</th>
                  <th className="pb-1 font-medium">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {result.segments.map((s, i) => (
                  <tr key={i}>
                    <td className="py-1.5 pr-3 font-mono text-xs truncate max-w-xs">
                      {s.db_path}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs">
                      {s.first_seq ?? '—'} → {s.last_seq ?? '—'}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs">
                      {s.count ?? '—'}
                    </td>
                    <td className="py-1.5 text-xs">
                      {s.valid ? (
                        <span className="text-green-700">VALID</span>
                      ) : (
                        <span className="text-red-700">
                          INVALID{s.reason ? ` — ${s.reason}` : ''}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Boundary table */}
          {result.boundaries.length > 0 && (
            <div className="mt-4">
              <div className="text-xs font-medium text-gray-700 mb-1">
                Boundary checks
              </div>
              <div className="space-y-1.5">
                {result.boundaries.map((b, i) => (
                  <div
                    key={i}
                    className={`rounded-md border p-2 text-xs font-mono ${
                      b.matches
                        ? 'border-green-200 bg-green-50'
                        : 'border-red-200 bg-red-50'
                    }`}
                  >
                    <div className="flex items-center gap-1.5">
                      {b.matches ? (
                        <CheckCircle2 size={12} className="text-green-700" />
                      ) : (
                        <XCircle size={12} className="text-red-700" />
                      )}
                      <span className="truncate">
                        {b.from_db.split('/').pop()} →{' '}
                        {b.to_db.split('/').pop()}: {b.matches ? 'OK' : 'BREAK'}
                      </span>
                    </div>
                    {!b.matches && (
                      <div className="mt-1 ml-5 text-[10px] text-red-800">
                        expected {trimHash(b.expected)} · got{' '}
                        {trimHash(b.actual)}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────── Page shell ───────────────────────────

export default function Archive() {
  const [stats, setStats] = useState(null)
  const [statsErr, setStatsErr] = useState(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let active = true
    api
      .chainStats()
      .then((s) => active && setStats(s))
      .catch((e) => active && setStatsErr(e.message))
    return () => {
      active = false
    }
  }, [tick])

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-gray-900 flex items-center gap-2">
          <ArchiveIcon size={22} /> Chain Segmentation &amp; Archival
        </h1>
        <p className="mt-1 text-sm text-gray-600">
          Peel old entries into a separate DB so the active chain stays
          small. The boundary invariant (last archived hmac == next
          segment's first prev_hash) guarantees no entries quietly
          disappear between segments.
        </p>
      </div>

      {/* Active chain quick stats */}
      <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
          Active chain (chain.db)
        </div>
        {statsErr ? (
          <div className="mt-1 text-sm text-red-700">{statsErr}</div>
        ) : stats ? (
          <div className="mt-1 grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <div className="font-mono text-lg text-gray-900">
                {stats.total_entries}
              </div>
              <div className="text-xs text-gray-500">entries</div>
            </div>
            <div>
              <div className="font-mono text-lg text-gray-900">
                {fmtBytes(stats.db_size_bytes)}
              </div>
              <div className="text-xs text-gray-500">on-disk size</div>
            </div>
            <div>
              <div className="font-mono text-lg text-gray-900">
                {stats.age_days?.toFixed(1) ?? '—'}
              </div>
              <div className="text-xs text-gray-500">days of history</div>
            </div>
            <div>
              <div className="font-mono text-lg text-gray-900">
                {stats.entries_per_day?.toFixed(0) ?? '—'}
              </div>
              <div className="text-xs text-gray-500">entries / day</div>
            </div>
          </div>
        ) : (
          <div className="mt-1 text-sm text-gray-500">loading…</div>
        )}
      </div>

      <ArchiveSection stats={stats} refetchStats={() => setTick((t) => t + 1)} />
      <ContinuitySection />
    </div>
  )
}
