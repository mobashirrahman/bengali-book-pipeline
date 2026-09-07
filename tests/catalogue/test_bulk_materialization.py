from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.importers.open_library import stage_open_library_dump
from pdf_craft.catalogue.importers.rokomari import stage_rokomari_from_file
from pdf_craft.catalogue.materialization import (
    materialize_source_records,
    parse_source_record,
)


def _dump(path: Path) -> None:
    rows = [
        {"key": "/books/OL1M", "type": "/type/edition", "data": {
            "title": "শেষের কবিতা", "authors": [{"name": "রবীন্দ্রনাথ ঠাকুর"}],
            "publishers": ["বিশ্বসাহিত্য ভবন"], "isbn_13": ["9789848309135"],
            "number_of_pages": 200, "publish_date": "1929",
        }},
        {"key": "/books/OL2M", "type": "/type/edition", "data": {
            "title": "শেষের কবিতা", "authors": [{"name": "রবীন্দ্রনাথ ঠাকুর"}],
            "isbn_13": ["not-an-isbn"],
        }},
        {"key": "/works/OL3W", "type": "/type/work", "data": {"title": "অন্য বই"}},
        {"invalid": True},
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_open_library_plain_gzip_replay_and_materialization(tmp_path: Path) -> None:
    plain = tmp_path / "dump.jsonl"
    _dump(plain)
    db = CatalogueDB(tmp_path / "catalogue.db")

    assert stage_open_library_dump(db, plain, batch_size=2) == (3, 1)
    assert stage_open_library_dump(db, plain, batch_size=2) == (3, 1)
    assert materialize_source_records(db) == {"seen": 3, "materialized": 3, "skipped": 0}
    assert materialize_source_records(db) == {"seen": 3, "materialized": 0, "skipped": 0}
    assert db.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_works").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_people").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_source_record_editions").fetchone()[0] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_metadata_assertions WHERE source_record_id=1").fetchone()[0] >= 4
    db.close()

    compressed = tmp_path / "dump.jsonl.gz"
    compressed.write_bytes(gzip.compress(plain.read_bytes()))
    db = CatalogueDB(tmp_path / "gzip.db")
    assert stage_open_library_dump(db, compressed) == (3, 1)
    db.close()


def test_open_library_tab_dump_preserves_envelope_and_isbn_provenance(tmp_path: Path) -> None:
    path = tmp_path / "editions.txt"
    payload = {"title": "Tab Book", "isbn_13": ["9780306406157"], "publishers": ["Pub"]}
    path.write_text(
        "/type/edition\t/books/OLTABM\t1\t2020-01-01T00:00:00.000000\t"
        + json.dumps(payload)
        + "\nmalformed\n",
        encoding="utf-8",
    )
    db = CatalogueDB(tmp_path / "catalogue.db")
    assert stage_open_library_dump(db, path) == (1, 1)
    stored = db.conn.execute("SELECT raw_json, external_id, record_type FROM catalogue_source_records").fetchone()
    assert json.loads(stored[0])["key"] == "/books/OLTABM"
    assert stored[1:] == ("/books/OLTABM", "edition")
    assert materialize_source_records(db) == {"seen": 1, "materialized": 1, "skipped": 0}
    assertion = db.conn.execute(
        "SELECT field_name, value_json, source_record_id FROM catalogue_metadata_assertions WHERE field_name='isbn'"
    ).fetchone()
    assert assertion[0] == "isbn"
    assert json.loads(assertion[1]) == "9780306406157"
    assert assertion[2] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_identifiers").fetchone()[0] == 1
    db.close()

def test_source_parsers_normalize_google_and_rokomari_shapes() -> None:
    google = parse_source_record("google_books", {"volumeInfo": {
        "title": "Title", "authors": ["Author"], "industryIdentifiers": [{"type": "ISBN_13", "identifier": "978-0-306-40615-7"}],
        "pageCount": 10, "publishedDate": "2000",
    }})
    assert google.authors == ("Author",)
    assert google.isbns == ("9780306406157",)
    rokomari = parse_source_record("rokomari", {"name": "Title", "specification": json.dumps({"Title": "Title", "Author": "Author", "ISBN": "978-0-306-40615-7"})})
    assert rokomari.authors == ("Author",)
    assert rokomari.isbns == ("9780306406157",)


def _realistic_rokomari_record(title: str = "শেষের কবিতা", identifier: str = "rk-42") -> dict[str, object]:
    return {
        "id": identifier,
        "url": f"https://www.rokomari.com/book/{identifier}",
        "name": title,
        "productType": "book",
        "description": "A preserved Rokomari description.",
        "authors": ["Noisy page-level author", "Another noisy value"],
        "publishers": ["Noisy publisher list"],
        "image": "https://images.rokomari.com/book-cover.jpg",
        "category": "বাংলা উপন্যাস",
        "specification": json.dumps({
            "Title": title,
            "Author": "রবীন্দ্রনাথ ঠাকুর",
            "Author/Editor": ["সম্পাদক নাম"],
            "Publisher": "বিশ্বসাহিত্য ভবন",
            "ISBN": "978-0-306-40615-7",
            "Edition": "1st",
            "Number of Pages": "240 pages",
            "Language": "বাংলা",
        }, ensure_ascii=False),
    }


