import type { Asset, BookRecord, EditionResponse, WorkResponse } from './types'

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
    documents: (edition?.documents ?? []).map((document) => ({ id: document.id, mediaType: document.media_type })),
    tags: [work.language, edition?.publisher].filter((value): value is string => Boolean(value)),
    ratings: work.ratings,
  }
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
    documents: edition.documents.map((document) => ({ id: document.id, mediaType: document.media_type })),
    tags: [edition.language, edition.publisher].filter((value): value is string => Boolean(value)),
  }
}
