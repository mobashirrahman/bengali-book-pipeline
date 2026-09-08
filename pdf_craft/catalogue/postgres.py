"""PostgreSQL storage for the normalized catalogue.

This module intentionally owns PostgreSQL migrations separately from the
SQLite compatibility and repair code.  The public surface is small: open a
request-scoped connection, initialize the schema, and inspect migration
status.  Import and materialization code can build on the same connection
contract later.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised by environments without the extra
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


POSTGRES_SCHEMA_VERSION = 8
POSTGRES_STATEMENT_TIMEOUT_MS = 5_000
POSTGRES_ADVISORY_LOCK_KEY = 7_861_041_223
POSTGRES_CLIENT_ENCODING = "UTF8"


class PostgresUnavailableError(RuntimeError):
    """Raised when PostgreSQL support was requested without psycopg installed."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    version: int
    name: str
    checksum: str
    applied_at: str


_LOCAL_FILE_BACKFILL_SQL = """
WITH current_documents AS (
    SELECT id, sha256, source_path, file_size, media_type, discovered_at, updated_at,
           ROW_NUMBER() OVER (PARTITION BY source_path ORDER BY id DESC) AS path_rank
    FROM catalogue_local_documents
)
INSERT INTO catalogue_document_locations
    (document_id, source_path, file_size, media_type, discovered_at, last_seen_at, updated_at)
SELECT id, source_path, file_size, media_type,
       COALESCE(discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(updated_at, CURRENT_TIMESTAMP::text)
FROM current_documents
WHERE path_rank = 1
ON CONFLICT (source_path) DO UPDATE SET
    document_id = EXCLUDED.document_id,
    file_size = EXCLUDED.file_size,
    media_type = EXCLUDED.media_type,
    last_seen_at = EXCLUDED.last_seen_at,
    updated_at = EXCLUDED.updated_at;

WITH current_documents AS (
    SELECT id, sha256, source_path, file_size, media_type, discovered_at, updated_at,
           ROW_NUMBER() OVER (PARTITION BY source_path ORDER BY id DESC) AS path_rank
    FROM catalogue_local_documents
)
INSERT INTO catalogue_local_inventory
    (source_path, discovery_root, status, extension, media_type, file_size,
     sha256, document_id, discovered_at, last_seen_at, updated_at)
SELECT source_path,
       CASE
           WHEN POSITION('/' IN source_path) = 0 THEN '.'
           WHEN REGEXP_REPLACE(source_path, '/[^/]*$', '') = '' THEN '/'
           ELSE REGEXP_REPLACE(source_path, '/[^/]*$', '')
       END,
       'imported',
       COALESCE(NULLIF(LOWER(SUBSTRING(source_path FROM '(\\.[^./]+)$')), ''), ''),
       media_type,
       file_size,
       sha256,
       id,
       COALESCE(discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(updated_at, CURRENT_TIMESTAMP::text)
FROM current_documents
WHERE path_rank = 1
ON CONFLICT (source_path) DO UPDATE SET
    discovery_root = EXCLUDED.discovery_root,
    status = EXCLUDED.status,
    extension = EXCLUDED.extension,
    media_type = EXCLUDED.media_type,
    file_size = EXCLUDED.file_size,
    sha256 = EXCLUDED.sha256,
    document_id = EXCLUDED.document_id,
    error = NULL,
    last_seen_at = EXCLUDED.last_seen_at,
    updated_at = EXCLUDED.updated_at;
"""


