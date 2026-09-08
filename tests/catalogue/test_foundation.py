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
    assert version == 4
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='catalogue_works'"
    ).fetchone()
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='catalogue_document_locations'"
    ).fetchone()
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='catalogue_local_inventory'"
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
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_document_locations").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_local_inventory").fetchone()[0] == 2
    assert file_sha256(first) == file_sha256(duplicate)

    indexed_again, skipped_again = ingest_local_documents(
        db, data, include_extensions={".pdf"}
    )
    assert (indexed_again, skipped_again) == (2, 0)
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_local_documents").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_document_locations").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_local_inventory").fetchone()[0] == 2
    db.close()


def test_local_ingestion_repoints_changed_path_and_keeps_old_document(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    changed = data / "book.pdf"
    changed.write_bytes(b"before")

    db = CatalogueDB(tmp_path / "db.sqlite3")
    ingest_local_documents(db, data)
    old_id = db.conn.execute(
        "SELECT id FROM catalogue_local_documents WHERE sha256=?",
        (file_sha256(changed),),
    ).fetchone()[0]

    changed.write_bytes(b"after")
    ingest_local_documents(db, data)

    rows = db.conn.execute(
        "SELECT id, sha256 FROM catalogue_local_documents ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert old_id in {row[0] for row in rows}
    location = db.conn.execute(
        "SELECT document_id, file_size FROM catalogue_document_locations"
    ).fetchone()
    assert location[0] != old_id
    assert location[1] == len(b"after")
    assert db.conn.execute(
        "SELECT document_id, status FROM catalogue_local_inventory"
    ).fetchone()[1] == "imported"
    changed.unlink()
    ingest_local_documents(db, data)
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_local_documents").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_document_locations").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_local_inventory").fetchone()[0] == 1
    db.close()


def test_local_inventory_records_unsupported_and_unreadable_files(
    tmp_path: Path, monkeypatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.txt").write_text("notes", encoding="utf-8")
    unreadable = data / "broken.pdf"
    unreadable.write_bytes(b"broken")

    def fail_hash(path: Path) -> str:
        if path == unreadable:
            raise OSError("cannot read fixture")
        return file_sha256(path)

    monkeypatch.setattr("pdf_craft.catalogue.foundation.file_sha256", fail_hash)
    db = CatalogueDB(tmp_path / "db.sqlite3")
    indexed, skipped = ingest_local_documents(db, data)

    assert (indexed, skipped) == (0, 1)
    statuses = dict(db.conn.execute(
        "SELECT status, COUNT(*) FROM catalogue_local_inventory GROUP BY status"
    ).fetchall())
    assert statuses == {"unreadable": 1, "unsupported": 1}
    error = db.conn.execute(
        "SELECT error FROM catalogue_local_inventory WHERE source_path LIKE '%broken.pdf'"
    ).fetchone()[0]
    assert error == "cannot read fixture"
    db.close()


def test_local_document_path_backfill_prefers_newest_same_path_row(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "db.sqlite3"
    db = CatalogueDB(db_path)
    db.conn.execute(
        "INSERT INTO catalogue_local_documents "
        "(id, sha256, source_path, file_size) VALUES (?, ?, ?, ?)",
        (4101, "legacy-old-sha", "legacy/book.pdf", 7),
    )
    db.conn.execute(
        "INSERT INTO catalogue_local_documents "
        "(id, sha256, source_path, file_size) VALUES (?, ?, ?, ?)",
        (4102, "legacy-new-sha", "legacy/book.pdf", 11),
    )
    source_path = str(Path("legacy/book.pdf").absolute())
    db.conn.execute(
        """INSERT INTO catalogue_document_locations
           (document_id, source_path, file_size, media_type)
           VALUES (?, ?, ?, ?)""",
        (4101, source_path, 7, "application/pdf"),
    )
    db.conn.execute(
        """INSERT INTO catalogue_local_inventory
           (source_path, discovery_root, status, extension, media_type,
            file_size, sha256, document_id, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_path, str(Path(source_path).parent), "imported", ".pdf",
            "application/pdf", 7, "legacy-old-sha", 4101, "stale row",
        ),
    )
    db.conn.commit()
    db.close()

    db = CatalogueDB(db_path)
    location = db.conn.execute(
        "SELECT document_id, file_size, media_type FROM catalogue_document_locations"
    ).fetchone()
    inventory = db.conn.execute(
        "SELECT document_id, sha256, file_size, status, error "
        "FROM catalogue_local_inventory"
    ).fetchone()
    assert tuple(location) == (4102, 11, "application/pdf")
    assert tuple(inventory) == (4102, "legacy-new-sha", 11, "imported", None)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_document_locations"
    ).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_local_inventory"
    ).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_local_documents"
    ).fetchone()[0] == 2
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
