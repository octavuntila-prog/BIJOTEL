// /keys — Ed25519 keypair management (v2.5.0, surfacing v2.1.0 REST API).
//
// Generates a fresh keypair server-side, displays the public PEM + short
// fingerprint, and tracks rotation history in localStorage so the operator
// can see "this key has been live since ..." without server-side state.
//
// The PRIVATE key never reaches the browser — POST /api/keygen returns the
// filesystem path on the server, never the key contents.

import { useEffect, useState } from 'react'
import {
  Copy,
  Check,
  KeyRound,
  RefreshCw,
  AlertTriangle,
  Fingerprint,
} from 'lucide-react'
import { api, ApiError } from '../api/client.js'

const LS_KEY = 'bijotel_ed25519_history'

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || '[]')
  } catch {
    return []
  }
}

function saveHistory(entry) {
  try {
    const prev = loadHistory()
    localStorage.setItem(LS_KEY, JSON.stringify([entry, ...prev].slice(0, 50)))
  } catch {
    // localStorage unavailable — silently skip
  }
}

export default function Keys() {
  const [history, setHistory] = useState(() => loadHistory())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [lastGen, setLastGen] = useState(null)
  const [confirmForce, setConfirmForce] = useState(false)
  const [copied, setCopied] = useState(false)

  const current = history[0] || null

  async function generate(force = false) {
    setBusy(true)
    setErr(null)
    try {
      const resp = await api.keygen({ force })
      const entry = {
        fingerprint: resp.fingerprint,
        private_key_path: resp.private_key_path,
        public_key_pem: resp.public_key_pem,
        generated_at: new Date().toISOString(),
        forced: force,
      }
      setLastGen(entry)
      saveHistory(entry)
      setHistory(loadHistory())
      setConfirmForce(false)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Server says key already exists — prompt the user.
        setConfirmForce(true)
      } else {
        setErr(e.message || 'keygen failed')
      }
    } finally {
      setBusy(false)
    }
  }

  async function copyPublicKey() {
    if (!current?.public_key_pem) return
    try {
      await navigator.clipboard.writeText(current.public_key_pem)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setErr('clipboard write failed — copy manually')
    }
  }

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-gray-900 flex items-center gap-2">
          <KeyRound size={22} /> Ed25519 Key Management
        </h1>
        <p className="mt-1 text-sm text-gray-600">
          Generate the signing key your auditors use to verify exports
          without the HMAC secret. The private key stays server-side; only
          the public key leaves the server.
        </p>
      </div>

      {/* Current key card */}
      <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
              Current key
            </div>
            <div className="mt-1 flex items-center gap-2">
              <Fingerprint size={18} className="text-bijotel-accent" />
              <span className="font-mono text-lg text-gray-900">
                {current?.fingerprint || '— (no key generated yet)'}
              </span>
            </div>
            {current && (
              <div className="mt-1 text-xs text-gray-500">
                Generated{' '}
                {new Date(current.generated_at).toLocaleString()} ·
                Private at <code>{current.private_key_path}</code>
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={() => generate(false)}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-md bg-bijotel-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            <RefreshCw size={14} className={busy ? 'animate-spin' : ''} />
            {current ? 'Rotate' : 'Generate'}
          </button>
        </div>

        {/* Force-overwrite confirmation */}
        {confirmForce && (
          <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3">
            <div className="flex items-start gap-2">
              <AlertTriangle size={16} className="text-amber-600 mt-0.5" />
              <div className="flex-1">
                <div className="text-sm font-medium text-amber-900">
                  Private key file already exists on server
                </div>
                <p className="mt-1 text-xs text-amber-800">
                  Rotating now invalidates every chain export already signed
                  with the previous key. Auditors with the old public key
                  will report INVALID for those archives. Continue?
                </p>
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    onClick={() => generate(true)}
                    disabled={busy}
                    className="rounded-md bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
                  >
                    Yes — rotate
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmForce(false)}
                    className="rounded-md px-3 py-1 text-xs font-medium text-amber-900 hover:bg-amber-100"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {err && (
          <div className="mt-4 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800">
            {err}
          </div>
        )}

        {/* Public PEM display */}
        {current && (
          <div className="mt-5">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-medium text-gray-700">
                Public key (share with auditors)
              </span>
              <button
                type="button"
                onClick={copyPublicKey}
                className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
              >
                {copied ? <Check size={12} /> : <Copy size={12} />}
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <pre className="overflow-x-auto rounded-md border border-gray-200 bg-gray-50 p-3 text-xs font-mono text-gray-800 leading-relaxed">
              {current.public_key_pem}
            </pre>
            <p className="mt-2 text-xs text-gray-500">
              Distribute this block (e.g. email, S3, signed PGP attachment)
              to anyone who needs to verify your signed exports. They run{' '}
              <code>bijotel verify-export export.json --public-key key.pem</code>{' '}
              against it.
            </p>
          </div>
        )}
      </div>

      {/* Rotation history */}
      {history.length > 0 && (
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-gray-900">
              Rotation history (this browser)
            </h2>
            <button
              type="button"
              onClick={() => {
                if (window.confirm('Clear local rotation history?')) {
                  localStorage.removeItem(LS_KEY)
                  setHistory([])
                }
              }}
              className="text-xs text-gray-500 hover:text-gray-700"
            >
              Clear
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
                  <th className="pb-2 pr-3 font-medium">Fingerprint</th>
                  <th className="pb-2 pr-3 font-medium">Generated</th>
                  <th className="pb-2 pr-3 font-medium">Type</th>
                  <th className="pb-2 font-medium">Server path</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {history.map((h, i) => (
                  <tr key={i} className={i === 0 ? 'bg-green-50' : ''}>
                    <td className="py-2 pr-3 font-mono text-xs">
                      {h.fingerprint}
                      {i === 0 && (
                        <span className="ml-2 inline-block rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700">
                          active
                        </span>
                      )}
                    </td>
                    <td className="py-2 pr-3 text-xs text-gray-600">
                      {new Date(h.generated_at).toLocaleString()}
                    </td>
                    <td className="py-2 pr-3 text-xs text-gray-600">
                      {h.forced ? 'rotation' : 'initial / replace'}
                    </td>
                    <td className="py-2 font-mono text-xs text-gray-500 truncate max-w-xs">
                      {h.private_key_path}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-gray-500">
            History lives in browser localStorage — it's a UX convenience,
            not the source of truth. The server filesystem ({history[0]?.private_key_path && history[0].private_key_path.split('bijotel_private')[0]}) is what
            actually persists across browser sessions.
          </p>
        </div>
      )}

      {/* How-to footer */}
      <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="text-sm font-semibold text-gray-900 mb-2">
          How the auditor uses this
        </h2>
        <ol className="ml-5 list-decimal space-y-1 text-sm text-gray-700">
          <li>You hand them the public PEM block above (out of band).</li>
          <li>
            You sign exports with the matching private key on this server —
            either via the <strong>Archive</strong> page (signed sidecar) or
            <code className="ml-1">bijotel export --sign-key</code> on the CLI.
          </li>
          <li>
            They run{' '}
            <code>bijotel verify-export export.json --public-key key.pem</code>{' '}
            and get a tamper-evidence proof without ever holding your HMAC
            secret.
          </li>
        </ol>
      </div>
    </div>
  )
}
