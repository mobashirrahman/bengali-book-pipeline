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
from .ratings import community_rating as _community_rating
from .ratings import get_user_rating as _get_user_rating


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


_ASSET_FIELDS = """id, edition_id, document_id, asset_type, storage_uri, source_url,
    sha256, mime_type, width, height, attribution, rights, is_selected, status,
    retrieved_at, metadata_json,
    CASE
        WHEN status='rejected' THEN 'rejected'
        WHEN sha256 IS NOT NULL AND mime_type IN ('image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp')
             AND width > 0 AND height > 0 AND storage_uri NOT LIKE 'remote:%' THEN 'validated'
        ELSE 'candidate'
    END AS verification_status"""


def _assets(conn: Any, edition_id: int | None = None, document_id: int | None = None) -> list[dict[str, Any]]:
    if edition_id is not None:
        return _many(conn, f"SELECT {_ASSET_FIELDS} FROM catalogue_assets WHERE edition_id=? ORDER BY is_selected DESC, id", (edition_id,))
    return _many(conn, f"SELECT {_ASSET_FIELDS} FROM catalogue_assets WHERE document_id=? ORDER BY is_selected DESC, id", (document_id,))


def get_work(conn: Any, work_id: int) -> dict[str, Any] | None:
    work = _one(conn, "SELECT * FROM catalogue_works WHERE id=?", (work_id,))
    if not work:
        return None
    editions = get_editions_for_work(conn, work_id)
    sources = _many(conn, """SELECT DISTINCT sr.source FROM catalogue_source_records sr
        JOIN catalogue_source_record_editions sre ON sre.source_record_id=sr.id
        JOIN catalogue_editions e ON e.id=sre.edition_id WHERE e.work_id=? ORDER BY sr.source""", (work_id,))
    return {**work, "identifiers": _identifiers(conn, "work", work_id), "editions": editions,
            "sources": [row["source"] for row in sources], "ratings": get_ratings(conn, work_id)}


def list_works(conn: Any, limit: int, after: int | None = None, has_documents: bool = False) -> list[dict[str, Any]]:
    # has_documents restricts the listing to works with at least one accepted
    # local document, so readers can bootstrap shelves from readable works
    # instead of the lowest-id metadata-only records.
    conditions: list[str] = []
    params: list[Any] = []
    if after is not None:
        conditions.append("catalogue_works.id>?")
        params.append(after)
    if has_documents:
        conditions.append("""EXISTS (SELECT 1 FROM catalogue_editions e
            JOIN catalogue_document_matches m ON m.edition_id=e.id AND m.status='accepted'
            WHERE e.work_id=catalogue_works.id)""")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = _many(conn, f"SELECT * FROM catalogue_works {where} ORDER BY id LIMIT ?", tuple(params + [limit]))
    return [{**row, "identifiers": _identifiers(conn, "work", row["id"]), "editions": [], "sources": [],
             "ratings": get_ratings(conn, row["id"])} for row in rows]


def _external_ratings(conn: Any, work_id: int) -> list[dict[str, Any]]:
    rows = _many(conn, """SELECT r.provider, r.value, r.scale, r.rating_count,
        r.review_count, r.source_url
        FROM catalogue_external_ratings r
        JOIN catalogue_editions e ON e.id=r.edition_id
        WHERE e.work_id=? ORDER BY r.provider, r.id""", (work_id,))
    ratings = [{
        "provider": row["provider"],
        "rating_value": float(row["value"]),
        "scale_max": float(row["scale"]),
        "rating_count": row["rating_count"],
        "review_count": row["review_count"],
        "source_url": row["source_url"],
        "status": "available",
        "reason": None,
    } for row in rows]
    if not any(row["provider"] == "goodreads" for row in ratings):
        ratings.append({
            "provider": "goodreads",
            "rating_value": None,
            "scale_max": 5.0,
            "rating_count": None,
            "review_count": None,
            "source_url": None,
            "status": "unavailable",
            "reason": "not_imported",
        })
    return ratings


def get_ratings(conn: Any, work_id: int, user_subject: str | None = None) -> dict[str, Any]:
    community = _community_rating(conn, work_id)
    community["user_rating"] = _get_user_rating(conn, work_id, user_subject)
    return {"work_id": work_id, "community": community, "external": _external_ratings(conn, work_id)}


def work_exists(conn: Any, work_id: int) -> bool:
    return _one(conn, "SELECT id FROM catalogue_works WHERE id=?", (work_id,)) is not None


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
    locations = _many(conn, """SELECT id, source_path, file_size, media_type,
        discovered_at, last_seen_at, updated_at
        FROM catalogue_document_locations WHERE document_id=? ORDER BY source_path, id""", (document_id,))
    return {
        **document,
        "matches": matches,
        "assets": _assets(conn, document_id=document_id),
        "artifacts": artifacts,
        "locations": locations,
    }


