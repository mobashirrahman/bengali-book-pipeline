"""Small, rate-limit-friendly Open Library adapter.

Bulk imports should use Open Library's data dumps. This adapter is intended
for targeted lookups after local matching has narrowed the candidate set.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..database import CatalogueDB

logger = logging.getLogger(__name__)

OPEN_LIBRARY_SEARCH_API = "https://openlibrary.org/search.json"
OPEN_LIBRARY_DUMP_PARSER_VERSION = "openlibrary-jsonl-v1"


def stage_open_library_dump(
    db: CatalogueDB, dump_path: str | Path, *, snapshot_key: str | None = None,
    batch_size: int = 500, request: dict[str, object] | None = None,
    parser_version: str = OPEN_LIBRARY_DUMP_PARSER_VERSION,
) -> tuple[int, int]:
    """Stream an Open Library line dump into source records.

    The dump is intentionally only staged: no API requests or canonical
    entities are touched here.  ``.gz`` files are decompressed as a stream.
    """
    from ..foundation import CatalogueFoundation, file_sha256
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    path = Path(dump_path)
    if not path.exists():
        raise FileNotFoundError(path)
    digest = file_sha256(path)
    request_data = request or {"file": str(path)}
    foundation = CatalogueFoundation(db)
    run = foundation.start_import_run("open_library", parser_version=parser_version, request=request_data, checksum=digest)
    snapshot = foundation.record_source_snapshot(
        "open_library", snapshot_key or path.name,
        {"path": str(path), "sha256": digest, "file_size": path.stat().st_size},
        import_run=run, request=request_data, parser_version=parser_version,
    )
    checkpoint = db.conn.execute("SELECT cursor, state_json FROM catalogue_import_checkpoints WHERE import_run_id=? AND checkpoint_key='records'", (run.id,)).fetchone()
    if run.status == "completed" and checkpoint:
        state = json.loads(checkpoint[1] or "{}")
        return int(state.get("staged", 0)), int(state.get("skipped", 0))
    cursor = int(checkpoint[0]) if checkpoint and checkpoint[0] else 0
    state = json.loads(checkpoint[1] or "{}") if checkpoint else {}
    staged, skipped = int(state.get("staged", 0)), int(state.get("skipped", 0))
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            last_line = cursor
            for line_number, line in enumerate(stream, start=1):
                if line_number <= cursor:
                    continue
                last_line = line_number
                item = _parse_dump_line(line)
                record, record_type, external_id, title = _dump_record(item, line_number)
                if record is None or not title:
                    skipped += 1
                else:
                    inserted = db.conn.execute(
                        "INSERT OR IGNORE INTO catalogue_source_records (snapshot_id, source, external_id, record_type, title, raw_json) VALUES (?, 'open_library', ?, ?, ?, ?)",
                        (snapshot.id, external_id, record_type, title, json.dumps(record, ensure_ascii=False, sort_keys=True)),
                    )
                    staged += int(inserted.rowcount > 0)
                if (line_number - cursor) % batch_size == 0:
                    _save_dump_checkpoint(db, run.id, line_number, staged, skipped)
            _save_dump_checkpoint(db, run.id, last_line, staged, skipped)
            db.conn.commit()
        foundation.finish_import_run(run)
    except Exception:
        db.conn.rollback()
        foundation.finish_import_run(run, status="failed")
        raise
    return staged, skipped


def _save_dump_checkpoint(db: CatalogueDB, run_id: int, cursor: int, staged: int, skipped: int) -> None:
    db.conn.execute(
        "INSERT INTO catalogue_import_checkpoints (import_run_id, checkpoint_key, cursor, state_json) VALUES (?, 'records', ?, ?) ON CONFLICT(import_run_id, checkpoint_key) DO UPDATE SET cursor=excluded.cursor, state_json=excluded.state_json, updated_at=datetime('now')",
        (run_id, cursor, json.dumps({"staged": staged, "skipped": skipped})),
    )
    db.conn.commit()


def _dump_record(item: object, line_number: int) -> tuple[dict[str, object] | None, str, str | None, str | None]:
    if not isinstance(item, dict):
        return None, "", None, None
    kind = item.get("type")
    data = item.get("data") if isinstance(item.get("data"), dict) else item
    if not isinstance(data, dict) or kind not in ("/type/edition", "/type/work", "edition", "work"):
        return None, "", None, None
    title = data.get("title") or item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "", None, None
    key = item.get("key") or data.get("key")
    external_id = str(key) if key else f"line:{line_number}"
    record_type = "work" if str(kind).endswith("work") else "edition"
    return item, record_type, external_id, title.strip()


def _parse_dump_line(line: str) -> object:
    """Parse JSON fixtures and Open Library's tab-separated dump format."""
    text = line.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fields = text.split("\t", 4)
        if len(fields) != 5:
            return None
        kind, key, _revision, _modified, payload = fields
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not kind or not key:
            return None
        # Keep the exact decoded payload under data while retaining the dump
        # envelope needed for source identity and materialization.
        return {"type": kind, "key": key, "data": data}


@dataclass(frozen=True)
class OpenLibraryResult:
    work_key: str | None
    edition_key: str | None
    title: str
    authors: list[str]
    publishers: list[str]
    first_publish_year: int | None
    isbns: list[str]
    cover_id: int | None
    raw: dict[str, object]


def search_open_library(
    query: str,
    *,
    limit: int = 10,
    client: httpx.Client | None = None,
) -> list[OpenLibraryResult]:
    params = {"q": query, "limit": max(1, min(limit, 100)), "fields": "*"}
    owns_client = client is None
    request_client = client or httpx.Client(timeout=30.0)
    try:
        response = request_client.get(
            OPEN_LIBRARY_SEARCH_API,
            params=params,
            headers={"User-Agent": "pdf-craft catalogue/0.1 (metadata import)"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Open Library search failed: %s", error)
        return []
    finally:
        if owns_client:
            request_client.close()

    results: list[OpenLibraryResult] = []
    for doc in payload.get("docs", []):
        if not isinstance(doc, dict) or not doc.get("title"):
            continue
        results.append(
            OpenLibraryResult(
                work_key=_as_key(doc.get("key")),
                edition_key=_as_key(doc.get("cover_edition_key")),
                title=str(doc["title"]),
                authors=_string_list(doc.get("author_name")),
                publishers=_string_list(doc.get("publisher")),
                first_publish_year=_as_int(doc.get("first_publish_year")),
                isbns=_string_list(doc.get("isbn")),
                cover_id=_as_int(doc.get("cover_i")),
                raw=doc,
            )
        )
    return results


def cover_url(identifier: str, *, size: str = "M") -> str:
    if size not in {"S", "M", "L"}:
        raise ValueError("size must be S, M, or L")
    return f"https://covers.openlibrary.org/b/olid/{identifier}-{size}.jpg"


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)] if value else []


def _as_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_key(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None
