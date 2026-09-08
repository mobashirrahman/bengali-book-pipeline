from __future__ import annotations

import sqlite3
from datetime import UTC
from pathlib import Path

from .models import (
    Author,
    AuthorRole,
    Book,
    Bookmark,
    FileRecord,
    ProcessingRecord,
    ProcessingState,
    ReadingList,
    ReadingProgress,
    Review,
    Subject,
    User,
)
from .schema import initialize_database


def _row_to_book(row: sqlite3.Row) -> Book:
    return Book(
        id=row["id"],
        title=row["title"],
        title_sort=row["title_sort"],
        description=row["description"],
        language=row["language"],
        publisher=row["publisher"],
        isbn=row["isbn"],
        edition=row["edition"],
        source_date=row["source_date"],
        series=row["series"],
        series_index=row["series_index"],
        page_count=row["page_count"],
        cover_path=row["cover_path"],
        rokomari_url=row["rokomari_url"],
        google_books_id=row["google_books_id"],
        open_library_key=row["open_library_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_author(row: sqlite3.Row) -> Author:
    return Author(
        id=row["id"],
        name=row["name"],
        name_sort=row["name_sort"],
    )


def _row_to_file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        book_id=row["book_id"],
        source_path=row["source_path"],
        sha256=row["sha256"],
        file_size=row["file_size"],
        output_md=row["output_md"],
        output_epub=row["output_epub"],
        chunks_path=row["chunks_path"],
        cover_image_path=row["cover_image_path"],
    )


def _row_to_subject(row: sqlite3.Row) -> Subject:
    return Subject(id=row["id"], name=row["name"])


class CatalogueDB:
    def __init__(self, db_path: str | Path, *, initialize: bool = True):
        self.db_path = Path(db_path)
        self.conn = (
            initialize_database(self.db_path)
            if initialize
            else self.open_connection(self.db_path)
        )
        self.conn.row_factory = sqlite3.Row

    @staticmethod
    def open_connection(db_path: str | Path) -> sqlite3.Connection:
        """Open an already initialized database with request-safe settings.

        Each request owns its connection: FastAPI may create, use, and close
        it on different worker threads, so thread affinity is disabled here.
        A request connection must never be shared across threads concurrently
        or stored globally; open one connection per request instead.
        """
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def connect(cls, db_path: str | Path) -> CatalogueDB:
        """Create a database wrapper without running schema initialization."""
        return cls(db_path, initialize=False)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> CatalogueDB:  # noqa: PYI034
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Books ───────────────────────────────────────────────────────

    def create_book(self, book: Book) -> int:
        cur = self.conn.execute(
            """INSERT INTO books (
                title, title_sort, description, language, publisher, isbn,
                edition, source_date, series, series_index, page_count,
                cover_path, rokomari_url, google_books_id, open_library_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                book.title, book.title_sort, book.description, book.language,
                book.publisher, book.isbn, book.edition, book.source_date,
                book.series, book.series_index, book.page_count, book.cover_path,
                book.rokomari_url, book.google_books_id, book.open_library_key,
            ),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_book(self, book_id: int) -> Book | None:
        row = self.conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        return _row_to_book(row) if row else None

    def find_book_by_isbn(self, isbn: str) -> Book | None:
        row = self.conn.execute("SELECT * FROM books WHERE isbn = ?", (isbn,)).fetchone()
        return _row_to_book(row) if row else None

    def find_books_by_title(
        self, title: str, limit: int = 10
    ) -> list[Book]:
        rows = self.conn.execute(
            "SELECT * FROM books WHERE title LIKE ? LIMIT ?",
            (f"%{title}%", limit),
        ).fetchall()
        return [_row_to_book(r) for r in rows]

    def update_book(self, book: Book) -> None:
        if book.id is None:
            raise ValueError("Book id is required for update")
        self.conn.execute(
            """UPDATE books SET
                title=?, title_sort=?, description=?, language=?, publisher=?,
                isbn=?, edition=?, source_date=?, series=?, series_index=?,
                page_count=?, cover_path=?, rokomari_url=?, google_books_id=?,
                open_library_key=?, updated_at=datetime('now')
            WHERE id=?""",
            (
                book.title, book.title_sort, book.description, book.language,
                book.publisher, book.isbn, book.edition, book.source_date,
                book.series, book.series_index, book.page_count, book.cover_path,
                book.rokomari_url, book.google_books_id, book.open_library_key,
                book.id,
            ),
        )
        self.conn.commit()

    def list_books(
        self,
        offset: int = 0,
        limit: int = 20,
        author_id: int | None = None,
        language: str | None = None,
    ) -> list[Book]:
        query = "SELECT DISTINCT b.* FROM books b"
        params: list[object] = []
        conditions: list[str] = []

        if author_id is not None:
            query += " JOIN book_authors ba ON b.id = ba.book_id"
            conditions.append("ba.author_id = ?")
            params.append(author_id)
        if language is not None:
            conditions.append("b.language = ?")
            params.append(language)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY b.id LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.conn.execute(query, params).fetchall()
        return [_row_to_book(r) for r in rows]

    def count_books(
        self,
        author_id: int | None = None,
        language: str | None = None,
    ) -> int:
        query = "SELECT COUNT(DISTINCT b.id) FROM books b"
        params: list[object] = []
        conditions: list[str] = []

        if author_id is not None:
            query += " JOIN book_authors ba ON b.id = ba.book_id"
            conditions.append("ba.author_id = ?")
            params.append(author_id)
        if language is not None:
            conditions.append("b.language = ?")
            params.append(language)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        row = self.conn.execute(query, params).fetchone()
        return row[0] if row else 0

    # ── Authors ─────────────────────────────────────────────────────

    def create_author(self, author: Author) -> int:
        cur = self.conn.execute(
            "INSERT INTO authors (name, name_sort) VALUES (?, ?)",
            (author.name, author.name_sort),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_or_create_author(self, name: str, name_sort: str | None = None) -> int:
        row = self.conn.execute(
            "SELECT id FROM authors WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        return self.create_author(Author(name=name, name_sort=name_sort))

    def get_author(self, author_id: int) -> Author | None:
        row = self.conn.execute(
            "SELECT * FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
        return _row_to_author(row) if row else None

    def find_authors(self, name: str, limit: int = 10) -> list[Author]:
        rows = self.conn.execute(
            "SELECT * FROM authors WHERE name LIKE ? LIMIT ?",
            (f"%{name}%", limit),
        ).fetchall()
        return [_row_to_author(r) for r in rows]

    def list_authors(self, offset: int = 0, limit: int = 50) -> list[Author]:
        rows = self.conn.execute(
            "SELECT * FROM authors ORDER BY name LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_author(r) for r in rows]

    def get_author_books(self, author_id: int) -> list[Book]:
        rows = self.conn.execute(
            """SELECT b.* FROM books b
               JOIN book_authors ba ON b.id = ba.book_id
               WHERE ba.author_id = ?
               ORDER BY b.title""",
            (author_id,),
        ).fetchall()
        return [_row_to_book(r) for r in rows]

    # ── Book-Author links ───────────────────────────────────────────

    def add_book_author(
        self, book_id: int, author_id: int, role: AuthorRole = AuthorRole.AUTHOR
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, role) VALUES (?, ?, ?)",
            (book_id, author_id, role.value),
        )
        self.conn.commit()

    def get_book_authors(self, book_id: int) -> list[Author]:
        rows = self.conn.execute(
            """SELECT a.* FROM authors a
               JOIN book_authors ba ON a.id = ba.author_id
               WHERE ba.book_id = ?
               ORDER BY ba.role, a.name""",
            (book_id,),
        ).fetchall()
        return [_row_to_author(r) for r in rows]

    # ── Subjects ────────────────────────────────────────────────────

    def get_or_create_subject(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM subjects WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO subjects (name) VALUES (?)", (name,)
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def add_book_subject(self, book_id: int, subject_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO book_subjects (book_id, subject_id) VALUES (?, ?)",
            (book_id, subject_id),
        )
        self.conn.commit()

    def get_book_subjects(self, book_id: int) -> list[Subject]:
        rows = self.conn.execute(
            """SELECT s.* FROM subjects s
               JOIN book_subjects bs ON s.id = bs.subject_id
               WHERE bs.book_id = ?""",
            (book_id,),
        ).fetchall()
        return [_row_to_subject(r) for r in rows]

    def list_subjects(self, offset: int = 0, limit: int = 50) -> list[Subject]:
        rows = self.conn.execute(
            "SELECT * FROM subjects ORDER BY name LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_subject(r) for r in rows]

    # ── Files ───────────────────────────────────────────────────────

    def create_file(self, file: FileRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO files (
                book_id, source_path, sha256, file_size,
                output_md, output_epub, chunks_path, cover_image_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file.book_id, file.source_path, file.sha256, file.file_size,
                file.output_md, file.output_epub, file.chunks_path, file.cover_image_path,
            ),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_file_by_sha256(self, sha256: str) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return _row_to_file(row) if row else None

    def get_book_file(self, book_id: int) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE book_id = ?", (book_id,)
        ).fetchone()
        return _row_to_file(row) if row else None

    def update_file(self, file: FileRecord) -> None:
        if file.id is None:
            raise ValueError("File id is required for update")
        self.conn.execute(
            """UPDATE files SET
                book_id=?, source_path=?, file_size=?,
                output_md=?, output_epub=?, chunks_path=?, cover_image_path=?
            WHERE id=?""",
            (
                file.book_id, file.source_path, file.file_size,
                file.output_md, file.output_epub, file.chunks_path,
                file.cover_image_path, file.id,
            ),
        )
        self.conn.commit()

    # ── Processing ──────────────────────────────────────────────────

    def create_processing(self, proc: ProcessingRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO processing (file_id, state, worker_host, job_id, error)
               VALUES (?, ?, ?, ?, ?)""",
            (proc.file_id, proc.state.value, proc.worker_host, proc.job_id, proc.error),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_processing_state(
        self,
        proc_id: int,
        state: ProcessingState,
        error: str | None = None,
        worker_host: str | None = None,
    ) -> None:
        from datetime import datetime
        now = datetime.now(UTC).isoformat()
        if state == ProcessingState.DONE or state == ProcessingState.FAILED:
            self.conn.execute(
                """UPDATE processing SET state=?, error=?, completed_at=?
                   WHERE id=?""",
                (state.value, error, now, proc_id),
            )
        else:
            self.conn.execute(
                """UPDATE processing SET state=?, error=?, worker_host=?, started_at=?
                   WHERE id=?""",
                (state.value, error, worker_host, now, proc_id),
            )
        self.conn.commit()

    def get_processing_by_state(self, state: ProcessingState) -> list[ProcessingRecord]:
        rows = self.conn.execute(
            "SELECT * FROM processing WHERE state = ?", (state.value,)
        ).fetchall()
        return [
            ProcessingRecord(
                id=r["id"],
                file_id=r["file_id"],
                state=ProcessingState(r["state"]),
                worker_host=r["worker_host"],
                job_id=r["job_id"],
                error=r["error"],
                started_at=r["started_at"],
                completed_at=r["completed_at"],
            )
            for r in rows
        ]

    # ── Users ───────────────────────────────────────────────────────

    def create_user(self, user: User) -> int:
        cur = self.conn.execute(
            "INSERT INTO users (username, display_name) VALUES (?, ?)",
            (user.username, user.display_name),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_user(self, user_id: int) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            created_at=row["created_at"],
        )

    def get_user_by_username(self, username: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            created_at=row["created_at"],
        )

    # ── Reading Lists ───────────────────────────────────────────────

    def create_reading_list(self, reading_list: ReadingList) -> int:
        cur = self.conn.execute(
            "INSERT INTO reading_lists (user_id, name) VALUES (?, ?)",
            (reading_list.user_id, reading_list.name),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_reading_list(self, list_id: int) -> ReadingList | None:
        row = self.conn.execute(
            "SELECT * FROM reading_lists WHERE id = ?", (list_id,)
        ).fetchone()
        if not row:
            return None
        return ReadingList(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            created_at=row["created_at"],
        )

    def add_book_to_reading_list(self, list_id: int, book_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO reading_list_books (list_id, book_id) VALUES (?, ?)",
            (list_id, book_id),
        )
        self.conn.commit()

    def get_reading_list_books(self, list_id: int) -> list[Book]:
        rows = self.conn.execute(
            """SELECT b.* FROM books b
               JOIN reading_list_books rlb ON b.id = rlb.book_id
               WHERE rlb.list_id = ?
               ORDER BY rlb.added_at""",
            (list_id,),
        ).fetchall()
        return [_row_to_book(r) for r in rows]

    def get_user_reading_lists(self, user_id: int) -> list[ReadingList]:
        rows = self.conn.execute(
            "SELECT * FROM reading_lists WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
        return [
            ReadingList(id=r["id"], user_id=r["user_id"], name=r["name"], created_at=r["created_at"])
            for r in rows
        ]

    # ── Bookmarks ───────────────────────────────────────────────────

    def create_bookmark(self, bookmark: Bookmark) -> int:
        cur = self.conn.execute(
            "INSERT INTO bookmarks (user_id, book_id, position, note) VALUES (?, ?, ?, ?)",
            (bookmark.user_id, bookmark.book_id, bookmark.position, bookmark.note),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_user_bookmarks(self, user_id: int, book_id: int) -> list[Bookmark]:
        rows = self.conn.execute(
            """SELECT * FROM bookmarks
               WHERE user_id = ? AND book_id = ?
               ORDER BY created_at""",
            (user_id, book_id),
        ).fetchall()
        return [
            Bookmark(
                id=r["id"], user_id=r["user_id"], book_id=r["book_id"],
                position=r["position"], note=r["note"], created_at=r["created_at"],
            )
            for r in rows
        ]

    # ── Reviews ─────────────────────────────────────────────────────

    def upsert_review(self, review: Review) -> int:
        existing = self.conn.execute(
            "SELECT id FROM reviews WHERE user_id = ? AND book_id = ?",
            (review.user_id, review.book_id),
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE reviews SET rating=?, text=? WHERE id=?",
                (review.rating, review.text, existing["id"]),
            )
            self.conn.commit()
            return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO reviews (user_id, book_id, rating, text) VALUES (?, ?, ?, ?)",
            (review.user_id, review.book_id, review.rating, review.text),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_book_reviews(self, book_id: int) -> list[Review]:
        rows = self.conn.execute(
            """SELECT r.*, u.username FROM reviews r
               JOIN users u ON r.user_id = u.id
               WHERE r.book_id = ?
               ORDER BY r.created_at""",
            (book_id,),
        ).fetchall()
        return [
            Review(
                id=r["id"], user_id=r["user_id"], book_id=r["book_id"],
                rating=r["rating"], text=r["text"], created_at=r["created_at"],
            )
            for r in rows
        ]

    # ── Reading Progress ────────────────────────────────────────────

    def upsert_reading_progress(self, progress: ReadingProgress) -> None:
        self.conn.execute(
            """INSERT INTO reading_progress (user_id, book_id, position, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, book_id) DO UPDATE SET
               position=excluded.position, updated_at=excluded.updated_at""",
            (progress.user_id, progress.book_id, progress.position),
        )
        self.conn.commit()

    def get_reading_progress(self, user_id: int, book_id: int) -> ReadingProgress | None:
        row = self.conn.execute(
            "SELECT * FROM reading_progress WHERE user_id = ? AND book_id = ?",
            (user_id, book_id),
        ).fetchone()
        if not row:
            return None
        return ReadingProgress(
            user_id=row["user_id"],
            book_id=row["book_id"],
            position=row["position"],
            updated_at=row["updated_at"],
        )

    # ── Favorites ───────────────────────────────────────────────────

    def toggle_favorite(self, user_id: int, book_id: int) -> bool:
        existing = self.conn.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND book_id = ?",
            (user_id, book_id),
        ).fetchone()
        if existing:
            self.conn.execute(
                "DELETE FROM favorites WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO favorites (user_id, book_id) VALUES (?, ?)",
            (user_id, book_id),
        )
        self.conn.commit()
        return True

    def get_user_favorites(self, user_id: int) -> list[Book]:
        rows = self.conn.execute(
            """SELECT b.* FROM books b
               JOIN favorites f ON b.id = f.book_id
               WHERE f.user_id = ?
               ORDER BY f.created_at""",
            (user_id,),
        ).fetchall()
        return [_row_to_book(r) for r in rows]

    def is_favorite(self, user_id: int, book_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND book_id = ?",
            (user_id, book_id),
        ).fetchone()
        return row is not None
