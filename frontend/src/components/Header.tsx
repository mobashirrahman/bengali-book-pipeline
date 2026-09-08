import { useEffect, useState } from 'react'
import type { Route } from '../lib/types'
import { routeHref } from '../lib/router'
import { SearchIcon } from './Icons'

interface HeaderProps { route: Route; navigate: (href: string) => void }

export function Header({ route, navigate }: HeaderProps) {
  const [query, setQuery] = useState(route.name === 'discover' ? route.query : '')
  useEffect(() => setQuery(route.name === 'discover' ? route.query : ''), [route])
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    navigate(routeHref({ name: 'discover', query: query.trim() }))
  }
  return <header className="site-header">
    <a className="wordmark" href="/" onClick={(event) => { event.preventDefault(); navigate('/') }}><span className="wordmark__symbol">○</span><span>folio</span></a>
    <nav className="desktop-nav" aria-label="Primary navigation">
      <a className={route.name === 'home' ? 'is-active' : ''} href="/" onClick={(event) => { event.preventDefault(); navigate('/') }}>Home</a>
      <a className={route.name === 'discover' ? 'is-active' : ''} href="/discover" onClick={(event) => { event.preventDefault(); navigate('/discover') }}>Discover</a>
      <a href="/about" onClick={(event) => event.preventDefault()}>Your shelf</a>
    </nav>
    <form className="header-search" onSubmit={submit} role="search">
      <SearchIcon /><input aria-label="Search the catalogue" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search books, authors..." /><button type="submit">Search</button>
    </form>
    <button className="avatar" aria-label="Your account">A</button>
  </header>
}

export function MobileNav({ route, navigate }: HeaderProps) {
  return <nav className="mobile-nav" aria-label="Mobile navigation">
    <a className={route.name === 'home' ? 'is-active' : ''} href="/" onClick={(event) => { event.preventDefault(); navigate('/') }}><span>⌂</span>Home</a>
    <a className={route.name === 'discover' ? 'is-active' : ''} href="/discover" onClick={(event) => { event.preventDefault(); navigate('/discover') }}><SearchIcon />Discover</a>
    <a href="/about" onClick={(event) => event.preventDefault()}><span>▤</span>Shelf</a>
    <a href="/profile" onClick={(event) => event.preventDefault()}><span>◌</span>Profile</a>
  </nav>
}
