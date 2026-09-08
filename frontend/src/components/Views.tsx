import { useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, createCatalogueApi, isAbortError } from '../lib/api'
import type { BookDocument, BookRecord, DataMode, RatingsResponse, Route, StatsResponse } from '../lib/types'
import { DEMO_BOOKS, DEMO_CATEGORIES, findDemoBook, searchDemoBooks } from '../lib/demoData'
import { CoverImage } from './CoverImage'
import { Shelf } from './Shelf'
import { ArrowIcon, BookIcon, SparkIcon } from './Icons'
import { BookCard } from './BookCard'
import { RatingControl } from './RatingControl'
import { EpubReader } from './EpubReader'

interface ViewProps {
  books: BookRecord[]
  mode: DataMode
  navigate: (href: string) => void
  onOpen: (book: BookRecord) => void
  stats?: StatsResponse
  loading?: boolean
  error?: string
  onRetry?: () => void
}

export function HomeView({ books, mode, navigate, onOpen, stats, loading, error, onRetry }: ViewProps) {
  if (loading && books.length === 0) return <main className="wrap loading-page" role="status" aria-label="Loading the catalogue"><div className="loading-block" /><div className="loading-block loading-block--short" /><p>Connecting to the live catalogue…</p></main>
  if (!loading && books.length === 0) return <main className="wrap empty-state page-empty"><BookIcon /><h1>{error ? 'The catalogue is unavailable' : 'No works in the catalogue yet'}</h1><p>{error ?? 'The live catalogue returned no works. Try again or browse the local preview.'}</p>{onRetry ? <button type="button" className="button button--dark" onClick={onRetry}>Retry</button> : <button type="button" className="button button--dark" onClick={() => navigate('/discover')}>Discover books</button>}</main>
  const feature = books[0] ?? DEMO_BOOKS[0]
  const recent = books.slice(1, 5)
  const essays = books.filter((book) => book.category === 'Essays')
  const nature = books.filter((book) => book.category === 'Nature')
  return <main>
    <section className="hero wrap" aria-labelledby="hero-title">
      <div className="hero__copy">
        <p className="eyebrow eyebrow--bright"><SparkIcon /> The folio selection · 04</p>
        <h1 id="hero-title">Books for the<br /><em>life of the mind.</em></h1>
        <p className="hero__intro">A slower, more thoughtful way to discover and read the books worth keeping close.</p>
        <div className="hero__actions"><button className="button button--light" onClick={() => onOpen(feature)}>Start reading <ArrowIcon /></button><button className="text-link text-link--light" onClick={() => navigate('/discover')}>Browse the library <span aria-hidden="true">→</span></button></div>
        <p className="hero__source">{mode === 'demo' ? 'Preview collection · records are explicitly local' : 'Connected to your catalogue · live records'}</p>
      </div>
      <div className="hero__cover"><CoverImage title={feature.title} sourceUrl={feature.coverUrl} accent={feature.accent} size="large" attribution={feature.coverAttribution} /><span className="hero__cover-note">01 / featured work</span></div>
      <div className="hero__aside"><span className="hero__aside-line" /><p>“A good book doesn’t fill the silence. It teaches you how to hear it.”</p><span className="hero__aside-author">— The Folio Journal</span></div>
    </section>
    <section className="stats-strip wrap" aria-label="Catalogue snapshot"><div><strong>{stats?.works ? compact(stats.works) : '08'}</strong><span>works to wander</span></div><div><strong>∞</strong><span>ways to begin</span></div><div><strong>01</strong><span>quiet place</span></div></section>
    <div className="wrap page-section"><Shelf title="Picked for your next hour" kicker="A little time well spent" books={recent} onOpen={onOpen} action="View all" onAction={() => navigate('/discover')} /><Shelf title="Essays for staying curious" kicker="Keep looking" books={essays} onOpen={onOpen} action="Explore essays" onAction={() => navigate('/discover?q=Essays')} /><Shelf title="The natural world, noticed" kicker="Out there, in here" books={nature} onOpen={onOpen} /></div>
  </main>
}

function compact(value: number) { return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value) }

interface DiscoverProps extends ViewProps { initialQuery: string; apiSearch: (query: string, signal?: AbortSignal) => Promise<BookRecord[]>; searchError?: string }

