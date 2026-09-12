"""Provenance-aware catalogue storage and local document ingestion.

This layer deliberately lives beside the original catalogue API.  The v1
tables are kept for compatibility while new imports use these normalized
tables, where external observations and local files are immutable facts.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .database import CatalogueDB


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_path(path: str | Path) -> str:
    """Return the stable path key used by the local-file tables."""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


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
        location_path = _normalized_path(source)
        self.db.conn.execute(
            """INSERT INTO catalogue_document_locations
               (document_id, source_path, file_size, media_type)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_path) DO UPDATE SET
               document_id=excluded.document_id,
               file_size=excluded.file_size,
               media_type=excluded.media_type,
               last_seen_at=datetime('now'),
               updated_at=datetime('now')""",
            (document_id, location_path, stat.st_size, media_type),
        )
        self.db.conn.commit()
        return LocalDocument(document_id, digest, str(source), stat.st_size, media_type)

    def record_local_inventory(
        self,
        path: str | Path,
        *,
        root: str | Path,
        status: str,
        document_id: int | None = None,
        sha256: str | None = None,
        error: str | None = None,
        stat_result: os.stat_result | None = None,
    ) -> None:
        """Record one observed filesystem entry without deleting history."""
        if status not in {"imported", "unsupported", "unreadable"}:
            raise ValueError("invalid local inventory status")
        source_path = _normalized_path(path)
        discovery_root = _normalized_path(root)
        extension = Path(path).suffix.lower()
        media_type = mimetypes.guess_type(Path(path).name)[0]
        file_size = stat_result.st_size if stat_result is not None else None
        mtime_ns = stat_result.st_mtime_ns if stat_result is not None else None
        self.db.conn.execute(
            """INSERT INTO catalogue_local_inventory
               (source_path, discovery_root, status, extension, media_type,
                file_size, mtime_ns, sha256, document_id, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_path) DO UPDATE SET
               discovery_root=excluded.discovery_root,
               status=excluded.status,
               extension=excluded.extension,
               media_type=excluded.media_type,
               file_size=excluded.file_size,
               mtime_ns=excluded.mtime_ns,
               sha256=excluded.sha256,
               document_id=excluded.document_id,
               error=excluded.error,
               last_seen_at=datetime('now'),
               updated_at=datetime('now')""",
            (
                source_path, discovery_root, status, extension, media_type,
                file_size, mtime_ns, sha256, document_id, error,
            ),
        )
        self.db.conn.commit()

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


def iter_local_files(root: str | Path) -> Iterator[Path]:
    """Yield every regular file below ``root`` in stable path order."""
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(base)
    yield from sorted(
        (path for path in base.rglob("*") if path.is_file()),
        key=lambda path: str(path),
    )


def ingest_local_documents(
    db: CatalogueDB,
    root: str | Path,
    *,
    include_extensions: set[str] | None = None,
) -> tuple[int, int]:
    """Index local files by content hash; return ``(created_or_updated, skipped)``."""
    base = Path(root)
    paths = iter_local_files(base)
    supported = {".pdf", ".epub", ".pcex"}
    normalized = (
        {extension.lower() for extension in include_extensions}
        if include_extensions is not None else supported
    )
    count = 0
    skipped = 0
    foundation = CatalogueFoundation(db)
    for path in paths:
        suffix = path.suffix.lower()
        try:
            stat_result = path.stat()
        except OSError as exc:
            foundation.record_local_inventory(
                path, root=base, status="unreadable", error=str(exc),
            )
            skipped += 1
            continue
        if suffix not in supported or suffix not in normalized:
            foundation.record_local_inventory(
                path, root=base, status="unsupported", stat_result=stat_result,
            )
            continue
        try:
            document = foundation.upsert_local_document(path)
            foundation.record_local_inventory(
                path, root=base, status="imported", document_id=document.id,
                sha256=document.sha256, stat_result=stat_result,
            )
            count += 1
        except (OSError, ValueError) as exc:
            foundation.record_local_inventory(
                path, root=base, status="unreadable", error=str(exc),
                stat_result=stat_result,
            )
            skipped += 1
    return count, skipped
