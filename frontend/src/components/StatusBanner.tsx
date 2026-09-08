import type { DataMode } from '../lib/types'

interface StatusBannerProps {
  mode: DataMode
  message?: string
  onTryLive?: () => void
}

export function StatusBanner({ mode, message, onTryLive }: StatusBannerProps) {
  return <aside className={`status-banner status-banner--${mode}`} role="status">
    <span className="status-banner__dot" aria-hidden="true" />
    <span><strong>{mode === 'demo' ? 'Demo mode' : 'Live catalogue'}</strong> · {message ?? (mode === 'demo' ? 'A deterministic local preview. Records are not live.' : 'Connected to the catalogue API.')}</span>
    {mode === 'demo' && onTryLive && <button onClick={onTryLive}>Try live catalogue</button>}
  </aside>
}
