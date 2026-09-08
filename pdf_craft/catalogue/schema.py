from __future__ import annotations

import sqlite3
from os.path import abspath, normcase, normpath
from pathlib import Path

SCHEMA_VERSION = 9
_MAX_COMPATIBLE_SCHEMA_VERSION = 9

_DDL = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    title_sort TEXT,
    description TEXT,
    language TEXT DEFAULT 'bn',
    publisher TEXT,
    isbn TEXT,
    edition TEXT,
    source_date TEXT,
    series TEXT,
    series_index REAL,
    page_count INTEGER,
    cover_path TEXT,
    rokomari_url TEXT,
    google_books_id TEXT,
    open_library_key TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    name_sort TEXT
);

CREATE TABLE IF NOT EXISTS book_authors (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'author',
    PRIMARY KEY (book_id, author_id, role)
);

CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS book_subjects (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    PRIMARY KEY (book_id, subject_id)
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER REFERENCES books(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    file_size INTEGER,
    output_md TEXT,
    output_epub TEXT,
    chunks_path TEXT,
    cover_image_path TEXT
);

CREATE TABLE IF NOT EXISTS processing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending',
    worker_host TEXT,
    job_id TEXT,
    error TEXT,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reading_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reading_list_books (
    list_id INTEGER NOT NULL REFERENCES reading_lists(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    added_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (list_id, book_id)
);

CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position TEXT NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    rating INTEGER CHECK(rating BETWEEN 1 AND 5),
    text TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, book_id)
);

CREATE TABLE IF NOT EXISTS reading_progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, book_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, book_id)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_isbn ON books(isbn);
CREATE INDEX IF NOT EXISTS idx_books_language ON books(language);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_book_id ON files(book_id);
CREATE INDEX IF NOT EXISTS idx_processing_state ON processing(state);
CREATE INDEX IF NOT EXISTS idx_book_authors_author_id ON book_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_book_subjects_subject_id ON book_subjects(subject_id);
"""

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, description, author_names, subjects
);
"""

_V2_DDL = """
-- The v1 tables remain available for compatibility with the prototype API.
-- These tables are the durable, provenance-aware catalogue foundation.
CREATE TABLE IF NOT EXISTS catalogue_works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    subtitle TEXT,
    sort_title TEXT,
    language TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalogue_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT,
    normalized_name TEXT NOT NULL,
    UNIQUE(normalized_name)
);

CREATE TABLE IF NOT EXISTS catalogue_editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER REFERENCES catalogue_works(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    subtitle TEXT,
    publisher TEXT,
    publication_date TEXT,
    edition_statement TEXT,
    language TEXT,
    description TEXT,
    page_count INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalogue_edition_people (
    edition_id INTEGER NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES catalogue_people(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (edition_id, person_id, role)
);

CREATE TABLE IF NOT EXISTS catalogue_identifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('work', 'edition')),
    entity_id INTEGER NOT NULL,
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    UNIQUE(namespace, normalized_value)
);

CREATE TABLE IF NOT EXISTS catalogue_source_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(source, snapshot_key, payload_sha256)
);

CREATE TABLE IF NOT EXISTS catalogue_source_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES catalogue_source_snapshots(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT,
    record_type TEXT NOT NULL DEFAULT 'edition',
    title TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(source, external_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS catalogue_metadata_assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('work', 'edition', 'person')),
    entity_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_record_id INTEGER REFERENCES catalogue_source_records(id) ON DELETE SET NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate', 'accepted', 'rejected')),
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    accepted_at TEXT
);

CREATE TABLE IF NOT EXISTS catalogue_local_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/pdf',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalogue_document_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
    edition_id INTEGER NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
    score REAL NOT NULL CHECK(score >= 0 AND score <= 1),
    method TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate', 'accepted', 'rejected')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reviewed_at TEXT,
    UNIQUE(document_id, edition_id)
);

CREATE TABLE IF NOT EXISTS catalogue_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id INTEGER REFERENCES catalogue_editions(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    sha256 TEXT,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    source_record_id INTEGER REFERENCES catalogue_source_records(id) ON DELETE SET NULL,
    attribution TEXT,
    rights TEXT,
    is_selected INTEGER NOT NULL DEFAULT 0 CHECK(is_selected IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalogue_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    profile TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(document_id, kind, sha256)
);

CREATE INDEX IF NOT EXISTS idx_catalogue_editions_work ON catalogue_editions(work_id);
CREATE INDEX IF NOT EXISTS idx_catalogue_editions_title
    ON catalogue_editions(title);
CREATE INDEX IF NOT EXISTS idx_catalogue_edition_people_person_role
    ON catalogue_edition_people(person_id, role, edition_id);
CREATE INDEX IF NOT EXISTS idx_catalogue_works_sort_title
    ON catalogue_works(sort_title);
CREATE INDEX IF NOT EXISTS idx_catalogue_identifiers_lookup
    ON catalogue_identifiers(namespace, normalized_value);
CREATE INDEX IF NOT EXISTS idx_catalogue_source_records_source
    ON catalogue_source_records(source, external_id);
CREATE INDEX IF NOT EXISTS idx_catalogue_source_records_source_id
    ON catalogue_source_records(source, id);
CREATE INDEX IF NOT EXISTS idx_catalogue_assertions_entity
    ON catalogue_metadata_assertions(entity_type, entity_id, field_name, status);
CREATE INDEX IF NOT EXISTS idx_catalogue_documents_path
    ON catalogue_local_documents(source_path);
CREATE INDEX IF NOT EXISTS idx_catalogue_matches_status
    ON catalogue_document_matches(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_catalogue_assets_edition
    ON catalogue_assets(edition_id, asset_type, is_selected);
"""

