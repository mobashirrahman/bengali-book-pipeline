"""Fuse every available metadata signal into ``local_documents.metadata_json``.

All 22,717 staged documents ship with an empty ``metadata_json``, so
:func:`pdf_craft.catalogue.resolution._document_fields` has nothing to match on
but the file path.  Rather than build a second matcher, this module writes the
fields that matcher already reads, so improving extraction improves matching
without touching the matcher at all.

Signals are *fused*, not cascaded, because they fail independently and complete
each other.  Measured on a five-document trial: one file had a perfect cover but
a watermark for ``/Title``; two had clean romanized ``/Info`` but covers OCR
could not read; two had neither and only the filename or cover left.  A strict
cascade would have taken the first answer and missed the better one.

Precedence differs by container, because the two embedded formats are not
equally trustworthy:

* **EPUB** puts the OPF first.  ``dc:title`` and ``dc:creator`` are real
  bibliographic metadata written by whoever produced the book, and they are
  right where the filename is a romanized slug: the OPF gives ``এলাটিং বেলাটিং``
  where the filename only offers ``Elating-belating-Shamsur-Rahoman``.
* **PDF** puts the filename first.  ``/Info`` is written by the scanning tool or
  the download site far more often than by a publisher, so it ranks below a
  matched scraper template and is gated on a trusted producer besides.

Cover OCR ranks last in both, and is the only signal at all for the 2,441
documents whose filenames are pure scraper placeholders.

Anything not confidently resolved is left absent on purpose.  ``_document_fields``
falls back to path inference when ``title`` is missing, and for this corpus that
fallback beats a bad guess: a wrong title is matched against a 194,060-work
catalogue and produces a confident wrong match.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from .epub_metadata import EpubMetadataError, extract_epub_metadata
from .filename_parser import parse_filename
from .pdf_metadata import PdfMetadataError, extract_pdf_metadata

# Bumped when extraction changes enough that an earlier pass is worth redoing.
EXTRACTION_VERSION = 2

_EPUB_TYPES = frozenset({"application/epub+zip", "application/epub"})


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    """Preserve order while dropping repeats and blanks."""
    seen: dict[str, None] = {}
    for value in values:
        cleaned = " ".join(str(value).split())
        if cleaned:
            seen.setdefault(cleaned, None)
    return tuple(seen)


def _embedded(path: Path, media_type: str | None) -> tuple[dict[str, Any], str | None]:
    """Read the container's own metadata, or report why it could not be read."""
    is_epub = path.suffix.lower() == ".epub" or (media_type or "").lower() in _EPUB_TYPES
    try:
        if is_epub:
            return dict(extract_epub_metadata(path)), None
        return dict(extract_pdf_metadata(path)), None
    except (EpubMetadataError, PdfMetadataError, OSError) as error:
        return {}, f"{type(error).__name__}: {error}"[:200]


