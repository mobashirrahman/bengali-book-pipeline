import type { BookRecord } from '../lib/types'
import { CoverImage } from './CoverImage'
import { ArrowIcon } from './Icons'

interface BookCardProps {
  book: BookRecord
  onOpen: (book: BookRecord) => void
  variant?: 'shelf' | 'grid'
}

export function BookCard({ book, onOpen, variant = 'shelf' }: BookCardProps) {
  return (
    <article className={`book-card book-card--${variant}`}>
      <button className="book-card__cover-button" onClick={() => onOpen(book)} aria-label={`Open ${book.title}`}>
        <CoverImage title={book.title} sourceUrl={book.coverUrl} accent={book.accent} size={variant === 'grid' ? 'medium' : 'small'} />
      </button>
      <div className="book-card__body">
        <p className="eyebrow">{book.category}</p>
        <h3><button className="text-button" onClick={() => onOpen(book)}>{book.title}</button></h3>
        <p className="book-card__author">{book.author}</p>
        <div className="book-card__meta"><span>{book.year}</span><span aria-hidden="true">·</span><span>{book.readingTime}</span></div>
      </div>
      <button className="card-arrow" onClick={() => onOpen(book)} aria-label={`View details for ${book.title}`}><ArrowIcon /></button>
    </article>
  )
}
