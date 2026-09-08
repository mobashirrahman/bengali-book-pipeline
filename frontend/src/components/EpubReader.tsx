import { useEffect, useRef, useState } from 'react'

interface EpubReaderProps {
  title: string
  contentUrl: string
}

type EpubState = 'loading' | 'ready' | 'error'

type EpubRendition = { display: () => Promise<unknown>; destroy?: () => void }
type EpubBook = { ready: Promise<unknown>; renderTo: (element: HTMLElement, options: { width: string; height: string; flow: 'paginated' }) => EpubRendition; destroy?: () => void }

export function EpubReader({ title, contentUrl }: EpubReaderProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<EpubState>('loading')
  const [error, setError] = useState<string>()

  useEffect(() => {
    let cancelled = false
    let book: EpubBook | undefined
    let rendition: EpubRendition | undefined
    setState('loading')
    setError(undefined)

    const render = async () => {
      try {
        const epubModule = await import('epubjs')
        if (cancelled || !containerRef.current) return
        const ePub = epubModule.default
        book = ePub(contentUrl)
        const nextRendition = book.renderTo(containerRef.current, { width: '100%', height: '100%', flow: 'paginated' })
        rendition = nextRendition
        await book.ready
        await nextRendition.display()
        if (!cancelled) setState('ready')
      } catch (reason: unknown) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : 'The EPUB could not be opened.')
          setState('error')
        }
      }
    }

    void render()
    return () => {
      cancelled = true
      rendition?.destroy?.()
      book?.destroy?.()
    }
  }, [contentUrl])

  return <section className={`epub-reader epub-reader--${state}`} aria-label={`${title} EPUB reader`}>
    {state === 'loading' && <div className="document-state"><span className="document-spinner" aria-hidden="true" /><strong>Opening EPUB</strong><span>Loading the protected edition…</span></div>}
    {state === 'error' && <div className="document-state document-state--error" role="alert"><strong>Unable to open this EPUB</strong><span>{error ?? 'The protected document returned an unreadable response.'}</span><span>Check your connection or try the download link above.</span></div>}
    <div ref={containerRef} className="epub-reader__surface" />
  </section>
}
