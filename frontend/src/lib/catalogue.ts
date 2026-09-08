import type { Asset, BookDocument, BookRecord, DataMode, EditionResponse, WorkResponse, WorkSummary } from './types'
import { isAbortError, type CatalogueApi } from './api'

const safeDate = (value?: string | null) => value?.slice(0, 4) || '—'
function coverDetails(asset: Asset | undefined, assetContentUrl?: (id: number) => string) {
  if (!asset) return { coverUrl: undefined, coverAssetId: undefined }
  const trusted = asset.verification_status === 'validated' && !asset.storage_uri.startsWith('remote:')
  if (trusted && asset.is_selected && assetContentUrl) {
    return {
      coverUrl: assetContentUrl(asset.id),
      coverAssetId: asset.id,
      coverWidth: asset.width ?? undefined,
      coverHeight: asset.height ?? undefined,
      coverVerificationStatus: asset.verification_status,
    }
  }
  return {
    coverUrl: undefined,
    coverAssetId: undefined,
    coverWidth: trusted ? asset.width ?? undefined : undefined,
    coverHeight: trusted ? asset.height ?? undefined : undefined,
    coverVerificationStatus: asset.verification_status,
  }
}

function coverAsset(assets: Asset[] | undefined) {
  return assets?.find((candidate) =>
    candidate.asset_type === 'cover' && candidate.verification_status === 'validated' && Boolean(candidate.is_selected) && !candidate.storage_uri.startsWith('remote:'))
}

export function workToBook(work: WorkResponse, assetContentUrl?: (id: number) => string): BookRecord {
  // Prefer an edition with an accepted local document so a later readable
  // edition is not hidden behind an older metadata-only edition.
  const edition = work.editions.find((candidate) => candidate.documents.length > 0) ?? work.editions[0]
  const people = edition?.people.filter((person) => person.role === 'author' || !person.role) ?? []
  const author = people.map((person) => person.name).join(', ') || 'Unknown author'
  const asset = coverAsset(edition?.assets)
  const cover = coverDetails(asset, assetContentUrl)
  // Aggregate accepted documents across every edition so a readable file on
  // a non-primary edition is never hidden. Primary-edition metadata (cover,
  // author, pages) still comes from the preferred edition above.
  const documents = collectDocuments(work.editions)
  return {
    id: String(work.id),
    workId: work.id,
    editionId: edition?.id,
    title: work.title,
    subtitle: work.subtitle ?? edition?.subtitle ?? undefined,
    author,
    description: work.description ?? edition?.description ?? 'No description has been added to this work yet.',
    language: work.language ?? edition?.language ?? 'Unknown language',
    year: safeDate(edition?.publication_date),
    pages: edition?.page_count ?? 0,
    category: edition?.edition_statement ?? 'Catalogue work',
    readingTime: edition?.page_count ? `${Math.max(1, Math.round(edition.page_count / 60))} hr read` : 'Reading time unknown',
    accent: 'linear-gradient(145deg, #56697b, #1d2028 70%)',
    ...cover,
    coverAttribution: asset?.attribution ?? undefined,
    sourceLabel: work.sources.join(', ') || 'Catalogue API',
    sourceKind: 'catalogue',
    documents,
    tags: [work.language, edition?.publisher].filter((value): value is string => Boolean(value)),
    ratings: work.ratings,
  }
}

function collectDocuments(editions: WorkResponse['editions']): BookDocument[] {  const documents: BookDocument[] = []
  const seen = new Set<number>()
  for (const edition of editions ?? []) {
    for (const document of edition.documents ?? []) {
      if (seen.has(document.id)) continue
      seen.add(document.id)
      documents.push({
        id: document.id,
        mediaType: document.media_type,
        editionId: edition.id,
        editionTitle: edition.title ?? undefined,
      })
    }
  }
  return documents
}

export function editionToBook(edition: EditionResponse, assetContentUrl?: (id: number) => string): BookRecord {
  const author = edition.people.map((person) => person.name).join(', ') || 'Unknown author'
  const asset = coverAsset(edition.assets)
  const cover = coverDetails(asset, assetContentUrl)
  return {
    id: String(edition.work_id ?? edition.id),
    workId: edition.work_id ?? undefined,
    editionId: edition.id,
    title: edition.title,
    subtitle: edition.subtitle ?? undefined,
    author,
    description: edition.description ?? 'No description has been added to this edition yet.',
    language: edition.language ?? 'Unknown language',
    year: safeDate(edition.publication_date),
    pages: edition.page_count ?? 0,
    category: edition.edition_statement ?? 'Edition',
    readingTime: edition.page_count ? `${Math.max(1, Math.round(edition.page_count / 60))} hr read` : 'Reading time unknown',
    accent: 'linear-gradient(145deg, #56697b, #1d2028 70%)',
    ...cover,
    coverAttribution: asset?.attribution ?? undefined,
    sourceLabel: edition.sources.join(', ') || 'Catalogue API',
    sourceKind: 'catalogue',
    documents: edition.documents.map((document) => ({ id: document.id, mediaType: document.media_type, editionId: edition.id, editionTitle: edition.title ?? undefined })),
    tags: [edition.language, edition.publisher].filter((value): value is string => Boolean(value)),
  }
}

export function filterReadable(books: BookRecord[], readableOnly: boolean, mode: DataMode): BookRecord[] {
  // Demo records carry no files, so the toggle only narrows live catalogue
  // results; demo browsing is unaffected.
  if (!readableOnly || mode !== 'live') return books
  return books.filter((book) => book.documents.length > 0)
}

export function uniqueSearchWorkIds(items: WorkSummary[]): number[] {
  // /v2/search returns a work row and an edition row for the same book, so
  // collapse to unique work ids in API order before slicing or hydrating.
  // Person rows never become books.
  const seen = new Set<number>()
  const ids: number[] = []
  for (const item of items) {
    if (item.kind === 'person') continue
    const workId = item.work_id ?? item.id
    if (seen.has(workId)) continue
    seen.add(workId)
    ids.push(workId)
  }
  return ids
}

export interface DiscoverSearchOptions {
  readableOnly?: boolean
  signal?: AbortSignal
}

export async function discoverSearch(
  api: Pick<CatalogueApi, 'search' | 'works' | 'work' | 'assetContentUrl'>,
  query: string,
  options: DiscoverSearchOptions = {},
): Promise<BookRecord[]> {
  if (!query.trim()) {
    // An empty query is browsing, not searching: a LIKE '%a%' scan returns
    // arbitrary metadata-only works, so list readable works instead and
    // hydrate them exactly like the home shelf does.
    const summaries = await api.works({ limit: 24, hasDocuments: true, signal: options.signal })
    return Promise.all(summaries.map(async (summary) => {
      try {
        return workToBook(await api.work(summary.id, options.signal), api.assetContentUrl)
      } catch (reason: unknown) {
        if (isAbortError(reason) || options.signal?.aborted) throw reason
        return workToBook(summary, api.assetContentUrl)
      }
    }))
  }
  const response = await api.search(query.trim(), {
    limit: 24,
    hasDocuments: options.readableOnly ? true : undefined,
    signal: options.signal,
  })
  const details = await Promise.all(uniqueSearchWorkIds(response.items).slice(0, 12).map(async (workId) => {
    try {
      return workToBook(await api.work(workId, options.signal), api.assetContentUrl)
    } catch (reason: unknown) {
      if (isAbortError(reason)) throw reason
      return undefined
    }
  }))
  return details.filter((book): book is BookRecord => Boolean(book))
}