_LOCAL_FILE_RUNTIME_BACKFILL_SQL = """
WITH current_documents AS (
    SELECT id, sha256, source_path, file_size, media_type, discovered_at, updated_at,
           ROW_NUMBER() OVER (PARTITION BY source_path ORDER BY id DESC) AS path_rank
    FROM catalogue_local_documents
)
INSERT INTO catalogue_document_locations
    (document_id, source_path, file_size, media_type, discovered_at, last_seen_at, updated_at)
SELECT doc.id, doc.source_path, doc.file_size, doc.media_type,
       COALESCE(doc.discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(doc.discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(doc.updated_at, CURRENT_TIMESTAMP::text)
FROM current_documents AS doc
WHERE doc.path_rank = 1
  AND NOT EXISTS (
      SELECT 1 FROM catalogue_document_locations existing
      WHERE existing.document_id = doc.id
  )
ON CONFLICT (source_path) DO UPDATE SET
    document_id = EXCLUDED.document_id,
    file_size = EXCLUDED.file_size,
    media_type = EXCLUDED.media_type,
    last_seen_at = EXCLUDED.last_seen_at,
    updated_at = EXCLUDED.updated_at;

WITH current_documents AS (
    SELECT id, sha256, source_path, file_size, media_type, discovered_at, updated_at,
           ROW_NUMBER() OVER (PARTITION BY source_path ORDER BY id DESC) AS path_rank
    FROM catalogue_local_documents
)
INSERT INTO catalogue_local_inventory
    (source_path, discovery_root, status, extension, media_type, file_size,
     sha256, document_id, discovered_at, last_seen_at, updated_at)
SELECT doc.source_path,
       CASE
           WHEN POSITION('/' IN doc.source_path) = 0 THEN '.'
           WHEN REGEXP_REPLACE(doc.source_path, '/[^/]*$', '') = '' THEN '/'
           ELSE REGEXP_REPLACE(doc.source_path, '/[^/]*$', '')
       END,
       'imported',
       COALESCE(NULLIF(LOWER(SUBSTRING(doc.source_path FROM '(\\.[^./]+)$')), ''), ''),
       doc.media_type,
       doc.file_size,
       doc.sha256,
       doc.id,
       COALESCE(doc.discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(doc.discovered_at, CURRENT_TIMESTAMP::text),
       COALESCE(doc.updated_at, CURRENT_TIMESTAMP::text)
FROM current_documents AS doc
WHERE doc.path_rank = 1
  AND NOT EXISTS (
      SELECT 1 FROM catalogue_local_inventory existing
      WHERE existing.document_id = doc.id
  )
ON CONFLICT (source_path) DO UPDATE SET
    discovery_root = EXCLUDED.discovery_root,
    status = EXCLUDED.status,
    extension = EXCLUDED.extension,
    media_type = EXCLUDED.media_type,
    file_size = EXCLUDED.file_size,
    sha256 = EXCLUDED.sha256,
    document_id = EXCLUDED.document_id,
    error = NULL,
    last_seen_at = EXCLUDED.last_seen_at,
    updated_at = EXCLUDED.updated_at;
"""


