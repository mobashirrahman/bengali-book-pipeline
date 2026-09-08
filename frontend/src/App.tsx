import { useCallback, useEffect, useMemo, useState } from 'react'
import { DiscoverView, HomeView, ReaderView, WorkView } from './components/Views'
import { Header, MobileNav } from './components/Header'
import { StatusBanner } from './components/StatusBanner'
import { createCatalogueApi } from './lib/api'
import { DEMO_BOOKS, findDemoBook } from './lib/demoData'
import { editionToBook, workToBook } from './lib/catalogue'
import { parseRoute, routeHref } from './lib/router'
import type { BookRecord, DataMode, Route, StatsResponse } from './lib/types'

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

  const loadCatalogue = useCallback(async (signal?: AbortSignal) => {
    if (forcedDemo) return
    setLoading(true)
    setError(undefined)
    try {
      const [health, workRows, catalogueStats] = await Promise.all([api.health(signal), api.works({ limit: 24, signal }), api.stats(signal)])
      if (health.status !== 'ok') throw new Error('The catalogue health check did not return an OK status.')
      setBooks(workRows.map(workToBook))
      setStats(catalogueStats)
      setMode('live')
    } catch (reason: unknown) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      const message = reason instanceof Error ? reason.message : 'The catalogue API is unavailable.'
      setBooks(DEMO_BOOKS)
      setMode('demo')
      setError(message)
    } finally {
      setLoading(false)
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

  const apiSearch = useCallback(async (query: string) => {
    if (mode === 'demo') return []
    const response = await api.search(query || 'a', { limit: 24 })
    const details = await Promise.all(response.items.filter((item) => item.kind !== 'person').slice(0, 12).map(async (item) => {
      try { return workToBook(await api.work(item.work_id ?? item.id)) } catch { return undefined }
    }))
    return details.filter((book): book is BookRecord => Boolean(book))
  }, [mode])

  const routeBook = useRouteBook(route, mode, selectedBook, books, setError)
  const bannerMessage = error ? `Showing a local preview because the API could not be reached: ${error}` : undefined
  return <div className="app-shell">
    <Header route={route} navigate={navigate} />
    <StatusBanner mode={mode} message={bannerMessage} onTryLive={mode === 'demo' && !forcedDemo ? retryLive : undefined} />
    {route.name === 'home' && <HomeView books={books.length ? books : DEMO_BOOKS} mode={mode} navigate={navigate} onOpen={(book) => navigate(routeHref({ name: 'work', id: book.id }))} stats={stats} />}
    {route.name === 'discover' && <DiscoverView books={books.length ? books : DEMO_BOOKS} mode={mode} navigate={navigate} onOpen={(book) => navigate(routeHref({ name: 'work', id: book.id }))} initialQuery={route.query} apiSearch={apiSearch} searchError={mode === 'live' ? error : undefined} />}
    {route.name === 'work' && <WorkView book={routeBook.book} mode={mode} navigate={navigate} related={books.length ? books : DEMO_BOOKS} loading={routeBook.loading || loading} error={routeBook.error} />}
    {route.name === 'reader' && <ReaderView book={routeBook.book} navigate={navigate} contentUrl={api.documentContentUrl} downloadUrl={api.documentDownloadUrl} />}
    <MobileNav route={route} navigate={navigate} />
  </div>
}

function useRouteBook(route: Route, mode: DataMode, initial: BookRecord | undefined, books: BookRecord[], setError: (value: string | undefined) => void) {
  const [book, setBook] = useState<BookRecord | undefined>(initial)
  const [loading, setLoading] = useState(false)
  const [error, setLocalError] = useState<string>()
  useEffect(() => {
    if (route.name !== 'work' && route.name !== 'reader') return
    if (mode === 'demo') { setBook(findDemoBook(route.id) ?? books.find((item) => item.id === route.id)); return }
    const numericId = Number(route.id)
    if (!Number.isInteger(numericId)) { setLocalError('This work id is not valid.'); return }
    const controller = new AbortController()
    setLoading(true); setLocalError(undefined); setError(undefined)
    api.work(numericId, controller.signal).then((work) => setBook(workToBook(work))).catch((reason: unknown) => {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      const message = reason instanceof Error ? reason.message : 'This work could not be loaded.'
      setLocalError(message); setError(message)
    }).finally(() => setLoading(false))
    return () => controller.abort()
  }, [route, mode, books, setError])
  return useMemo(() => ({ book, loading, error }), [book, loading, error])
}
