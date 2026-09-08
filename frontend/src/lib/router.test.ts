import { describe, expect, it } from 'vitest'
import { parseRoute, routeHref } from './router'

describe('route parsing', () => {
  it('parses the known routes', () => {
    expect(parseRoute('/', '')).toEqual({ name: 'home' })
    expect(parseRoute('/shelf', '')).toEqual({ name: 'shelf' })
    expect(parseRoute('/discover', '?q=night')).toEqual({ name: 'discover', query: 'night' })
    expect(parseRoute('/works/7', '')).toEqual({ name: 'work', id: '7' })
    expect(parseRoute('/read/7', '')).toEqual({ name: 'reader', id: '7' })
  })

  it('accepts trailing slashes on known routes while preserving unknown route behavior', () => {
    expect(parseRoute('/shelf/', '')).toEqual({ name: 'shelf' })
    expect(parseRoute('/discover/', '?q=night')).toEqual({ name: 'discover', query: 'night' })
    expect(parseRoute('/works/7/', '')).toEqual({ name: 'work', id: '7' })
    expect(parseRoute('/read/7/', '')).toEqual({ name: 'reader', id: '7' })
    expect(parseRoute('/nope/', '')).toEqual({ name: 'notFound', path: '/nope' })
  })

  it('sends unknown or incomplete paths to a not-found route with a clear exit', () => {
    expect(parseRoute('/nope', '')).toEqual({ name: 'notFound', path: '/nope' })
    expect(parseRoute('/works', '')).toEqual({ name: 'notFound', path: '/works' })
    expect(parseRoute('/read', '')).toEqual({ name: 'notFound', path: '/read' })
    expect(parseRoute('/works/7/extra', '')).toEqual({ name: 'notFound', path: '/works/7/extra' })
    expect(routeHref({ name: 'notFound', path: '/nope' })).toBe('/')
  })

  it('round-trips known routes through hrefs', () => {
    expect(parseRoute('/discover', '?q=garden')).toEqual({ name: 'discover', query: 'garden' })
    expect(routeHref({ name: 'work', id: '7' })).toBe('/works/7')
    expect(routeHref({ name: 'reader', id: '7' })).toBe('/read/7')
    expect(routeHref({ name: 'shelf' })).toBe('/shelf')
  })
})
