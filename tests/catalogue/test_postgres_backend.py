from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdf_craft.catalogue.api.app import init_app
from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.postgres import (
    _MIGRATIONS,
    POSTGRES_SCHEMA_VERSION,
    PostgresCatalogueDB,
    _backfill_local_file_catalogue,
    initialize_postgres,
    postgres_status,
    resolve_postgres_dsn,
)
from pdf_craft.catalogue.transfer import transfer_sqlite_to_postgres


def test_postgres_dsn_resolution_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATALOGUE_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError, match="SQLite fallback is disabled"):
        resolve_postgres_dsn()
    raw_dsn = "postgresql://example/catalogue?application_name=catalogue%20test&sslmode=disable"
    assert resolve_postgres_dsn(raw_dsn) == raw_dsn
    monkeypatch.setenv("CATALOGUE_POSTGRES_DSN", "postgresql://env/catalogue")
    assert resolve_postgres_dsn() == "postgresql://env/catalogue"


def test_postgres_migrations_are_separate_and_ordered() -> None:
    assert [migration.version for migration in _MIGRATIONS] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert _MIGRATIONS[-1].version == POSTGRES_SCHEMA_VERSION
    assert "idx_catalogue_works_sort_title" in _MIGRATIONS[4].sql
    assert "idx_catalogue_source_records_source_id" in _MIGRATIONS[4].sql
    assert "idx_catalogue_editions_title" in _MIGRATIONS[5].sql
    assert "idx_catalogue_edition_people_person_role" in _MIGRATIONS[5].sql
    assert "catalogue_document_locations" in _MIGRATIONS[6].sql
    assert "catalogue_local_inventory" in _MIGRATIONS[6].sql
    assert "INSERT INTO catalogue_document_locations" in _MIGRATIONS[7].sql
    assert "INSERT INTO catalogue_local_inventory" in _MIGRATIONS[7].sql
    assert "ORDER BY id DESC" in _MIGRATIONS[7].sql
    assert "catalogue_external_ratings" in _MIGRATIONS[-1].sql
    assert "catalogue_user_ratings" in _MIGRATIONS[-1].sql
    assert "sqlite_master" not in "".join(migration.sql for migration in _MIGRATIONS)
    assert "catalogue_schema_migrations" not in _MIGRATIONS[0].sql


PG_DSN = os.environ.get("CATALOGUE_POSTGRES_DSN")
skip_without_dsn = pytest.mark.skipif(
    not PG_DSN, reason="CATALOGUE_POSTGRES_DSN is not configured"
)


@pytest.mark.integration
@skip_without_dsn
def test_postgres_fresh_and_repeat_migrations_and_connection_settings() -> None:
    first = initialize_postgres(PG_DSN)
    second = initialize_postgres(PG_DSN)
    assert [row.version for row in first] == list(range(1, POSTGRES_SCHEMA_VERSION + 1))
    assert [row.version for row in second] == list(range(1, POSTGRES_SCHEMA_VERSION + 1))
    status = postgres_status(PG_DSN)
    assert status["current_version"] == POSTGRES_SCHEMA_VERSION
    assert len(status["migrations"]) == POSTGRES_SCHEMA_VERSION


@pytest.mark.integration
@skip_without_dsn
def test_postgres_raw_local_dsn_forces_utf8_text() -> None:
    db = PostgresCatalogueDB.connect(PG_DSN)
    try:
        encoding = db.conn.execute("SHOW client_encoding").fetchone()["client_encoding"]
        value = db.conn.execute("SELECT %s::text AS value", ("শেষের কবিতা",)).fetchone()["value"]
        assert encoding == "UTF8"
        assert isinstance(value, str)
        assert isinstance(postgres_status(PG_DSN)["migrations"][0]["name"], str)
    finally:
        db.close()
    db = None


