import { describe, expect, it } from 'vitest'
import { workToBook } from './catalogue'

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
      { id: 101, mediaType: 'application/pdf' },
      { id: 102, mediaType: 'application/epub+zip' },
    ])
    expect(book.coverUrl).toBe('https://covers.example/book.jpg')
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
    expect(book.documents).toEqual([{ id: 99, mediaType: 'application/epub+zip' }])
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
        assets: [{ id: 42, asset_type: 'cover', storage_uri: 'sha256/cover.png', mime_type: 'image/png', is_selected: 1 }],
      }],
    }, (assetId) => `/v2/assets/${assetId}/content`)
    expect(book.coverUrl).toBe('/v2/assets/42/content')
    expect(book.coverAssetId).toBe(42)
  })
})
