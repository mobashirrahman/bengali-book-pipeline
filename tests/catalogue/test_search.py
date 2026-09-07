from __future__ import annotations

import tempfile
from pathlib import Path

from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.search import get_book_stats, rebuild_fts_index, search_books
from pdf_craft.catalogue.models import Book


class TestSearch:
    def _setup_db(self, tmp_path: Path) -> CatalogueDB:
        db = CatalogueDB(tmp_path / "test.db")
        db.create_book(Book(title="শেষের কবিতা", description="A collection of poems"))
        db.create_book(Book(title="গোরা", description="A novel by Tagore"))
        db.create_book(Book(title="নৌকাডুবি", description="A novel"))
        a1 = db.get_or_create_author("রবীন্দ্রনাথ ঠাকুর")
        a2 = db.get_or_create_author("বঙ্কিমচন্দ্র চট্টোপাধ্যায়")
        db.add_book_author(1, a1)
        db.add_book_author(2, a1)
        db.add_book_author(3, a2)
        rebuild_fts_index(db)
        return db

    def test_search_by_title(self, tmp_path: Path) -> None:
        db = self._setup_db(tmp_path)
        results = search_books(db, "শেষের কবিতা")
        assert len(results) >= 1
        assert results[0].title == "শেষের কবিতা"
        db.close()

    def test_search_returns_empty_for_no_match(self, tmp_path: Path) -> None:
        db = self._setup_db(tmp_path)
        results = search_books(db, "xyz123")
        assert len(results) == 0
        db.close()


class TestStats:
    def test_get_book_stats(self, tmp_path: Path) -> None:
        db = CatalogueDB(tmp_path / "test.db")
        db.create_book(Book(title="Book 1"))
        db.create_book(Book(title="Book 2"))
        db.get_or_create_author("Author 1")

        stats = get_book_stats(db)
        assert stats["total_books"] == 2
        assert stats["total_authors"] == 1
        db.close()