export function DiscoverView({ books, mode, navigate, onOpen, initialQuery, apiSearch, searchError }: DiscoverProps) {
  const [query, setQuery] = useState(initialQuery)
  const [activeQuery, setActiveQuery] = useState(initialQuery)
  const [results, setResults] = useState<BookRecord[]>(books)
  const [isSearching, setIsSearching] = useState(false)
  const [error, setError] = useState(searchError)
  const [searchAttempt, setSearchAttempt] = useState(0)
  useEffect(() => { setQuery(initialQuery); setActiveQuery(initialQuery) }, [initialQuery])
  useEffect(() => { if (searchError) setError(searchError) }, [searchError])
  useEffect(() => {
    if (mode === 'demo') { setResults(searchDemoBooks(activeQuery)); setIsSearching(false); return }
    const controller = new AbortController()
    let stale = false
    setIsSearching(true); setError(undefined)
    apiSearch(activeQuery, controller.signal).then((next) => {
      if (stale) return
      setResults(next)
      // A newer search succeeding clears a prior catalogue/search error
      // instead of letting it linger over fresh results.
      setError(undefined)
    }).catch((reason: unknown) => {
      if (stale || controller.signal.aborted) return
      if (isAbortError(reason)) return
      setError(reason instanceof Error ? reason.message : 'Search failed')
    }).finally(() => { if (!stale && !controller.signal.aborted) setIsSearching(false) })
    return () => { stale = true; controller.abort() }
  }, [activeQuery, apiSearch, mode, searchAttempt])
  const retrySearch = () => setSearchAttempt((attempt) => attempt + 1)
  const submit = (event: React.FormEvent) => { event.preventDefault(); navigate(`/discover${query.trim() ? `?q=${encodeURIComponent(query.trim())}` : ''}`); setActiveQuery(query.trim()) }
  const activeCategory = useMemo(() => DEMO_CATEGORIES.find((category) => category.toLocaleLowerCase() === activeQuery.toLocaleLowerCase()), [activeQuery])
  const filtered = activeCategory && mode === 'demo' ? results.filter((book) => book.category === activeCategory) : results
  const showLoading = isSearching && filtered.length === 0
  const showEmpty = !isSearching && filtered.length === 0 && !error
  const showSearchError = !isSearching && filtered.length === 0 && Boolean(error)
  return <main className="wrap discover-page">
    <div className="page-intro"><p className="eyebrow">The library · {mode === 'demo' ? 'local preview' : 'live catalogue'}</p><h1>Find your next<br /><em>good book.</em></h1><p>Search the collection by title, author, or idea. Take your time.</p></div>
    <form className="large-search" onSubmit={submit} role="search"><span aria-hidden="true">⌕</span><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Try “night”, “garden”, or an author" aria-label="Search the library" /><button className="button button--dark" type="submit">Search</button></form>
    <div className="category-tabs" aria-label="Browse by category"><button type="button" className={!activeQuery ? 'is-active' : ''} aria-pressed={!activeQuery} onClick={() => { setQuery(''); setActiveQuery(''); navigate('/discover') }}>All books</button>{DEMO_CATEGORIES.map((category) => <button type="button" key={category} className={activeCategory === category ? 'is-active' : ''} aria-pressed={activeCategory === category} onClick={() => { setQuery(category); setActiveQuery(category); navigate(`/discover?q=${category}`) }}>{category}</button>)}</div>
    {error && <div className="inline-error" role="alert"><strong>Couldn’t reach the catalogue.</strong> {error}<button type="button" onClick={retrySearch} aria-label="Retry search">Retry</button></div>}
    <div className="results-heading"><div><p className="eyebrow" aria-live="polite">{isSearching ? 'Searching...' : `${filtered.length} ${filtered.length === 1 ? 'result' : 'results'}`}</p><h2>{activeQuery ? `Results for “${activeQuery}”` : 'All books'}</h2></div><span className="results-heading__sort">Curated order <span aria-hidden="true">⌄</span></span></div>
    {showLoading
      ? <div className="empty-state" role="status"><h2>Searching the catalogue…</h2><p>Hold on while matching records arrive.</p></div>
      : showSearchError
        ? <div className="empty-state"><BookIcon /><h2>Search is unavailable.</h2><p>The catalogue did not answer. Your previous results are gone; try again.</p><button type="button" className="button button--dark" onClick={retrySearch}>Retry search</button></div>
        : showEmpty
          ? <div className="empty-state"><BookIcon /><h2>No books here yet.</h2><p>Try a different phrase, or browse the full collection.</p><button type="button" className="button button--dark" onClick={() => { setQuery(''); setActiveQuery(''); navigate('/discover') }}>Clear search</button></div>
          : <div className="book-grid">{filtered.map((book) => <BookCard key={book.id} book={book} onOpen={onOpen} variant="grid" />)}</div>}
  </main>
}

