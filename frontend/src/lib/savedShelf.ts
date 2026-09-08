import type { BookRecord, DataMode } from './types'

export const shelfKey = (mode: DataMode) => `folio:shelf:v1:${mode}`
export interface SavedBook { id: string; title: string; author: string }

export function readShelf(storage: Pick<Storage, 'getItem'>, mode: DataMode): SavedBook[] {
  try {
    const value: unknown = JSON.parse(storage.getItem(shelfKey(mode)) ?? '[]')
    if (!Array.isArray(value)) return []
    const seen = new Set<string>()
    return value.filter((item): item is SavedBook => {
      if (!item || typeof item.id !== 'string' || typeof item.title !== 'string' || typeof item.author !== 'string' || seen.has(item.id)) return false
      if (mode === 'live' ? !/^\d+$/.test(item.id) : !item.id.startsWith('demo-')) return false
      seen.add(item.id)
      return true
    })
  } catch { return [] }
}

export function toggleSaved(books: SavedBook[], book: Pick<BookRecord, 'id' | 'title' | 'author'>): SavedBook[] {
  return books.some((item) => item.id === book.id)
    ? books.filter((item) => item.id !== book.id)
    : [...books, { id: book.id, title: book.title, author: book.author }]
}
