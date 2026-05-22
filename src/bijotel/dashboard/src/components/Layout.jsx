// Layout — persistent shell shared by every page.
//
// Sidebar (dark) | Content area (light).
// Top bar shows live chain status + an API-key drawer.
// Mobile: sidebar collapses into a top-bar hamburger.

import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import {
  Link as LinkIcon,
  Shield,
  TrendingUp,
  Activity,
  Menu,
  X,
  KeyRound,
} from 'lucide-react'
import { api, getStoredApiKey, setStoredApiKey } from '../api/client.js'
import StatusBadge from './StatusBadge.jsx'

const NAV = [
  { to: '/chain', label: 'Chain Explorer', Icon: LinkIcon },
  { to: '/policy', label: 'Policy Decisions', Icon: Shield },
  { to: '/regression', label: 'Regression Monitor', Icon: TrendingUp },
  { to: '/system', label: 'System Status', Icon: Activity },
]

function Sidebar({ open, onClose }) {
  return (
    <>
      {/* Mobile overlay */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-black/30 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-40 w-64 bg-bijotel-sidebar text-white transform transition-transform duration-150 lg:translate-x-0 lg:static lg:inset-auto ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex items-center justify-between h-16 px-5 border-b border-white/10">
          <div className="flex items-center gap-2">
            <div className="h-6 w-6 rounded bg-bijotel-accent/90 grid place-items-center text-xs font-bold">
              B
            </div>
            <span className="font-semibold tracking-tight">BIJOTEL</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="lg:hidden text-white/70 hover:text-white"
            aria-label="Close menu"
          >
            <X size={20} />
          </button>
        </div>

        <nav className="px-3 py-4 space-y-0.5">
          {NAV.map(({ to, label, Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={onClose}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition ${
                  isActive
                    ? 'bg-bijotel-sidebar-hover text-white'
                    : 'text-white/70 hover:bg-bijotel-sidebar-hover hover:text-white'
                }`
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="absolute inset-x-0 bottom-0 p-4 text-xs text-white/40 border-t border-white/10">
          BIJOTEL v1.1.0 · Forensic-grade LLM audit
        </div>
      </aside>
    </>
  )
}

function ApiKeyDrawer({ onApplied }) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState(getStoredApiKey())
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
        title="Set API key (only if your server has BIJOTEL_API_KEY)"
      >
        <KeyRound size={14} />
        {value ? 'Key set' : 'API key'}
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-72 rounded-lg border border-gray-200 bg-white p-3 shadow-lg z-20">
          <label className="block text-xs font-medium text-gray-700 mb-1">
            Bearer API key
          </label>
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Paste BIJOTEL_API_KEY value"
            className="w-full rounded-md border border-gray-300 px-2 py-1 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-bijotel-accent"
          />
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setValue('')
                setStoredApiKey('')
                setOpen(false)
                onApplied?.()
              }}
              className="rounded-md px-2 py-1 text-xs text-gray-600 hover:bg-gray-50"
            >
              Clear
            </button>
            <button
              type="button"
              onClick={() => {
                setStoredApiKey(value)
                setOpen(false)
                onApplied?.()
              }}
              className="rounded-md bg-bijotel-accent px-3 py-1 text-xs font-medium text-white hover:opacity-90"
            >
              Save
            </button>
          </div>
          <p className="mt-2 text-[10px] leading-snug text-gray-500">
            Stored in browser localStorage. Cleared on Sign-out / Clear.
            Skip if server runs without auth (dev mode).
          </p>
        </div>
      )}
    </div>
  )
}

function ChainStatusPill({ tick }) {
  // Lightweight liveness signal — calls /health (always public, no auth).
  // tick: any value that changes to force a refetch (used when user clicks
  // the pill or saves a new API key).
  const [state, setState] = useState({ tone: 'neutral', label: 'loading…' })
  useEffect(() => {
    let active = true
    api
      .health()
      .then((h) => {
        if (!active) return
        const ok = h?.status === 'ok'
        setState({
          tone: ok ? 'ok' : 'warn',
          label: ok ? `API · v${h.version}` : 'degraded',
        })
      })
      .catch(() => {
        if (active) setState({ tone: 'error', label: 'unreachable' })
      })
    return () => {
      active = false
    }
  }, [tick])

  return <StatusBadge tone={state.tone} label={state.label} />
}

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [tick, setTick] = useState(0)

  return (
    <div className="flex h-full bg-bijotel-content text-gray-900">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="flex flex-col flex-1 min-w-0">
        <header className="h-16 bg-white border-b border-gray-200 flex items-center justify-between px-4 lg:px-6">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="lg:hidden text-gray-700"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open menu"
            >
              <Menu size={20} />
            </button>
            <h1 className="hidden sm:block text-sm text-gray-500">
              Forensic-grade tamper-evident audit chain
            </h1>
          </div>
          <div className="flex items-center gap-3">
            <ChainStatusPill tick={tick} />
            <ApiKeyDrawer onApplied={() => setTick((t) => t + 1)} />
          </div>
        </header>

        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
