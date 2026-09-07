from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.models import (
    AuthorRole,
    Book,
    Bookmark,
    Favorite,
    FileRecord,
    ProcessingRecord,
    ProcessingState,
    ReadingList,
    ReadingProgress,
    Review,
    User,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_catalogue.db"


@pytest.fixture
def db(db_path: Path) -> CatalogueDB:
    catalogue = CatalogueDB(db_path)
    yield catalogue
    catalogue.close()


class TestSchemaAndCreation:
    def test_initialize_creates_file(self, db_path: Path) -> None:
        catalogue = CatalogueDB(db_path)
        assert db_path.exists()
        catalogue.close()

    def test_double_initialize_is_idempotent(self, db_path: Path) -> None:
        CatalogueDB(db_path).close()
        catalogue = CatalogueDB(db_path)
        catalogue.close()


class TestBooks:
    def test_create_and_get_book(self, db: CatalogueDB) -> None:
        book = Book(title="শেষের কবিতা", language="bn")
        book_id = db.create_book(book)
        assert book_id > 0

        fetched = db.get_book(book_id)
        assert fetched is not None
        assert fetched.title == "শেষের কবিতা"
        assert fetched.language == "bn"

    def test_find_book_by_isbn(self, db: CatalogueDB) -> None:
        book = Book(title="গোরা", isbn="9781234567890")
        db.create_book(book)

        found = db.find_book_by_isbn("9781234567890")
        assert found is not None
        assert found.title == "গোরা"

    def test_find_books_by_title(self, db: CatalogueDB) -> None:
        db.create_book(Book(title="নৌকাডুবি"))
        db.create_book(Book(title="নৌকাডুবি (সংক্ষিপ্ত)"))
        db.create_book(Book(title="গোরা"))

        results = db.find_books_by_title("নৌকাডুবি")
        assert len(results) == 2

    def test_update_book(self, db: CatalogueDB) -> None:
        book = Book(title="Test Book")
        book_id = db.create_book(book)
        book.id = book_id
        book.isbn = "1234567890"
        db.update_book(book)

        fetched = db.get_book(book_id)
        assert fetched is not None
        assert fetched.isbn == "1234567890"

    def test_list_books(self, db: CatalogueDB) -> None:
        for i in range(5):
            db.create_book(Book(title=f"Book {i}"))

        books = db.list_books(limit=3)
        assert len(books) == 3

        books = db.list_books(offset=3, limit=3)
        assert len(books) == 2

    def test_count_books(self, db: CatalogueDB) -> None:
        assert db.count_books() == 0
        db.create_book(Book(title="Book 1"))
        db.create_book(Book(title="Book 2"))
        assert db.count_books() == 2


class TestAuthors:
    def test_create_and_get_author(self, db: CatalogueDB) -> None:
        from pdf_craft.catalogue.models import Author
        author = Author(name="রবীন্দ্রনাথ ঠাকুর")
        author_id = db.create_author(author)
        assert author_id > 0

        fetched = db.get_author(author_id)
        assert fetched is not None
        assert fetched.name == "রবীন্দ্রনাথ ঠাকুর"

    def test_get_or_create_author(self, db: CatalogueDB) -> None:
        id1 = db.get_or_create_author("বঙ্কিমচন্দ্র চট্টোপাধ্যায়")
        id2 = db.get_or_create_author("বঙ্কিমচন্দ্র চট্টোপাধ্যায়")
        assert id1 == id2

    def test_book_author_link(self, db: CatalogueDB) -> None:
        from pdf_craft.catalogue.models import Author
        book_id = db.create_book(Book(title="কপালকুণ্ডলা"))
        author_id = db.create_author(Author(name="বঙ্কিমচন্দ্র চট্টোপাধ্যায়"))
        db.add_book_author(book_id, author_id, AuthorRole.AUTHOR)

        authors = db.get_book_authors(book_id)
        assert len(authors) == 1
        assert authors[0].name == "বঙ্কিমচন্দ্র চট্টোপাধ্যায়"

    def test_get_author_books(self, db: CatalogueDB) -> None:
        from pdf_craft.catalogue.models import Author
        author_id = db.create_author(Author(name="হুমায়ুন আহমেদ"))
        b1 = db.create_book(Book(title="মিসির আলি"))
        b2 = db.create_book(Book(title="তোমার ঵োনা"))
        db.add_book_author(b1, author_id, AuthorRole.AUTHOR)
        db.add_book_author(b2, author_id, AuthorRole.AUTHOR)

        books = db.get_author_books(author_id)
        assert len(books) == 2


class TestSubjects:
    def test_get_or_create_subject(self, db: CatalogueDB) -> None:
        id1 = db.get_or_create_subject("Classic Novel")
        id2 = db.get_or_create_subject("Classic Novel")
        assert id1 == id2

    def test_book_subject_link(self, db: CatalogueDB) -> None:
        book_id = db.create_book(Book(title="Test"))
        subject_id = db.get_or_create_subject("Fiction")
        db.add_book_subject(book_id, subject_id)

        subjects = db.get_book_subjects(book_id)
        assert len(subjects) == 1
        assert subjects[0].name == "Fiction"


class TestFiles:
    def test_create_and_get_file(self, db: CatalogueDB) -> None:
        book_id = db.create_book(Book(title="Test"))
        file = FileRecord(
            book_id=book_id,
            source_path="/data/test.pdf",
            sha256="abc123",
            file_size=1024,
        )
        file_id = db.create_file(file)
        assert file_id > 0

        fetched = db.get_file_by_sha256("abc123")
        assert fetched is not None
        assert fetched.source_path == "/data/test.pdf"

    def test_unique_sha256(self, db: CatalogueDB) -> None:
        book_id = db.create_book(Book(title="Test"))
        f1 = FileRecord(book_id=book_id, source_path="/a.pdf", sha256="dup")
        f2 = FileRecord(book_id=book_id, source_path="/b.pdf", sha256="dup")
        db.create_file(f1)
        with pytest.raises(Exception):
            db.create_file(f2)


class TestProcessing:
    def test_create_and_update_processing(self, db: CatalogueDB) -> None:
        book_id = db.create_book(Book(title="Test"))
        file_id = db.create_file(FileRecord(
            book_id=book_id, source_path="/test.pdf", sha256="h1",
        ))
        proc = ProcessingRecord(file_id=file_id, state=ProcessingState.PENDING)
        proc_id = db.create_processing(proc)
        assert proc_id > 0

        db.update_processing_state(proc_id, ProcessingState.DONE)
        pending = db.get_processing_by_state(ProcessingState.PENDING)
        assert len(pending) == 0


class TestUsers:
    def test_create_and_get_user(self, db: CatalogueDB) -> None:
        user = User(username="testuser", display_name="Test User")
        user_id = db.create_user(user)
        assert user_id > 0

        fetched = db.get_user(user_id)
        assert fetched is not None
        assert fetched.username == "testuser"

    def test_get_user_by_username(self, db: CatalogueDB) -> None:
        db.create_user(User(username="admin"))
        fetched = db.get_user_by_username("admin")
        assert fetched is not None

    def test_unique_username(self, db: CatalogueDB) -> None:
        db.create_user(User(username="admin"))
        with pytest.raises(Exception):
            db.create_user(User(username="admin"))


class TestReadingLists:
    def test_create_and_add_book(self, db: CatalogueDB) -> None:
        user_id = db.create_user(User(username="reader"))
        book_id = db.create_book(Book(title="শেষের কবিতা"))

        rl_id = db.create_reading_list(ReadingList(user_id=user_id, name="Favorites"))
        db.add_book_to_reading_list(rl_id, book_id)

        books = db.get_reading_list_books(rl_id)
        assert len(books) == 1
        assert books[0].title == "শেষের কবিতা"


class TestBookmarks:
    def test_create_bookmark(self, db: CatalogueDB) -> None:
        user_id = db.create_user(User(username="reader"))
        book_id = db.create_book(Book(title="Test"))
        bm = Bookmark(user_id=user_id, book_id=book_id, position="page:10", note="Chapter 3")
        bm_id = db.create_bookmark(bm)
        assert bm_id > 0

        bookmarks = db.get_user_bookmarks(user_id, book_id)
        assert len(bookmarks) == 1
        assert bookmarks[0].position == "page:10"


class TestReviews:
    def test_upsert_review(self, db: CatalogueDB) -> None:
        user_id = db.create_user(User(username="critic"))
        book_id = db.create_book(Book(title="Test"))

        rev = Review(user_id=user_id, book_id=book_id, rating=5, text="Excellent")
        rev_id = db.upsert_review(rev)
        assert rev_id > 0

        rev2 = Review(user_id=user_id, book_id=book_id, rating=4, text="Very good")
        rev_id2 = db.upsert_review(rev2)
        assert rev_id2 == rev_id

        reviews = db.get_book_reviews(book_id)
        assert len(reviews) == 1
        assert reviews[0].rating == 4


class TestReadingProgress:
    def test_upsert_and_get(self, db: CatalogueDB) -> None:
        user_id = db.create_user(User(username="reader"))
        book_id = db.create_book(Book(title="Test"))

        prog = ReadingProgress(user_id=user_id, book_id=book_id, position="chapter:5:120")
        db.upsert_reading_progress(prog)

        fetched = db.get_reading_progress(user_id, book_id)
        assert fetched is not None
        assert fetched.position == "chapter:5:120"

        prog2 = ReadingProgress(user_id=user_id, book_id=book_id, position="chapter:6:0")
        db.upsert_reading_progress(prog2)

        fetched2 = db.get_reading_progress(user_id, book_id)
        assert fetched2 is not None
        assert fetched2.position == "chapter:6:0"


class TestFavorites:
    def test_toggle_favorite(self, db: CatalogueDB) -> None:
        user_id = db.create_user(User(username="reader"))
        book_id = db.create_book(Book(title="Test"))

        added = db.toggle_favorite(user_id, book_id)
        assert added is True
        assert db.is_favorite(user_id, book_id) is True

        removed = db.toggle_favorite(user_id, book_id)
        assert removed is False
        assert db.is_favorite(user_id, book_id) is False

    def test_get_user_favorites(self, db: CatalogueDB) -> None:
        user_id = db.create_user(User(username="reader"))
        b1 = db.create_book(Book(title="Book 1"))
        b2 = db.create_book(Book(title="Book 2"))
        db.toggle_favorite(user_id, b1)
        db.toggle_favorite(user_id, b2)

        favs = db.get_user_favorites(user_id)
        assert len(favs) == 2
