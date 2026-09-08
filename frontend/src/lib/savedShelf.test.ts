import { describe, expect, it } from 'vitest'
import { readShelf, shelfKey, toggleSaved } from './savedShelf'

const memoryStorage = (initial: Record<string, string> = {}) => {
  const store = new Map(Object.entries(initial))
  return {
    getItem: (key: string) => store.get(key) ?? null,
  }
}

describe('saved shelf', () => {
  it('uses separate keys per mode', () => {
    expect(shelfKey('demo')).not.toBe(shelfKey('live'))
  })

  it('returns an empty shelf for missing, corrupt, or foreign records', () => {
    expect(readShelf(memoryStorage(), 'demo')).toEqual([])
    expect(readShelf(memoryStorage({ [shelfKey('demo')]: 'not-json' }), 'demo')).toEqual([])
    // Live shelves only keep numeric catalogue ids; demo shelves only keep demo ids.
    expect(readShelf(memoryStorage({ [shelfKey('live')]: JSON.stringify([{ id: 'demo-x', title: 'T', author: 'A' }]) }), 'live')).toEqual([])
    expect(readShelf(memoryStorage({ [shelfKey('demo')]: JSON.stringify([{ id: '7', title: 'T', author: 'A' }]) }), 'demo')).toEqual([])
  })

  it('toggles books without duplicating ids', () => {
    const book = { id: 'demo-x', title: 'T', author: 'A' }
    const added = toggleSaved([], book)
    expect(added).toHaveLength(1)
    expect(toggleSaved(added, book)).toEqual([])
  })
})