def test_rokomari_parser_prefers_specific_authors_and_preserves_fidelity() -> None:
    item = parse_source_record("rokomari", _realistic_rokomari_record(), external_id="rk-42")

    assert item.authors == ("সম্পাদক নাম", "রবীন্দ্রনাথ ঠাকুর")
    assert item.publisher == "বিশ্বসাহিত্য ভবন"
    assert item.language == "বাংলা"
    assert item.page_count == 240
    assert item.isbns == ("9780306406157",)
    assert item.cover_urls == ("https://images.rokomari.com/book-cover.jpg",)
    assert item.source_url == "https://www.rokomari.com/book/rk-42"
    assert item.subjects == ("বাংলা উপন্যাস",)
    assert ("rokomari", "rk-42") in item.external_identifiers
    assert ("rokomari:id", "rk-42") in item.external_identifiers


def test_rokomari_materialization_is_provenant_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "rokomari.jsonl"
    path.write_text(json.dumps(_realistic_rokomari_record(), ensure_ascii=False) + "\n", encoding="utf-8")
    db = CatalogueDB(tmp_path / "catalogue.db")

    assert stage_rokomari_from_file(db, path, batch_size=1) == (1, 0)
    assert materialize_source_records(db, source="rokomari", batch_size=1) == {
        "seen": 1, "materialized": 1, "skipped": 0,
    }
    edition = db.conn.execute(
        """SELECT e.* FROM catalogue_editions e
           JOIN catalogue_source_record_editions sre ON sre.edition_id=e.id
           WHERE sre.source_record_id=1"""
    ).fetchone()
    assert edition["publisher"] == "বিশ্বসাহিত্য ভবন"
    assert edition["language"] == "বাংলা"
    assert edition["page_count"] == 240
    assert [tuple(row) for row in db.conn.execute(
        "SELECT name FROM catalogue_people ORDER BY id"
    ).fetchall()] == [("সম্পাদক নাম",), ("রবীন্দ্রনাথ ঠাকুর",)]
    assert [tuple(row) for row in db.conn.execute(
        "SELECT namespace, value FROM catalogue_identifiers ORDER BY namespace"
    ).fetchall()] == [
        ("isbn", "9780306406157"),
        ("rokomari", "rk-42"),
        ("rokomari:id", "rk-42"),
    ]
    cover = db.conn.execute(
        "SELECT source_url, source_record_id FROM catalogue_assets WHERE edition_id=?",
        (edition["id"],),
    ).fetchone()
    assert tuple(cover) == ("https://images.rokomari.com/book-cover.jpg", 1)
    assertions = {
        row[0]: json.loads(row[1])
        for row in db.conn.execute(
            "SELECT field_name, value_json FROM catalogue_metadata_assertions WHERE source_record_id=1"
        )
    }
    assert assertions["source_url"] == "https://www.rokomari.com/book/rk-42"
    assert assertions["subject"] == "বাংলা উপন্যাস"
    assert assertions["cover_url"] == "https://images.rokomari.com/book-cover.jpg"
    assert materialize_source_records(db, source="rokomari", batch_size=1) == {
        "seen": 1, "materialized": 0, "skipped": 0,
    }
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_assets").fetchone()[0] == 1
    db.close()


def test_materialization_resumes_after_a_committed_batch(tmp_path: Path) -> None:
    path = tmp_path / "rokomari.jsonl"
    rows = [_realistic_rokomari_record(f"Book {index}", f"rk-{index}") for index in range(3)]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    db = CatalogueDB(tmp_path / "catalogue.db")
    assert stage_rokomari_from_file(db, path, batch_size=1) == (3, 0)

    def fail_after_first_batch(cursor: int, _materialized: int) -> None:
        if cursor == 1:
            raise RuntimeError("simulated materialization interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        materialize_source_records(db, source="rokomari", batch_size=1, on_batch=fail_after_first_batch)
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_source_record_editions").fetchone()[0] == 1
    checkpoint = db.conn.execute(
        """SELECT c.cursor FROM catalogue_import_checkpoints c
           JOIN catalogue_import_runs r ON r.id=c.import_run_id
           WHERE r.source='catalogue_materialization'"""
    ).fetchone()
    assert checkpoint[0] == "1"
    assert materialize_source_records(db, source="rokomari", batch_size=1) == {
        "seen": 3, "materialized": 2, "skipped": 0,
    }
    assert materialize_source_records(db, source="rokomari", batch_size=1) == {
        "seen": 3, "materialized": 0, "skipped": 0,
    }
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_source_record_editions").fetchone()[0] == 3
    db.close()


def test_rokomari_resume_after_committed_batch_failure(tmp_path: Path) -> None:
    path = tmp_path / "rokomari.jsonl"
    rows = [{
        "id": str(i), "productType": "book", "name": f"Book {i}",
        "specification": {"Title": f"Book {i}"},
    } for i in range(3)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    db = CatalogueDB(tmp_path / "catalogue.db")

    def fail_once(_cursor: int, _staged: int) -> None:
        raise RuntimeError("simulated worker failure")

    try:
        stage_rokomari_from_file(db, path, batch_size=1, on_batch=fail_once)
    except RuntimeError:
        pass
    else:
        raise AssertionError("failure callback did not fire")
    assert stage_rokomari_from_file(db, path, batch_size=1) == (3, 0)
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_source_records").fetchone()[0] == 3
    db.close()