def get_asset(conn: Any, asset_id: int) -> dict[str, Any] | None:
    return _one(conn, f"SELECT {_ASSET_FIELDS} FROM catalogue_assets WHERE id=?", (asset_id,))


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
    counts["readable_works"] = _execute(
        conn, """SELECT COUNT(DISTINCT e.work_id) AS count FROM catalogue_editions e
            JOIN catalogue_document_matches m ON m.edition_id=e.id AND m.status='accepted'"""
    ).fetchone()["count" if _is_postgres(conn) else 0]
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


def search(
    conn: Any, query: str, limit: int, after: str | None = None, has_documents: bool = False
) -> dict[str, Any]:
    # Each entity kind filters inside its own UNION branch rather than sharing
    # one predicate over the combined rows.  A shared predicate compared the
    # union's bare `id` against identifier and edition-people ids regardless of
    # kind, so a work matched whenever its id happened to equal an unrelated
    # edition's id -- searching an author name returned arbitrary books.
    # Filtering per branch also keeps the non-correlated IN subqueries, so the
    # union stays small enough to sort by title without scanning the catalogue.
    cursor = _decode_cursor(after)
    needle = f"%{query}%"
    like = "ILIKE" if _is_postgres(conn) else "LIKE"
    if _is_postgres(conn):
        order = "LOWER(s.title), s.id, s.kind"
        predicate = "" if cursor is None else "(LOWER(s.title) > LOWER(?) OR (LOWER(s.title) = LOWER(?) AND (s.id > ? OR (s.id = ? AND s.kind > ?))))"
    else:
        order = "s.title COLLATE NOCASE, s.id, s.kind"
        predicate = "" if cursor is None else "(s.title COLLATE NOCASE > ? COLLATE NOCASE OR (s.title COLLATE NOCASE = ? COLLATE NOCASE AND (s.id > ? OR (s.id = ? AND s.kind > ?))))"

    # has_documents mirrors the /v2/works filter: only works and editions that
    # resolve to an accepted local document are returned.  People can never
    # carry a document, so that branch drops out entirely.
    work_readable = """ AND EXISTS (SELECT 1 FROM catalogue_editions de
        JOIN catalogue_document_matches dm ON dm.edition_id=de.id AND dm.status='accepted'
        WHERE de.work_id=w.id)""" if has_documents else ""
    edition_readable = """ AND EXISTS (SELECT 1 FROM catalogue_document_matches dm
        WHERE dm.edition_id=e.id AND dm.status='accepted')""" if has_documents else ""

    branches = [
        f"""SELECT 'work' AS kind, w.id, w.title, w.id AS work_id FROM catalogue_works w
            WHERE (w.title {like} ?
                OR w.id IN (SELECT entity_id FROM catalogue_identifiers
                            WHERE entity_type='work' AND value {like} ?)
                OR w.id IN (SELECT e2.work_id FROM catalogue_editions e2
                            JOIN catalogue_edition_people ep ON ep.edition_id=e2.id
                            JOIN catalogue_people p ON p.id=ep.person_id
                            WHERE p.name {like} ?)){work_readable}""",
        f"""SELECT 'edition' AS kind, e.id, e.title, e.work_id FROM catalogue_editions e
            WHERE (e.title {like} ?
                OR e.id IN (SELECT entity_id FROM catalogue_identifiers
                            WHERE entity_type='edition' AND value {like} ?)
                OR e.id IN (SELECT ep.edition_id FROM catalogue_edition_people ep
                            JOIN catalogue_people p ON p.id=ep.person_id
                            WHERE p.name {like} ?)){edition_readable}""",
    ]
    params: list[Any] = [needle] * 6
    if not has_documents:
        branches.append(f"SELECT 'person' AS kind, p.id, p.name AS title, NULL AS work_id FROM catalogue_people p WHERE p.name {like} ?")
        params.append(needle)
    if cursor:
        params.extend((cursor[0], cursor[0], cursor[1], cursor[1], cursor[2]))

    union = " UNION ALL ".join(branches)
    where = f"WHERE {predicate} " if predicate else ""
    rows = _many(conn, f"""SELECT s.kind, s.id, s.title, s.work_id FROM (
        {union}
    ) s {where}ORDER BY {order} LIMIT ?""", tuple(params + [limit + 1]))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1]["title"], rows[-1]["id"], rows[-1]["kind"]) if has_more and rows else None
    return {"items": rows, "next": next_cursor}
