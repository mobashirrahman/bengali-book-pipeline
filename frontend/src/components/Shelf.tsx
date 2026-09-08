import type { BookRecord } from '../lib/types'
import { BookCard } from './BookCard'

interface ShelfProps {
  title: string
  kicker?: string
  books: BookRecord[]
  onOpen: (book: BookRecord) => void
  action?: string
  onAction?: () => void
}

export function Shelf({ title, kicker, books, onOpen, action, onAction }: ShelfProps) {
  if (!books.length) return null
  return (
    <section className="shelf" aria-labelledby={`shelf-${title.replace(/\W/g, '-').toLowerCase()}`}>
      <div className="shelf__heading">
        <div><p className="eyebrow">{kicker ?? 'Curated for you'}</p><h2 id={`shelf-${title.replace(/\W/g, '-').toLowerCase()}`}>{title}</h2></div>
        {action && <button className="quiet-button" onClick={onAction}>{action} <span aria-hidden="true">→</span></button>}
      </div>
      <div className="shelf__scroller" tabIndex={0} aria-label={`${title} shelf. Use shift and mouse wheel or horizontal scroll to browse.`}>
        {books.map((book) => <BookCard key={book.id} book={book} onOpen={onOpen} />)}
      </div>
    </section>
  )
}
