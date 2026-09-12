"""Materialize immutable source observations into the canonical catalogue."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
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
    source_url: str | None = None
    subjects: tuple[str, ...] = ()
    external_identifiers: tuple[tuple[str, str], ...] = ()


def parse_source_record(
    source: str,
    raw: dict[str, object],
    title: str | None = None,
    external_id: str | None = None,
) -> NormalizedSource:
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
    if source == "rokomari":
        specific_authors = _strings(value.get("Author/Editor")) + _strings(value.get("Author"))
        # The top-level `authors` key is the store's popular-authors sidebar,
        # identical across product pages, not this book's credits.
        authors = _unique_strings(specific_authors or _strings(value.get("authors")))
        publisher = _text(
            value.get("Publisher")
            or value.get("publisher")
            or _first(raw.get("publishers"))
            or raw.get("publisher")
        )
    else:
        authors = _strings(value.get("authors") or value.get("author") or value.get("Author") or value.get("authors_name") or raw.get("authors"))
        publisher = _text(value.get("publisher") or value.get("Publisher") or _first(value.get("publishers")))
    date = _text(value.get("publishedDate") or value.get("publish_date") or value.get("Publication Date") or value.get("first_publish_year"))
    language = _text(value.get("language") or value.get("Language") or raw.get("language"))
    page_count = _int(value.get("pageCount") or value.get("number_of_pages") or value.get("Number of Pages") or raw.get("pageCount"))
    description = _text(raw.get("description") or value.get("description") or value.get("Description"))
    identifiers = value.get("industryIdentifiers") or value.get("isbn") or value.get("ISBN") or value.get("isbn13") or value.get("isbn_13")
    if source == "rokomari":
        identifiers = value.get("ISBN") or raw.get("isbn") or identifiers
    if source == "open_library" and not identifiers:
        identifiers = value.get("isbn10") or value.get("isbn_10")
    isbns = tuple(sorted(set(_valid_isbns(identifiers))))
    image_links = value.get("cover_urls") or value.get("imageLinks") or value.get("cover_url")
    if source == "rokomari":
        image_links = raw.get("image") or image_links
    if not image_links and source == "open_library" and value.get("cover_i"):
        image_links = f"https://covers.openlibrary.org/b/id/{value['cover_i']}-L.jpg"
    covers = tuple(_unique_strings(_strings(image_links)))
    attribution = _text(value.get("attribution") or value.get("credit"))
    rights = _text(value.get("rights") or value.get("license"))
    subjects = ()
    if source == "rokomari":
        subjects = tuple(_unique_strings(_strings(raw.get("category")) + _strings(raw.get("subjects")) + _strings(value.get("subjects"))))
    source_url = _text(raw.get("url") or value.get("url"))
    external_identifiers = _source_identifiers(source, raw, external_id)
    return NormalizedSource(
        title=source_title or "",
        authors=tuple(authors),
        publisher=publisher,
        publication_date=date,
        language=language,
        page_count=page_count,
        description=description,
        isbns=isbns,
        cover_urls=covers,
        attribution=attribution,
        rights=rights,
        source_url=source_url,
        subjects=subjects,
        external_identifiers=external_identifiers,
    )


def materialize_source_records(
    db: CatalogueDB,
    *,
    source: str | None = None,
    dry_run: bool = False,
    batch_size: int = 500,
    on_batch: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Materialize staged records in bounded, resumable transactions."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    from .foundation import CatalogueFoundation

    where = " WHERE source=?" if source else ""
    params: tuple[object, ...] = (source,) if source else ()
    total = int(db.conn.execute("SELECT COUNT(*) FROM catalogue_source_records" + where, params).fetchone()[0])
    report = {"seen": total, "materialized": 0, "skipped": 0}
    if dry_run:
        return _materialize_dry_run(db, source=source, batch_size=batch_size, report=report)

    foundation = CatalogueFoundation(db)
    run = foundation.start_import_run(
        "catalogue_materialization",
        parser_version="catalogue-materialization-v2",
        request={"source": source or "*"},
    )
    checkpoint = db.conn.execute(
        "SELECT cursor FROM catalogue_import_checkpoints WHERE import_run_id=? AND checkpoint_key='records'",
        (run.id,),
    ).fetchone()
    cursor = int(checkpoint[0]) if checkpoint and checkpoint[0] else 0
    state = db.conn.execute(
        "SELECT state_json FROM catalogue_import_checkpoints WHERE import_run_id=? AND checkpoint_key='records'",
        (run.id,),
    ).fetchone()
    totals = json.loads(state[0] or "{}") if state else {}
    processed_skipped = int(totals.get("skipped", 0))
    processed_materialized = int(totals.get("materialized", 0))
    query = "SELECT id, source, external_id, title, raw_json FROM catalogue_source_records" + where + " AND id>?" if source else "SELECT id, source, external_id, title, raw_json FROM catalogue_source_records WHERE id>?"
    query += " ORDER BY id LIMIT ?"

    try:
        while True:
            query_params: tuple[object, ...] = (source, cursor, batch_size) if source else (cursor, batch_size)
            rows = db.conn.execute(query, query_params).fetchall()
            if not rows:
                break
            if run.status == "completed":
                db.conn.execute("UPDATE catalogue_import_runs SET status='running', completed_at=NULL WHERE id=?", (run.id,))
                db.conn.commit()
                run = type(run)(run.id, run.source, run.parser_version, run.checksum, "running")
            db.conn.execute("BEGIN")
            batch_materialized = batch_skipped = 0
            for row in rows:
                if db.conn.execute("SELECT 1 FROM catalogue_source_record_editions WHERE source_record_id=?", (row["id"],)).fetchone():
                    continue
                try:
                    normalized = parse_source_record(
                        row["source"], json.loads(row["raw_json"]), row["title"], row["external_id"]
                    )
                    if not normalized.title:
                        batch_skipped += 1
                        continue
                    _materialize_one(db, int(row["id"]), normalized)
                    batch_materialized += 1
                except (TypeError, ValueError, json.JSONDecodeError):
                    batch_skipped += 1
            db.conn.commit()
            cursor = int(rows[-1]["id"])
            processed_materialized += batch_materialized
            processed_skipped += batch_skipped
            foundation.set_import_checkpoint(
                run, "records", cursor=str(cursor),
                state={"materialized": processed_materialized, "skipped": processed_skipped},
            )
            report["materialized"] += batch_materialized
            report["skipped"] += batch_skipped
            if on_batch:
                on_batch(cursor, report["materialized"])
        foundation.finish_import_run(run)
    except Exception:
        if db.conn.in_transaction:
            db.conn.rollback()
        foundation.finish_import_run(run, status="failed")
        raise
    return report


def _materialize_dry_run(
    db: CatalogueDB, *, source: str | None, batch_size: int, report: dict[str, int]
) -> dict[str, int]:
    where = " WHERE source=?" if source else ""
    params_prefix: tuple[object, ...] = (source,) if source else ()
    cursor = 0
    while True:
        rows = db.conn.execute(
            "SELECT id, source, external_id, title, raw_json FROM catalogue_source_records"
            + where + (" AND id>?" if source else " WHERE id>?") + " ORDER BY id LIMIT ?",
            params_prefix + (cursor, batch_size),
        ).fetchall()
        if not rows:
            return report
        for row in rows:
            try:
                item = parse_source_record(row["source"], json.loads(row["raw_json"]), row["title"], row["external_id"])
                if item.title:
                    report["materialized"] += 1
                else:
                    report["skipped"] += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                report["skipped"] += 1
        cursor = int(rows[-1]["id"])


def repair_edition_people(
    db: CatalogueDB, batch_size: int = 500, *, dry_run: bool = False,
) -> dict[str, int]:
    """Remove author rows the Rokomari sidebar fallback created.

    Candidate editions are those mapped from a rokomari source record whose
    re-parse yields no authors. The correct author set for each candidate is
    the union over every source record mapped to that edition, so authors
    contributed by other sources are preserved. Commits per batch; callers
    pass dry_run to report counts without writing.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    report = {"editions_examined": 0, "people_rows_deleted": 0, "people_deleted": 0, "assertions_deleted": 0}
    seen_editions: set[int] = set()
    cursor = 0
    try:
        while True:
            rows = db.conn.execute(
                "SELECT id, source, external_id, title, raw_json FROM catalogue_source_records"
                " WHERE source='rokomari' AND id>? ORDER BY id LIMIT ?",
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                break
            cursor = int(rows[-1]["id"])
            for row in rows:
                try:
                    reparsed = parse_source_record(
                        row["source"], json.loads(row["raw_json"]), row["title"], row["external_id"]
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if reparsed.authors:
                    continue
                edition_rows = db.conn.execute(
                    "SELECT edition_id FROM catalogue_source_record_editions WHERE source_record_id=?",
                    (row["id"],),
                ).fetchall()
                for edition_row in edition_rows:
                    edition_id = int(edition_row[0])
                    if edition_id in seen_editions:
                        continue
                    seen_editions.add(edition_id)
                    report["editions_examined"] += 1
                    _repair_one_edition(db, edition_id, report)
            if not dry_run:
                db.conn.commit()
        # Swept once, after every batch. Both statements scan a whole table
        # (3.8M assertions, 76k people), and the result is identical to
        # sweeping per batch, so doing it inside the loop cost 424 full scans.
        _delete_orphan_person_rows(db, report)
        if not dry_run:
            db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    # A dry run performs the deletes and rolls back only at the very end. It
    # cannot roll back per batch: the orphan sweep runs last and can only see
    # a person as orphaned once the rows referencing them are actually gone,
    # so an earlier rollback would report people_deleted=0 for work a live run
    # really does. Reporting the true numbers is worth the larger transaction.
    if dry_run:
        db.conn.rollback()
    return report


def _repair_one_edition(db: CatalogueDB, edition_id: int, report: dict[str, int]) -> None:
    mapped = db.conn.execute(
        "SELECT sr.source, sr.external_id, sr.title, sr.raw_json"
        " FROM catalogue_source_record_editions sre"
        " JOIN catalogue_source_records sr ON sr.id=sre.source_record_id"
        " WHERE sre.edition_id=?",
        (edition_id,),
    ).fetchall()
    keep: set[str] = set()
    for source, external_id, title, raw_json in mapped:
        try:
            parsed = parse_source_record(source, json.loads(raw_json), title, external_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        keep.update(_normalize_person_name(name) for name in parsed.authors)
    # Only author rows are in scope. The sidebar fallback could only ever
    # create role='author', and the set recomputed above is an author set, so
    # judging an editor, translator or illustrator row against it would delete
    # a credit this bug never touched.
    rows = db.conn.execute(
        "SELECT ep.person_id, ep.role, p.name FROM catalogue_edition_people ep"
        " JOIN catalogue_people p ON p.id=ep.person_id"
        " WHERE ep.edition_id=? AND ep.role='author'"
        " ORDER BY ep.position, ep.person_id, ep.role",
        (edition_id,),
    ).fetchall()
    survivors = [(row[0], row[1]) for row in rows if _normalize_person_name(str(row[2])) in keep]
    for row in rows:
        if _normalize_person_name(str(row[2])) not in keep:
            # The primary key is (edition_id, person_id, role), so the role has
            # to be part of the delete. Keying on the person alone would drop
            # every role that person holds on the edition -- today all rows are
            # 'author', but an editor or translator row must not vanish with it.
            deleted = db.conn.execute(
                "DELETE FROM catalogue_edition_people WHERE edition_id=? AND person_id=? AND role=?",
                (edition_id, row[0], row[1]),
            )
            report["people_rows_deleted"] += deleted.rowcount
    for position, (person_id, role) in enumerate(survivors):
        db.conn.execute(
            "UPDATE catalogue_edition_people SET position=? WHERE edition_id=? AND person_id=? AND role=?",
            (position, edition_id, person_id, role),
        )


def _delete_orphan_person_rows(db: CatalogueDB, report: dict[str, int]) -> None:
    deleted_assertions = db.conn.execute(
        "DELETE FROM catalogue_metadata_assertions WHERE entity_type='person'"
        " AND NOT EXISTS (SELECT 1 FROM catalogue_edition_people WHERE person_id=catalogue_metadata_assertions.entity_id)"
    )
    report["assertions_deleted"] += deleted_assertions.rowcount
    deleted_people = db.conn.execute(
        "DELETE FROM catalogue_people"
        " WHERE NOT EXISTS (SELECT 1 FROM catalogue_edition_people WHERE person_id=catalogue_people.id)"
    )
    report["people_deleted"] += deleted_people.rowcount


def _materialize_one(db: CatalogueDB, source_record_id: int, item: NormalizedSource) -> int:
    conn = db.conn
    work_id = _get_work(conn, item)
    edition_id = _get_edition(conn, item, work_id)
    conn.execute("INSERT INTO catalogue_source_record_editions (source_record_id, edition_id) VALUES (?, ?)", (source_record_id, edition_id))
    for position, name in enumerate(item.authors):
        person_id = _get_person(conn, name)
        conn.execute("INSERT OR IGNORE INTO catalogue_edition_people (edition_id, person_id, role, position) VALUES (?, ?, 'author', ?)", (edition_id, person_id, position))
        _assert(conn, "person", person_id, "name", name, source_record_id)
    values = {"title": item.title, "publisher": item.publisher, "date": item.publication_date, "language": item.language, "page_count": item.page_count, "description": item.description, "source_url": item.source_url}
    for field, value in values.items():
        if value is not None:
            _assert(conn, "edition", edition_id, field, value, source_record_id)
    for subject in item.subjects:
        _assert(conn, "edition", edition_id, "subject", subject, source_record_id)
    for namespace, value in item.external_identifiers:
        _assert(conn, "edition", edition_id, "identifier", {"namespace": namespace, "value": value}, source_record_id)
        _insert_identifier(conn, edition_id, namespace, value)
    for isbn in item.isbns:
        _assert(conn, "edition", edition_id, "isbn", isbn, source_record_id)
        _insert_identifier(conn, edition_id, "isbn", isbn)
    for url in item.cover_urls:
        _assert(conn, "edition", edition_id, "cover_url", url, source_record_id)
        _insert(
            conn, edition_id=edition_id, storage_uri="remote:" + url, source_url=url,
            asset_type="cover", sha256=None, mime_type=None, width=None, height=None,
            source_record_id=source_record_id, attribution=item.attribution,
            rights=item.rights,
        )
    return edition_id


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


def _normalize_person_name(name: str) -> str:
    return re.sub(r"\s+", " ", normalize_bengali(name)).strip().lower()


def _get_person(conn, name: str) -> int:
    normalized = _normalize_person_name(name)
    row = conn.execute("SELECT id FROM catalogue_people WHERE normalized_name=?", (normalized,)).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute("INSERT INTO catalogue_people (name, sort_name, normalized_name) VALUES (?, ?, ?)", (name, name, normalized))
    return int(cur.lastrowid)


def _assert(conn, entity_type: str, entity_id: int, field: str, value: object, source_record_id: int) -> None:
    """Record one observation for a source row on the bulk path.

    The source-record-to-edition mapping is inserted in the same transaction
    before assertions are written. A committed source record is skipped on a
    later run, so a duplicate lookup here only adds work to the bulk import.
    """
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    conn.execute("INSERT INTO catalogue_metadata_assertions (entity_type, entity_id, field_name, value_json, source_record_id, confidence) VALUES (?, ?, ?, ?, ?, 1.0)", (entity_type, entity_id, field, encoded, source_record_id))


def _insert_identifier(conn, edition_id: int, namespace: str, value: str) -> None:
    """Attach an identifier without replacing an identifier already owned elsewhere."""
    normalized = value.strip()
    if not normalized:
        return
    conn.execute(
        "INSERT OR IGNORE INTO catalogue_identifiers (entity_type, entity_id, namespace, value, normalized_value) VALUES ('edition', ?, ?, ?, ?)",
        (edition_id, namespace, value, normalized),
    )


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


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _source_identifiers(
    source: str, raw: dict[str, object], external_id: str | None
) -> tuple[tuple[str, str], ...]:
    if source != "rokomari":
        return ()
    candidates: list[tuple[str, str]] = []
    raw_values = (
        ("rokomari:id", raw.get("id")),
        ("rokomari:product_id", raw.get("productId")),
        ("rokomari:sku", raw.get("sku")),
    )
    canonical = _text(external_id)
    if not canonical or canonical.startswith("line:"):
        for _namespace, value in raw_values:
            if _text(value):
                canonical = _text(value)
                break
    if not canonical:
        canonical = _text(raw.get("url"))
    if canonical:
        candidates.append(("rokomari", canonical))
    for namespace, value in raw_values:
        text = _text(value)
        if text:
            candidates.append((namespace, text))
    return tuple(_unique_pairs(candidates))


def _unique_pairs(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    output: list[tuple[str, str]] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _int(value: object) -> int | None:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            match = re.search(r"\d+", value)
            return int(match.group(0)) if match else None
        return int(value)
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