interface WorkViewProps { saved?: boolean; onToggleShelf?: (book: BookRecord) => void; book?: BookRecord; mode: DataMode; navigate: (href: string) => void; related: BookRecord[]; loading?: boolean; error?: string }

const detailApi = createCatalogueApi(import.meta.env.VITE_API_BASE_URL ?? '')

type RatingSyncState = 'idle' | 'saving' | 'saved' | 'unavailable' | 'error' | 'demo'

export function WorkView({ book, mode, navigate, related, loading, error, saved, onToggleShelf }: WorkViewProps) {
  const [ratings, setRatings] = useState<RatingsResponse | undefined>(book?.ratings)
  const [ratingsLoading, setRatingsLoading] = useState(Boolean(book && mode === 'live'))
  const [ratingsError, setRatingsError] = useState<string>()
  const [ratingsAttempt, setRatingsAttempt] = useState(0)
  const [rating, setRating] = useState(book?.ratings?.community.user_rating ?? 0)
  const [ratingSync, setRatingSync] = useState<RatingSyncState>(mode === 'demo' ? 'demo' : 'idle')
  const [ratingError, setRatingError] = useState<string>()
  const mutationController = useRef<AbortController | undefined>(undefined)

  useEffect(() => {
    if (!book) return
    setRating(book.ratings?.community.user_rating ?? 0)
    setRatingSync(mode === 'demo' ? 'demo' : 'idle')
    setRatingError(undefined)
    if (mode === 'demo') {
      setRatings(book.ratings)
      setRatingsLoading(false)
      setRatingsError(undefined)
      return
    }
    const workId = book.workId ?? Number(book.id)
    if (!Number.isInteger(workId)) {
      setRatings(undefined)
      setRatingsLoading(false)
      setRatingsError('Ratings are unavailable for this work.')
      return
    }
    const controller = new AbortController()
    setRatingsLoading(true)
    setRatingsError(undefined)
    detailApi.ratings(workId, controller.signal).then((next) => {
      setRatings(next)
      setRating(next.community.user_rating ?? 0)
    }).catch((reason: unknown) => {
      if (isAbortError(reason)) return
      setRatingsError(reason instanceof Error ? reason.message : 'Ratings could not be loaded.')
    }).finally(() => {
      if (!controller.signal.aborted) setRatingsLoading(false)
    })
    return () => controller.abort()
  }, [book?.id, book?.workId, mode, ratingsAttempt])

  useEffect(() => () => mutationController.current?.abort(), [])

  const updateRating = async (value: number) => {
    setRating(value)
    setRatingError(undefined)
    if (mode === 'demo') {
      setRatingSync('demo')
      return
    }
    const workId = book?.workId ?? Number(book?.id)
    if (!Number.isInteger(workId)) return
    mutationController.current?.abort()
    const controller = new AbortController()
    mutationController.current = controller
    setRatingSync('saving')
    try {
      const next = await detailApi.setRating(workId, value, controller.signal)
      setRatings(next)
      setRating(next.community.user_rating ?? value)
      setRatingSync('saved')
    } catch (reason: unknown) {
      if (isAbortError(reason)) return
      if (reason instanceof ApiError && reason.status === 401) {
        setRatingSync('unavailable')
      } else {
        setRatingSync('error')
        setRatingError(reason instanceof Error ? reason.message : 'Your rating could not be synced.')
      }
    } finally {
      if (mutationController.current === controller) mutationController.current = undefined
    }
  }

  const clearRating = async () => {
    setRatingError(undefined)
    if (mode === 'demo') {
      setRating(0)
      setRatingSync('demo')
      return
    }
    const workId = book?.workId ?? Number(book?.id)
    if (!Number.isInteger(workId)) return
    mutationController.current?.abort()
    const controller = new AbortController()
    mutationController.current = controller
    setRatingSync('saving')
    try {
      const next = await detailApi.deleteRating(workId, controller.signal)
      setRatings(next)
      setRating(next.community.user_rating ?? 0)
      setRatingSync('saved')
    } catch (reason: unknown) {
      if (isAbortError(reason)) return
      if (reason instanceof ApiError && reason.status === 401) {
        setRatingSync('unavailable')
      } else {
        setRatingSync('error')
        setRatingError(reason instanceof Error ? reason.message : 'Your rating could not be removed.')
      }
    } finally {
      if (mutationController.current === controller) mutationController.current = undefined
    }
  }

  const ratingNote = mode === 'demo'
    ? 'Demo only · not synced'
    : ratingSync === 'saving' ? 'Saving…'
      : ratingSync === 'saved' ? 'Synced'
        : ratingSync === 'error' ? 'Couldn’t sync'
          : 'Sign in to sync'
  if (loading) return <main className="wrap loading-page"><div className="loading-block" /><div className="loading-block loading-block--short" /></main>
  if (!book || error) return <main className="wrap empty-state page-empty"><BookIcon /><h1>Work unavailable</h1><p>{error ?? 'This work could not be found in the catalogue.'}</p><button className="button button--dark" onClick={() => navigate('/discover')}>Back to discover</button></main>
  return <main>
    <section className="detail wrap"><div className="detail__cover"><CoverImage title={book.title} sourceUrl={book.coverUrl} accent={book.accent} size="large" attribution={book.coverAttribution} /><p className="provenance"><span className={`provenance__dot provenance__dot--${book.sourceKind}`} />{book.sourceKind === 'demo' ? 'Demo record · local preview' : `Source: ${book.sourceLabel}`}</p></div><div className="detail__copy"><p className="eyebrow">{book.category} <span aria-hidden="true">·</span> {book.year}</p><h1>{book.title}</h1>{book.subtitle && <p className="detail__subtitle">{book.subtitle}</p>}<p className="detail__author">by <strong>{book.author}</strong></p><p className="detail__description">{book.description}</p><div className="detail__actions"><button className="button button--light" onClick={() => navigate(`/read/${book.id}`)}>Read now <ArrowIcon /></button><button className="button button--outline" aria-pressed={Boolean(saved)} onClick={() => onToggleShelf?.(book)}>{saved ? 'Remove from shelf' : '+ Add to shelf'}</button></div><div className="detail__meta"><span><strong>{book.pages || '—'}</strong> pages</span><span><strong>{book.readingTime}</strong></span><span><strong>{book.language}</strong></span></div><RatingControl value={rating} onChange={updateRating} onClear={rating ? clearRating : undefined} disabled={ratingSync === 'saving'} note={ratingNote} />{ratingError && <p className="rating-control__error" role="alert">{ratingError}</p>}<RatingsPanel ratings={ratings} loading={ratingsLoading} error={ratingsError} onRetry={() => setRatingsAttempt((attempt) => attempt + 1)} /></div></section>
    <section className="wrap detail-notes"><div><p className="eyebrow">About this edition</p><p>Folio keeps catalogue provenance visible as you read. Metadata may come from multiple sources; the selected cover and edition are shown above.</p></div><div><p className="eyebrow">Topics</p><div className="tag-list">{book.tags.map((tag) => <span key={tag}>{tag}</span>)}</div></div></section>
    <div className="wrap page-section"><Shelf title="Keep exploring" kicker="After this one" books={related.filter((item) => item.id !== book.id).slice(0, 4)} onOpen={(item) => navigate(`/works/${item.id}`)} /></div>
    <p className="detail-mode-note">{mode === 'demo' ? 'You are viewing an explicit demo record. It is not a live catalogue item.' : 'Live catalogue record'}</p>
  </main>
}