_MIGRATIONS = (
    Migration(
        1,
        "normalized_catalogue_core",
        """
        CREATE TABLE catalogue_works (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            title TEXT NOT NULL,
            subtitle TEXT,
            sort_title TEXT,
            language TEXT,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        );
        CREATE TABLE catalogue_people (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL,
            sort_name TEXT,
            normalized_name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE catalogue_editions (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            work_id BIGINT REFERENCES catalogue_works(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            subtitle TEXT,
            publisher TEXT,
            publication_date TEXT,
            edition_statement TEXT,
            language TEXT,
            description TEXT,
            page_count INTEGER,
            created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        );
        CREATE TABLE catalogue_edition_people (
            edition_id BIGINT NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
            person_id BIGINT NOT NULL REFERENCES catalogue_people(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (edition_id, person_id, role)
        );
        CREATE TABLE catalogue_identifiers (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('work', 'edition')),
            entity_id BIGINT NOT NULL,
            namespace TEXT NOT NULL,
            value TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            UNIQUE(namespace, normalized_value)
        );
        CREATE TABLE catalogue_source_snapshots (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            source TEXT NOT NULL,
            snapshot_key TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(source, snapshot_key, payload_sha256)
        );
        CREATE TABLE catalogue_source_records (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            snapshot_id BIGINT NOT NULL REFERENCES catalogue_source_snapshots(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            external_id TEXT,
            record_type TEXT NOT NULL DEFAULT 'edition',
            title TEXT,
            raw_json TEXT NOT NULL,
            UNIQUE(source, external_id, snapshot_id)
        );
        CREATE TABLE catalogue_metadata_assertions (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('work', 'edition', 'person')),
            entity_id BIGINT NOT NULL,
            field_name TEXT NOT NULL,
            value_json TEXT NOT NULL,
            source_record_id BIGINT REFERENCES catalogue_source_records(id) ON DELETE SET NULL,
            confidence DOUBLE PRECISION NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate', 'accepted', 'rejected')),
            observed_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            accepted_at TEXT
        );
        CREATE TABLE catalogue_local_documents (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            file_size BIGINT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'application/pdf',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            discovered_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        );
        CREATE TABLE catalogue_document_matches (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            document_id BIGINT NOT NULL REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
            edition_id BIGINT NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
            score DOUBLE PRECISION NOT NULL CHECK(score >= 0 AND score <= 1),
            method TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate', 'accepted', 'rejected')),
            evidence_json TEXT NOT NULL DEFAULT '{}',
            reviewed_at TEXT,
            UNIQUE(document_id, edition_id)
        );
        CREATE TABLE catalogue_assets (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            edition_id BIGINT REFERENCES catalogue_editions(id) ON DELETE CASCADE,
            document_id BIGINT REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
            asset_type TEXT NOT NULL,
            storage_uri TEXT NOT NULL,
            sha256 TEXT,
            mime_type TEXT,
            width INTEGER,
            height INTEGER,
            source_record_id BIGINT REFERENCES catalogue_source_records(id) ON DELETE SET NULL,
            attribution TEXT,
            rights TEXT,
            is_selected SMALLINT NOT NULL DEFAULT 0 CHECK(is_selected IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        );
        CREATE TABLE catalogue_artifacts (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            document_id BIGINT NOT NULL REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            storage_uri TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            profile TEXT,
            created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            UNIQUE(document_id, kind, sha256)
        );
        CREATE INDEX idx_catalogue_editions_work ON catalogue_editions(work_id);
        CREATE INDEX idx_catalogue_identifiers_lookup ON catalogue_identifiers(namespace, normalized_value);
        CREATE INDEX idx_catalogue_source_records_source ON catalogue_source_records(source, external_id);
        CREATE INDEX idx_catalogue_assertions_entity ON catalogue_metadata_assertions(entity_type, entity_id, field_name, status);
        CREATE INDEX idx_catalogue_documents_path ON catalogue_local_documents(source_path);
        CREATE INDEX idx_catalogue_matches_status ON catalogue_document_matches(status, score DESC);
        CREATE INDEX idx_catalogue_assets_edition ON catalogue_assets(edition_id, asset_type, is_selected);
        """,
    ),
    Migration(
        2,
        "import_provenance_and_checkpoints",
        """
        CREATE TABLE catalogue_import_runs (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            source TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            request_json TEXT NOT NULL DEFAULT '{}',
            checksum TEXT,
            status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running', 'completed', 'failed')),
            started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            completed_at TEXT,
            UNIQUE(source, checksum, parser_version)
        );
        CREATE TABLE catalogue_import_checkpoints (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            import_run_id BIGINT NOT NULL REFERENCES catalogue_import_runs(id) ON DELETE CASCADE,
            checkpoint_key TEXT NOT NULL,
            cursor TEXT,
            state_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            UNIQUE(import_run_id, checkpoint_key)
        );
        CREATE TABLE catalogue_source_record_editions (
            source_record_id BIGINT PRIMARY KEY REFERENCES catalogue_source_records(id) ON DELETE CASCADE,
            edition_id BIGINT NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE
        );
        CREATE TABLE catalogue_snapshot_provenance (
            snapshot_id BIGINT NOT NULL REFERENCES catalogue_source_snapshots(id) ON DELETE CASCADE,
            import_run_id BIGINT NOT NULL REFERENCES catalogue_import_runs(id) ON DELETE CASCADE,
            request_json TEXT NOT NULL DEFAULT '{}',
            parser_version TEXT,
            recorded_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            PRIMARY KEY(snapshot_id, import_run_id)
        );
        ALTER TABLE catalogue_source_snapshots ADD COLUMN import_run_id BIGINT REFERENCES catalogue_import_runs(id) ON DELETE SET NULL;
        ALTER TABLE catalogue_source_snapshots ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE catalogue_source_snapshots ADD COLUMN parser_version TEXT;
        ALTER TABLE catalogue_document_matches ADD COLUMN reviewer TEXT;
        ALTER TABLE catalogue_document_matches ADD COLUMN review_reason TEXT;
        CREATE UNIQUE INDEX idx_catalogue_one_accepted_match ON catalogue_document_matches(document_id) WHERE status = 'accepted';
        CREATE INDEX idx_catalogue_import_checkpoints_run ON catalogue_import_checkpoints(import_run_id, checkpoint_key);
        """,
    ),
    Migration(
        3,
        "asset_provenance_and_selection_history",
        """
        CREATE TABLE catalogue_asset_provenance (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            asset_id BIGINT NOT NULL REFERENCES catalogue_assets(id) ON DELETE CASCADE,
            source_record_id BIGINT REFERENCES catalogue_source_records(id) ON DELETE SET NULL,
            source_url TEXT,
            attribution TEXT,
            rights TEXT,
            observed_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(asset_id, source_record_id, source_url)
        );
        CREATE TABLE catalogue_asset_selection_history (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            asset_id BIGINT NOT NULL REFERENCES catalogue_assets(id) ON DELETE CASCADE,
            edition_id BIGINT NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
            asset_type TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            selected_by TEXT,
            selection_method TEXT NOT NULL
        );
        ALTER TABLE catalogue_assets ADD COLUMN source_url TEXT;
        ALTER TABLE catalogue_assets ADD COLUMN status TEXT NOT NULL DEFAULT 'candidate';
        ALTER TABLE catalogue_assets ADD COLUMN rank_score DOUBLE PRECISION;
        ALTER TABLE catalogue_assets ADD COLUMN selection_method TEXT;
        ALTER TABLE catalogue_assets ADD COLUMN selected_at TEXT;
        ALTER TABLE catalogue_assets ADD COLUMN selected_by TEXT;
        ALTER TABLE catalogue_assets ADD COLUMN retrieved_at TEXT;
        ALTER TABLE catalogue_assets ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
        CREATE INDEX idx_catalogue_asset_provenance_asset ON catalogue_asset_provenance(asset_id);
        CREATE INDEX idx_catalogue_asset_selection_history_edition ON catalogue_asset_selection_history(edition_id, asset_type, selected_at);
        CREATE INDEX idx_catalogue_assets_source_url ON catalogue_assets(edition_id, asset_type, source_url);
        """,
    ),
    Migration(
        4,
        "sqlite_transfer_ledger",
        """
        CREATE TABLE catalogue_transfer_runs (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running', 'completed', 'failed')),
            started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            completed_at TEXT,
            UNIQUE(source_path, source_fingerprint)
        );
        CREATE TABLE catalogue_transfer_checkpoints (
            transfer_run_id BIGINT NOT NULL REFERENCES catalogue_transfer_runs(id) ON DELETE CASCADE,
            table_name TEXT NOT NULL,
            source_offset BIGINT NOT NULL DEFAULT 0,
            rows_copied BIGINT NOT NULL DEFAULT 0,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            PRIMARY KEY(transfer_run_id, table_name)
        );
        """,
    ),
    Migration(
        5,
        "work_sort_title_lookup",
        """
        CREATE INDEX idx_catalogue_works_sort_title
            ON catalogue_works(sort_title);
        CREATE INDEX idx_catalogue_source_records_source_id
            ON catalogue_source_records(source, id);
        """,
    ),
    Migration(
        6,
        "matcher_lookup_indexes",
        """
        CREATE INDEX idx_catalogue_editions_title
            ON catalogue_editions(title);
        CREATE INDEX idx_catalogue_edition_people_person_role
            ON catalogue_edition_people(person_id, role, edition_id);
        """,
    ),
    Migration(
        7,
        "local_file_inventory_and_locations",
        """
        CREATE TABLE catalogue_document_locations (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            document_id BIGINT NOT NULL REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
            source_path TEXT NOT NULL UNIQUE,
            file_size BIGINT NOT NULL,
            media_type TEXT NOT NULL,
            discovered_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            last_seen_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        );
        CREATE TABLE catalogue_local_inventory (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            discovery_root TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('imported', 'unsupported', 'unreadable')),
            extension TEXT NOT NULL,
            media_type TEXT,
            file_size BIGINT,
            mtime_ns BIGINT,
            sha256 TEXT,
            document_id BIGINT REFERENCES catalogue_local_documents(id) ON DELETE SET NULL,
            error TEXT,
            discovered_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            last_seen_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        );
        CREATE INDEX idx_catalogue_document_locations_document
            ON catalogue_document_locations(document_id, source_path);
        CREATE INDEX idx_catalogue_local_inventory_status
            ON catalogue_local_inventory(status, source_path);
        CREATE INDEX idx_catalogue_local_inventory_document
            ON catalogue_local_inventory(document_id);
        """,
    ),
    Migration(
        8,
        "backfill_local_file_inventory",
        _LOCAL_FILE_BACKFILL_SQL,
    ),
)


