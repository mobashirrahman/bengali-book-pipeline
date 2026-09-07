from __future__ import annotations

import json
from pathlib import Path

from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.foundation import (
    CatalogueFoundation,
    file_sha256,
    ingest_local_documents,
)


def test_schema_migrates_existing_v1_database(tmp_path: Path) -> None:
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    version = db.conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 2
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='catalogue_works'"
    ).fetchone()
    db.close()


def test_local_ingestion_is_hash_based_and_idempotent(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    first = data / "author" / "book.pdf"
    first.parent.mkdir()
    first.write_bytes(b"pdf-content")
    duplicate = data / "other.pdf"
    duplicate.write_bytes(b"pdf-content")

    db = CatalogueDB(tmp_path / "db.sqlite3")
    indexed, skipped = ingest_local_documents(db, data, include_extensions={".pdf"})

    assert (indexed, skipped) == (2, 0)
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_local_documents").fetchone()[0] == 1
    assert file_sha256(first) == file_sha256(duplicate)

    indexed_again, skipped_again = ingest_local_documents(
        db, data, include_extensions={".pdf"}
    )
    assert (indexed_again, skipped_again) == (2, 0)
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_local_documents").fetchone()[0] == 1
    db.close()


def test_source_records_and_assertions_preserve_raw_payload(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite3")
    foundation = CatalogueFoundation(db)
    snapshot = foundation.record_source_snapshot(
        "test-source", "snapshot-1", {"records": 1}
    )
    payload = {"title": "শেষের কবিতা", "authors": ["রবীন্দ্রনাথ ঠাকুর"]}
    source_id = foundation.record_source_record(
        snapshot, external_id="book-1", payload=payload, title=payload["title"]
    )
    assertion_id = foundation.add_assertion(
        entity_type="edition", entity_id=1, field_name="title",
        value=payload["title"], source_record_id=source_id, confidence=0.95,
    )

    stored = db.conn.execute(
        "SELECT raw_json FROM catalogue_source_records WHERE id=?", (source_id,)
    ).fetchone()[0]
    assert json.loads(stored) == payload
    assert assertion_id > 0
    db.close()