_V3_DDL = """
CREATE TABLE IF NOT EXISTS catalogue_import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    request_json TEXT NOT NULL DEFAULT '{}',
    checksum TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running', 'completed', 'failed')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    UNIQUE(source, checksum, parser_version)
);

CREATE TABLE IF NOT EXISTS catalogue_import_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_run_id INTEGER NOT NULL REFERENCES catalogue_import_runs(id) ON DELETE CASCADE,
    checkpoint_key TEXT NOT NULL,
    cursor TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(import_run_id, checkpoint_key)
);

CREATE TABLE IF NOT EXISTS catalogue_source_record_editions (
    source_record_id INTEGER PRIMARY KEY REFERENCES catalogue_source_records(id) ON DELETE CASCADE,
    edition_id INTEGER NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS catalogue_snapshot_provenance (
    snapshot_id INTEGER NOT NULL REFERENCES catalogue_source_snapshots(id) ON DELETE CASCADE,
    import_run_id INTEGER NOT NULL REFERENCES catalogue_import_runs(id) ON DELETE CASCADE,
    request_json TEXT NOT NULL DEFAULT '{}',
    parser_version TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(snapshot_id, import_run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_catalogue_one_accepted_match
    ON catalogue_document_matches(document_id) WHERE status = 'accepted';
CREATE INDEX IF NOT EXISTS idx_catalogue_import_checkpoints_run
    ON catalogue_import_checkpoints(import_run_id, checkpoint_key);
"""

_ASSET_DDL = """
CREATE TABLE IF NOT EXISTS catalogue_asset_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES catalogue_assets(id) ON DELETE CASCADE,
    source_record_id INTEGER REFERENCES catalogue_source_records(id) ON DELETE SET NULL,
    source_url TEXT,
    attribution TEXT,
    rights TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(asset_id, source_record_id, source_url)
);

CREATE TABLE IF NOT EXISTS catalogue_asset_selection_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES catalogue_assets(id) ON DELETE CASCADE,
    edition_id INTEGER NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    selected_by TEXT,
    selection_method TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalogue_asset_provenance_asset
    ON catalogue_asset_provenance(asset_id);
CREATE INDEX IF NOT EXISTS idx_catalogue_asset_selection_history_edition
    ON catalogue_asset_selection_history(edition_id, asset_type, selected_at);
"""

_ASSET_COLUMNS = {
    "source_url": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'candidate'",
    "rank_score": "REAL",
    "selection_method": "TEXT",
    "selected_at": "TEXT",
    "selected_by": "TEXT",
    "retrieved_at": "TEXT",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}

