import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, createCatalogueApi } from './api'

describe('catalogue API adapter', () => {
  afterEach(() => vi.restoreAllMocks())

  it('builds typed v2 search requests with cursor pagination', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], next: 'cursor-2' }), { status: 200 }))
    const response = await createCatalogueApi('http://localhost:8000').search('night sky', { limit: 12, after: 'cursor-1' })
    expect(response.next).toBe('cursor-2')
    expect(fetchMock).toHaveBeenCalledWith('http://localhost:8000/v2/search?q=night+sky&limit=12&after=cursor-1', expect.objectContaining({ headers: { Accept: 'application/json' } }))
  })

  it('turns API errors into an inspectable ApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: 'Database not initialized' }), { status: 503 }))
    await expect(createCatalogueApi().health()).rejects.toMatchObject({ name: 'ApiError', status: 503, message: 'Database not initialized', path: '/v2/health' })
  })

  it('passes cancellation to the browser fetch request', async () => {
    const controller = new AbortController()
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => {
      expect(init?.signal).toBe(controller.signal)
      return Promise.reject(new DOMException('The operation was aborted.', 'AbortError'))
    })
    await expect(createCatalogueApi().works({ signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('does not hide a failed HTTP response behind an empty result', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 500 }))
    await expect(createCatalogueApi().stats()).rejects.toBeInstanceOf(ApiError)
  })

  it('exposes protected content and download URLs from the configured API origin', () => {
    const api = createCatalogueApi('https://catalogue.example.test/')
    expect(api.documentContentUrl(42)).toBe('https://catalogue.example.test/v2/documents/42/content')
    expect(api.documentDownloadUrl(42)).toBe('https://catalogue.example.test/v2/documents/42/download')
    expect(api.assetContentUrl(17)).toBe('https://catalogue.example.test/v2/assets/17/content')
  })

  it('fetches and mutates work ratings through the stable response contract', async () => {
    const payload = { work_id: 7, community: { average: null, count: 0, user_rating: null }, external: [] }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(new Response(JSON.stringify(payload), { status: 200 })))
    const api = createCatalogueApi('http://localhost:8000')
    await expect(api.ratings(7)).resolves.toEqual(payload)
    await api.setRating(7, 4)
    await api.deleteRating(7)
    expect(fetchMock).toHaveBeenNthCalledWith(2, 'http://localhost:8000/v2/works/7/rating', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ rating: 4 }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(3, 'http://localhost:8000/v2/works/7/rating', expect.objectContaining({ method: 'DELETE' }))
  })
})
