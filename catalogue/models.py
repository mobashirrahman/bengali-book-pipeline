from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuthorRole(Enum):
    AUTHOR = "author"
    EDITOR = "editor"
    TRANSLATOR = "translator"


class ProcessingState(Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Author:
    id: int | None = None
    name: str = ""
    name_sort: str | None = None


@dataclass
class Subject:
    id: int | None = None
    name: str = ""


@dataclass
class Book:
    id: int | None = None
    title: str = ""
    title_sort: str | None = None
    description: str | None = None
    language: str = "bn"
    publisher: str | None = None
    isbn: str | None = None
    edition: str | None = None
    source_date: str | None = None
    series: str | None = None
    series_index: float | None = None
    page_count: int | None = None
    cover_path: str | None = None
    rokomari_url: str | None = None
    google_books_id: str | None = None
    open_library_key: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class BookAuthor:
    book_id: int = 0
    author_id: int = 0
    role: AuthorRole = AuthorRole.AUTHOR


@dataclass
class BookSubject:
    book_id: int = 0
    subject_id: int = 0


@dataclass
class FileRecord:
    id: int | None = None
    book_id: int | None = None
    source_path: str = ""
    sha256: str = ""
    file_size: int | None = None
    output_md: str | None = None
    output_epub: str | None = None
    chunks_path: str | None = None
    cover_image_path: str | None = None


@dataclass
class ProcessingRecord:
    id: int | None = None
    file_id: int | None = None
    state: ProcessingState = ProcessingState.PENDING
    worker_host: str | None = None
    job_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class User:
    id: int | None = None
    username: str = ""
    display_name: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class ReadingList:
    id: int | None = None
    user_id: int | None = None
    name: str = ""
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class Bookmark:
    id: int | None = None
    user_id: int | None = None
    book_id: int | None = None
    position: str = ""
    note: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class Review:
    id: int | None = None
    user_id: int | None = None
    book_id: int | None = None
    rating: int | None = None
    text: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class ReadingProgress:
    user_id: int = 0
    book_id: int = 0
    position: str = ""
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class Favorite:
    user_id: int = 0
    book_id: int = 0
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class BookWithAuthors:
    book: Book
    authors: list[Author] = field(default_factory=list)
    subjects: list[Subject] = field(default_factory=list)
    file: FileRecord | None = None
