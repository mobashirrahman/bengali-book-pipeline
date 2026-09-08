import { afterEach, describe, expect, it, vi } from 'vitest'
import { discoverSearch, filterReadable, workToBook } from './catalogue'
import { createCatalogueApi } from './api'
import type { BookRecord, WorkResponse, WorkSummary } from './types'

describe('catalogue work mapping', () => {
  it('preserves accepted edition document ids and media types for the reader', () => {
    const book = workToBook({
      id: 7,
      title: 'A Work',
      subtitle: null,
      language: 'English',
      description: null,
      identifiers: [],
      sources: ['catalogue'],
      editions: [{
        id: 9,
        work_id: 7,
        title: 'A Work',
        subtitle: null,
        publisher: 'Folio Press',
        publication_date: '2025-01-01',
        page_count: 120,
        edition_statement: null,
        language: 'English',
        description: null,
        people: [],
        identifiers: [],
        assets: [{ id: 1, asset_type: 'cover', storage_uri: 'remote:https://covers.example/book.jpg', is_selected: 1 }],
        sources: ['catalogue'],
        documents: [
          { id: 101, sha256: 'pdf', source_path: 'work.pdf', file_size: 100, media_type: 'application/pdf' },
          { id: 102, sha256: 'epub', source_path: 'work.epub', file_size: 200, media_type: 'application/epub+zip' },
        ],
      }],
    })
    expect(book.documents).toEqual([
      { id: 101, mediaType: 'application/pdf', editionId: 9, editionTitle: 'A Work' },
      { id: 102, mediaType: 'application/epub+zip', editionId: 9, editionTitle: 'A Work' },
    ])
    expect(book.coverUrl).toBeUndefined()
  })

  it('chooses a later readable edition when the first edition has no document', () => {
    const book = workToBook({
      id: 7,
      title: 'Collected Essays',
      identifiers: [],
      sources: ['catalogue'],
      editions: [
        {
          id: 70, work_id: 7, title: 'Print edition', subtitle: null, publisher: 'Press',
          publication_date: '2020-01-01', page_count: 100, edition_statement: null,
          language: 'English', description: null, people: [], identifiers: [], assets: [],
          sources: ['catalogue'], documents: [],
        },
        {
          id: 71, work_id: 7, title: 'Readable edition', subtitle: null, publisher: 'Press',
          publication_date: '2021-01-01', page_count: 100, edition_statement: null,
          language: 'English', description: null, people: [], identifiers: [], assets: [],
          sources: ['catalogue'], documents: [{ id: 99, sha256: 'sha', source_path: 'book.epub', file_size: 10, media_type: 'application/epub+zip' }],
        },
      ],
    })
    expect(book.editionId).toBe(71)
    expect(book.documents).toEqual([{ id: 99, mediaType: 'application/epub+zip', editionId: 71, editionTitle: 'Readable edition' }])
  })

  it('aggregates documents across every edition and dedupes shared files', () => {
    const book = workToBook({
      id: 7,
      title: 'Many Editions',
      identifiers: [],
      sources: ['catalogue'],
      editions: [
        {
          id: 70, work_id: 7, title: 'First edition', subtitle: null, publisher: 'Press',
          publication_date: '2020-01-01', page_count: 100, edition_statement: null,
          language: 'English', description: null, people: [], identifiers: [], assets: [],
          sources: ['catalogue'],
          documents: [{ id: 1, sha256: 'pdf', source_path: 'book.pdf', file_size: 10, media_type: 'application/pdf' }],
        },
        {
          id: 71, work_id: 7, title: 'Second edition', subtitle: null, publisher: 'Press',
          publication_date: '2021-01-01', page_count: 100, edition_statement: null,
          language: 'English', description: null, people: [], identifiers: [], assets: [],
          sources: ['catalogue'],
          documents: [
            { id: 1, sha256: 'pdf', source_path: 'book.pdf', file_size: 10, media_type: 'application/pdf' },
            { id: 2, sha256: 'epub', source_path: 'book.epub', file_size: 10, media_type: 'application/epub+zip' },
          ],
        },
      ],
    })
    expect(book.editionId).toBe(70)
    expect(book.documents).toEqual([
      { id: 1, mediaType: 'application/pdf', editionId: 70, editionTitle: 'First edition' },
      { id: 2, mediaType: 'application/epub+zip', editionId: 71, editionTitle: 'Second edition' },
    ])
  })

  it('does not expose local asset paths as browser cover URLs', () => {
    const book = workToBook({
      id: 8, title: 'Local Cover', identifiers: [], sources: [], editions: [{
        id: 80, title: 'Local Cover', people: [], identifiers: [], sources: [], documents: [],
        assets: [{ id: 2, asset_type: 'cover', storage_uri: '/srv/catalogue/assets/cover.jpg', is_selected: 1 }],
      }],
    })
    expect(book.coverUrl).toBeUndefined()
  })

  it('maps a selected local asset to the API content URL and preserves its id', () => {
    const book = workToBook({
      id: 8, title: 'Local Cover', identifiers: [], sources: [], editions: [{
        id: 80, title: 'Local Cover', people: [], identifiers: [], sources: [], documents: [],
        assets: [{ id: 42, asset_type: 'cover', storage_uri: 'sha256/cover.png', mime_type: 'image/png', width: 600, height: 900, verification_status: 'validated', is_selected: 1 }],
      }],
    }, (assetId) => `/v2/assets/${assetId}/content`)
    expect(book.coverUrl).toBe('/v2/assets/42/content')
    expect(book.coverAssetId).toBe(42)
  })
})

