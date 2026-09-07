from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

from ..database import CatalogueDB
from ..matching import normalize_bengali
from ..models import AuthorRole, Book

logger = logging.getLogger(__name__)


def import_rokomari(
    db: CatalogueDB,
    dataset_name: str = "sayurio/rokomari-bd-product-data",
    cache_dir: str | Path | None = None,
) -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        raise ImportError(
            "The 'datasets' package is required for Rokomari import. "
            "Install it with: pip install 'pdf-craft[catalogue]'"
        )

    logger.info("Loading Rokomari dataset from %s...", dataset_name)
    ds = load_dataset(dataset_name, split="train", cache_dir=str(cache_dir) if cache_dir else None)

    imported = 0
    skipped = 0

    for i, record in enumerate(ds):
        if i % 10000 == 0:
            logger.info("Processing record %d / %d (imported=%d, skipped=%d)", i, len(ds), imported, skipped)

        spec = record.get("specification")
        if spec is None or record.get("productType") != "book":
            skipped += 1
            continue

        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except (json.JSONDecodeError, TypeError):
                skipped += 1
                continue

        if not isinstance(spec, dict):
            skipped += 1
            continue

        title = spec.get("Title") or record.get("name", "")
        if not title or not title.strip():
            skipped += 1
            continue
        title = title.strip()

        existing = db.find_books_by_title(title, limit=1)
        if existing:
            skipped += 1
            continue

        author_name = spec.get("Author") or ""
        if isinstance(author_name, list):
            author_name = author_name[0] if author_name else ""
        author_name = author_name.strip()

        publisher = spec.get("Publisher") or ""
        if isinstance(publisher, list):
            publisher = publisher[0] if publisher else ""
        publisher = publisher.strip() or None

        isbn_raw = spec.get("ISBN") or ""
        isbn = isbn_raw.strip() if isbn_raw else None
        if isbn:
            isbn = re.sub(r"[^\d]", "", isbn)
            if len(isbn) < 10:
                isbn = None

        edition = spec.get("Edition") or None
        pages_raw = spec.get("Number of Pages") or ""
        page_count = None
        if pages_raw:
            try:
                page_count = int(re.sub(r"[^\d]", "", str(pages_raw)))
            except (ValueError, TypeError):
                pass

        language = spec.get("Language") or "bn"
        if isinstance(language, str) and "বাংলা" in language:
            language = "bn"

        book = Book(
            title=title,
            title_sort=normalize_bengali(title).lower(),
            publisher=publisher,
            isbn=isbn,
            edition=edition,
            language=language,
            page_count=page_count,
            rokomari_url=record.get("url"),
        )

        book_id = db.create_book(book)

        if author_name:
            author_id = db.get_or_create_author(author_name)
            db.add_book_author(book_id, author_id, AuthorRole.AUTHOR)

        category = record.get("category")
        if category and isinstance(category, str) and category.strip():
            subject_id = db.get_or_create_subject(category.strip())
            db.add_book_subject(book_id, subject_id)

        rating_value = record.get("ratingValue")
        if rating_value:
            try:
                float(rating_value)
            except (ValueError, TypeError):
                pass

        imported += 1

    logger.info("Rokomari import complete: %d imported, %d skipped", imported, skipped)
    return imported


def import_rokomari_from_file(db: CatalogueDB, jsonl_path: str | Path) -> int:
    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    imported = 0
    skipped = 0

    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)
            spec = record.get("specification")
            if spec is None or record.get("productType") != "book":
                skipped += 1
                continue

            if isinstance(spec, str):
                try:
                    spec = json.loads(spec)
                except (json.JSONDecodeError, TypeError):
                    skipped += 1
                    continue

            if not isinstance(spec, dict):
                skipped += 1
                continue

            title = spec.get("Title") or record.get("name", "")
            if not title or not title.strip():
                skipped += 1
                continue
            title = title.strip()

            existing = db.find_books_by_title(title, limit=1)
            if existing:
                skipped += 1
                continue

            author_name = spec.get("Author") or ""
            if isinstance(author_name, list):
                author_name = author_name[0] if author_name else ""
            author_name = author_name.strip()

            publisher = spec.get("Publisher") or ""
            if isinstance(publisher, list):
                publisher = publisher[0] if publisher else ""
            publisher = publisher.strip() or None

            isbn_raw = spec.get("ISBN") or ""
            isbn = isbn_raw.strip() if isbn_raw else None
            if isbn:
                isbn = re.sub(r"[^\d]", "", isbn)
                if len(isbn) < 10:
                    isbn = None

            edition = spec.get("Edition") or None
            pages_raw = spec.get("Number of Pages") or ""
            page_count = None
            if pages_raw:
                try:
                    page_count = int(re.sub(r"[^\d]", "", str(pages_raw)))
                except (ValueError, TypeError):
                    pass

            book = Book(
                title=title,
                title_sort=normalize_bengali(title).lower(),
                publisher=publisher,
                isbn=isbn,
                edition=edition,
                language="bn",
                page_count=page_count,
                rokomari_url=record.get("url"),
            )

            book_id = db.create_book(book)

            if author_name:
                author_id = db.get_or_create_author(author_name)
                db.add_book_author(book_id, author_id, AuthorRole.AUTHOR)

            category = record.get("category")
            if category and isinstance(category, str) and category.strip():
                subject_id = db.get_or_create_subject(category.strip())
                db.add_book_subject(book_id, subject_id)

            imported += 1

            if i % 10000 == 0:
                logger.info("Processed %d records (imported=%d, skipped=%d)", i, imported, skipped)

    logger.info("Rokomari file import complete: %d imported, %d skipped", imported, skipped)
    return imported


