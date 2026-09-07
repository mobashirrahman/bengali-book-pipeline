from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.importers.rokomari import (
    import_rokomari_from_file,
    stage_rokomari_from_file,
)
from pdf_craft.catalogue.models import Book


def _make_rokomari_record(
    title: str,
    author: str,
    isbn: str = "",
    publisher: str = "",
    category: str = "Fiction",
) -> dict:
    return {
        "url": f"https://www.rokomari.com/book/{title}",
        "name": title,
        "productType": "book",
        "description": f"A book called {title}",
        "authors": [author],
        "publishers": [publisher] if publisher else [],
        "image": "",
        "specification": json.dumps({
            "Title": title,
            "Author": author,
            "ISBN": isbn,
            "Publisher": publisher,
            "Edition": "1st",
            "Number of Pages": "200",
            "Language": "বাংলা",
        }),
        "price": "200",
        "regularPrice": "250",
        "discount": "20%",
        "brand": publisher,
        "category": category,
        "priceCurrency": "BDT",
        "offerPrice": "200",
        "availability": "InStock",
        "ratingValue": "4.5",
        "ratingCount": "100",
        "reviewCount": "50",
    }


class TestRokomariImport:
    def test_import_from_jsonl(self, tmp_path: Path) -> None:
        records = [
            _make_rokomari_record("শেষের কবিতা", "রবীন্দ্রনাথ ঠাকুর", "9789848309135", "বিশ্বসাহিত্য ভবন"),
            _make_rokomari_record("গোরা", "রবীন্দ্রনাথ ঠাকুর", "9789848309136"),
            _make_rokomari_record("নৌকাডুবি", "রবীন্দ্রনাথ ঠাকুর"),
        ]
        jsonl_path = tmp_path / "rokomari.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        db_path = tmp_path / "test.db"
        db = CatalogueDB(db_path)
        count = import_rokomari_from_file(db, jsonl_path)

        assert count == 3
        books = db.list_books(limit=10)
        assert len(books) == 3
        titles = {b.title for b in books}
        assert "শেষের কবিতা" in titles

        db.close()

    def test_skips_non_book_products(self, tmp_path: Path) -> None:
        records = [
            _make_rokomari_record("শেষের কবিতা", "রবীন্দ্রনাথ ঠাকুর"),
            {
                "url": "https://www.rokomari.com/book/authors",
                "name": "Author Page",
                "productType": "author_page",
                "specification": None,
            },
        ]
        jsonl_path = tmp_path / "rokomari.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        db_path = tmp_path / "test.db"
        db = CatalogueDB(db_path)
        count = import_rokomari_from_file(db, jsonl_path)

        assert count == 1
        db.close()

    def test_skips_duplicates(self, tmp_path: Path) -> None:
        records = [
            _make_rokomari_record("শেষের কবিতা", "রবীন্দ্রনাথ ঠাকুর"),
            _make_rokomari_record("শেষের কবিতা", "রবীন্দ্রনাথ ঠাকুর"),
        ]
        jsonl_path = tmp_path / "rokomari.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        db_path = tmp_path / "test.db"
        db = CatalogueDB(db_path)
        count = import_rokomari_from_file(db, jsonl_path)

        assert count == 1
        books = db.list_books(limit=10)
        assert len(books) == 1
        db.close()

    def test_extracts_isbn(self, tmp_path: Path) -> None:
        records = [
            _make_rokomari_record("কপালকুণ্ডলা", "বঙ্কিমচন্দ্র চট্টোপাধ্যায়", "978-81-295-0000-0"),
        ]
        jsonl_path = tmp_path / "rokomari.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        db_path = tmp_path / "test.db"
        db = CatalogueDB(db_path)
        import_rokomari_from_file(db, jsonl_path)

        books = db.list_books(limit=1)
        assert len(books) == 1
        assert books[0].isbn == "9788129500000"
        db.close()

    def test_creates_authors_and_subjects(self, tmp_path: Path) -> None:
        records = [
            _make_rokomari_record("শেষের কবিতা", "রবীন্দ্রনাথ ঠাকুর", category="Poetry"),
        ]
        jsonl_path = tmp_path / "rokomari.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        db_path = tmp_path / "test.db"
        db = CatalogueDB(db_path)
        import_rokomari_from_file(db, jsonl_path)

        books = db.list_books(limit=1)
        book = books[0]
        authors = db.get_book_authors(book.id)  # type: ignore[arg-type]
        subjects = db.get_book_subjects(book.id)  # type: ignore[arg-type]
        assert len(authors) == 1
        assert authors[0].name == "রবীন্দ্রনাথ ঠাকুর"
        assert len(subjects) == 1
        assert subjects[0].name == "Poetry"
        db.close()

    def test_stages_raw_records_without_creating_v1_books(self, tmp_path: Path) -> None:
        record = _make_rokomari_record("শেষের কবিতা", "রবীন্দ্রনাথ ঠাকুর")
        jsonl_path = tmp_path / "rokomari.jsonl"
        jsonl_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        db = CatalogueDB(tmp_path / "test.db")
        staged, skipped = stage_rokomari_from_file(db, jsonl_path)

        assert (staged, skipped) == (1, 0)
        assert db.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM catalogue_source_records"
        ).fetchone()[0] == 1
        raw = db.conn.execute(
            "SELECT raw_json FROM catalogue_source_records"
        ).fetchone()[0]
        assert json.loads(raw)["name"] == record["name"]
        db.close()
