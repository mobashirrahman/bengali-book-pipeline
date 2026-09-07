"""Provenance-aware catalogue storage and local document ingestion.

This layer deliberately lives beside the original catalogue API.  The v1
tables are kept for compatibility while new imports use these normalized
tables, where external observations and local files are immutable facts.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .database import CatalogueDB


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class LocalDocument:
    id: int
    sha256: str
    source_path: str
    file_size: int
    media_type: str


@dataclass(frozen=True)
class SourceSnapshot:
    id: int
    source: str
    snapshot_key: str
    payload_sha256: str


@dataclass(frozen=True)
class ImportRun:
    id: int
    source: str
    parser_version: str
    checksum: str | None
    status: str


class CatalogueFoundation:
    """Operations for the normalized catalogue foundation."""

    def __init__(self, db: CatalogueDB):
        self.db = db

    def start_import_run(
        self, source: str, *, parser_version: str, request: object = None,
        checksum: str | None = None,
    ) -> ImportRun:
        """Create or resume an idempotent source import run."""
        request_json = _json(request or {})
        # The v2 unique key predates request provenance.  Keep its safe
        # idempotency while deriving a distinct run identity for a different
        # request over the same payload.
        request_hash = _sha256_bytes(request_json.encode("utf-8"))
        run_checksum = checksum if request_json == "{}" else (
            f"{checksum}:{request_hash}" if checksum is not None else request_hash
        )
        row = self.db.conn.execute(
            """SELECT id, status FROM catalogue_import_runs
               WHERE source=? AND checksum IS ? AND parser_version=?
               AND request_json=?""",
            (source, run_checksum, parser_version, request_json),
        ).fetchone()
        if row:
            if row[1] == "failed":
                self.db.conn.execute(
                    """UPDATE catalogue_import_runs
                       SET status='running', completed_at=NULL
                       WHERE id=? AND status='failed'""", (row[0],)
                )
                self.db.conn.commit()
            return ImportRun(
                int(row[0]), source, parser_version, checksum,
                "running" if row[1] == "failed" else row[1],
            )
        cur = self.db.conn.execute(
            """INSERT INTO catalogue_import_runs
               (source, parser_version, request_json, checksum)
               VALUES (?, ?, ?, ?)""",
            (source, parser_version, request_json, run_checksum),
        )
        self.db.conn.commit()
        return ImportRun(int(cur.lastrowid), source, parser_version, checksum, "running")

    def set_import_checkpoint(
        self, run: ImportRun | int, checkpoint_key: str, *,
        cursor: str | None = None, state: object = None,
    ) -> None:
        """Persist a restart cursor and arbitrary JSON state."""
        run_id = run.id if isinstance(run, ImportRun) else run
        self.db.conn.execute(
            """INSERT INTO catalogue_import_checkpoints
               (import_run_id, checkpoint_key, cursor, state_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(import_run_id, checkpoint_key) DO UPDATE SET
               cursor=excluded.cursor, state_json=excluded.state_json,
               updated_at=datetime('now')""",
            (run_id, checkpoint_key, cursor, _json(state or {})),
        )
        self.db.conn.commit()

    def finish_import_run(self, run: ImportRun | int, *, status: str = "completed") -> None:
        """Mark an import run completed or failed."""
        if status not in {"completed", "failed"}:
            raise ValueError("status must be completed or failed")
        run_id = run.id if isinstance(run, ImportRun) else run
        self.db.conn.execute(
            """UPDATE catalogue_import_runs SET status=?, completed_at=datetime('now')
               WHERE id=? AND status='running'""", (status, run_id)
        )
        self.db.conn.commit()

    def upsert_local_document(
        self,
        path: str | Path,
        *,
        sha256: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> LocalDocument:
        source = Path(path)
        stat = source.stat()
        digest = sha256 or file_sha256(source)
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        metadata_json = _json(metadata or {})
        row = self.db.conn.execute(
            """SELECT id FROM catalogue_local_documents WHERE sha256 = ?""",
            (digest,),
        ).fetchone()
        if row:
            self.db.conn.execute(
                """UPDATE catalogue_local_documents
                   SET source_path=?, file_size=?, media_type=?, metadata_json=?, updated_at=?
                   WHERE id=?""",
                (str(source), stat.st_size, media_type, metadata_json, _now(), row[0]),
            )
            document_id = int(row[0])
        else:
            cursor = self.db.conn.execute(
                """INSERT INTO catalogue_local_documents
                   (sha256, source_path, file_size, media_type, metadata_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (digest, str(source), stat.st_size, media_type, metadata_json),
            )
            document_id = int(cursor.lastrowid)
        self.db.conn.commit()
        return LocalDocument(document_id, digest, str(source), stat.st_size, media_type)

    def record_source_snapshot(
        self,
        source: str,
        snapshot_key: str,
        payload: object,
        *,
        import_run: ImportRun | int | None = None,
        request: object = None,
        parser_version: str | None = None,
    ) -> SourceSnapshot:
        payload_json = _json(payload)
        payload_hash = _sha256_bytes(payload_json.encode("utf-8"))
        row = self.db.conn.execute(
            """SELECT id FROM catalogue_source_snapshots
               WHERE source=? AND snapshot_key=? AND payload_sha256=?""",
            (source, snapshot_key, payload_hash),
        ).fetchone()
        if row:
            snapshot_id = int(row[0])
        else:
            run_id = import_run.id if isinstance(import_run, ImportRun) else import_run
            cursor = self.db.conn.execute(
                """INSERT INTO catalogue_source_snapshots
                   (source, snapshot_key, payload_sha256, payload_json,
                    import_run_id, request_json, parser_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source, snapshot_key, payload_hash, payload_json, run_id,
                 _json(request or {}), parser_version),
            )
            snapshot_id = int(cursor.lastrowid)
        run_id = import_run.id if isinstance(import_run, ImportRun) else import_run
        if run_id is not None:
            self.db.conn.execute(
                """INSERT OR IGNORE INTO catalogue_snapshot_provenance
                   (snapshot_id, import_run_id, request_json, parser_version)
                   VALUES (?, ?, ?, ?)""",
                (snapshot_id, run_id, _json(request or {}), parser_version),
            )
        self.db.conn.commit()
        return SourceSnapshot(snapshot_id, source, snapshot_key, payload_hash)

    def record_source_record(
        self,
        snapshot: SourceSnapshot,
        *,
        external_id: str | None,
        payload: object,
        record_type: str = "edition",
        title: str | None = None,
    ) -> int:
        raw_json = _json(payload)
        existing = self.db.conn.execute(
            """SELECT id FROM catalogue_source_records
               WHERE source=? AND external_id IS ? AND snapshot_id=?""",
            (snapshot.source, external_id, snapshot.id),
        ).fetchone()
        if existing:
            return int(existing[0])
        cursor = self.db.conn.execute(
            """INSERT INTO catalogue_source_records
               (snapshot_id, source, external_id, record_type, title, raw_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (snapshot.id, snapshot.source, external_id, record_type, title, raw_json),
        )
        self.db.conn.commit()
        return int(cursor.lastrowid)

    def add_assertion(
        self,
        *,
        entity_type: str,
        entity_id: int,
        field_name: str,
        value: object,
        source_record_id: int | None,
        confidence: float,
        status: str = "candidate",
    ) -> int:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        cursor = self.db.conn.execute(
            """INSERT INTO catalogue_metadata_assertions
               (entity_type, entity_id, field_name, value_json,
                source_record_id, confidence, status, accepted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_type, entity_id, field_name, _json(value),
                source_record_id, confidence, status,
                _now() if status == "accepted" else None,
            ),
        )
        self.db.conn.commit()
        return int(cursor.lastrowid)

    def add_artifact(
        self,
        document_id: int,
        *,
        kind: str,
        storage_uri: str,
        sha256: str,
        profile: str | None = None,
    ) -> int:
        cursor = self.db.conn.execute(
            """INSERT OR IGNORE INTO catalogue_artifacts
               (document_id, kind, storage_uri, sha256, profile)
               VALUES (?, ?, ?, ?, ?)""",
            (document_id, kind, storage_uri, sha256, profile),
        )
        self.db.conn.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        row = self.db.conn.execute(
            """SELECT id FROM catalogue_artifacts
               WHERE document_id=? AND kind=? AND sha256=?""",
            (document_id, kind, sha256),
        ).fetchone()
        if not row:
            raise RuntimeError("artifact insert did not return an id")
        return int(row[0])


def iter_local_documents(root: str | Path) -> Iterator[Path]:
    """Yield supported local documents in stable order without modifying them."""
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(base)
    supported = {".pdf", ".epub", ".pcex"}
    yield from sorted(
        (path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in supported),
        key=lambda path: str(path),
    )


def ingest_local_documents(
    db: CatalogueDB,
    root: str | Path,
    *,
    include_extensions: set[str] | None = None,
) -> tuple[int, int]:
    """Index local files by content hash; return ``(created_or_updated, skipped)``."""
    foundation = CatalogueFoundation(db)
    paths = iter_local_documents(root)
    if include_extensions is not None:
        normalized = {extension.lower() for extension in include_extensions}
        paths = (path for path in paths if path.suffix.lower() in normalized)
    count = 0
    skipped = 0
    for path in paths:
        try:
            foundation.upsert_local_document(path)
            count += 1
        except (OSError, ValueError):
            skipped += 1
    return count, skipped