_LOCAL_FILE_DDL = """
CREATE TABLE IF NOT EXISTS catalogue_document_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES catalogue_local_documents(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL UNIQUE,
    file_size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalogue_local_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    discovery_root TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('imported', 'unsupported', 'unreadable')),
    extension TEXT NOT NULL,
    media_type TEXT,
    file_size INTEGER,
    mtime_ns INTEGER,
    sha256 TEXT,
    document_id INTEGER REFERENCES catalogue_local_documents(id) ON DELETE SET NULL,
    error TEXT,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_catalogue_document_locations_document
    ON catalogue_document_locations(document_id, source_path);
CREATE INDEX IF NOT EXISTS idx_catalogue_inventory_status
    ON catalogue_local_inventory(status, source_path);
CREATE INDEX IF NOT EXISTS idx_catalogue_inventory_document
    ON catalogue_local_inventory(document_id);
"""

_RATINGS_DDL = """
CREATE TABLE IF NOT EXISTS catalogue_external_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    edition_id INTEGER NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
    source_record_id INTEGER REFERENCES catalogue_source_records(id) ON DELETE SET NULL,
    external_id TEXT,
    value REAL NOT NULL CHECK(value >= 0),
    scale REAL NOT NULL CHECK(scale > 0),
    rating_count INTEGER CHECK(rating_count IS NULL OR rating_count >= 0),
    review_count INTEGER CHECK(review_count IS NULL OR review_count >= 0),
    source_url TEXT,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(provider, edition_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS catalogue_user_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL REFERENCES catalogue_works(id) ON DELETE CASCADE,
    user_subject TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_subject, work_id)
);

CREATE INDEX IF NOT EXISTS idx_catalogue_external_ratings_edition
    ON catalogue_external_ratings(edition_id, provider, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_catalogue_external_ratings_provider
    ON catalogue_external_ratings(provider, external_id);
CREATE INDEX IF NOT EXISTS idx_catalogue_user_ratings_work
    ON catalogue_user_ratings(work_id, updated_at DESC);
"""

_CATALOGUE_WORK_COLUMNS = {
    "subtitle": "TEXT",
    "sort_title": "TEXT",
    "language": "TEXT",
    "description": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}


def _read_existing_schema_version(conn: sqlite3.Connection) -> int | None:
    schema_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if schema_table is None:
        return None
    existing = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if existing is None:
        return None
    return int(existing[0])


def _ensure_fts(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='books_fts'"
    ).fetchone()
    if not existing:
        conn.executescript(_FTS_DDL)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _ensure_catalogue_work_columns(conn: sqlite3.Connection) -> None:
    """Repair a partially-created v2 works table before its indexes replay."""
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalogue_works'"
    ).fetchone()
    if existing is None:
        return
    for column, definition in _CATALOGUE_WORK_COLUMNS.items():
        if not _has_column(conn, "catalogue_works", column):
            conn.execute(
                f"ALTER TABLE catalogue_works ADD COLUMN {column} {definition}"
            )


def _ensure_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(_V3_DDL)
    if not _has_column(conn, "catalogue_source_snapshots", "import_run_id"):
        conn.execute(
            """ALTER TABLE catalogue_source_snapshots ADD COLUMN import_run_id INTEGER
               REFERENCES catalogue_import_runs(id) ON DELETE SET NULL"""
        )
    if not _has_column(conn, "catalogue_source_snapshots", "request_json"):
        conn.execute(
            "ALTER TABLE catalogue_source_snapshots ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'"
        )
    if not _has_column(conn, "catalogue_source_snapshots", "parser_version"):
        conn.execute("ALTER TABLE catalogue_source_snapshots ADD COLUMN parser_version TEXT")
    if not _has_column(conn, "catalogue_document_matches", "reviewer"):
        conn.execute("ALTER TABLE catalogue_document_matches ADD COLUMN reviewer TEXT")
    if not _has_column(conn, "catalogue_document_matches", "review_reason"):
        conn.execute("ALTER TABLE catalogue_document_matches ADD COLUMN review_reason TEXT")


def _ensure_assets(conn: sqlite3.Connection) -> None:
    """Add asset provenance/selection columns to fresh or older v2 databases."""
    # Keep this migration additive: deployments which already report schema
    # version 2 (including the v1 compatibility tables) remain readable.
    conn.executescript(_ASSET_DDL)
    for column, definition in _ASSET_COLUMNS.items():
        if not _has_column(conn, "catalogue_assets", column):
            conn.execute(f"ALTER TABLE catalogue_assets ADD COLUMN {column} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalogue_assets_source_url "
        "ON catalogue_assets(edition_id, asset_type, source_url)"
    )


