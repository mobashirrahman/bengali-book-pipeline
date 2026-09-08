import { useEffect, useState } from 'react'
import { initials } from '../lib/format'

interface CoverImageProps {
  title: string
  sourceUrl?: string
  accent: string
  size?: 'small' | 'medium' | 'large'
  className?: string
  attribution?: string
}

export function CoverImage({ title, sourceUrl, accent, size = 'medium', className = '', attribution }: CoverImageProps) {
  const [state, setState] = useState<'loading' | 'loaded' | 'error'>(sourceUrl ? 'loading' : 'error')
  useEffect(() => setState(sourceUrl ? 'loading' : 'error'), [sourceUrl])
  const hasImage = Boolean(sourceUrl) && state !== 'error'
  return (
    <div className={`cover cover--${size} ${className}`} style={{ '--cover-accent': accent } as React.CSSProperties}>
      {hasImage && <img className={`cover__image ${state === 'loaded' ? 'is-loaded' : ''}`} src={sourceUrl} alt="" onLoad={() => setState('loaded')} onError={() => setState('error')} />}
      {state === 'loading' && <span className="cover__loading" aria-label="Loading cover" />}
      {!hasImage && <div className="cover__fallback" aria-label={`${title} cover unavailable`}>
        <span className="cover__mark">{initials(title)}</span>
        <span className="cover__fallback-title">{title}</span>
        <span className="cover__unavailable">Cover unavailable</span>
      </div>}
      {attribution && <span className="cover__attribution">{attribution}</span>}
    </div>
  )
}
