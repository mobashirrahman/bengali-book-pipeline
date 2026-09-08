import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { DiscoverView, HomeView, NotFoundView, ReaderView, WorkView } from './components/Views'
import { Header, MobileNav } from './components/Header'
import { StatusBanner } from './components/StatusBanner'
import { createCatalogueApi, isAbortError } from './lib/api'
import { DEMO_BOOKS, findDemoBook } from './lib/demoData'
import { workToBook } from './lib/catalogue'
import { parseRoute, routeHref } from './lib/router'
import type { BookRecord, DataMode, Route, StatsResponse } from './lib/types'

import { readShelf, shelfKey, toggleSaved, type SavedBook } from './lib/savedShelf'

const api = createCatalogueApi(import.meta.env.VITE_API_BASE_URL ?? '')
const forcedDemo = import.meta.env.VITE_DEMO_MODE === 'true'

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute())
  const [mode, setMode] = useState<DataMode>(forcedDemo ? 'demo' : 'live')
  const [books, setBooks] = useState<BookRecord[]>(forcedDemo ? DEMO_BOOKS : [])
  const [stats, setStats] = useState<StatsResponse>()
  const [loading, setLoading] = useState(!forcedDemo)
  const [error, setError] = useState<string>()
  const [liveAttempt, setLiveAttempt] = useState(0)

  const [saved, setSaved] = useState<SavedBook[]>([])
  const [shelfError, setShelfError] = useState<string>()
  useEffect(() => {
    const refresh = () => { try { setSaved(readShelf(window.localStorage, mode)) } catch { setShelfError('Browser storage is unavailable.') } }
    refresh()
    window.addEventListener('storage', refresh)
    return () => window.removeEventListener('storage', refresh)
  }, [mode])
  const toggleShelf = (book: BookRecord) => {
    try {
      const next = toggleSaved(readShelf(window.localStorage, mode), book)
      window.localStorage.setItem(shelfKey(mode), JSON.stringify(next))
      setSaved(next)
      setShelfError(undefined)
    } catch { setShelfError('Your shelf could not be saved. Browser storage may be full or disabled.') }
  }

  const navigate = useCallback((href: string) => {
    window.history.pushState({}, '', href)
    setRoute(parseRoute())
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }, [])

  useEffect(() => {
    const onPopState = () => setRoute(parseRoute())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const catalogueRequestRef = useRef(0)
  const loadCatalogue = useCallback(async (signal?: AbortSignal) => {
    if (forcedDemo) return
    const requestId = catalogueRequestRef.current + 1
    catalogueRequestRef.current = requestId
    const current = () => catalogueRequestRef.current === requestId && !signal?.aborted
    setLoading(true)
    setError(undefined)
    try {
      const [health, workRows, catalogueStats] = await Promise.all([api.health(signal), api.works({ limit: 24, signal }), api.stats(signal)])
      if (!current()) return
      if (health.status !== 'ok') throw new Error('The catalogue health check did not return an OK status.')
      setBooks(workRows.map((work) => workToBook(work, api.assetContentUrl)))
      setStats(catalogueStats)
      setMode('live')
    } catch (reason: unknown) {
      if (isAbortError(reason) || signal?.aborted) return
      if (!current()) return
      const message = reason instanceof Error ? reason.message : 'The catalogue API is unavailable.'
      setBooks(DEMO_BOOKS)
      setMode('demo')
      setError(message)
    } finally {
      if (current()) setLoading(false)
    }
  }, [liveAttempt])

  useEffect(() => {
    const controller = new AbortController()
    void loadCatalogue(controller.signal)
    return () => controller.abort()
  }, [loadCatalogue])

  const retryLive = () => { setMode('live'); setLiveAttempt((attempt) => attempt + 1) }
  const selectedId = route.name === 'work' || route.name === 'reader' ? route.id : undefined
  const selectedBook = books.find((book) => book.id === selectedId) ?? (mode === 'demo' && selectedId ? findDemoBook(selectedId) : undefined)

  const apiSearch = useCallback(async (query: string, signal?: AbortSignal) => {
    if (mode === 'demo') return []
    const response = await api.search(query || 'a', { limit: 24, signal })
    const details = await Promise.all(response.items.filter((item) => item.kind !== 'person').slice(0, 12).map(async (item) => {
      try { return workToBook(await api.work(item.work_id ?? item.id, signal), api.assetContentUrl) } catch (reason: unknown) {
        if (isAbortError(reason)) throw reason
        return undefined
      }
    }))
    return details.filter((book): book is BookRecord => Boolean(book))
  }, [mode])

  const routeBook = useRouteBook(route, mode, selectedBook, books)
  // Live shelves render exactly what the catalogue returned; the local
  // preview collection is only a fallback while in demo mode.
  const visibleBooks = mode === 'live' ? books : (books.length ? books : DEMO_BOOKS)
  useEffect(() => {
    const title = route.name === 'work' || route.name === 'reader'
      ? `${routeBook.book?.title ?? 'Work'} · Folio`
      : route.name === 'discover' ? 'Discover · Folio'
        : route.name === 'shelf' ? 'Your shelf · Folio'
          : route.name === 'notFound' ? 'Page not found · Folio' : 'Folio'
    document.title = title
  }, [route, routeBook.book])
  const bannerMessage = error ? `Showing a local preview because the API could not be reached: ${error}` : undefined
  return <div className="app-shell">
    <Header route={route} navigate={navigate} />
    <StatusBanner mode={mode} message={bannerMessage} onTryLive={mode === 'demo' && !forcedDemo ? retryLive : undefined} />
    {loading && !forcedDemo && <p className="wrap inline-status" role="status">Connecting to the live catalogue…</p>}
    {shelfError && <p className="wrap inline-error" role="alert">{shelfError}</p>}
    {route.name === 'shelf' && <main className="wrap discover-page"><div className="page-intro"><p className="eyebrow">Saved on this browser</p><h1>Your shelf</h1><p>Keep books here for later. This shelf is stored on this device; demo and live books have separate shelves.</p></div>{saved.length ? <div className="book-grid">{saved.map((item) => <article key={item.id}><h2><button className="text-button" onClick={() => navigate(`/works/${item.id}`)}>{item.title}</button></h2><p>{item.author}</p><button className="button button--outline" onClick={() => toggleShelf(item as BookRecord)}>Remove from shelf</button></article>)}</div> : <div className="empty-state"><h2>Your shelf is empty</h2><p>Open a book and choose Add to shelf.</p><button className="button button--light" onClick={() => navigate('/discover')}>Discover books</button></div>}</main>}
    {route.name === 'home' && <HomeView books={visibleBooks} mode={mode} navigate={navigate} onOpen={(book) => navigate(routeHref({ name: 'work', id: book.id }))} stats={stats} loading={loading} error={error} onRetry={mode === 'demo' && !forcedDemo ? retryLive : undefined} />}
    {route.name === 'discover' && <DiscoverView books={visibleBooks} mode={mode} navigate={navigate} onOpen={(book) => navigate(routeHref({ name: 'work', id: book.id }))} initialQuery={route.query} apiSearch={apiSearch} searchError={mode === 'live' ? error : undefined} />}
    {route.name === 'work' && <WorkView saved={saved.some((item) => item.id === routeBook.book?.id)} onToggleShelf={toggleShelf} book={routeBook.book} mode={mode} navigate={navigate} related={visibleBooks} loading={routeBook.loading || loading} error={routeBook.error} />}
    {route.name === 'reader' && <ReaderView book={routeBook.book} loading={routeBook.loading || loading} navigate={navigate} contentUrl={api.documentContentUrl} downloadUrl={api.documentDownloadUrl} />}
    {route.name === 'notFound' && <NotFoundView path={route.path} navigate={navigate} />}
    <MobileNav route={route} navigate={navigate} />
  </div>
}

function useRouteBook(route: Route, mode: DataMode, initial: BookRecord | undefined, books: BookRecord[]) {
  const [book, setBook] = useState<BookRecord | undefined>(initial)
  const [loading, setLoading] = useState(false)
  const [error, setLocalError] = useState<string>()
  const requestRef = useRef(0)
  useEffect(() => {
    if (route.name !== 'work' && route.name !== 'reader') return
    const requestId = requestRef.current + 1
    requestRef.current = requestId
    const fresh = (value: BookRecord | undefined, nextError: string | undefined, nextLoading: boolean) => {
      if (requestRef.current !== requestId) return
      setBook(value)
      setLocalError(nextError)
      setLoading(nextLoading)
    }
    if (mode === 'demo') {
      const cached = findDemoBook(route.id) ?? books.find((item) => item.id === route.id)
      fresh(cached, cached ? undefined : 'This demo record could not be found in the local preview.', false)
      return
    }
    const numericId = Number(route.id)
    if (!Number.isInteger(numericId)) {
      fresh(undefined, 'This work id is not valid. Check the link and try again.', false)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    if (requestRef.current === requestId) setLocalError(undefined)
    api.work(numericId, controller.signal).then((work) => {
      if (requestRef.current !== requestId || controller.signal.aborted) return
      setBook(workToBook(work, api.assetContentUrl))
    }).catch((reason: unknown) => {
      if (requestRef.current !== requestId || controller.signal.aborted) return
      if (isAbortError(reason)) return
      const message = reason instanceof Error ? reason.message : 'This work could not be loaded.'
      setLocalError(message)
    }).finally(() => {
      if (requestRef.current === requestId && !controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [route, mode, books])
  return useMemo(() => ({ book, loading, error }), [book, loading, error])
}
