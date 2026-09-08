import { describe, expect, it } from 'vitest'
import { DEMO_BOOKS, findDemoBook, searchDemoBooks } from './demoData'

describe('explicit demo catalogue', () => {
  it('is deterministic and every record has a visible fallback cover', () => {
    expect(DEMO_BOOKS).toHaveLength(8)
    expect(DEMO_BOOKS.every((book) => book.sourceKind === 'demo' && book.sourceLabel.includes('demo'))).toBe(true)
    expect(DEMO_BOOKS.every((book) => book.accent.startsWith('linear-gradient'))).toBe(true)
  })

  it('supports case-insensitive local search without an API', () => {
    expect(searchDemoBooks('GARDEN').map((book) => book.title)).toEqual(['Wild Garden, Patient Hands'])
    expect(searchDemoBooks('')).toEqual(DEMO_BOOKS)
    expect(findDemoBook('demo-paper-moons')?.author).toBe('Sofia Arendt')
  })
})
