"""Bounded, repeat-safe transfer from the authoritative SQLite catalogue."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .postgres import (
    PostgresCatalogueDB,
    _normalize_postgres_value,
    initialize_postgres,
    resolve_postgres_dsn,
)


@dataclass(frozen=True)
class TransferTable:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]


# The order follows the normalized foreign-key graph.  Transfer metadata is
# kept out of this list; it belongs to PostgreSQL and records the transfer.
TRANSFER_TABLES = (
    TransferTable("catalogue_works", ("id", "title", "subtitle", "sort_title", "language", "description", "created_at", "updated_at"), ("id",)),
    TransferTable("catalogue_people", ("id", "name", "sort_name", "normalized_name"), ("id",)),
    TransferTable("catalogue_import_runs", ("id", "source", "parser_version", "request_json", "checksum", "status", "started_at", "completed_at"), ("id",)),
    TransferTable("catalogue_import_checkpoints", ("id", "import_run_id", "checkpoint_key", "cursor", "state_json", "updated_at"), ("id",)),
    TransferTable("catalogue_editions", ("id", "work_id", "title", "subtitle", "publisher", "publication_date", "edition_statement", "language", "description", "page_count", "created_at", "updated_at"), ("id",)),
    TransferTable("catalogue_source_snapshots", ("id", "source", "snapshot_key", "fetched_at", "payload_sha256", "payload_json", "import_run_id", "request_json", "parser_version"), ("id",)),
    TransferTable("catalogue_local_documents", ("id", "sha256", "source_path", "file_size", "media_type", "metadata_json", "discovered_at", "updated_at"), ("id",)),
    TransferTable("catalogue_source_records", ("id", "snapshot_id", "source", "external_id", "record_type", "title", "raw_json"), ("id",)),
    TransferTable("catalogue_edition_people", ("edition_id", "person_id", "role", "position"), ("edition_id", "person_id", "role")),
    TransferTable("catalogue_identifiers", ("id", "entity_type", "entity_id", "namespace", "value", "normalized_value"), ("id",)),
    TransferTable("catalogue_metadata_assertions", ("id", "entity_type", "entity_id", "field_name", "value_json", "source_record_id", "confidence", "status", "observed_at", "accepted_at"), ("id",)),
    TransferTable("catalogue_document_matches", ("id", "document_id", "edition_id", "score", "method", "status", "evidence_json", "reviewed_at", "reviewer", "review_reason"), ("id",)),
    TransferTable("catalogue_source_record_editions", ("source_record_id", "edition_id"), ("source_record_id",)),
    TransferTable("catalogue_snapshot_provenance", ("snapshot_id", "import_run_id", "request_json", "parser_version", "recorded_at"), ("snapshot_id", "import_run_id")),
    TransferTable("catalogue_assets", ("id", "edition_id", "document_id", "asset_type", "storage_uri", "sha256", "mime_type", "width", "height", "source_record_id", "attribution", "rights", "is_selected", "created_at", "source_url", "status", "rank_score", "selection_method", "selected_at", "selected_by", "retrieved_at", "metadata_json"), ("id",)),
    TransferTable("catalogue_artifacts", ("id", "document_id", "kind", "storage_uri", "sha256", "profile", "created_at"), ("id",)),
    TransferTable("catalogue_asset_provenance", ("id", "asset_id", "source_record_id", "source_url", "attribution", "rights", "observed_at", "metadata_json"), ("id",)),
    TransferTable("catalogue_asset_selection_history", ("id", "asset_id", "edition_id", "asset_type", "selected_at", "selected_by", "selection_method"), ("id",)),
)

_SEQUENCE_TABLES = tuple(table.name for table in TRANSFER_TABLES if table.primary_key == ("id",))


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    value = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    return hashlib.sha256(value).hexdigest()


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _source_rows(
    conn: sqlite3.Connection,
    table: TransferTable,
    columns: tuple[str, ...],
    batch_size: int,
    offset: int,
) -> list[tuple[Any, ...]]:
    selected = ", ".join(columns)
    ordering = ", ".join(table.primary_key)
    rows = conn.execute(
        f"SELECT {selected} FROM {table.name} ORDER BY {ordering} LIMIT ? OFFSET ?",
        (batch_size, offset),
    ).fetchall()
    return [tuple(row[column] for column in columns) for row in rows]


def _upsert_sql(table: TransferTable, columns: tuple[str, ...]) -> str:
    names = ", ".join(columns)
    values = ", ".join(["%s"] * len(columns))
    updates = ", ".join(
        f"{column}=EXCLUDED.{column}"
        for column in columns
        if column not in table.primary_key
    )
    conflict = ", ".join(table.primary_key)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return f"INSERT INTO {table.name} ({names}) VALUES ({values}) ON CONFLICT ({conflict}) {action}"


def _checkpoint(db: PostgresCatalogueDB, run_id: int, table: str) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT source_offset, rows_copied, completed FROM catalogue_transfer_checkpoints "
        "WHERE transfer_run_id=%s AND table_name=%s",
        (run_id, table),
    ).fetchone()
    return _normalize_postgres_value(row) if row else None


def _ensure_checkpoint(db: PostgresCatalogueDB, run_id: int, table: str) -> dict[str, Any]:
    db.conn.execute(
        "INSERT INTO catalogue_transfer_checkpoints(transfer_run_id, table_name) "
        "VALUES (%s, %s) ON CONFLICT (transfer_run_id, table_name) DO NOTHING",
        (run_id, table),
    )
    db.commit()
    return _checkpoint(db, run_id, table)  # type: ignore[return-value]


def _transfer_run(db: PostgresCatalogueDB, source_path: Path, fingerprint: str) -> int:
    row = db.conn.execute(
        "SELECT id FROM catalogue_transfer_runs WHERE source_path=%s AND source_fingerprint=%s",
        (str(source_path.resolve()), fingerprint),
    ).fetchone()
    if row:
        return int(row["id"])
    row = db.conn.execute(
        "INSERT INTO catalogue_transfer_runs(source_path, source_fingerprint) "
        "VALUES (%s, %s) RETURNING id",
        (str(source_path.resolve()), fingerprint),
    ).fetchone()
    db.commit()
    return int(row["id"])


def _reset_sequences(db: PostgresCatalogueDB) -> None:
    for table in _SEQUENCE_TABLES:
        db.conn.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
            f"COALESCE(MAX(id), 1), COUNT(*) > 0) FROM {table}",
            (table,),
        )
    db.commit()


def transfer_sqlite_to_postgres(
    sqlite_path: str | Path,
    dsn: str | None = None,
    *,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Transfer normalized SQLite rows in bounded transactions.

    The SQLite database is opened read-only in spirit: this function only
    issues SELECTs against it. Each PostgreSQL batch and its checkpoint are
    committed together, so a failed batch rolls back and can be retried.
    """
    if batch_size < 1 or batch_size > 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    source = Path(sqlite_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    resolved_dsn = resolve_postgres_dsn(dsn)
    initialize_postgres(resolved_dsn)
    source_conn = sqlite3.connect(str(source))
    source_conn.row_factory = sqlite3.Row
    db = PostgresCatalogueDB.connect(resolved_dsn)
    fingerprint = _fingerprint(source)
    run_id: int | None = None
    report: dict[str, Any] = {"tables": {}, "resumed": False}
    try:
        run_id = _transfer_run(db, source, fingerprint)
        existing_run = db.conn.execute(
            "SELECT status FROM catalogue_transfer_runs WHERE id=%s", (run_id,)
        ).fetchone()
        existing_status = _normalize_postgres_value(existing_run["status"]) if existing_run else None
        report["resumed"] = bool(existing_run and existing_status != "running")
        for table in TRANSFER_TABLES:
            available = _sqlite_columns(source_conn, table.name)
            columns = tuple(column for column in table.columns if column in available)
            if not columns:
                report["tables"][table.name] = 0
                continue
            checkpoint = _ensure_checkpoint(db, run_id, table.name)
            if checkpoint["completed"]:
                report["tables"][table.name] = int(checkpoint["rows_copied"])
                continue
            offset = int(checkpoint["source_offset"])
            copied = int(checkpoint["rows_copied"])
            statement = _upsert_sql(table, columns)
            while True:
                rows = _source_rows(source_conn, table, columns, batch_size, offset)
                if not rows:
                    db.conn.execute(
                        "UPDATE catalogue_transfer_checkpoints SET completed=TRUE, updated_at=CURRENT_TIMESTAMP::text "
                        "WHERE transfer_run_id=%s AND table_name=%s",
                        (run_id, table.name),
                    )
                    db.commit()
                    break
                try:
                    with db.conn.cursor() as cursor:
                        cursor.executemany(statement, rows)
                    offset += len(rows)
                    copied += len(rows)
                    db.conn.execute(
                        "UPDATE catalogue_transfer_checkpoints SET source_offset=%s, rows_copied=%s, updated_at=CURRENT_TIMESTAMP::text "
                        "WHERE transfer_run_id=%s AND table_name=%s",
                        (offset, copied, run_id, table.name),
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            report["tables"][table.name] = copied
        _reset_sequences(db)
        db.conn.execute(
            "UPDATE catalogue_transfer_runs SET status='completed', updated_at=CURRENT_TIMESTAMP::text, completed_at=CURRENT_TIMESTAMP::text WHERE id=%s",
            (run_id,),
        )
        db.commit()
        report.update({"run_id": run_id, "status": "completed"})
        return report
    except Exception:
        db.rollback()
        if run_id is not None:
            db.conn.execute(
                "UPDATE catalogue_transfer_runs SET status='failed', updated_at=CURRENT_TIMESTAMP::text WHERE id=%s",
                (run_id,),
            )
            db.commit()
        raise
    finally:
        source_conn.close()
        db.close()