function RatingsPanel({ ratings, loading, error, onRetry }: { ratings?: RatingsResponse; loading: boolean; error?: string; onRetry: () => void }) {
  return <section className="ratings-panel" aria-labelledby="ratings-title" aria-busy={loading}>
    <div className="ratings-panel__heading"><p className="eyebrow" id="ratings-title">Ratings</p>{loading && <span className="ratings-panel__status">Loading…</span>}</div>
    {loading ? <div className="ratings-panel__loading" role="status">Checking the catalogue sources…</div> : error ? <div className="ratings-panel__error" role="alert"><span>{error}</span><button className="text-link" onClick={onRetry}>Retry</button></div> : ratings ? <>
      <div className="ratings-panel__community"><strong>{ratings.community.average === null ? '—' : ratings.community.average.toFixed(1)}</strong><span>Community average · {ratings.community.count} {ratings.community.count === 1 ? 'rating' : 'ratings'}</span></div>
      <div className="ratings-panel__sources">
        {ratings.external.filter((entry) => entry.status === 'available').map((entry) => <ExternalRating key={`${entry.provider}-${entry.source_url ?? 'record'}`} entry={entry} />)}
        {ratings.external.filter((entry) => entry.status === 'unavailable').map((entry) => <div className="ratings-source ratings-source--unavailable" key={entry.provider}><span>{providerLabel(entry.provider)}</span><small>{entry.reason === 'not_imported' ? 'Not imported yet' : 'Unavailable'}</small></div>)}
      </div>
    </> : <p className="ratings-panel__empty">Ratings are unavailable for this record.</p>}
  </section>
}

