export type DataMode = 'live' | 'demo'

export interface HealthResponse {
  status: string
  backend: string
}

export interface StatsResponse {
  works: number
  editions: number
  people: number
  identifiers: number
  local_documents: number
  document_matches: number
  assets: number
  artifacts: number
  source_coverage: Array<{ source: string; records: number }>
}

export interface Identifier {
  namespace: string
  value: string
}

export interface Person {
  id: number
  name: string
  sort_name?: string | null
  role?: string
  position?: number
}

export interface Asset {
  id: number
  edition_id?: number | null
  document_id?: number | null
  asset_type: string
  storage_uri: string
  source_url?: string | null
  sha256?: string | null
  mime_type?: string | null
  width?: number | null
  height?: number | null
  attribution?: string | null
  rights?: string | null
  is_selected: number
  status?: string
  verification_status?: 'candidate' | 'validated' | 'rejected' | string
  retrieved_at?: string | null
  metadata_json?: string
}

export type RatingStatus = 'available' | 'unavailable'

export interface CommunityRating {
  average: number | null
  count: number
  user_rating: number | null
}

export interface ExternalRating {
  provider: string
  rating_value: number | null
  scale_max: number
  rating_count: number | null
  review_count: number | null
  source_url: string | null
  status: RatingStatus
  reason: string | null
}

export interface RatingsResponse {
  work_id: number
  community: CommunityRating
  external: ExternalRating[]
}

export interface WorkSummary {
  id: number
  title: string
  subtitle?: string | null
  work_id?: number | null
  kind?: 'work' | 'edition' | 'person'
}

export interface EditionSummary {
  id: number
  title: string
  subtitle?: string | null
  publisher?: string | null
  publication_date?: string | null
  page_count?: number | null
  language?: string | null
}

export interface WorkResponse {
  id: number
  title: string
  subtitle?: string | null
  sort_title?: string | null
  language?: string | null
  description?: string | null
  created_at?: string | null
  updated_at?: string | null
  identifiers: Identifier[]
  editions: EditionResponse[]
  sources: string[]
  ratings?: RatingsResponse
}

export interface EditionResponse extends EditionSummary {
  work_id?: number | null
  edition_statement?: string | null
  description?: string | null
  people: Person[]
  identifiers: Identifier[]
  assets: Asset[]
  sources: string[]
  documents: Array<{ id: number; sha256: string; source_path: string; file_size: number; media_type: string }>
  work?: WorkSummary | null
}

export interface BookDocument {
  id: number
  mediaType: string
}

export interface DocumentResponse {
  id: number
  sha256: string
  source_path: string
  file_size: number
  media_type: string
  metadata_json: string
  matches: Array<Record<string, unknown>>
  assets: Asset[]
  artifacts: Array<Record<string, unknown>>
  locations: Array<Record<string, unknown>>
}

export interface SearchResponse {
  items: WorkSummary[]
  next: string | null
}

export interface BookRecord {
  id: string
  workId?: number
  title: string
  subtitle?: string
  author: string
  description: string
  language: string
  year: string
  pages: number
  category: string
  readingTime: string
  accent: string
  coverUrl?: string
  coverAssetId?: number
  coverAttribution?: string
  coverWidth?: number
  coverHeight?: number
  coverVerificationStatus?: string
  sourceLabel: string
  sourceKind: 'demo' | 'catalogue'
  editionId?: number
  documents: BookDocument[]
  tags: string[]
  ratings?: RatingsResponse
}

export type Route =
  | { name: 'home' }
  | { name: 'shelf' }
  | { name: 'discover'; query: string }
  | { name: 'work'; id: string }
  | { name: 'reader'; id: string }
  | { name: 'notFound'; path: string }
