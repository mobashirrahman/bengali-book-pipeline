from __future__ import annotations

import sqlite3

from .database import CatalogueDB
from .models import Author, Book


def search_books(
    db: CatalogueDB,
    query: str,
    limit: int = 20,
    offset: int = 0,
) -> list[Book]:
    try:
        rows = db.conn.execute(
            """SELECT b.* FROM books b
               JOIN books_fts ON b.id = books_fts.rowid
               WHERE books_fts MATCH ?
               ORDER BY rank
               LIMIT ? OFFSET ?""",
            (query, limit, offset),
        ).fetchall()
        from .database import _row_to_book
        return [_row_to_book(r) for r in rows]
    except sqlite3.OperationalError:
        return db.find_books_by_title(query, limit=limit)


def search_books_with_authors(
    db: CatalogueDB,
    query: str,
    limit: int = 20,
    offset: int = 0,
) -> list[tuple[Book, list[Author]]]:
    books = search_books(db, query, limit=limit, offset=offset)
    results = []
    for book in books:
        authors = db.get_book_authors(book.id)  # type: ignore[arg-type]
        results.append((book, authors))
    return results


def rebuild_fts_index(db: CatalogueDB) -> None:
    db.conn.execute("DELETE FROM books_fts")
    db.conn.execute(
        """INSERT INTO books_fts(rowid, title, description, author_names, subjects)
           SELECT b.id, b.title, b.description,
                  COALESCE(GROUP_CONCAT(DISTINCT a.name), ''),
                  COALESCE(GROUP_CONCAT(DISTINCT s.name), '')
           FROM books b
           LEFT JOIN book_authors ba ON b.id = ba.book_id
           LEFT JOIN authors a ON ba.author_id = a.id
           LEFT JOIN book_subjects bs ON b.id = bs.book_id
           LEFT JOIN subjects s ON bs.subject_id = s.id
           GROUP BY b.id"""
    )
    db.conn.commit()


def get_book_stats(db: CatalogueDB) -> dict[str, int | str]:
    stats: dict[str, int | str] = {}
    row = db.conn.execute("SELECT COUNT(*) FROM books").fetchone()
    stats["total_books"] = row[0] if row else 0
    row = db.conn.execute("SELECT COUNT(*) FROM authors").fetchone()
    stats["total_authors"] = row[0] if row else 0
    row = db.conn.execute("SELECT COUNT(*) FROM subjects").fetchone()
    stats["total_subjects"] = row[0] if row else 0
    row = db.conn.execute("SELECT COUNT(DISTINCT language) FROM books").fetchone()
    stats["total_languages"] = row[0] if row else 0
    row = db.conn.execute("SELECT COUNT(*) FROM files").fetchone()
    stats["total_files"] = row[0] if row else 0
    return stats
