"""Reviewable v2 source-to-document resolution operations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from rapidfuzz import fuzz

from .database import CatalogueDB
from .isbn import normalize_isbn
from .matching import (
    fuzzy_match_score,
    infer_author_from_path,
    infer_title_from_path,
    normalize_bengali,
)

MATCHER_VERSION = "catalogue-resolution-v2"
_FUZZY_POOL_LIMIT = 100
_FUZZY_PERSIST_LIMIT = 10
_EXACT_POOL_LIMIT = 100


@dataclass(frozen=True)
class _CanonicalEdition:
    id: int
    title: str
    authors: tuple[str, ...]
    normalized_authors: tuple[str, ...]
    isbns: tuple[str, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def generate_candidates(db: CatalogueDB, document_id: int) -> list[int]:
    """Persist reviewable candidates from canonical catalogue entities only."""
    document = db.conn.execute("SELECT * FROM catalogue_local_documents WHERE id=?", (document_id,)).fetchone()
    if not document:
        raise ValueError(f"unknown local document: {document_id}")
    local_title, local_authors, local_isbns = _document_fields(document)
    title_key = _normalized_name(local_title)
    author_keys = {_normalized_name(author) for author in local_authors if _normalized_name(author)}
    exact_isbn_ids = _ids_by_isbn(db, local_isbns)
    title_ids = _ids_by_title(db, title_key, local_title)
    exact_ids = list(dict.fromkeys(exact_isbn_ids + title_ids))
    canonical = _load_canonical_editions(db, exact_ids)
    candidates: list[int] = []
    selected: set[int] = set()

    for edition_id in exact_isbn_ids:
        item = canonical.get(edition_id)
        if item is None:
            continue
        shared = sorted(set(local_isbns).intersection(item.isbns))
        candidates.append(_persist_candidate(
            db, document_id, edition_id, 1.0, "exact_isbn",
            {"matcher_version": MATCHER_VERSION, "signals": ["exact_isbn"], "isbn": shared},
        ))
        selected.add(edition_id)

    for edition_id in title_ids:
        item = canonical.get(edition_id)
        if item is None or edition_id in selected:
            continue
        author_exact = bool(author_keys.intersection(item.normalized_authors))
        score = 1.0 if author_exact else 0.8
        method = "exact_title_author" if author_exact else "exact_title"
        signals = ["exact_title"] + (["exact_author"] if author_exact else [])
        candidates.append(_persist_candidate(
            db, document_id, edition_id, score, method,
            {
                "matcher_version": MATCHER_VERSION,
                "signals": signals,
                "title_score": 1.0,
                "author_score": 1.0 if author_exact else 0.0,
            },
        ))
        selected.add(edition_id)

    fuzzy_ids = _fuzzy_pool_ids(db, title_key, author_keys, selected, local_title)
    fuzzy_candidates = _load_canonical_editions(db, fuzzy_ids)
    scored: list[tuple[float, int, dict[str, object]]] = []
    for edition_id in fuzzy_ids[:_FUZZY_POOL_LIMIT]:
        item = fuzzy_candidates.get(edition_id)
        if item is None:
            continue
        candidate_author = " | ".join(item.authors)
        title_score = _title_similarity(local_title, item.title)
        author_score = fuzzy_match_score(" | ".join(local_authors), candidate_author) if local_authors and candidate_author else 0.0
        score = 0.8 * title_score + 0.2 * author_score
        volume_relation = _volume_relation(local_title, item.title)
        if volume_relation == "mismatch":
            score -= 0.25
        elif volume_relation == "missing":
            score -= 0.10
        if title_score < 0.55 or (local_authors and candidate_author and author_score < 0.4):
            continue
        if score < 0.55:
            continue
        signals = ["fuzzy_title_author" if local_authors and candidate_author else "fuzzy_title"]
        if volume_relation in {"match", "mismatch", "missing"}:
            signals.append("volume_" + volume_relation)
        scored.append((score, edition_id, {
            "matcher_version": MATCHER_VERSION,
            "signals": signals,
            "title_score": title_score,
            "author_score": author_score,
            "volume_relation": volume_relation,
        }))
    for score, edition_id, evidence in sorted(scored, key=lambda value: (-value[0], value[1]))[:_FUZZY_PERSIST_LIMIT]:
        candidates.append(_persist_candidate(db, document_id, edition_id, score, "fuzzy", evidence))
    db.conn.commit()
    return candidates


def _document_fields(document: object) -> tuple[str, tuple[str, ...], set[str]]:
    try:
        metadata = json.loads(document["metadata_json"] or "{}")  # type: ignore[index]
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    title = _text(metadata.get("title")) or infer_title_from_path(document["source_path"])  # type: ignore[index]
    author_value = metadata.get("author") or metadata.get("authors")
    authors = tuple(_strings(author_value))
    if not authors:
        fallback = infer_author_from_path(document["source_path"])  # type: ignore[index]
        authors = (fallback,) if fallback else ()
    isbn_values: list[object] = []
    for key in ("isbn", "isbns", "isbn13", "isbn_13"):
        value = metadata.get(key)
        isbn_values.extend(value if isinstance(value, list) else [value] if value else [])
    isbns: set[str] = set()
    for value in isbn_values:
        try:
            isbns.add(normalize_isbn(str(value)))
        except (TypeError, ValueError):
            continue
    return title.strip(), authors, isbns


def _ids_by_isbn(db: CatalogueDB, isbns: set[str]) -> list[int]:
    if not isbns:
        return []
    placeholders = ", ".join("?" for _ in isbns)
    rows = db.conn.execute(
        "SELECT entity_id FROM catalogue_identifiers WHERE entity_type='edition' AND namespace='isbn' AND normalized_value IN (" + placeholders + ") ORDER BY entity_id LIMIT ?",
        tuple(sorted(isbns)) + (_EXACT_POOL_LIMIT,),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _ids_by_title(db: CatalogueDB, title_key: str, title: str) -> list[int]:
    if not title_key:
        return []
    work_rows = db.conn.execute(
        """SELECT e.id
           FROM catalogue_editions e
           WHERE e.work_id IN (
               SELECT w.id FROM catalogue_works w WHERE w.sort_title=?
           )
           ORDER BY e.id LIMIT ?""",
        (title_key, _EXACT_POOL_LIMIT),
    ).fetchall()
    title_rows = db.conn.execute(
        "SELECT id FROM catalogue_editions WHERE title=? ORDER BY id LIMIT ?",
        (title, _EXACT_POOL_LIMIT),
    ).fetchall()
    return sorted(
        {int(row[0]) for row in (*work_rows, *title_rows)}
    )[:_EXACT_POOL_LIMIT]


def _ids_by_authors(db: CatalogueDB, author_keys: set[str]) -> list[int]:
    if not author_keys:
        return []
    placeholders = ", ".join("?" for _ in author_keys)
    rows = db.conn.execute(
        """SELECT DISTINCT ep.edition_id
           FROM catalogue_people p
           JOIN catalogue_edition_people ep ON ep.person_id=p.id
           WHERE ep.role='author' AND p.normalized_name IN (""" + placeholders + ") ORDER BY ep.edition_id LIMIT ?",
        tuple(sorted(author_keys)) + (_EXACT_POOL_LIMIT,),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _fuzzy_pool_ids(
    db: CatalogueDB,
    title_key: str,
    author_keys: set[str],
    selected: set[int],
    title: str | None = None,
) -> list[int]:
    if not title_key:
        return []
    prefix = title_key[: min(8, len(title_key))]
    work_rows = db.conn.execute(
        """SELECT e.id
           FROM catalogue_editions e
           WHERE e.work_id IN (
               SELECT w.id FROM catalogue_works w WHERE w.sort_title GLOB ?
           )
           ORDER BY e.id LIMIT ?""",
        (_glob_prefix(prefix), _FUZZY_POOL_LIMIT),
    ).fetchall()
    title_prefix = (title or title_key)[: min(8, len(title or title_key))]
    title_rows = db.conn.execute(
        "SELECT id FROM catalogue_editions WHERE title GLOB ? ORDER BY id LIMIT ?",
        (_glob_prefix(title_prefix), _FUZZY_POOL_LIMIT),
    ).fetchall()
    ids = sorted({int(row[0]) for row in (*work_rows, *title_rows)})
    if len(ids) < _FUZZY_POOL_LIMIT and author_keys:
        ids.extend(
            edition_id
            for edition_id in _ids_by_authors(db, author_keys)
            if edition_id not in selected and edition_id not in ids
        )
    return [edition_id for edition_id in ids if edition_id not in selected][:_FUZZY_POOL_LIMIT]


def _glob_prefix(prefix: str) -> str:
    """Build a literal, indexable SQLite GLOB prefix pattern."""
    escaped = prefix.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")
    return escaped + "*"


def _load_canonical_editions(db: CatalogueDB, edition_ids: list[int]) -> dict[int, _CanonicalEdition]:
    if not edition_ids:
        return {}
    placeholders = ", ".join("?" for _ in edition_ids)
    rows = db.conn.execute(
        """SELECT e.id, e.title, w.title AS work_title
           FROM catalogue_editions e
           LEFT JOIN catalogue_works w ON w.id=e.work_id
           WHERE e.id IN (""" + placeholders + ")""",
        tuple(edition_ids),
    ).fetchall()
    people_sql = (
        "SELECT ep.edition_id, p.name, p.normalized_name "
        "FROM catalogue_edition_people ep "
        "JOIN catalogue_people p ON p.id=ep.person_id "
        "WHERE ep.edition_id IN (" + placeholders + ") "
        "AND ep.role='author' ORDER BY ep.edition_id, ep.position, p.id"
    )
    people_rows = db.conn.execute(people_sql, tuple(edition_ids)).fetchall()
    people_by_edition: dict[int, list[tuple[str, str]]] = {}
    for person in people_rows:
        people_by_edition.setdefault(int(person[0]), []).append((str(person[1]), str(person[2])))
    identifier_sql = (
        "SELECT entity_id, normalized_value FROM catalogue_identifiers "
        "WHERE entity_type='edition' AND namespace='isbn' "
        "AND entity_id IN (" + placeholders + ")"
    )
    identifier_rows = db.conn.execute(identifier_sql, tuple(edition_ids)).fetchall()
    identifiers_by_edition: dict[int, list[str]] = {}
    for identifier in identifier_rows:
        identifiers_by_edition.setdefault(int(identifier[0]), []).append(str(identifier[1]))
    output: dict[int, _CanonicalEdition] = {}
    for row in rows:
        edition_id = int(row[0])
        people = people_by_edition.get(edition_id, [])
        authors = tuple(person[0] for person in people)
        normalized_authors = tuple(person[1] for person in people)
        output[edition_id] = _CanonicalEdition(
            edition_id, str(row[1] or row[2] or ""), authors, normalized_authors,
            tuple(identifiers_by_edition.get(edition_id, [])),
        )
    return output


def _persist_candidate(
    db: CatalogueDB,
    document_id: int,
    edition_id: int,
    score: float,
    method: str,
    evidence: dict[str, object],
) -> int:
    existing = db.conn.execute(
        "SELECT id FROM catalogue_document_matches WHERE document_id=? AND edition_id=?",
        (document_id, edition_id),
    ).fetchone()
    if existing:
        return int(existing[0])
    cur = db.conn.execute(
        """INSERT INTO catalogue_document_matches
           (document_id, edition_id, score, method, status, evidence_json)
           VALUES (?, ?, ?, ?, 'candidate', ?)""",
        (document_id, edition_id, score, method, json.dumps(evidence, ensure_ascii=False, sort_keys=True)),
    )
    return int(cur.lastrowid)


def _normalized_name(value: object) -> str:
    return re.sub(r"\s+", " ", normalize_bengali(str(value or "")).strip()).lower()


def _title_similarity(query: str, candidate: str) -> float:
    """Avoid maxing a partial score when one title is only a short fragment."""
    query_key = _normalized_name(query)
    candidate_key = _normalized_name(candidate)
    if not query_key or not candidate_key:
        return 0.0
    if min(len(query_key), len(candidate_key)) / max(len(query_key), len(candidate_key)) < 0.7:
        return fuzz.token_sort_ratio(query_key, candidate_key) / 100.0
    return fuzzy_match_score(query_key, candidate_key)


def _volume_relation(query: str, candidate: str) -> str | None:
    """Compare explicit small volume numbers without guessing arbitrary digits."""
    query_volume = _volume_number(query)
    candidate_volume = _volume_number(candidate)
    if query_volume is None and candidate_volume is None:
        return None
    if query_volume is None or candidate_volume is None:
        return "missing"
    return "match" if query_volume == candidate_volume else "mismatch"


def _volume_number(title: str) -> int | None:
    translated = title.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
    numbers = [int(value) for value in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", translated)]
    return numbers[-1] if numbers and numbers[-1] <= 99 else None


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _strings(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    return [str(item.get("name") if isinstance(item, dict) else item).strip() for item in values if str(item.get("name") if isinstance(item, dict) else item).strip()]


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