function ExternalRating({ entry }: { entry: RatingsResponse['external'][number] }) {
  const label = <><span>{providerLabel(entry.provider)}</span><strong>{entry.rating_value === null ? '—' : `${entry.rating_value} / ${entry.scale_max}`}</strong><small>{entry.rating_count === null ? 'No rating count supplied' : `${entry.rating_count.toLocaleString()} ratings`}</small></>
  return <div className="ratings-source">{entry.source_url ? <a href={entry.source_url} target="_blank" rel="noreferrer">{label}</a> : label}</div>
}

function providerLabel(provider: string) {
  if (provider === 'rokomari') return 'Rokomari'
  if (provider === 'google_books') return 'Google Books'
  if (provider === 'goodreads') return 'Goodreads'
  return provider
}

interface ReaderProps {
  book?: BookRecord
  loading?: boolean
  navigate: (href: string) => void
  contentUrl: (documentId: number) => string
  downloadUrl: (documentId: number) => string
}

export function ReaderView({ book, loading, navigate, contentUrl, downloadUrl }: ReaderProps) {
  const [menuOpen, setMenuOpen] = useState(false)
  if (!book && loading) return <main className="wrap loading-page" role="status" aria-label="Loading the work"><div className="loading-block" /><div className="loading-block loading-block--short" /><p>Opening this work…</p></main>
  if (!book) return <main className="reader reader--empty"><BookIcon /><h1>Reader unavailable</h1><p>This work could not be loaded. It may have an invalid id or the catalogue may be unreachable.</p><button className="button button--light" onClick={() => navigate('/discover')}>Return to library</button></main>
  if (book.sourceKind === 'demo') return <ReaderChrome book={book} navigate={navigate} menuOpen={menuOpen} setMenuOpen={setMenuOpen}><DemoReaderContent /></ReaderChrome>
  return <LiveReader book={book} navigate={navigate} contentUrl={contentUrl} downloadUrl={downloadUrl} menuOpen={menuOpen} setMenuOpen={setMenuOpen} />
}

function LiveReader({ book, navigate, contentUrl, downloadUrl, menuOpen, setMenuOpen }: { book: BookRecord; navigate: (href: string) => void; contentUrl: (documentId: number) => string; downloadUrl: (documentId: number) => string; menuOpen: boolean; setMenuOpen: (value: boolean) => void }) {
  const [selectedId, setSelectedId] = useState(book?.documents[0]?.id)
  useEffect(() => setSelectedId(book?.documents[0]?.id), [book?.id, book?.documents])
  const selected = book?.documents.find((document) => document.id === selectedId) ?? book?.documents[0]
  if (!selected) return <ReaderChrome book={book} navigate={navigate} menuOpen={menuOpen} setMenuOpen={setMenuOpen}><DocumentUnavailable /></ReaderChrome>
  return <ReaderChrome book={book} navigate={navigate} menuOpen={menuOpen} setMenuOpen={setMenuOpen}>
    <div className="reader__document-toolbar">
      <div><p className="eyebrow">Protected edition</p><strong>{documentLabel(selected)}</strong></div>
      <div className="reader__document-actions">
        {book.documents.length > 1 && <label>Edition file <select value={selected.id} onChange={(event) => setSelectedId(Number(event.target.value))}>{book.documents.map((document) => <option key={document.id} value={document.id}>{documentLabel(document)}</option>)}</select></label>}
        <a className="button button--dark" href={downloadUrl(selected.id)} download>Download {fileExtension(selected.mediaType)}</a>
      </div>
    </div>
    <DocumentSurface book={book} document={selected} contentUrl={contentUrl(selected.id)} />
  </ReaderChrome>
}

