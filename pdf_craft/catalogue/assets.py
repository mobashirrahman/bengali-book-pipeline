"""Content-addressed cover candidates and safe cover selection.

Remote candidates are metadata only until :func:`fetch_remote_cover` is
called.  The functions here accept a ``CatalogueDB`` so callers can use the
same transaction and SQLite connection as materialization.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .database import CatalogueDB

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
_SOURCE_PRIORITY = {"open_library": 40, "google_books": 35, "rokomari": 30}


class AssetError(ValueError):
    """Raised when a cover is unsafe or is not a supported image."""


@dataclass(frozen=True)
class AssetCandidate:
    id: int
    edition_id: int
    asset_type: str
    storage_uri: str
    source_url: str | None
    sha256: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    source_record_id: int | None
    attribution: str | None
    rights: str | None
    is_selected: bool
    status: str
    rank_score: float | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validated(row: Any) -> bool:
    return bool(
        row["sha256"]
        and row["mime_type"] in _IMAGE_MIMES
        and row["width"]
        and row["height"]
    )


def _row(row: Any) -> AssetCandidate:
    return AssetCandidate(
        id=int(row["id"]), edition_id=int(row["edition_id"]),
        asset_type=row["asset_type"], storage_uri=row["storage_uri"],
        source_url=row["source_url"], sha256=row["sha256"],
        mime_type=row["mime_type"], width=row["width"], height=row["height"],
        source_record_id=row["source_record_id"], attribution=row["attribution"],
        rights=row["rights"], is_selected=bool(row["is_selected"]),
        status=row["status"], rank_score=row["rank_score"],
    )


def _record_provenance(
    conn: Any,
    asset_id: int,
    *,
    source_record_id: int | None,
    source_url: str | None,
    attribution: str | None,
    rights: str | None,
    metadata: dict[str, object] | None,
) -> None:
    if source_record_id is None and source_url is None and attribution is None and rights is None:
        return
    conn.execute(
        """INSERT OR IGNORE INTO catalogue_asset_provenance
        (asset_id, source_record_id, source_url, attribution, rights, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            source_record_id,
            source_url,
            attribution,
            rights,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _image_info(data: bytes, mime: str | None = None) -> tuple[str, int, int]:
    """Return normalized MIME and dimensions using small header parsers."""
    if not data or data.lstrip().lower().startswith((b"<html", b"<!doctype", b"<head", b"<body")):
        raise AssetError("cover response is HTML or empty placeholder")
    detected: str | None = None
    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        detected, width, height = "image/png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    elif data.startswith(b"\xff\xd8"):
        detected = "image/jpeg"
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            pos += 2
            if marker in (0xD8, 0xD9):
                continue
            if pos + 2 > len(data):
                break
            length = int.from_bytes(data[pos:pos + 2], "big")
            if marker in range(0xC0, 0xC4) and pos + 7 < len(data):
                height = int.from_bytes(data[pos + 3:pos + 5], "big")
                width = int.from_bytes(data[pos + 5:pos + 7], "big")
                break
            pos += max(length, 2)
    elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        detected, width, height = "image/gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 16:
        detected = "image/webp"
        if data[12:16] == b"VP8X":
            if len(data) < 30:
                raise AssetError("invalid WebP VP8X header")
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif data[12:16] == b"VP8 " and len(data) >= 30:
            frame = data[20:]
            sync = frame.find(b"\x9d\x01\x2a")
            if sync >= 0 and len(frame) >= sync + 7:
                width = int.from_bytes(frame[sync + 3:sync + 5], "little") & 0x3FFF
                height = int.from_bytes(frame[sync + 5:sync + 7], "little") & 0x3FFF
        elif data[12:16] == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = data[21:25]
            width = 1 + (bits[0] | ((bits[1] & 0x3F) << 8))
            height = 1 + ((bits[1] >> 6) | (bits[2] << 2) | ((bits[3] & 0x0F) << 10))
    if detected is None or not width or not height:
        raise AssetError("cover is not a valid supported image")
    declared = (mime or "").split(";", 1)[0].strip().lower()
    if declared and declared not in _IMAGE_MIMES:
        raise AssetError(f"unsupported image content type: {declared}")
    if declared and declared != detected:
        raise AssetError(f"image MIME does not match content: {declared} vs {detected}")
    return detected, width, height


def _insert(conn: Any, *, edition_id: int, storage_uri: str, source_url: str | None,
            asset_type: str, sha256: str | None, mime_type: str | None,
            width: int | None, height: int | None, source_record_id: int | None,
            attribution: str | None, rights: str | None, metadata: dict[str, object] | None = None) -> int:
    if sha256:
        found = conn.execute(
            "SELECT id FROM catalogue_assets WHERE edition_id=? AND asset_type=? AND sha256=?",
            (edition_id, asset_type, sha256),
        ).fetchone()
        if found:
            _record_provenance(
                conn,
                int(found[0]),
                source_record_id=source_record_id,
                source_url=source_url,
                attribution=attribution,
                rights=rights,
                metadata=metadata,
            )
            return int(found[0])
    elif source_url:
        found = conn.execute(
            "SELECT id FROM catalogue_assets WHERE edition_id=? AND asset_type=? AND source_url=?",
            (edition_id, asset_type, source_url),
        ).fetchone()
        if found:
            _record_provenance(
                conn,
                int(found[0]),
                source_record_id=source_record_id,
                source_url=source_url,
                attribution=attribution,
                rights=rights,
                metadata=metadata,
            )
            return int(found[0])
    cur = conn.execute(
        """INSERT INTO catalogue_assets
        (edition_id, asset_type, storage_uri, source_url, sha256, mime_type, width, height,
         source_record_id, attribution, rights, retrieved_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (edition_id, asset_type, storage_uri, source_url, sha256, mime_type, width, height,
         source_record_id, attribution, rights, _now() if sha256 else None,
         json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)),
    )
    asset_id = int(cur.lastrowid)
    _record_provenance(
        conn,
        asset_id,
        source_record_id=source_record_id,
        source_url=source_url,
        attribution=attribution,
        rights=rights,
        metadata=metadata,
    )
    return asset_id


def register_remote_cover(db: CatalogueDB, edition_id: int, url: str, *, source_record_id: int | None = None,
                          attribution: str | None = None, rights: str | None = None,
                          asset_type: str = "cover", metadata: dict[str, object] | None = None) -> AssetCandidate:
    """Register a URL as a cover candidate without making a network request."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise AssetError("cover URL must use http or https")
    asset_id = _insert(db.conn, edition_id=edition_id, storage_uri="remote:" + url,
                       source_url=url, asset_type=asset_type, sha256=None, mime_type=None,
                       width=None, height=None, source_record_id=source_record_id,
                       attribution=attribution, rights=rights, metadata=metadata)
    db.conn.commit()
    return _row(db.conn.execute("SELECT * FROM catalogue_assets WHERE id=?", (asset_id,)).fetchone())


def register_local_bytes(db: CatalogueDB, edition_id: int, data: bytes, asset_root: str | Path, *,
                         source_record_id: int | None = None, attribution: str | None = None,
                         rights: str | None = None, asset_type: str = "cover",
                         source_url: str | None = None, mime_type: str | None = None,
                         metadata: dict[str, object] | None = None) -> AssetCandidate:
    """Store validated bytes below ``asset_root`` at a deterministic SHA path."""
    detected, width, height = _image_info(data, mime_type)
    digest = hashlib.sha256(data).hexdigest()
    suffix = mimetypes.guess_extension(detected) or ".bin"
    path = Path(asset_root) / "sha256" / digest[:2] / (digest + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise AssetError("content-addressed asset collision")
    if not path.exists():
        path.write_bytes(data)
    asset_id = _insert(db.conn, edition_id=edition_id, storage_uri=str(path), source_url=source_url,
                       asset_type=asset_type, sha256=digest, mime_type=detected, width=width,
                       height=height, source_record_id=source_record_id, attribution=attribution,
                       rights=rights, metadata=metadata)
    db.conn.commit()
    return _row(db.conn.execute("SELECT * FROM catalogue_assets WHERE id=?", (asset_id,)).fetchone())


def register_local_file(db: CatalogueDB, edition_id: int, path: str | Path, asset_root: str | Path, **kwargs: object) -> AssetCandidate:
    """Register a local image file using the same content-addressed path rule."""
    return register_local_bytes(db, edition_id, Path(path).read_bytes(), asset_root, **kwargs)


def fetch_remote_cover(db: CatalogueDB, candidate_id: int, asset_root: str | Path, *,
                       client: Any | None = None, max_bytes: int = 10 * 1024 * 1024,
                       timeout: float = 20.0) -> AssetCandidate:
    """Stream and validate a remote candidate through an injectable httpx client."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    row = db.conn.execute("SELECT * FROM catalogue_assets WHERE id=?", (candidate_id,)).fetchone()
    if not row or not row["source_url"]:
        raise AssetError("candidate has no remote URL")
    url = row["source_url"]
    if urlparse(url).scheme.lower() not in {"http", "https"}:
        raise AssetError("cover URL must use http or https")
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        with http.stream("GET", url, timeout=timeout) as response:
            response.raise_for_status()
            declared = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if declared and declared not in _IMAGE_MIMES:
                raise AssetError(f"unsupported image content type: {declared}")
            length = response.headers.get("content-length")
            if length and int(length) > max_bytes:
                raise AssetError("cover exceeds maximum size")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise AssetError("cover exceeds maximum size")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise AssetError(f"cover download failed: {exc}") from exc
    finally:
        if own:
            http.close()
    return register_local_bytes(db, int(row["edition_id"]), b"".join(chunks), asset_root,
                                source_record_id=row["source_record_id"], attribution=row["attribution"],
                                rights=row["rights"], asset_type=row["asset_type"], source_url=url,
                                mime_type=declared)


def rank_cover_candidates(db: CatalogueDB, edition_id: int, *, asset_type: str = "cover") -> list[AssetCandidate]:
    """Return candidates ordered by source trust, validity, size, then locality."""
    was_in_transaction = db.conn.in_transaction
    rows = db.conn.execute(
        """SELECT a.*, r.source FROM catalogue_assets a
           LEFT JOIN catalogue_source_records r ON r.id=a.source_record_id
           WHERE a.edition_id=? AND a.asset_type=? AND a.status='candidate'
             AND a.sha256 IS NOT NULL AND a.mime_type IN ('image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp')
             AND a.width > 0 AND a.height > 0""", (edition_id, asset_type)
    ).fetchall()
    def score(row: Any) -> float:
        source = _SOURCE_PRIORITY.get(row["source"], 0)
        valid = 30 if _validated(row) else 0
        pixels = min((row["width"] or 0) * (row["height"] or 0) / 1_000_000, 20)
        local = 10 if row["sha256"] else 0
        return source + valid + pixels + local
    ranked = sorted(rows, key=lambda r: (-score(r), int(r["id"])))
    for r in ranked:
        db.conn.execute("UPDATE catalogue_assets SET rank_score=? WHERE id=?", (score(r), r["id"]))
    if not was_in_transaction:
        db.conn.commit()
    return [replace(_row(r), rank_score=score(r)) for r in ranked]


def select_cover(db: CatalogueDB, edition_id: int, *, candidate_id: int | None = None,
                 asset_type: str = "cover", manual: bool = False, selected_by: str | None = None) -> AssetCandidate:
    """Select one candidate atomically, retaining every prior candidate."""
    conn = db.conn
    conn.execute("BEGIN")
    try:
        if candidate_id is None:
            candidates = rank_cover_candidates(db, edition_id, asset_type=asset_type)
            if not candidates:
                raise AssetError("no cover candidates available")
            candidate_id = candidates[0].id
        row = conn.execute("SELECT * FROM catalogue_assets WHERE id=? AND edition_id=? AND asset_type=?", (candidate_id, edition_id, asset_type)).fetchone()
        if not row:
            raise AssetError("cover candidate does not belong to edition")
        if not _validated(row):
            raise AssetError("cover candidate has not been validated or downloaded")
        previous = conn.execute(
            "SELECT * FROM catalogue_assets WHERE edition_id=? AND asset_type=? AND is_selected=1",
            (edition_id, asset_type),
        ).fetchone()
        if previous and not conn.execute(
            "SELECT 1 FROM catalogue_asset_selection_history WHERE asset_id=? AND selected_at=?",
            (previous["id"], previous["selected_at"]),
        ).fetchone():
            conn.execute(
                """INSERT INTO catalogue_asset_selection_history
                (asset_id, edition_id, asset_type, selected_at, selected_by, selection_method)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    previous["id"],
                    edition_id,
                    asset_type,
                    previous["selected_at"] or _now(),
                    previous["selected_by"],
                    previous["selection_method"] or "unknown",
                ),
            )
        conn.execute("UPDATE catalogue_assets SET is_selected=0 WHERE edition_id=? AND asset_type=?", (edition_id, asset_type))
        conn.execute("UPDATE catalogue_assets SET is_selected=1, selection_method=?, selected_at=?, selected_by=? WHERE id=?", ("manual" if manual else "ranked", _now(), selected_by, candidate_id))
        selected = conn.execute("SELECT * FROM catalogue_assets WHERE id=?", (candidate_id,)).fetchone()
        conn.execute(
            """INSERT INTO catalogue_asset_selection_history
            (asset_id, edition_id, asset_type, selected_at, selected_by, selection_method)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                candidate_id,
                edition_id,
                asset_type,
                selected["selected_at"],
                selected["selected_by"],
                selected["selection_method"],
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _row(conn.execute("SELECT * FROM catalogue_assets WHERE id=?", (candidate_id,)).fetchone())