def resolve_postgres_dsn(dsn: str | None = None) -> str:
    """Resolve an explicit DSN or the configured environment DSN."""
    value = dsn or os.environ.get("CATALOGUE_POSTGRES_DSN")
    if not value:
        raise ValueError(
            "PostgreSQL requires --dsn or CATALOGUE_POSTGRES_DSN; SQLite fallback is disabled"
        )
    return value


def _require_psycopg() -> Any:
    if psycopg is None:
        raise PostgresUnavailableError(
            "PostgreSQL support requires the catalogue extra: pip install 'pdf-craft[catalogue]'"
        )
    return psycopg


def _normalize_postgres_value(value: Any) -> Any:
    """Decode SQL_ASCII text results while retaining structured values."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [_normalize_postgres_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_postgres_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _normalize_postgres_value(item) for key, item in value.items()}
    return value


def _normalize_postgres_row(row: Any) -> Any:
    return _normalize_postgres_value(row)


class PostgresCatalogueDB:
    """A deliberately thin psycopg wrapper with explicit lifecycle methods."""

    backend = "postgres"

    def __init__(self, conn: Any, dsn: str):
        self.conn = conn
        self.dsn = dsn

    @classmethod
    def connect(
        cls,
        dsn: str | None = None,
        *,
        statement_timeout_ms: int = POSTGRES_STATEMENT_TIMEOUT_MS,
        connect_timeout: int = 10,
    ) -> PostgresCatalogueDB:
        driver = _require_psycopg()
        resolved = resolve_postgres_dsn(dsn)
        conn = driver.connect(
            resolved,
            row_factory=dict_row,
            connect_timeout=connect_timeout,
        )
        try:
            # SQL_ASCII databases otherwise make psycopg expose TEXT columns
            # as bytes. Keep DSN options untouched and force the session
            # encoding after libpq has parsed the original DSN.
            conn.execute("SET client_encoding TO 'UTF8'")
            conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{statement_timeout_ms}ms",))
            conn.execute("SET TIME ZONE 'UTC'")
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        return cls(conn, resolved)

    def close(self) -> None:
        """Rollback any uncommitted work before closing the connection."""
        try:
            self.conn.rollback()
        finally:
            self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def __enter__(self) -> PostgresCatalogueDB:  # noqa: PYI034
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.conn.rollback()
        self.close()


def _migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _ensure_ledger(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS catalogue_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
        )"""
    )
    conn.commit()