function ReaderChrome({ book, navigate, menuOpen, setMenuOpen, children }: { book: BookRecord; navigate: (href: string) => void; menuOpen: boolean; setMenuOpen: (value: boolean) => void; children: React.ReactNode }) {
  return <main className="reader"><header className="reader__header"><button className="reader__back" onClick={() => navigate(`/works/${book.id}`)} aria-label="Back to work detail">← <span>Exit reader</span></button><div className="reader__title"><span>{book.title}</span><span className="reader__progress">{book.sourceKind === 'demo' ? 'Demo preview' : 'Protected edition'}</span></div><button className="reader__menu" onClick={() => setMenuOpen(!menuOpen)} aria-expanded={menuOpen}>Aa <span>Reading settings</span></button></header><div className="reader__layout"><aside className={`reader__toc ${menuOpen ? 'is-open' : ''}`}><p className="eyebrow">Contents</p><button className="is-active">01 <span>{book.sourceKind === 'demo' ? 'Opening note' : 'Document'}</span></button><button>02 <span>First light</span></button><button>03 <span>The long way home</span></button></aside><section className="reader__content">{children}</section></div><footer className="reader__footer"><span>{book.sourceKind === 'demo' ? 'Folio demo preview' : 'Folio protected reader'}</span><span>{book.pages || '—'} pages</span><button onClick={() => navigate(`/works/${book.id}`)}>About this work →</button></footer></main>
}

function DemoReaderContent() {
  return <article className="reader__article"><p className="reader__chapter">Demo preview</p><h1>Opening note</h1><p className="reader__lead">Every book begins somewhere quieter than we expect.</p><p>This is an explicit demo preview. The live document stream has not been connected to this local catalogue record.</p><p>When a live edition is available, Folio opens protected PDFs in the browser or renders EPUB content directly in this reading room.</p><blockquote>“Reading is a form of attention, and attention is a way of saying: I am here.”</blockquote><p className="reader__end">— Folio notebook</p></article>
}

function DocumentUnavailable() {
  return <div className="document-state document-state--unavailable" role="status"><BookIcon /><strong>No readable document is attached</strong><span>This live catalogue work has metadata, but no accepted PDF or EPUB is available yet.</span></div>
}

function DocumentSurface({ book, document, contentUrl }: { book: BookRecord; document: BookDocument; contentUrl: string }) {
  const mediaType = document.mediaType.toLocaleLowerCase()
  if (mediaType.includes('epub')) return <EpubReader title={book.title} contentUrl={contentUrl} />
  if (mediaType.includes('pdf')) return <PdfReader title={book.title} contentUrl={contentUrl} />
  return <div className="document-state document-state--unavailable" role="status"><strong>Unsupported document format</strong><span>This edition exposes {document.mediaType || 'an unknown media type'}; Folio can currently read PDF and EPUB documents.</span></div>
}

const PDF_LOAD_TIMEOUT_MS = 20000

function PdfReader({ title, contentUrl }: { title: string; contentUrl: string }) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    setState('loading')
    // iframe onError is unreliable across browsers, so bound the loading
    // state: a document that never reports back becomes an explicit error
    // with retry instead of spinning forever. The download link above
    // remains available as a fallback.
    const timer = window.setTimeout(() => {
      setState((current) => (current === 'loading' ? 'error' : current))
    }, PDF_LOAD_TIMEOUT_MS)
    return () => window.clearTimeout(timer)
  }, [contentUrl, attempt])
  return <section className={`pdf-reader pdf-reader--${state}`} aria-label={`${title} PDF reader`}><div className="pdf-reader__state" aria-live="polite">{state === 'loading' && 'Opening protected PDF…'}{state === 'error' && <span>The PDF could not be displayed. Use the download link above or <button type="button" className="text-link" onClick={() => setAttempt((value) => value + 1)}>reload the document</button>.</span>}</div><iframe key={`${contentUrl}#${attempt}`} title={`${title} PDF`} src={contentUrl} onLoad={() => setState('ready')} onError={() => setState('error')} /></section>
}

export function NotFoundView({ path, navigate }: { path: string; navigate: (href: string) => void }) {
  return <main className="wrap empty-state page-empty"><BookIcon /><h1>That page isn’t on the shelf</h1><p>{path ? `“${path}” doesn’t match anything in Folio. It may have moved or the link may be wrong.` : 'This page doesn’t match anything in Folio.'}</p><div className="detail__actions"><button type="button" className="button button--dark" onClick={() => navigate('/')}>Back home</button><button type="button" className="button button--outline" onClick={() => navigate('/discover')}>Discover books</button></div></main>
}

function documentLabel(document: BookDocument) { return `${fileExtension(document.mediaType).toUpperCase()} · protected document ${document.id}` }
function fileExtension(mediaType: string) { return mediaType.toLocaleLowerCase().includes('epub') ? 'EPUB' : mediaType.toLocaleLowerCase().includes('pdf') ? 'PDF' : 'file' }