describe('readable-only filter', () => {
  const readable: BookRecord = {
    id: '1', title: 'Readable', author: 'A', description: '', language: 'English',
    year: '2024', pages: 10, category: 'Essays', readingTime: '1 hr read',
    accent: '', sourceLabel: 'catalogue', sourceKind: 'catalogue',
    documents: [{ id: 7, mediaType: 'application/epub+zip', editionId: 9, editionTitle: 'E' }],
    tags: [],
  }
  const metadataOnly: BookRecord = { ...readable, id: '2', title: 'Metadata', documents: [] }

  it('keeps only books with documents for live results when enabled', () => {
    expect(filterReadable([readable, metadataOnly], true, 'live')).toEqual([readable])
  })

  it('returns everything when the toggle is off', () => {
    expect(filterReadable([readable, metadataOnly], false, 'live')).toEqual([readable, metadataOnly])
  })

  it('leaves the demo preview untouched', () => {
    expect(filterReadable([readable, metadataOnly], true, 'demo')).toEqual([readable, metadataOnly])
  })
})

describe('discover search', () => {
  afterEach(() => vi.restoreAllMocks())

  const workDetail = (id: number, title: string): WorkResponse => ({
    id,
    title,
    identifiers: [],
    sources: ['catalogue'],
    editions: [{
      id: id * 10,
      work_id: id,
      title,
      people: [],
      identifiers: [],
      assets: [],
      sources: ['catalogue'],
      documents: [{ id: id * 100, sha256: 'sha', source_path: 'book.pdf', file_size: 10, media_type: 'application/pdf' }],
    }],
  })

  const stubCatalogue = (searchItems: WorkSummary[], worksList: WorkResponse[] = []) => {
    const urls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input)
      urls.push(url)
      if (url.includes('/v2/search?')) {
        return Promise.resolve(new Response(JSON.stringify({ items: searchItems, next: null }), { status: 200 }))
      }
      if (url.includes('/v2/works?')) {
        return Promise.resolve(new Response(JSON.stringify(worksList), { status: 200 }))
      }
      const match = url.match(/\/v2\/works\/(\d+)/)
      if (match) {
        const id = Number(match[1])
        return Promise.resolve(new Response(JSON.stringify(worksList.find((work) => work.id === id) ?? workDetail(id, `Work ${id}`)), { status: 200 }))
      }
      return Promise.reject(new Error(`unexpected catalogue request: ${url}`))
    })
    return urls
  }

  const api = () => createCatalogueApi('http://localhost:8000')

  it('browses readable works without a search request when the query is empty', async () => {
    const urls = stubCatalogue([], [workDetail(1, 'First'), workDetail(2, 'Second')])
    const books = await discoverSearch(api(), '   ', { readableOnly: true })
    expect(urls.some((url) => url.includes('/v2/search?'))).toBe(false)
    expect(urls).toContain('http://localhost:8000/v2/works?limit=24&has_documents=true')
    expect(books.map((book) => book.workId)).toEqual([1, 2])
  })

  it('collapses a work row and an edition row for the same work into one book', async () => {
    const urls = stubCatalogue([
      { id: 113223, kind: 'work', title: 'Shared' },
      { id: 9001, work_id: 113223, kind: 'edition', title: 'Shared edition' },
    ])
    const books = await discoverSearch(api(), 'shared', { readableOnly: true })
    expect(books).toHaveLength(1)
    expect(books[0].workId).toBe(113223)
    expect(urls.filter((url) => url.endsWith('/v2/works/113223'))).toHaveLength(1)
  })

  it('preserves API result order after de-duplication', async () => {
    stubCatalogue([
      { id: 3, kind: 'work', title: 'Third' },
      { id: 1, kind: 'work', title: 'First' },
      { id: 3001, work_id: 3, kind: 'edition', title: 'Third edition' },
      { id: 2, kind: 'work', title: 'Second' },
      { id: 1001, work_id: 1, kind: 'edition', title: 'First edition' },
    ])
    const books = await discoverSearch(api(), 'query', { readableOnly: false })
    expect(books.map((book) => book.workId)).toEqual([3, 1, 2])
  })

  it('never turns a person item into a book', async () => {
    const urls = stubCatalogue([
      { id: 9, kind: 'person', title: 'Some Author' },
      { id: 5, kind: 'work', title: 'A Work' },
    ])
    const books = await discoverSearch(api(), 'author', { readableOnly: false })
    expect(books.map((book) => book.workId)).toEqual([5])
    expect(urls.some((url) => url.endsWith('/v2/works/9'))).toBe(false)
  })
})