def _migration_rows(conn: Any) -> list[MigrationStatus]:
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM catalogue_schema_migrations ORDER BY version"
    ).fetchall()
    return [MigrationStatus(**_normalize_postgres_row(row)) for row in rows]


def _backfill_local_file_catalogue(conn: Any) -> None:
    """Keep legacy local document rows visible in the normalized tables."""
    conn.execute(_LOCAL_FILE_RUNTIME_BACKFILL_SQL)
    conn.commit()


def _migrate(conn: Any) -> list[MigrationStatus]:
    _ensure_ledger(conn)
    applied = {row.version: row for row in _migration_rows(conn)}
    for migration in _MIGRATIONS:
        checksum = _migration_checksum(migration.sql)
        current = applied.get(migration.version)
        if current is not None:
            if current.name != migration.name or current.checksum != checksum:
                raise RuntimeError(
                    f"PostgreSQL migration {migration.version} checksum/name mismatch"
                )
            continue
        try:
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO catalogue_schema_migrations(version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, checksum),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return _migration_rows(conn)


def initialize_postgres(
    dsn: str | None = None,
    *,
    statement_timeout_ms: int = POSTGRES_STATEMENT_TIMEOUT_MS,
) -> list[MigrationStatus]:
    """Apply all PostgreSQL migrations while holding the process-wide lock."""
    db = PostgresCatalogueDB.connect(dsn, statement_timeout_ms=statement_timeout_ms)
    locked = False
    try:
        db.conn.execute("SELECT pg_advisory_lock(%s)", (POSTGRES_ADVISORY_LOCK_KEY,))
        locked = True
        migrations = _migrate(db.conn)
        return migrations
    finally:
        if locked:
            try:
                db.conn.execute("SELECT pg_advisory_unlock(%s)", (POSTGRES_ADVISORY_LOCK_KEY,))
                db.conn.commit()
            except psycopg.Error:  # type: ignore[union-attr]
                db.conn.rollback()
        db.close()


def postgres_status(dsn: str | None = None) -> dict[str, Any]:
    """Return migration status without mutating the schema."""
    db = PostgresCatalogueDB.connect(dsn)
    try:
        exists = db.conn.execute(
            "SELECT to_regclass('public.catalogue_schema_migrations') AS table_name"
        ).fetchone()["table_name"]
        exists = _normalize_postgres_value(exists)
        migrations = _migration_rows(db.conn) if exists else []
        return {
            "backend": "postgres",
            "current_version": migrations[-1].version if migrations else 0,
            "latest_version": POSTGRES_SCHEMA_VERSION,
            "migrations": [status.__dict__ for status in migrations],
        }
    finally:
        db.close()


def postgres_connection(dsn: str | None = None) -> PostgresCatalogueDB:
    """Open an initialized request or command connection."""
    return PostgresCatalogueDB.connect(resolve_postgres_dsn(dsn))
