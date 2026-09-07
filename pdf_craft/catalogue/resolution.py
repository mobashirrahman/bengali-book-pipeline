"""Reviewable v2 source-to-document resolution operations."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from .database import CatalogueDB
from .isbn import normalize_isbn
from .matching import fuzzy_match_score, infer_author_from_path, infer_title_from_path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _source_fields(raw: dict[str, object], title: str | None) -> tuple[str, str, list[str]]:
    info = raw.get("volumeInfo") if isinstance(raw.get("volumeInfo"), dict) else raw
    assert isinstance(info, dict)
    source_title = str(info.get("title") or title or "").strip()
    authors = info.get("authors") or info.get("author") or []
    if isinstance(authors, str):
        authors = [authors]
    authors = [str(author).strip() for author in authors if str(author).strip()] if isinstance(authors, list) else []
    identifiers = info.get("industryIdentifiers") or []
    isbns: list[str] = []
    if isinstance(identifiers, list):
        for identifier in identifiers:
            if isinstance(identifier, dict) and identifier.get("identifier"):
                try:
                    isbns.append(normalize_isbn(str(identifier["identifier"])))
                except ValueError:
                    continue
    for key in ("isbn", "ISBN"):
        if info.get(key):
            try:
                isbns.append(normalize_isbn(str(info[key])))
            except ValueError:
                pass
    return source_title, " ".join(authors), sorted(set(isbns))


def _ensure_edition(db: CatalogueDB, record_id: int, title: str, raw: dict[str, object], authors: str, isbns: list[str]) -> int:
    row = db.conn.execute("SELECT edition_id FROM catalogue_source_record_editions WHERE source_record_id=?", (record_id,)).fetchone()
    if row:
        return int(row[0])
    cur = db.conn.execute("INSERT INTO catalogue_editions (title, publisher, language) VALUES (?, ?, ?)", (title, raw.get("volumeInfo", {}).get("publisher") if isinstance(raw.get("volumeInfo"), dict) else None, raw.get("volumeInfo", {}).get("language") if isinstance(raw.get("volumeInfo"), dict) else None))
    edition_id = int(cur.lastrowid)
    db.conn.execute("INSERT INTO catalogue_source_record_editions (source_record_id, edition_id) VALUES (?, ?)", (record_id, edition_id))
    for isbn in isbns:
        db.conn.execute("INSERT OR IGNORE INTO catalogue_identifiers (entity_type, entity_id, namespace, value, normalized_value) VALUES ('edition', ?, 'isbn', ?, ?)", (edition_id, isbn, isbn))
    for position, author in enumerate(authors.split(" | ") if " | " in authors else ([authors] if authors else [])):
        normalized = re.sub(r"\s+", " ", author).strip().lower()
        person = db.conn.execute("SELECT id FROM catalogue_people WHERE normalized_name=?", (normalized,)).fetchone()
        if not person:
            person = (db.conn.execute("INSERT INTO catalogue_people (name, normalized_name) VALUES (?, ?)", (author, normalized)).lastrowid,)
        db.conn.execute("INSERT OR IGNORE INTO catalogue_edition_people (edition_id, person_id, role, position) VALUES (?, ?, 'author', ?)", (edition_id, person[0], position))
    db.conn.commit()
    return edition_id


def generate_candidates(db: CatalogueDB, document_id: int) -> list[int]:
    """Persist pending candidates for a local document and return their IDs."""
    document = db.conn.execute("SELECT * FROM catalogue_local_documents WHERE id=?", (document_id,)).fetchone()
    if not document:
        raise ValueError(f"unknown local document: {document_id}")
    metadata = json.loads(document["metadata_json"] or "{}")
    local_title = str(metadata.get("title") or infer_title_from_path(document["source_path"])).strip()
    local_author = str(metadata.get("author") or infer_author_from_path(document["source_path"])).strip()
    local_isbns: set[str] = set()
    for value in ([metadata.get("isbn")] if metadata.get("isbn") else []) + list(metadata.get("isbns", [])):
        try:
            local_isbns.add(normalize_isbn(str(value)))
        except (ValueError, TypeError):
            continue
    candidates: list[int] = []
    rows = db.conn.execute("SELECT id, title, raw_json FROM catalogue_source_records ORDER BY id").fetchall()
    for record in rows:
        raw = json.loads(record["raw_json"])
        title, author, isbns = _source_fields(raw, record["title"])
        shared = sorted(local_isbns.intersection(isbns))
        if shared:
            score, method, evidence = 1.0, "isbn", {"isbn": shared, "signals": ["exact_isbn"]}
        else:
            title_score = fuzzy_match_score(local_title, title)
            author_score = fuzzy_match_score(local_author, author) if local_author and author else 0.0
            if title_score < 0.75 or (local_author and author and author_score < 0.55):
                continue
            score = min(1.0, 0.75 * title_score + (0.25 * author_score if local_author and author else 0.0))
            method = "title_author" if local_author and author else "title"
            evidence = {"title_score": title_score, "author_score": author_score, "signals": [method]}
        edition_id = _ensure_edition(db, int(record["id"]), title, raw, author, isbns)
        existing = db.conn.execute("SELECT id, status FROM catalogue_document_matches WHERE document_id=? AND edition_id=?", (document_id, edition_id)).fetchone()
        if existing:
            candidates.append(int(existing[0]))
            continue
        cur = db.conn.execute("INSERT INTO catalogue_document_matches (document_id, edition_id, score, method, status, evidence_json) VALUES (?, ?, ?, ?, 'candidate', ?)", (document_id, edition_id, score, method, json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
        candidates.append(int(cur.lastrowid))
    db.conn.commit()
    return candidates


def review_match(db: CatalogueDB, match_id: int, *, reviewer: str, decision: str, reason: str) -> None:
    """Accept or reject one pending candidate in a guarded transaction."""
    if decision not in {"accepted", "rejected"} or not reviewer.strip() or not reason.strip():
        raise ValueError("decision must be accepted/rejected with reviewer and reason")
    conn = db.conn
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT document_id, status FROM catalogue_document_matches WHERE id=?", (match_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown match: {match_id}")
        if row[1] != "candidate":
            raise ValueError("only pending candidates can be reviewed")
        if decision == "accepted" and conn.execute("SELECT 1 FROM catalogue_document_matches WHERE document_id=? AND status='accepted'", (row[0],)).fetchone():
            raise ValueError("local document already has an accepted edition")
        conn.execute("UPDATE catalogue_document_matches SET status=?, reviewer=?, review_reason=?, reviewed_at=? WHERE id=? AND status='candidate'", (decision, reviewer, reason, _now(), match_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def accept_match(db: CatalogueDB, match_id: int, *, reviewer: str, reason: str) -> None:
    review_match(db, match_id, reviewer=reviewer, decision="accepted", reason=reason)


def reject_match(db: CatalogueDB, match_id: int, *, reviewer: str, reason: str) -> None:
    review_match(db, match_id, reviewer=reviewer, decision="rejected", reason=reason)