@pytest.mark.integration
@skip_without_dsn
def test_postgres_initialize_backfills_newest_legacy_local_document_path() -> None:
    old_id, new_id = 930000001, 930000002
    source_path = "/tmp/catalogue-legacy-changed.pdf"
    db = PostgresCatalogueDB.connect(PG_DSN)
    try:
        db.conn.execute(
            "DELETE FROM catalogue_local_inventory WHERE source_path=%s",
            (source_path,),
        )
        db.conn.execute(
            "DELETE FROM catalogue_document_locations WHERE source_path=%s",
            (source_path,),
        )
        db.conn.execute(
            "DELETE FROM catalogue_local_documents WHERE id=%s OR id=%s",
            (old_id, new_id),
        )
        db.conn.execute(
            "INSERT INTO catalogue_local_documents "
            "(id, sha256, source_path, file_size) VALUES (%s, %s, %s, %s)",
            (old_id, "postgres-old-sha", source_path, 7),
        )
        db.conn.execute(
            "INSERT INTO catalogue_local_documents "
            "(id, sha256, source_path, file_size) VALUES (%s, %s, %s, %s)",
            (new_id, "postgres-new-sha", source_path, 11),
        )
        db.commit()
    finally:
        db.close()

    try:
        db = PostgresCatalogueDB.connect(PG_DSN)
        try:
            _backfill_local_file_catalogue(db.conn)
        finally:
            db.close()
        db = PostgresCatalogueDB.connect(PG_DSN)
        location = db.conn.execute(
            "SELECT document_id, file_size FROM catalogue_document_locations "
            "WHERE source_path=%s",
            (source_path,),
        ).fetchone()
        inventory = db.conn.execute(
            "SELECT document_id, sha256, file_size FROM catalogue_local_inventory "
            "WHERE source_path=%s",
            (source_path,),
        ).fetchone()
        assert (location["document_id"], location["file_size"]) == (new_id, 11)
        assert (
            inventory["document_id"],
            inventory["sha256"],
            inventory["file_size"],
        ) == (new_id, "postgres-new-sha", 11)
    finally:
        cleanup = PostgresCatalogueDB.connect(PG_DSN)
        try:
            cleanup.conn.execute(
                "DELETE FROM catalogue_local_inventory WHERE source_path=%s",
                (source_path,),
            )
            cleanup.conn.execute(
                "DELETE FROM catalogue_document_locations WHERE source_path=%s",
                (source_path,),
            )
            cleanup.conn.execute(
                "DELETE FROM catalogue_local_documents WHERE id=%s OR id=%s",
                (old_id, new_id),
            )
            cleanup.commit()
        finally:
            cleanup.close()
        if db is not None:
            db.close()


@pytest.mark.integration
@skip_without_dsn
def test_postgres_api_reads_and_v1_failure(tmp_path: Path) -> None:
    initialize_postgres(PG_DSN)
    fixture_id = 910000001
    db = PostgresCatalogueDB.connect(PG_DSN)
    try:
        db.conn.execute("DELETE FROM catalogue_works WHERE id=%s", (fixture_id,))
        db.conn.execute("INSERT INTO catalogue_works(id, title) VALUES(%s, %s)", (fixture_id, "Postgres API fixture"))
        db.commit()
    finally:
        db.close()
    client = TestClient(init_app(postgres_dsn=PG_DSN))
    assert client.get("/v2/health").json()["backend"] == "postgres"
    assert client.get("/v2/stats").json()["works"] >= 1
    assert client.get("/v2/search", params={"q": "Postgres API fixture"}).json()["items"]
    assert client.get(f"/v2/works/{fixture_id}").status_code == 200
    assert client.get("/v1/stats").status_code == 503
    cleanup = PostgresCatalogueDB.connect(PG_DSN)
    try:
        cleanup.conn.execute("DELETE FROM catalogue_works WHERE id=%s", (fixture_id,))
        cleanup.commit()
    finally:
        cleanup.close()


