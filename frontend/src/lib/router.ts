import type { Route } from './types'

export function parseRoute(pathname = window.location.pathname, search = window.location.search): Route {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] === 'discover') return { name: 'discover', query: new URLSearchParams(search).get('q') ?? '' }
  if (parts[0] === 'works' && parts[1]) return { name: 'work', id: parts[1] }
  if (parts[0] === 'read' && parts[1]) return { name: 'reader', id: parts[1] }
  return { name: 'home' }
}

export function routeHref(route: Route): string {
  if (route.name === 'discover') return `/discover${route.query ? `?q=${encodeURIComponent(route.query)}` : ''}`
  if (route.name === 'work') return `/works/${encodeURIComponent(route.id)}`
  if (route.name === 'reader') return `/read/${encodeURIComponent(route.id)}`
  return '/'
}
