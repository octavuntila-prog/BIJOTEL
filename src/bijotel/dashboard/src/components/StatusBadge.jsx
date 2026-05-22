// StatusBadge — colored dot + label.
//
// Usage:
//   <StatusBadge tone="ok" label="VALID" />
//   <StatusBadge tone="warn" label="UNKNOWN" />
//   <StatusBadge tone="error" label="INVALID" />

const TONES = {
  ok: {
    dot: 'bg-emerald-500',
    text: 'text-emerald-700',
    pill: 'bg-emerald-50 ring-emerald-200',
  },
  warn: {
    dot: 'bg-amber-500',
    text: 'text-amber-700',
    pill: 'bg-amber-50 ring-amber-200',
  },
  error: {
    dot: 'bg-red-500',
    text: 'text-red-700',
    pill: 'bg-red-50 ring-red-200',
  },
  neutral: {
    dot: 'bg-gray-400',
    text: 'text-gray-700',
    pill: 'bg-gray-50 ring-gray-200',
  },
  info: {
    dot: 'bg-blue-500',
    text: 'text-blue-700',
    pill: 'bg-blue-50 ring-blue-200',
  },
}

export default function StatusBadge({ tone = 'neutral', label, title }) {
  const t = TONES[tone] || TONES.neutral
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${t.pill} ${t.text}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${t.dot}`} aria-hidden="true" />
      {label}
    </span>
  )
}
