import type {
  DocumentResponse,
  EditionResponse,
  HealthResponse,
  SearchResponse,
  StatsResponse,
  RatingsResponse,
  WorkResponse,
  WorkSummary,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly path: string

  constructor(message: string, status: number, path: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.path = path
  }
}

// Abort rejections are not always DOMException instances (jsdom, fetch
// polyfills and test doubles may reject with a plain { name } object), so
// detect cancellation by shape instead of instanceof.
export function isAbortError(reason: unknown): boolean {
  return (
    typeof reason === 'object' &&
    reason !== null &&
    (reason as { name?: unknown }).name === 'AbortError'
  )
}

export interface CatalogueApi {
  health(signal?: AbortSignal): Promise<HealthResponse>
  stats(signal?: AbortSignal): Promise<StatsResponse>
  search(query: string, options?: { limit?: number; after?: string; hasDocuments?: boolean; signal?: AbortSignal }): Promise<SearchResponse>
  works(options?: { limit?: number; after?: number; hasDocuments?: boolean; signal?: AbortSignal }): Promise<WorkResponse[]>
  work(id: number, signal?: AbortSignal): Promise<WorkResponse>
  edition(id: number, signal?: AbortSignal): Promise<EditionResponse>
  document(id: number, signal?: AbortSignal): Promise<DocumentResponse>
  ratings(workId: number, signal?: AbortSignal): Promise<RatingsResponse>
  setRating(workId: number, rating: number, signal?: AbortSignal): Promise<RatingsResponse>
  deleteRating(workId: number, signal?: AbortSignal): Promise<RatingsResponse>
  documentContentUrl(id: number): string
  documentDownloadUrl(id: number): string
  assetContentUrl(id: number): string
}

const asJson = async <T>(response: Response, path: string): Promise<T> => {
  if (!response.ok) {
    let detail = `Catalogue request failed (${response.status})`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // Preserve the status-based message when an API returns no JSON body.
    }
    throw new ApiError(detail, response.status, path)
  }
  return response.json() as Promise<T>
}

export function createCatalogueApi(baseUrl = ''): CatalogueApi {
  const url = (path: string) => `${baseUrl.replace(/\/$/, '')}${path}`
  const request = async <T>(path: string, signal?: AbortSignal, init: RequestInit = {}): Promise<T> => {
    let response: Response
    try {
      response = await fetch(url(path), {
        ...init,
        headers: { Accept: 'application/json', ...init.headers },
        signal,
      })
    } catch (reason: unknown) {
      // Preserve caller cancellation so UI guards can ignore it quietly.
      if (isAbortError(reason)) throw reason
      const detail = reason instanceof Error && reason.message ? `: ${reason.message}` : ''
      throw new ApiError(`The catalogue API could not be reached${detail}. Check your connection and try again.`, 0, path)
    }
    return asJson<T>(response, path)
  }

  return {
    health: (signal) => request<HealthResponse>('/v2/health', signal),
    stats: (signal) => request<StatsResponse>('/v2/stats', signal),
    search: (query, options = {}) => {
      const params = new URLSearchParams({ q: query, limit: String(options.limit ?? 20) })
      if (options.after) params.set('after', options.after)
      if (options.hasDocuments) params.set('has_documents', 'true')
      return request<SearchResponse>(`/v2/search?${params}`, options.signal)
    },
    works: (options = {}) => {
      const params = new URLSearchParams({ limit: String(options.limit ?? 20) })
      if (options.after !== undefined) params.set('after', String(options.after))
      if (options.hasDocuments) params.set('has_documents', 'true')
      return request<WorkResponse[]>(`/v2/works?${params}`, options.signal)
    },
    work: (id, signal) => request<WorkResponse>(`/v2/works/${encodeURIComponent(id)}`, signal),
    edition: (id, signal) => request<EditionResponse>(`/v2/editions/${encodeURIComponent(id)}`, signal),
    document: (id, signal) => request<DocumentResponse>(`/v2/documents/${encodeURIComponent(id)}`, signal),
    ratings: (workId, signal) => request<RatingsResponse>(`/v2/works/${encodeURIComponent(workId)}/ratings`, signal),
    setRating: (workId, rating, signal) => request<RatingsResponse>(`/v2/works/${encodeURIComponent(workId)}/rating`, signal, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating }),
    }),
    deleteRating: (workId, signal) => request<RatingsResponse>(`/v2/works/${encodeURIComponent(workId)}/rating`, signal, { method: 'DELETE' }),
    documentContentUrl: (id) => url(`/v2/documents/${encodeURIComponent(id)}/content`),
    documentDownloadUrl: (id) => url(`/v2/documents/${encodeURIComponent(id)}/download`),
    assetContentUrl: (id) => url(`/v2/assets/${encodeURIComponent(id)}/content`),
  }
}

export function searchResultToBook(result: WorkSummary): BookRecordLike {
  return { id: String(result.work_id ?? result.id), title: result.title, subtitle: result.subtitle ?? undefined }
}

export interface BookRecordLike {
  id: string
  title: string
  subtitle?: string
}