def build_document_metadata(
    source_path: str,
    media_type: str | None = None,
    *,
    read_embedded: bool = True,
    cover_reading: Any | None = None,
) -> dict[str, Any]:
    """Fuse filename, embedded and cover signals for one document.

    ``cover_reading`` is an optional :class:`~pdf_craft.catalogue.cover_ocr.CoverReading`.
    It is passed in rather than produced here because OCR needs a GPU-resident
    model that must be amortised across the corpus, and because most documents
    never need it.
    """
    path = Path(source_path)
    parsed = parse_filename(source_path)
    provenance: dict[str, Any] = {
        "version": EXTRACTION_VERSION,
        "filename_source": parsed.source,
        "filename_confidence": parsed.confidence,
        "signals": [],
    }

    # A link list or a single chapter is not a book; recording that is more
    # useful than recording a title scraped off its filename.
    if parsed.is_not_a_book:
        provenance["status"] = "not_a_book"
        return {"_extraction": provenance}

    is_epub = path.suffix.lower() == ".epub" or (media_type or "").lower() in _EPUB_TYPES
    filename_titles = list(parsed.titles)
    filename_authors = list(parsed.authors)
    embedded_titles: list[str] = []
    embedded_authors: list[str] = []
    isbns: list[str] = []
    if parsed.titles or parsed.authors:
        provenance["signals"].append("filename")

    if read_embedded:
        embedded, error = _embedded(path, media_type)
        if error:
            provenance["embedded_error"] = error
        if embedded:
            provenance["signals"].append("embedded")
        embedded_titles = [str(value) for value in _as_list(embedded.get("title"))]
        embedded_authors = [str(value) for value in _as_list(embedded.get("authors"))]
        isbns = [str(value) for value in _as_list(embedded.get("isbns"))]

    # See the module docstring: the OPF outranks an EPUB's filename, while a
    # PDF's /Info does not outrank a matched scraper template.
    if is_epub:
        titles = embedded_titles + filename_titles
        authors = embedded_authors + filename_authors
        provenance["precedence"] = "embedded_first"
    else:
        titles = filename_titles + embedded_titles
        authors = filename_authors + embedded_authors
        provenance["precedence"] = "filename_first"

    if cover_reading is not None:
        cover_titles = tuple(getattr(cover_reading, "title_candidates", ()))
        cover_authors = tuple(getattr(cover_reading, "author_candidates", ()))
        if cover_titles or cover_authors:
            provenance["signals"].append("cover_ocr")
        titles.extend(cover_titles)
        authors.extend(cover_authors)

    result: dict[str, Any] = {}
    merged_titles = _dedupe(titles)
    merged_authors = _dedupe(authors)
    if merged_titles:
        # `_document_fields` reads a single `title`; the rest are kept so a later
        # matching pass can try them without re-extracting.
        result["title"] = merged_titles[0]
        if len(merged_titles) > 1:
            result["title_candidates"] = list(merged_titles)
    if merged_authors:
        result["authors"] = list(merged_authors)
    if isbns:
        result["isbns"] = list(_dedupe(isbns))
    for field in ("volume", "edition", "year"):
        value = getattr(parsed, field, None)
        if value:
            result[field] = value

    provenance["status"] = "ok" if result else "empty"
    result["_extraction"] = provenance
    return result


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def backfill_document_metadata(
    db: Any,
    *,
    media_type: str | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    read_embedded: bool = True,
    batch_size: int = 250,
    progress: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    """Write fused metadata for staged documents that do not yet have any.

    ``dry_run`` performs every extraction and rolls back once at the end, so its
    counts are exactly what a live run would write.
    """
    conn = db.conn
    clauses: list[str] = []
    params: list[Any] = []
    if not force:
        # '{}' is the column default and means "never attempted"; anything else
        # was written by a previous pass.
        clauses.append("(metadata_json IS NULL OR TRIM(metadata_json) IN ('', '{}'))")
    if media_type:
        clauses.append("media_type = ?")
        params.append(media_type)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT id, source_path, media_type FROM catalogue_local_documents{where} ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    report = {
        "documents_examined": 0, "with_title": 0, "with_authors": 0,
        "with_isbn": 0, "not_a_book": 0, "empty": 0,
    }
    pending = 0
    for document_id, source_path, row_media in conn.execute(sql, tuple(params)).fetchall():
        metadata = build_document_metadata(source_path, row_media, read_embedded=read_embedded)
        conn.execute(
            "UPDATE catalogue_local_documents SET metadata_json=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), document_id),
        )
        report["documents_examined"] += 1
        status = str(metadata.get("_extraction", {}).get("status", "empty"))
        if metadata.get("title"):
            report["with_title"] += 1
        if metadata.get("authors"):
            report["with_authors"] += 1
        if metadata.get("isbns"):
            report["with_isbn"] += 1
        if status in report:
            report[status] += 1
        pending += 1
        # A dry run must not commit at all, so batches only flush when live.
        if not dry_run and pending >= batch_size:
            conn.commit()
            pending = 0
            if progress is not None:
                progress(dict(report))
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report
