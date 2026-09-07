"""Read-only queries for the normalized catalogue backend.

The repository accepts a DB-API connection so the API can create and close a
connection per request.  Queries are deliberately SQLite-portable SQL; the
connection boundary can be replaced when a PostgreSQL backend is introduced.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .postgres import _normalize_postgres_value


def _is_postgres(conn: Any) -> bool:
    return hasattr(conn, "info")


def _sql(conn: Any, sql: str) -> str:
    return sql.replace("?", "%s") if _is_postgres(conn) else sql


def _execute(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(_sql(conn, sql), params)


def _one(conn: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = _execute(conn, sql, params).fetchone()
    result = dict(row) if row else None
    return _normalize_postgres_value(result) if _is_postgres(conn) and result else result


def _many(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = [dict(row) for row in _execute(conn, sql, params).fetchall()]
    return [_normalize_postgres_value(row) for row in rows] if _is_postgres(conn) else rows


def _identifiers(conn: Any, entity_type: str, entity_id: int) -> list[dict[str, Any]]:
    return _many(conn, """SELECT namespace, value FROM catalogue_identifiers
        WHERE entity_type=? AND entity_id=? ORDER BY namespace, value, id""", (entity_type, entity_id))


def _people(conn: Any, edition_id: int) -> list[dict[str, Any]]:
    return _many(conn, """SELECT p.id, p.name, p.sort_name, ep.role, ep.position
        FROM catalogue_people p JOIN catalogue_edition_people ep ON ep.person_id=p.id
        WHERE ep.edition_id=? ORDER BY ep.position, ep.role, p.id""", (edition_id,))


def _assets(conn: Any, edition_id: int | None = None, document_id: int | None = None) -> list[dict[str, Any]]:
    if edition_id is not None:
        return _many(conn, """SELECT id, edition_id, document_id, asset_type, storage_uri,
            sha256, mime_type, width, height, attribution, rights, is_selected
            FROM catalogue_assets WHERE edition_id=? ORDER BY is_selected DESC, id""", (edition_id,))
    return _many(conn, """SELECT id, edition_id, document_id, asset_type, storage_uri,
        sha256, mime_type, width, height, attribution, rights, is_selected
        FROM catalogue_assets WHERE document_id=? ORDER BY is_selected DESC, id""", (document_id,))


def get_work(conn: Any, work_id: int) -> dict[str, Any] | None:
    work = _one(conn, "SELECT * FROM catalogue_works WHERE id=?", (work_id,))
    if not work:
        return None
    editions = get_editions_for_work(conn, work_id)
    sources = _many(conn, """SELECT DISTINCT sr.source FROM catalogue_source_records sr
        JOIN catalogue_source_record_editions sre ON sre.source_record_id=sr.id
        JOIN catalogue_editions e ON e.id=sre.edition_id WHERE e.work_id=? ORDER BY sr.source""", (work_id,))
    return {**work, "identifiers": _identifiers(conn, "work", work_id), "editions": editions,
            "sources": [row["source"] for row in sources]}


def list_works(conn: Any, limit: int, after: int | None = None) -> list[dict[str, Any]]:
    if after is None:
        rows = _many(conn, "SELECT * FROM catalogue_works ORDER BY id LIMIT ?", (limit,))
    else:
        rows = _many(conn, "SELECT * FROM catalogue_works WHERE id>? ORDER BY id LIMIT ?", (after, limit))
    return [{**row, "identifiers": _identifiers(conn, "work", row["id"]), "editions": [], "sources": []} for row in rows]


def get_editions_for_work(conn: Any, work_id: int) -> list[dict[str, Any]]:
    return [get_edition(conn, row["id"]) for row in _many(
        conn, "SELECT id FROM catalogue_editions WHERE work_id=? ORDER BY id", (work_id,)
    )]  # type: ignore[list-item]


def get_edition(conn: Any, edition_id: int) -> dict[str, Any] | None:
    edition = _one(conn, "SELECT * FROM catalogue_editions WHERE id=?", (edition_id,))
    if not edition:
        return None
    work = _one(conn, "SELECT id, title, subtitle FROM catalogue_works WHERE id=?", (edition["work_id"],)) if edition["work_id"] else None
    sources = _many(conn, """SELECT DISTINCT sr.source FROM catalogue_source_records sr
        JOIN catalogue_source_record_editions sre ON sre.source_record_id=sr.id
        WHERE sre.edition_id=? ORDER BY sr.source""", (edition_id,))
    return {**edition, "work": work, "people": _people(conn, edition_id),
            "identifiers": _identifiers(conn, "edition", edition_id), "assets": _assets(conn, edition_id),
            "sources": [row["source"] for row in sources],
            "documents": _many(conn, """SELECT d.id, d.sha256, d.source_path, d.file_size, d.media_type
                FROM catalogue_local_documents d JOIN catalogue_document_matches m ON m.document_id=d.id
                WHERE m.edition_id=? AND m.status='accepted' ORDER BY d.id""", (edition_id,))}


def get_document(conn: Any, document_id: int) -> dict[str, Any] | None:
    document = _one(conn, "SELECT * FROM catalogue_local_documents WHERE id=?", (document_id,))
    if not document:
        return None
    matches = _many(conn, """SELECT m.id, m.edition_id, m.score, m.method, m.status,
        m.evidence_json, e.title AS edition_title FROM catalogue_document_matches m
        JOIN catalogue_editions e ON e.id=m.edition_id WHERE m.document_id=?
        AND m.status IN ('accepted','candidate') ORDER BY CASE m.status WHEN 'accepted' THEN 0 ELSE 1 END, m.score DESC, m.id""", (document_id,))
    artifacts = _many(conn, "SELECT id, kind, storage_uri, sha256, profile, created_at FROM catalogue_artifacts WHERE document_id=? ORDER BY id", (document_id,))
    return {**document, "matches": matches, "assets": _assets(conn, document_id=document_id), "artifacts": artifacts}


def get_asset(conn: Any, asset_id: int) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM catalogue_assets WHERE id=?", (asset_id,))


def stats(conn: Any) -> dict[str, Any]:
    tables = ("catalogue_works", "catalogue_editions", "catalogue_people", "catalogue_identifiers", "catalogue_local_documents", "catalogue_document_matches", "catalogue_assets", "catalogue_artifacts")
    counts = {
        name.removeprefix("catalogue_"): _execute(
            conn, f"SELECT COUNT(*) AS count FROM {name}"
        ).fetchone()["count" if _is_postgres(conn) else 0]
        for name in tables
    }
    sources = _many(conn, "SELECT source, COUNT(*) AS records FROM catalogue_source_records GROUP BY source ORDER BY source")
    counts["source_coverage"] = sources
    return counts


def _decode_cursor(value: str | None) -> tuple[str, int, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if (
            not isinstance(decoded, list)
            or len(decoded) != 3
            or not isinstance(decoded[0], str)
            or not isinstance(decoded[1], int)
            or isinstance(decoded[1], bool)
            or decoded[2] not in {"work", "edition", "person"}
        ):
            raise ValueError("invalid cursor")
        return decoded[0], decoded[1], decoded[2]
    except (TypeError, ValueError, UnicodeError, base64.binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc


def encode_cursor(title: str, item_id: int, kind: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([title, item_id, kind]).encode()).decode().rstrip("=")


def search(conn: Any, query: str, limit: int, after: str | None = None) -> dict[str, Any]:
    cursor = _decode_cursor(after)
    needle = f"%{query}%"
    params: list[Any] = [needle, needle, needle]
    if _is_postgres(conn):
        title_match = "s.title ILIKE ?"
        order = "LOWER(s.title), s.id, s.kind"
        predicate = "" if cursor is None else " AND (LOWER(s.title) > LOWER(?) OR (LOWER(s.title) = LOWER(?) AND (s.id > ? OR (s.id = ? AND s.kind > ?))))"
    else:
        title_match = "s.title LIKE ?"
        order = "s.title COLLATE NOCASE, s.id, s.kind"
        predicate = "" if cursor is None else " AND (s.title COLLATE NOCASE > ? COLLATE NOCASE OR (s.title COLLATE NOCASE = ? COLLATE NOCASE AND (s.id > ? OR (s.id = ? AND s.kind > ?))))"
    if cursor:
        params.extend((cursor[0], cursor[0], cursor[1], cursor[1], cursor[2]))
    rows = _many(conn, f"""SELECT s.kind, s.id, s.title, s.work_id FROM (
        SELECT 'work' AS kind, id, title, id AS work_id FROM catalogue_works
        UNION ALL SELECT 'edition', id, title, work_id FROM catalogue_editions
        UNION ALL SELECT 'person', p.id, p.name, NULL FROM catalogue_people p
    ) s WHERE ({title_match} OR s.id IN (SELECT entity_id FROM catalogue_identifiers WHERE value {"ILIKE" if _is_postgres(conn) else "LIKE"} ?)
        OR (s.kind IN ('work','edition') AND s.id IN (SELECT ep.edition_id FROM catalogue_edition_people ep JOIN catalogue_people p ON p.id=ep.person_id WHERE p.name {"ILIKE" if _is_postgres(conn) else "LIKE"} ?)))
        {predicate} ORDER BY {order} LIMIT ?""", tuple(params + [limit + 1]))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1]["title"], rows[-1]["id"], rows[-1]["kind"]) if has_more and rows else None
    return {"items": rows, "next": next_cursor}