@pytest.mark.integration
@skip_without_dsn
def test_postgres_transfer_preserves_rows_and_is_repeat_safe(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "source.db"
    source = CatalogueDB(sqlite_path)
    work_id, person_id, edition_id, document_id = 920000001, 920000002, 920000003, 920000004
    import_run_id, import_checkpoint_id, identifier_id = 920000005, 920000006, 920000009
    source.conn.execute("INSERT INTO catalogue_works(id, title) VALUES (?, ?)", (work_id, "Transfer fixture"))
    source.conn.execute("INSERT INTO catalogue_people(id, name, normalized_name) VALUES (?, ?, ?)", (person_id, "Transfer Author", "transfer author"))
    source.conn.execute("INSERT INTO catalogue_import_runs(id, source, parser_version, request_json, status) VALUES (?, ?, ?, ?, ?)", (import_run_id, "transfer-test", "test-v1", '{"fixture":true}', "completed"))
    source.conn.execute("INSERT INTO catalogue_import_checkpoints(id, import_run_id, checkpoint_key, cursor, state_json) VALUES (?, ?, ?, ?, ?)", (import_checkpoint_id, import_run_id, "records", "cursor-1", '{"offset":1}'))
    source.conn.execute("INSERT INTO catalogue_editions(id, work_id, title) VALUES (?, ?, ?)", (edition_id, work_id, "Transfer Edition"))
    source.conn.execute("INSERT INTO catalogue_edition_people VALUES (?, ?, 'author', 0)", (edition_id, person_id))
    source.conn.execute("INSERT INTO catalogue_identifiers(id, entity_type, entity_id, namespace, value, normalized_value) VALUES (?, 'edition', ?, 'isbn', '9780000000001', '9780000000001')", (identifier_id, edition_id))
    source.conn.execute("INSERT INTO catalogue_local_documents(id, sha256, source_path, file_size) VALUES (?, ?, ?, ?)", (document_id, "transfer-sha", "transfer.pdf", 12))
    source.conn.execute("INSERT INTO catalogue_document_locations(id, document_id, source_path, file_size, media_type) VALUES (?, ?, ?, ?, ?)", (920000007, document_id, "/tmp/transfer.pdf", 12, "application/pdf"))
    source.conn.execute("INSERT INTO catalogue_local_inventory(id, source_path, discovery_root, status, extension, file_size, sha256, document_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (920000008, "/tmp/transfer.pdf", "/tmp", "imported", ".pdf", 12, "transfer-sha", document_id))
    source.conn.commit()
    source.close()
    first = transfer_sqlite_to_postgres(sqlite_path, PG_DSN, batch_size=1)
    second = transfer_sqlite_to_postgres(sqlite_path, PG_DSN, batch_size=1)
    assert first["status"] == second["status"] == "completed"
    assert second["resumed"] is True
    db = PostgresCatalogueDB.connect(PG_DSN)
    try:
        row = db.conn.execute("SELECT title FROM catalogue_works WHERE id=%s", (work_id,)).fetchone()
        assert row["title"] == "Transfer fixture"
        assert db.conn.execute("SELECT document_id FROM catalogue_document_locations WHERE id=%s", (920000007,)).fetchone()["document_id"] == document_id
        assert db.conn.execute("SELECT status FROM catalogue_local_inventory WHERE id=%s", (920000008,)).fetchone()["status"] == "imported"
        assert db.conn.execute("SELECT COUNT(*) AS count FROM catalogue_edition_people WHERE edition_id=%s", (edition_id,)).fetchone()["count"] == 1
        checkpoint_data = db.conn.execute("SELECT checkpoint_key, cursor, state_json FROM catalogue_import_checkpoints WHERE id=%s", (import_checkpoint_id,)).fetchone()
        assert checkpoint_data == {"checkpoint_key": "records", "cursor": "cursor-1", "state_json": '{"offset":1}'}
        checkpoint = db.conn.execute("SELECT completed FROM catalogue_transfer_checkpoints WHERE transfer_run_id=%s AND table_name='catalogue_works'", (first["run_id"],)).fetchone()
        assert checkpoint["completed"] is True
    finally:
        db.close()
        cleanup = PostgresCatalogueDB.connect(PG_DSN)
        try:
            cleanup.conn.execute("DELETE FROM catalogue_document_matches WHERE document_id=%s", (document_id,))
            cleanup.conn.execute("DELETE FROM catalogue_assets WHERE document_id=%s", (document_id,))
            cleanup.conn.execute("DELETE FROM catalogue_artifacts WHERE document_id=%s", (document_id,))
            cleanup.conn.execute("DELETE FROM catalogue_identifiers WHERE id=%s", (identifier_id,))
            cleanup.conn.execute("DELETE FROM catalogue_edition_people WHERE edition_id=%s OR person_id=%s", (edition_id, person_id))
            cleanup.conn.execute("DELETE FROM catalogue_editions WHERE id=%s", (edition_id,))
            cleanup.conn.execute("DELETE FROM catalogue_people WHERE id=%s", (person_id,))
            cleanup.conn.execute("DELETE FROM catalogue_local_inventory WHERE document_id=%s", (document_id,))
            cleanup.conn.execute("DELETE FROM catalogue_document_locations WHERE document_id=%s", (document_id,))
            cleanup.conn.execute("DELETE FROM catalogue_local_documents WHERE id=%s", (document_id,))
            cleanup.conn.execute("DELETE FROM catalogue_import_checkpoints WHERE import_run_id=%s", (import_run_id,))
            cleanup.conn.execute("DELETE FROM catalogue_import_runs WHERE id=%s", (import_run_id,))
            cleanup.conn.execute("DELETE FROM catalogue_works WHERE id=%s", (work_id,))
            cleanup.commit()
        finally:
            cleanup.close()
