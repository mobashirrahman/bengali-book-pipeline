"""External-rating import and community-rating persistence helpers."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any


def _is_postgres(conn: Any) -> bool:
    return hasattr(conn, "info")


def _sql(conn: Any, sql: str) -> str:
    return sql.replace("?", "%s") if _is_postgres(conn) else sql


def _execute(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(_sql(conn, sql), params)


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _number(value: object, *, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _count(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.startswith(("https://", "http://")) else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _rating_from_record(source: str, raw: dict[str, object]) -> dict[str, object] | None:
    """Extract only explicitly supplied, validated source values.

    Goodreads is intentionally accepted only in the documented flat shape or
    the same shape under a ``goodreads`` object.  No title matching or other
    source's value is used as a fallback.
    """
    if source == "rokomari":
        value = _number(raw.get("ratingValue"), maximum=5)
        if value is None:
            return None
        return {
            "value": value,
            "scale": 5.0,
            "rating_count": _count(raw.get("ratingCount")),
            "review_count": _count(raw.get("reviewCount")),
            "source_url": _url(raw.get("url")),
            "external_id": _text(raw.get("id")) or _text(raw.get("productId")),
            "fields": ("ratingValue", "ratingCount", "reviewCount"),
        }
    if source == "google_books":
        value = raw.get("volumeInfo")
        if not isinstance(value, dict):
            return None
        average = _number(value.get("averageRating"), maximum=5)
        if average is None:
            return None
        return {
            "value": average,
            "scale": 5.0,
            "rating_count": _count(value.get("ratingsCount")),
            "review_count": None,
            "source_url": _url(raw.get("selfLink")) or _url(value.get("infoLink")),
            "external_id": _text(raw.get("id")),
            "fields": ("volumeInfo.averageRating", "volumeInfo.ratingsCount"),
        }
    if source == "goodreads":
        value: object = raw.get("goodreads", raw)
        if not isinstance(value, dict):
            return None
        average = _number(value.get("average_rating"), maximum=5)
        ratings_count = _count(value.get("ratings_count"))
        if average is None or ratings_count is None:
            return None
        return {
            "value": average,
            "scale": 5.0,
            "rating_count": ratings_count,
            "review_count": _count(value.get("reviews_count")),
            "source_url": _url(value.get("url") or value.get("link")),
            "external_id": _text(value.get("id")) or _text(raw.get("id")),
            "fields": ("average_rating", "ratings_count", "reviews_count"),
        }
    return None


def _upsert_external_rating(conn: Any, row: Any, rating: dict[str, object]) -> bool:
    source_record_id = int(_row_value(row, "id"))
    edition_id = int(_row_value(row, "edition_id"))
    provider = str(_row_value(row, "source"))
    observed_at = _text(_row_value(row, "fetched_at")) or _now()
    metadata = json.dumps({
        "source_record_id": source_record_id,
        "snapshot_id": _row_value(row, "snapshot_id"),
        "record_type": _row_value(row, "record_type"),
        "fields": rating["fields"],
    }, ensure_ascii=False, sort_keys=True)
    values = (
        provider,
        edition_id,
        source_record_id,
        rating.get("external_id"),
        rating["value"],
        rating["scale"],
        rating.get("rating_count"),
        rating.get("review_count"),
        rating.get("source_url"),
        observed_at,
        observed_at,
        metadata,
    )
    existing = _execute(
        conn,
        "SELECT id, provider, edition_id, source_record_id, external_id, value, scale, "
        "rating_count, review_count, source_url, observed_at, retrieved_at, metadata_json "
        "FROM catalogue_external_ratings WHERE provider=? AND edition_id=? AND source_record_id=?",
        (provider, edition_id, source_record_id),
    ).fetchone()
    if existing is not None:
        old = tuple(_row_value(existing, key, index) for index, key in enumerate((
            "id", "provider", "edition_id", "source_record_id", "external_id", "value", "scale",
            "rating_count", "review_count", "source_url", "observed_at", "retrieved_at", "metadata_json",
        )))
        if old[1:] == values:
            return False
        _execute(conn, """UPDATE catalogue_external_ratings SET
            external_id=?, value=?, scale=?, rating_count=?, review_count=?, source_url=?,
            observed_at=?, retrieved_at=?, metadata_json=? WHERE id=?""", values[3:] + (old[0],))
        return True
    _execute(conn, """INSERT INTO catalogue_external_ratings
        (provider, edition_id, source_record_id, external_id, value, scale, rating_count,
         review_count, source_url, observed_at, retrieved_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)
    return True


