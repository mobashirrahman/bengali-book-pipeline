"""Materialize immutable source observations into the canonical catalogue."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .assets import _insert
from .database import CatalogueDB
from .isbn import normalize_isbn
from .matching import normalize_bengali, title_sort_key


@dataclass(frozen=True)
class NormalizedSource:
    title: str
    authors: tuple[str, ...] = ()
    publisher: str | None = None
    publication_date: str | None = None
    language: str | None = None
    page_count: int | None = None
    description: str | None = None
    isbns: tuple[str, ...] = ()
    cover_urls: tuple[str, ...] = ()
    attribution: str | None = None
    rights: str | None = None


def parse_source_record(source: str, raw: dict[str, object], title: str | None = None) -> NormalizedSource:
    """Normalize the three supported source shapes without fuzzy decisions."""
    if source == "google_books":
        value = raw.get("volumeInfo") if isinstance(raw.get("volumeInfo"), dict) else raw
    elif source == "open_library":
        value = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    else:
        value = raw.get("specification") if isinstance(raw.get("specification"), dict) else raw
        if isinstance(raw.get("specification"), str):
            try:
                value = json.loads(raw["specification"])
            except (TypeError, json.JSONDecodeError):
                value = raw
    if not isinstance(value, dict):
        value = raw
    source_title = _text(value.get("title") or value.get("Title") or raw.get("name") or title)
    authors = _strings(value.get("authors") or value.get("author") or value.get("Author") or value.get("authors_name") or raw.get("authors"))
    publisher = _text(value.get("publisher") or value.get("Publisher") or _first(value.get("publishers")))
    date = _text(value.get("publishedDate") or value.get("publish_date") or value.get("Publication Date") or value.get("first_publish_year"))
    language = _text(value.get("language") or value.get("Language"))
    page_count = _int(value.get("pageCount") or value.get("number_of_pages") or value.get("Number of Pages"))
    description = _text(value.get("description") or value.get("Description"))
    identifiers = value.get("industryIdentifiers") or value.get("isbn") or value.get("ISBN") or value.get("isbn13") or value.get("isbn_13")
    if source == "open_library" and not identifiers:
        identifiers = value.get("isbn10") or value.get("isbn_10")
    isbns = tuple(sorted(set(_valid_isbns(identifiers))))
    image_links = value.get("cover_urls") or value.get("imageLinks") or value.get("cover_url")
    if not image_links and source == "open_library" and value.get("cover_i"):
        image_links = f"https://covers.openlibrary.org/b/id/{value['cover_i']}-L.jpg"
    covers = tuple(_strings(image_links))
    attribution = _text(value.get("attribution") or value.get("credit"))
    rights = _text(value.get("rights") or value.get("license"))
    return NormalizedSource(source_title, tuple(authors), publisher, date, language, page_count, description, isbns, covers, attribution, rights)


def materialize_source_records(db: CatalogueDB, *, source: str | None = None, dry_run: bool = False) -> dict[str, int]:
    """Materialize staged records incrementally and return a compact report."""
    query = "SELECT id, source, title, raw_json FROM catalogue_source_records"
    params: tuple[object, ...] = ()
    if source:
        query += " WHERE source=?"
        params = (source,)
    rows = db.conn.execute(query + " ORDER BY id", params)
    report = {"seen": 0, "materialized": 0, "skipped": 0}
    for row in rows:
        report["seen"] += 1
        if db.conn.execute("SELECT 1 FROM catalogue_source_record_editions WHERE source_record_id=?", (row["id"],)).fetchone():
            continue
        try:
            normalized = parse_source_record(row["source"], json.loads(row["raw_json"]), row["title"])
            if not normalized.title:
                report["skipped"] += 1
                continue
            if dry_run:
                report["materialized"] += 1
                continue
            _materialize_one(db, int(row["id"]), normalized)
            report["materialized"] += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            report["skipped"] += 1
    return report


def _materialize_one(db: CatalogueDB, source_record_id: int, item: NormalizedSource) -> int:
    conn = db.conn
    conn.execute("BEGIN")
    try:
        work_id = _get_work(conn, item)
        edition_id = _get_edition(conn, item, work_id)
        conn.execute("INSERT INTO catalogue_source_record_editions (source_record_id, edition_id) VALUES (?, ?)", (source_record_id, edition_id))
        for position, name in enumerate(item.authors):
            person_id = _get_person(conn, name)
            conn.execute("INSERT OR IGNORE INTO catalogue_edition_people (edition_id, person_id, role, position) VALUES (?, ?, 'author', ?)", (edition_id, person_id, position))
            _assert(conn, "person", person_id, "name", name, source_record_id)
        values = {"title": item.title, "publisher": item.publisher, "date": item.publication_date, "language": item.language, "page_count": item.page_count, "description": item.description}
        for field, value in values.items():
            if value is not None:
                _assert(conn, "edition", edition_id, field, value, source_record_id)
        for isbn in item.isbns:
            _assert(conn, "edition", edition_id, "isbn", isbn, source_record_id)
            existing = conn.execute("SELECT entity_type, entity_id FROM catalogue_identifiers WHERE namespace='isbn' AND normalized_value=?", (isbn,)).fetchone()
            if not existing:
                conn.execute("INSERT INTO catalogue_identifiers (entity_type, entity_id, namespace, value, normalized_value) VALUES ('edition', ?, 'isbn', ?, ?)", (edition_id, isbn, isbn))
            elif existing[1] == edition_id:
                pass
            # A conflicting ISBN remains attached to its first edition; the
            # source assertion is retained below and the new edition survives.
        for url in item.cover_urls:
            _assert(conn, "edition", edition_id, "cover_url", url, source_record_id)
            _insert(
                conn, edition_id=edition_id, storage_uri="remote:" + url, source_url=url,
                asset_type="cover", sha256=None, mime_type=None, width=None, height=None,
                source_record_id=source_record_id, attribution=item.attribution,
                rights=item.rights,
            )
        conn.commit()
        return edition_id
    except Exception:
        conn.rollback()
        raise


def _get_work(conn, item: NormalizedSource) -> int:
    key = title_sort_key(item.title)
    author_keys = tuple(sorted(re.sub(r"\s+", " ", normalize_bengali(a)).lower() for a in item.authors))
    rows = conn.execute("SELECT id FROM catalogue_works WHERE sort_title=?", (key,)).fetchall()
    for row in rows:
        people = tuple(sorted(r[0] for r in conn.execute("SELECT p.normalized_name FROM catalogue_people p JOIN catalogue_edition_people ep ON ep.person_id=p.id JOIN catalogue_editions e ON e.id=ep.edition_id WHERE e.work_id=? AND ep.role='author'", (row[0],))))
        if not author_keys or people == author_keys:
            return int(row[0])
    cur = conn.execute("INSERT INTO catalogue_works (title, sort_title, language, description) VALUES (?, ?, ?, ?)", (item.title, key, item.language, item.description))
    return int(cur.lastrowid)


def _get_edition(conn, item: NormalizedSource, work_id: int) -> int:
    # ISBN equality is strong enough for cross-source edition reuse. Without
    # it, each source observation gets its own edition as required by the phase.
    for isbn in item.isbns:
        row = conn.execute("SELECT entity_id FROM catalogue_identifiers WHERE namespace='isbn' AND normalized_value=? AND entity_type='edition'", (isbn,)).fetchone()
        if row and conn.execute("SELECT id FROM catalogue_editions WHERE id=? AND work_id=?", (row[0], work_id)).fetchone():
            return int(row[0])
    cur = conn.execute("INSERT INTO catalogue_editions (work_id, title, publisher, publication_date, language, description, page_count) VALUES (?, ?, ?, ?, ?, ?, ?)", (work_id, item.title, item.publisher, item.publication_date, item.language, item.description, item.page_count))
    return int(cur.lastrowid)


def _get_person(conn, name: str) -> int:
    normalized = re.sub(r"\s+", " ", normalize_bengali(name)).strip().lower()
    row = conn.execute("SELECT id FROM catalogue_people WHERE normalized_name=?", (normalized,)).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute("INSERT INTO catalogue_people (name, sort_name, normalized_name) VALUES (?, ?, ?)", (name, name, normalized))
    return int(cur.lastrowid)


def _assert(conn, entity_type: str, entity_id: int, field: str, value: object, source_record_id: int) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if conn.execute("SELECT 1 FROM catalogue_metadata_assertions WHERE entity_type=? AND entity_id=? AND field_name=? AND value_json=? AND source_record_id=?", (entity_type, entity_id, field, encoded, source_record_id)).fetchone():
        return
    conn.execute("INSERT INTO catalogue_metadata_assertions (entity_type, entity_id, field_name, value_json, source_record_id, confidence) VALUES (?, ?, ?, ?, ?, 1.0)", (entity_type, entity_id, field, encoded, source_record_id))


def _text(value: object) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _first(value: object) -> object:
    return value[0] if isinstance(value, list) and value else value


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        value = [value] if value else []
    return [str(v.get("name") if isinstance(v, dict) else v).strip() for v in value if str(v.get("name") if isinstance(v, dict) else v).strip()]


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _valid_isbns(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output = []
    for raw in values:
        if isinstance(raw, dict):
            raw = raw.get("identifier")
        if raw:
            try:
                output.append(normalize_isbn(str(raw)))
            except ValueError:
                continue
    return output
