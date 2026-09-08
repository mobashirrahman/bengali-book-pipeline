export function formatCount(value: number | undefined): string {
  if (value === undefined) return '—'
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function initials(title: string): string {
  return title.split(/\s+/).filter(Boolean).slice(0, 3).map((part) => part[0]).join('').toUpperCase()
}