def materialize_external_ratings(
    db: Any,
    *,
    source: str | None = None,
    batch_size: int = 500,
) -> dict[str, int]:
    """Materialize supported ratings from staged records in bounded batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    conn = db.conn
    where = " AND sr.source=?" if source else ""
    total = int(_execute(
        conn,
        "SELECT COUNT(*) FROM catalogue_source_records sr "
        "JOIN catalogue_source_record_editions sre ON sre.source_record_id=sr.id WHERE 1=1" + where,
        (source,) if source else (),
    ).fetchone()[0 if not _is_postgres(conn) else "count"])
    report = {"seen": total, "materialized": 0, "skipped": 0}
    cursor = 0
    while True:
        rows = _execute(conn, """SELECT sr.id, sr.source, sr.external_id, sr.record_type,
            sr.raw_json, sr.snapshot_id, sre.edition_id, ss.fetched_at
            FROM catalogue_source_records sr
            JOIN catalogue_source_record_editions sre ON sre.source_record_id=sr.id
            LEFT JOIN catalogue_source_snapshots ss ON ss.id=sr.snapshot_id
            WHERE sr.id>?""" + (" AND sr.source=?" if source else "") +
            " ORDER BY sr.id LIMIT ?", ((cursor, source, batch_size) if source else (cursor, batch_size))).fetchall()
        if not rows:
            break
        try:
            for row in rows:
                try:
                    raw = json.loads(_row_value(row, "raw_json"))
                    if not isinstance(raw, dict):
                        report["skipped"] += 1
                        continue
                    rating = _rating_from_record(str(_row_value(row, "source")), raw)
                    if rating is None:
                        report["skipped"] += 1
                        continue
                    if _upsert_external_rating(conn, row, rating):
                        report["materialized"] += 1
                except (TypeError, ValueError, json.JSONDecodeError):
                    report["skipped"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        cursor = int(_row_value(rows[-1], "id"))
    return report


def community_rating(conn: Any, work_id: int) -> dict[str, object]:
    row = _execute(
        conn,
        "SELECT AVG(rating) AS average, COUNT(*) AS count FROM catalogue_user_ratings WHERE work_id=?",
        (work_id,),
    ).fetchone()
    average = _row_value(row, "average", 0)
    count = _row_value(row, "count", 0)
    return {"average": float(average) if average is not None else None, "count": int(count or 0)}


def get_user_rating(conn: Any, work_id: int, user_subject: str | None) -> int | None:
    if not user_subject:
        return None
    row = _execute(conn, "SELECT rating FROM catalogue_user_ratings WHERE work_id=? AND user_subject=?", (work_id, user_subject)).fetchone()
    return int(_row_value(row, "rating")) if row is not None else None


def upsert_user_rating(conn: Any, work_id: int, user_subject: str, rating: int) -> None:
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        raise ValueError("rating must be an integer between 1 and 5")
    now = _now()
    _execute(conn, """INSERT INTO catalogue_user_ratings
        (work_id, user_subject, rating, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(work_id, user_subject) DO UPDATE SET
            rating=excluded.rating,
            updated_at=excluded.updated_at""", (work_id, user_subject, rating, now, now))


def delete_user_rating(conn: Any, work_id: int, user_subject: str) -> bool:
    cur = _execute(conn, "DELETE FROM catalogue_user_ratings WHERE work_id=? AND user_subject=?", (work_id, user_subject))
    return cur.rowcount > 0


__all__ = [
    "community_rating",
    "delete_user_rating",
    "get_user_rating",
    "materialize_external_ratings",
    "upsert_user_rating",
]
