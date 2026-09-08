import type { Route } from './types'

export function parseRoute(pathname = window.location.pathname, search = window.location.search): Route {
  const normalized = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname
  if (normalized === '/shelf') return { name: 'shelf' }
  if (normalized === '/' || normalized === '') return { name: 'home' }
  const parts = normalized.split('/').filter(Boolean)
  if (parts[0] === 'discover' && parts.length === 1) return { name: 'discover', query: new URLSearchParams(search).get('q') ?? '' }
  if (parts[0] === 'works' && parts.length === 2 && parts[1]) return { name: 'work', id: parts[1] }
  if (parts[0] === 'read' && parts.length === 2 && parts[1]) return { name: 'reader', id: parts[1] }
  return { name: 'notFound', path: normalized }
}

export function routeHref(route: Route): string {
  if (route.name === 'shelf') return '/shelf'
  if (route.name === 'notFound') return '/'
  if (route.name === 'discover') return `/discover${route.query ? `?q=${encodeURIComponent(route.query)}` : ''}`
  if (route.name === 'work') return `/works/${encodeURIComponent(route.id)}`
  if (route.name === 'reader') return `/read/${encodeURIComponent(route.id)}`
  return '/'
}
