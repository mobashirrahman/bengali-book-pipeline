"""Content-addressed cover candidates and safe cover selection.

Remote candidates are metadata only until :func:`fetch_remote_cover` is
called.  The functions here accept a ``CatalogueDB`` so callers can use the
same transaction and SQLite connection as materialization.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import socket
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .database import CatalogueDB

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
_SOURCE_PRIORITY = {"open_library": 40, "google_books": 35, "rokomari": 30}
MIN_HIGH_RESOLUTION = (600, 900)
MIN_COVER_WIDTH, MIN_COVER_HEIGHT = MIN_HIGH_RESOLUTION
MAX_COVER_BYTES = 10 * 1024 * 1024
MAX_COVER_TIMEOUT = 60.0
MAX_COVER_BATCH_SIZE = 1000


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


@dataclass(frozen=True)
class CoverVerificationOutcome:
    candidate_id: int
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class CoverVerificationBatchResult:
    examined: int
    validated: int
    rejected: int
    selected: int
    next_after_id: int | None
    complete: bool
    outcomes: tuple[CoverVerificationOutcome, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validated(row: Any) -> bool:
    return bool(
        row["status"] != "rejected"
        and (row["status"] == "validated" or not row["source_url"])
        and row["sha256"]
        and row["mime_type"] in _IMAGE_MIMES
        and row["width"]
        and row["height"]
    )


def _safe_remote_url(url: str, *, resolve_host: bool = True) -> None:
    """Reject URL forms which could target local or private network services."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise AssetError("cover URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise AssetError("cover URL must use http or https")
    if parsed.username or parsed.password:
        raise AssetError("cover URL must not include credentials")
    hostname = hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise AssetError("cover URL targets a private network")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        addresses = []
        if resolve_host:
            try:
                addresses = [
                    ipaddress.ip_address(info[4][0])
                    for info in socket.getaddrinfo(
                        hostname,
                        port or (443 if parsed.scheme.lower() == "https" else 80),
                        type=socket.SOCK_STREAM,
                    )
                ]
            except (OSError, ValueError) as exc:
                raise AssetError("cover host could not be resolved safely") from exc
            if not addresses:
                raise AssetError("cover host has no public address")
    if any(not address.is_global for address in addresses):
        raise AssetError("cover URL targets a private network")


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
    existing = conn.execute(
        """SELECT id FROM catalogue_asset_provenance
           WHERE asset_id=? AND source_record_id IS ? AND source_url IS ?""",
        (asset_id, source_record_id, source_url),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE catalogue_asset_provenance
               SET attribution=?, rights=?, metadata_json=? WHERE id=?""",
            (
                attribution,
                rights,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                existing[0],
            ),
        )
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
                         metadata: dict[str, object] | None = None,
                         asset_id: int | None = None) -> AssetCandidate:
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
    if asset_id is None:
        asset_id = _insert(db.conn, edition_id=edition_id, storage_uri=str(path), source_url=source_url,
                           asset_type=asset_type, sha256=digest, mime_type=detected, width=width,
                           height=height, source_record_id=source_record_id, attribution=attribution,
                           rights=rights, metadata=metadata)
    else:
        existing = db.conn.execute(
            "SELECT id, edition_id, asset_type FROM catalogue_assets WHERE id=?",
            (asset_id,),
        ).fetchone()
        if not existing or int(existing["edition_id"]) != edition_id or existing["asset_type"] != asset_type:
            raise AssetError("asset does not belong to edition")
        db.conn.execute(
            """UPDATE catalogue_assets
               SET storage_uri=?, source_url=COALESCE(?, source_url), sha256=?, mime_type=?,
                   width=?, height=?, source_record_id=COALESCE(?, source_record_id),
                   attribution=COALESCE(?, attribution), rights=COALESCE(?, rights),
                   retrieved_at=?, metadata_json=?, status='validated'
               WHERE id=?""",
            (str(path), source_url, digest, detected, width, height, source_record_id,
             attribution, rights, _now(), json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), asset_id),
        )
    db.conn.execute(
        "UPDATE catalogue_assets SET status='validated' WHERE id=? AND status != 'rejected'",
        (asset_id,),
    )
    db.conn.commit()
    return _row(db.conn.execute("SELECT * FROM catalogue_assets WHERE id=?", (asset_id,)).fetchone())


def register_local_file(db: CatalogueDB, edition_id: int, path: str | Path, asset_root: str | Path, **kwargs: object) -> AssetCandidate:
    """Register a local image file using the same content-addressed path rule."""
    return register_local_bytes(db, edition_id, Path(path).read_bytes(), asset_root, **kwargs)


def fetch_remote_cover(db: CatalogueDB, candidate_id: int, asset_root: str | Path, *,
                       client: Any | None = None, max_bytes: int = MAX_COVER_BYTES,
                       timeout: float = 20.0, min_width: int = 0,
                       min_height: int = 0) -> AssetCandidate:
    """Stream and validate a remote candidate through an injectable httpx client."""
    if max_bytes < 1 or max_bytes > MAX_COVER_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_COVER_BYTES}")
    if timeout <= 0 or timeout > MAX_COVER_TIMEOUT:
        raise ValueError(f"timeout must be between 0 and {MAX_COVER_TIMEOUT} seconds")
    row = db.conn.execute("SELECT * FROM catalogue_assets WHERE id=?", (candidate_id,)).fetchone()
    if not row or not row["source_url"]:
        raise AssetError("candidate has no remote URL")
    source_url = row["source_url"]
    _safe_remote_url(source_url, resolve_host=client is None)
    if row["status"] == "validated" and row["storage_uri"] and not str(row["storage_uri"]).startswith("remote:"):
        existing = Path(str(row["storage_uri"]))
        if existing.is_file():
            if existing.stat().st_size > max_bytes:
                raise AssetError("cover exceeds maximum size")
            if (row["width"] or 0) < min_width or (row["height"] or 0) < min_height:
                raise AssetError(
                    f"cover dimensions {row['width']}x{row['height']} are below minimum {min_width}x{min_height}"
                )
            return _row(row)
    own = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=False)
    current_url = source_url
    response_data: bytes | None = None
    declared: str | None = None
    try:
        for redirect_count in range(4):
            _safe_remote_url(current_url, resolve_host=client is None)
            with http.stream("GET", current_url, timeout=timeout, follow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location:
                        raise AssetError("cover redirect has no location")
                    if redirect_count == 3:
                        raise AssetError("too many cover redirects")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                declared = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if declared and declared not in _IMAGE_MIMES:
                    raise AssetError(f"unsupported image content type: {declared}")
                length = response.headers.get("content-length")
                if length:
                    try:
                        declared_length = int(length)
                    except ValueError as exc:
                        raise AssetError("invalid cover content length") from exc
                    if declared_length < 0 or declared_length > max_bytes:
                        raise AssetError("cover exceeds maximum size")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise AssetError("cover exceeds maximum size")
                    chunks.append(chunk)
                response_data = b"".join(chunks)
                break
    except httpx.HTTPError as exc:
        raise AssetError(f"cover download failed: {exc}") from exc
    finally:
        if own:
            http.close()
    if response_data is None:
        raise AssetError("cover redirect did not produce an image")
    detected, width, height = _image_info(response_data, declared)
    if width < min_width or height < min_height:
        raise AssetError(
            f"cover dimensions {width}x{height} are below minimum {min_width}x{min_height}"
        )
    digest = hashlib.sha256(response_data).hexdigest()
    verified_at = _now()
    metadata = _metadata(row["metadata_json"])
    verification = {
        "status": "validated",
        "mime_type": detected,
        "width": width,
        "height": height,
        "sha256": digest,
        "verified_at": verified_at,
        "source_url": source_url,
        "final_url": current_url,
    }
    metadata["verification"] = verification
    fetched = register_local_bytes(
        db,
        int(row["edition_id"]),
        response_data,
        asset_root,
        source_record_id=row["source_record_id"],
        attribution=row["attribution"],
        rights=row["rights"],
        asset_type=row["asset_type"],
        source_url=source_url,
        mime_type=detected,
        metadata=metadata,
        asset_id=candidate_id,
    )
    _record_provenance(db.conn, candidate_id, source_record_id=row["source_record_id"],
                       source_url=source_url, attribution=row["attribution"],
                       rights=row["rights"], metadata=verification)
    db.conn.execute(
        """UPDATE catalogue_asset_provenance SET metadata_json=?
           WHERE asset_id=? AND source_url=?""",
        (json.dumps(verification, ensure_ascii=False, sort_keys=True), candidate_id, source_url),
    )
    db.conn.commit()
    return fetched


def _metadata(value: Any) -> dict[str, object]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = {}
    else:
        decoded = value
    return dict(decoded) if isinstance(decoded, dict) else {}


def _record_verification_failure(db: CatalogueDB, row: Any, reason: str) -> None:
    observed_at = _now()
    verification = {"status": "rejected", "reason": reason, "verified_at": observed_at,
                    "source_url": row["source_url"]}
    metadata = _metadata(row["metadata_json"])
    metadata["verification"] = verification
    db.conn.execute(
        "UPDATE catalogue_assets SET status='rejected', retrieved_at=?, metadata_json=? WHERE id=?",
        (observed_at, json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["id"]),
    )
    _record_provenance(
        db.conn, int(row["id"]), source_record_id=row["source_record_id"],
        source_url=row["source_url"], attribution=row["attribution"], rights=row["rights"],
        metadata=verification,
    )
    db.conn.execute(
        """UPDATE catalogue_asset_provenance SET metadata_json=?
           WHERE asset_id=? AND source_url=?""",
        (json.dumps(verification, ensure_ascii=False, sort_keys=True), row["id"], row["source_url"]),
    )
    db.conn.commit()


def verify_cover_batch(
    db: CatalogueDB,
    asset_root: str | Path,
    *,
    limit: int = 100,
    after_id: int | None = None,
    client: Any | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    timeout: float = 20.0,
    min_width: int = MIN_COVER_WIDTH,
    min_height: int = MIN_COVER_HEIGHT,
) -> CoverVerificationBatchResult:
    """Verify a bounded, resumable batch of already-registered cover URLs."""
    if limit < 1 or limit > MAX_COVER_BATCH_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_COVER_BATCH_SIZE}")
    if after_id is not None and after_id < 0:
        raise ValueError("after_id must be non-negative")
    if min_width < 1 or min_height < 1:
        raise ValueError("cover dimensions must be positive")
    rows = db.conn.execute(
        """SELECT * FROM catalogue_assets
           WHERE asset_type='cover' AND status='candidate' AND source_url IS NOT NULL
             AND storage_uri LIKE 'remote:%' AND id>?
           ORDER BY id LIMIT ?""",
        (after_id or 0, limit),
    ).fetchall()
    outcomes: list[CoverVerificationOutcome] = []
    verified_editions: set[int] = set()
    for row in rows:
        try:
            fetch_remote_cover(
                db, int(row["id"]), asset_root, client=client, max_bytes=max_bytes,
                timeout=timeout, min_width=min_width, min_height=min_height,
            )
        except AssetError as exc:
            _record_verification_failure(db, row, str(exc))
            outcomes.append(CoverVerificationOutcome(int(row["id"]), "rejected", str(exc)))
        else:
            verified_editions.add(int(row["edition_id"]))
            outcomes.append(CoverVerificationOutcome(int(row["id"]), "validated"))
    selected = 0
    for edition_id in sorted(verified_editions):
        manual = db.conn.execute(
            "SELECT 1 FROM catalogue_assets WHERE edition_id=? AND is_selected=1 AND selection_method='manual'",
            (edition_id,),
        ).fetchone()
        if manual:
            continue
        try:
            select_cover(db, edition_id, selected_by="cover-verifier")
        except AssetError:
            # A validated candidate should normally be selectable, but keep a
            # verification batch resumable if a concurrent catalogue edit
            # removes the final eligible candidate.
            continue
        selected += 1
    next_id = int(rows[-1]["id"]) if rows else after_id
    remaining = False
    if next_id is not None:
        remaining = db.conn.execute(
            """SELECT 1 FROM catalogue_assets
               WHERE asset_type='cover' AND status='candidate' AND source_url IS NOT NULL
                 AND storage_uri LIKE 'remote:%' AND id>? LIMIT 1""",
            (next_id,),
        ).fetchone() is not None
    return CoverVerificationBatchResult(
        examined=len(rows), validated=sum(item.status == "validated" for item in outcomes),
        rejected=sum(item.status == "rejected" for item in outcomes), selected=selected,
        next_after_id=next_id,
        complete=not remaining, outcomes=tuple(outcomes),
    )


def rank_cover_candidates(db: CatalogueDB, edition_id: int, *, asset_type: str = "cover") -> list[AssetCandidate]:
    """Return candidates ordered by source trust, validity, size, then locality."""
    was_in_transaction = db.conn.in_transaction
    rows = db.conn.execute(
        """SELECT a.*, r.source FROM catalogue_assets a
           LEFT JOIN catalogue_source_records r ON r.id=a.source_record_id
           WHERE a.edition_id=? AND a.asset_type=? AND a.status IN ('candidate', 'validated')
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
