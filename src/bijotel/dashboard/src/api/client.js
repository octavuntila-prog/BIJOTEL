// BIJOTEL API client.
//
// In dev: Vite proxies /api → http://localhost:8080 (bijotel serve).
// In prod (Day 12): the dashboard is served from the same origin as the
// API; /api is stripped at the reverse-proxy layer. Either way, the
// component code uses the same paths.
//
// Auth: if an API key is stored in localStorage under "bijotel_api_key",
// it's attached as `Authorization: Bearer <key>` on every request. Empty
// or missing → no header (the backend's APIKeyMiddleware is opt-in, so
// dev mode works without any config).

const API_BASE = '/api'

const LS_KEY = 'bijotel_api_key'

export function getStoredApiKey() {
  try {
    return localStorage.getItem(LS_KEY) || ''
  } catch {
    // localStorage unavailable (private mode, server-side render, ...)
    return ''
  }
}

export function setStoredApiKey(value) {
  try {
    if (!value) {
      localStorage.removeItem(LS_KEY)
    } else {
      localStorage.setItem(LS_KEY, value)
    }
  } catch {
    // no-op; storage may be disabled
  }
}

// Custom error class — components can branch on `err.status === 401`
// without parsing the message string.
export class ApiError extends Error {
  constructor(message, { status, body } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function fetchAPI(path, options = {}) {
  const apiKey = getStoredApiKey()
  const headers = {
    Accept: 'application/json',
    ...(options.body && !options.isFormData
      ? { 'Content-Type': 'application/json' }
      : {}),
    ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    ...(options.headers || {}),
  }
  let res
  try {
    res = await fetch(`${API_BASE}${path}`, { ...options, headers })
  } catch (e) {
    throw new ApiError(`network error: ${e.message}`, { status: 0 })
  }

  // Try to parse body — JSON for application/json, text otherwise.
  const ct = res.headers.get('content-type') || ''
  let body
  if (ct.includes('application/json')) {
    body = await res.json().catch(() => null)
  } else {
    body = await res.text().catch(() => null)
  }

  if (!res.ok) {
    const detail =
      (body && typeof body === 'object' && body.detail) || body || res.statusText
    throw new ApiError(`HTTP ${res.status}: ${detail}`, {
      status: res.status,
      body,
    })
  }
  return body
}

// ---- Endpoint wrappers (mirrors the v1.1.0 OpenAPI surface) ----

export const api = {
  // Meta
  health: () => fetchAPI('/health'),
  version: () => fetchAPI('/version'),

  // Chain
  chainList: (params = {}) => {
    const q = new URLSearchParams(
      Object.fromEntries(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== ''),
      ),
    ).toString()
    return fetchAPI(`/chain${q ? `?${q}` : ''}`)
  },
  chainDetail: (seq) => fetchAPI(`/chain/${seq}`),
  chainStats: () => fetchAPI('/chain/stats'),
  chainVerify: (full = false) =>
    fetchAPI('/chain/verify', {
      method: 'POST',
      body: JSON.stringify({ full }),
    }),

  // Policy
  policyRules: () => fetchAPI('/policy/rules'),
  policyEvaluate: (payload) =>
    fetchAPI('/policy/evaluate', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // Layers
  layers: () => fetchAPI('/layers'),

  // Regression
  regressionLatest: () => fetchAPI('/regression/latest'),
  regressionHistory: (params = {}) => {
    const q = new URLSearchParams(params).toString()
    return fetchAPI(`/regression/history${q ? `?${q}` : ''}`)
  },
  regressionRun: (payload = {}) =>
    fetchAPI('/regression/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // Export (binary download — use a custom path for blob)
  exportChain: async () => {
    const apiKey = getStoredApiKey()
    const res = await fetch(`${API_BASE}/export`, {
      method: 'POST',
      headers: {
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
      },
    })
    if (!res.ok) {
      let detail
      try {
        detail = (await res.json()).detail
      } catch {
        detail = res.statusText
      }
      throw new ApiError(`HTTP ${res.status}: ${detail}`, { status: res.status })
    }
    const blob = await res.blob()
    // Pull filename from Content-Disposition; fall back to timestamped default.
    const cd = res.headers.get('content-disposition') || ''
    const match = cd.match(/filename="?([^";]+)"?/)
    const filename = match ? match[1] : `bijotel-export-${Date.now()}.json`
    return { blob, filename }
  },
  exportVerify: async (file) => {
    const apiKey = getStoredApiKey()
    const fd = new FormData()
    fd.append('file', file)
    return fetchAPI('/export/verify', {
      method: 'POST',
      body: fd,
      isFormData: true,
      headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
    })
  },

  // ─────────────────── v2.3.0 — Ed25519 + archive surface ───────────────────

  /**
   * Generate an Ed25519 keypair server-side. Returns the public key inline
   * (safe to share with auditors) and the on-disk path of the private key
   * (which never leaves the server).
   */
  keygen: (payload = {}) =>
    fetchAPI('/keygen', {
      method: 'POST',
      body: JSON.stringify({
        output_dir: payload.output_dir || './keys',
        force: !!payload.force,
      }),
    }),

  /**
   * Peel oldest chain entries into a separate archive DB. Pass
   * `dry_run: true` for a preview; `false` to actually write.
   */
  archive: (payload) =>
    fetchAPI('/archive', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /**
   * Walk an ordered list of chain DB paths and verify boundary linkage
   * for each adjacent pair.
   */
  verifyContinuity: (db_paths) =>
    fetchAPI('/verify-continuity', {
      method: 'POST',
      body: JSON.stringify({ db_paths }),
    }),

  // ─────────────────── v2.2.0 — range verify ───────────────────

  /**
   * Range-aware chain verify. `last_n`, `seq_start/seq_end`, and
   * `since_ns/until_ns` are mutually compatible (filters AND together).
   * When any is set, `full: true` is implied so per-row HMAC is recomputed
   * over the slice.
   */
  chainVerifyRange: (params = {}) =>
    fetchAPI('/chain/verify', {
      method: 'POST',
      body: JSON.stringify({ full: true, ...params }),
    }),

  // ─────────────────── v1.9.0 — energy surface ───────────────────

  energySummary: (params = {}) => {
    const q = new URLSearchParams(
      Object.fromEntries(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== ''),
      ),
    ).toString()
    return fetchAPI(`/energy/summary${q ? `?${q}` : ''}`)
  },
}