def stage_rokomari_from_file(
    db: CatalogueDB,
    jsonl_path: str | Path,
    *,
    snapshot_key: str | None = None,
    batch_size: int = 500,
    request: dict[str, object] | None = None,
    parser_version: str = "rokomari-jsonl-v2",
    on_batch: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Store a Rokomari snapshot and raw records without merging metadata.

    The existing ``import_rokomari_from_file`` function is retained for the
    prototype v1 tables. New catalogue work should use this staging function,
    then resolve source records into works and editions through reviewable
    assertions.
    """
    from ..foundation import CatalogueFoundation, file_sha256

    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    foundation = CatalogueFoundation(db)
    digest = file_sha256(path)
    run = foundation.start_import_run(
        "rokomari", parser_version=parser_version,
        request=request or {"file": str(path)}, checksum=digest,
    )
    snapshot = foundation.record_source_snapshot(
        "rokomari",
        snapshot_key or path.name,
        {"path": str(path), "sha256": digest, "file_size": path.stat().st_size},
        import_run=run, request=request or {"file": str(path)},
        parser_version=parser_version,
    )
    checkpoint = db.conn.execute(
        "SELECT cursor, state_json FROM catalogue_import_checkpoints WHERE import_run_id=? AND checkpoint_key='records'",
        (run.id,),
    ).fetchone()
    if run.status == "completed" and checkpoint:
        state = json.loads(checkpoint[1] or "{}")
        return int(state.get("staged", 0)), int(state.get("skipped", 0))
    cursor = int(checkpoint[0]) if checkpoint and checkpoint[0] else 0
    state = json.loads(checkpoint[1] or "{}") if checkpoint else {}
    staged = int(state.get("staged", 0))
    skipped = int(state.get("skipped", 0))
    try:
        with path.open(encoding="utf-8") as stream:
            last_line = cursor
            for line_number, line in enumerate(stream, start=1):
                if line_number <= cursor:
                    continue
                last_line = line_number
                if not line.strip():
                    skipped += 1
                else:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        record = None
                    if not isinstance(record, dict):
                        skipped += 1
                    else:
                        if record.get("productType") != "book":
                            skipped += 1
                        else:
                            spec = record.get("specification")
                            if isinstance(spec, str):
                                try:
                                    spec = json.loads(spec)
                                except (TypeError, json.JSONDecodeError):
                                    spec = None
                            title = spec.get("Title") if isinstance(spec, dict) else None
                            if not isinstance(title, str) or not title.strip():
                                skipped += 1
                            else:
                                external_id = record.get("id") or record.get("productId") or record.get("sku") or record.get("url") or f"line:{line_number}"
                                inserted = db.conn.execute(
                                    "INSERT OR IGNORE INTO catalogue_source_records (snapshot_id, source, external_id, record_type, title, raw_json) VALUES (?, 'rokomari', ?, 'edition', ?, ?)",
                                    (snapshot.id, str(external_id), title.strip(), json.dumps(record, ensure_ascii=False, sort_keys=True)),
                                )
                                staged += int(inserted.rowcount > 0)
                if (line_number - cursor) % batch_size == 0:
                    db.conn.execute("INSERT INTO catalogue_import_checkpoints (import_run_id, checkpoint_key, cursor, state_json) VALUES (?, 'records', ?, ?) ON CONFLICT(import_run_id, checkpoint_key) DO UPDATE SET cursor=excluded.cursor, state_json=excluded.state_json, updated_at=datetime('now')", (run.id, line_number, json.dumps({"staged": staged, "skipped": skipped})))
                    db.conn.commit()
                    if on_batch:
                        on_batch(line_number, staged)
            db.conn.execute("INSERT INTO catalogue_import_checkpoints (import_run_id, checkpoint_key, cursor, state_json) VALUES (?, 'records', ?, ?) ON CONFLICT(import_run_id, checkpoint_key) DO UPDATE SET cursor=excluded.cursor, state_json=excluded.state_json, updated_at=datetime('now')", (run.id, last_line, json.dumps({"staged": staged, "skipped": skipped})))
            db.conn.commit()
        foundation.finish_import_run(run)
    except Exception:
        db.conn.rollback()
        foundation.finish_import_run(run, status="failed")
        raise
    return staged, skipped