def _ensure_local_file_catalogue(conn: sqlite3.Connection) -> None:
    """Install local inventory tables and preserve existing document paths."""
    conn.executescript(_LOCAL_FILE_DDL)
    rows = conn.execute(
        """SELECT id, source_path, file_size, media_type, discovered_at, updated_at
           FROM catalogue_local_documents ORDER BY id DESC"""
    ).fetchall()
    seen_paths: set[str] = set()
    for row in rows:
        # The normalized tables use an absolute, normalized path key so that
        # repeated CLI invocations with equivalent paths remain idempotent.
        source_path = normcase(normpath(abspath(str(row[1]))))
        if source_path in seen_paths:
            # The logical document rows are retained as history; only the
            # newest row owns a current normalized location and inventory row.
            continue
        seen_paths.add(source_path)
        discovery_root = str(Path(source_path).parent)
        location_update = conn.execute(
            """UPDATE catalogue_document_locations
               SET document_id=?, file_size=?, media_type=?,
                   last_seen_at=COALESCE(?, datetime('now')),
                   updated_at=COALESCE(?, datetime('now'))
               WHERE source_path=?""",
            (row[0], row[2], row[3], row[4], row[5], source_path),
        )
        if location_update.rowcount == 0:
            conn.execute(
                """INSERT INTO catalogue_document_locations
                   (document_id, source_path, file_size, media_type, discovered_at,
                    last_seen_at, updated_at)
                   VALUES (?, ?, ?, ?, COALESCE(?, datetime('now')),
                           COALESCE(?, datetime('now')), COALESCE(?, datetime('now')))""",
                (row[0], source_path, row[2], row[3], row[4], row[4], row[5]),
            )
        inventory_update = conn.execute(
            """UPDATE catalogue_local_inventory
               SET discovery_root=?, status='imported', extension=?, media_type=?,
                   file_size=?, sha256=(SELECT sha256 FROM catalogue_local_documents WHERE id=?),
                   document_id=?, error=NULL,
                   last_seen_at=COALESCE(?, datetime('now')),
                   updated_at=COALESCE(?, datetime('now'))
               WHERE source_path=?""",
            (
                discovery_root, Path(source_path).suffix.lower(), row[3], row[2],
                row[0], row[0], row[4], row[5], source_path,
            ),
        )
        if inventory_update.rowcount == 0:
            conn.execute(
                """INSERT INTO catalogue_local_inventory
                   (source_path, discovery_root, status, extension, media_type,
                    file_size, sha256, document_id, discovered_at, last_seen_at,
                    updated_at)
                   VALUES (?, ?, 'imported', ?, ?, ?,
                           (SELECT sha256 FROM catalogue_local_documents WHERE id=?),
                           ?, COALESCE(?, datetime('now')), COALESCE(?, datetime('now')),
                           COALESCE(?, datetime('now')))""",
                (
                    source_path, discovery_root, Path(source_path).suffix.lower(),
                    row[3], row[2], row[0], row[0], row[4], row[4], row[5],
                ),
            )


def _ensure_ratings(conn: sqlite3.Connection) -> None:
    """Install additive external and community rating tables."""
    conn.executescript(_RATINGS_DDL)


def initialize_database(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    existing_version = _read_existing_schema_version(conn)
    if (
        existing_version is not None
        and existing_version > _MAX_COMPATIBLE_SCHEMA_VERSION
    ):
        conn.close()
        raise RuntimeError(
            f"Database schema version {existing_version} is newer than supported "
            f"version {_MAX_COMPATIBLE_SCHEMA_VERSION}"
        )

    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    _ensure_fts(conn)

    existing = conn.execute(
        "SELECT version FROM schema_version LIMIT 1"
    ).fetchone()
    if existing is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        existing = (1,)

    current_version = int(existing[0])
    if current_version < 2:
        _ensure_catalogue_work_columns(conn)
        conn.executescript(_V2_DDL)
        conn.execute("UPDATE schema_version SET version = 2")
        current_version = 2
    else:
        # Versioned databases can be interrupted between DDL statements.
        # Replaying CREATE IF NOT EXISTS repairs those partial installations.
        _ensure_catalogue_work_columns(conn)
        conn.executescript(_V2_DDL)
    _ensure_v3(conn)
    _ensure_assets(conn)
    _ensure_local_file_catalogue(conn)
    _ensure_ratings(conn)
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def get_db_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn
