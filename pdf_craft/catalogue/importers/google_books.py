from __future__ import annotations

import logging
import hashlib
import json
import time
from dataclasses import dataclass

import httpx

from ..database import CatalogueDB
from ..matching import normalize_bengali
from ..foundation import CatalogueFoundation

logger = logging.getLogger(__name__)

GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"
GOOGLE_PARSER_VERSION = "google-books-v2"


@dataclass
class GoogleBookResult:
    title: str
    authors: list[str]
    publisher: str | None
    published_date: str | None
    isbn: str | None
    page_count: int | None
    description: str | None
    language: str | None
    categories: list[str]
    thumbnail: str | None
    google_books_id: str
    rating: float | None


def stage_google_books_response(
    db: CatalogueDB,
    response: dict[str, object],
    *,
    request: dict[str, object] | None = None,
    snapshot_key: str = "response",
    parser_version: str = GOOGLE_PARSER_VERSION,
) -> tuple[int, int]:
    """Stage a Google Books API response as raw, namespaced source records.

    This function is deliberately independent of ``enrich_book_from_google``;
    it never reads or writes the v1 ``books`` table.
    """
    items = response.get("items", [])
    if not isinstance(items, list):
        items = []
    encoded = json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
    checksum = hashlib.sha256(encoded).hexdigest()
    foundation = CatalogueFoundation(db)
    run = foundation.start_import_run(
        "google_books", parser_version=parser_version, request=request, checksum=checksum
    )
    snapshot = foundation.record_source_snapshot(
        "google_books", snapshot_key, response, import_run=run,
        request=request, parser_version=parser_version,
    )
    staged = skipped = 0
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            skipped += 1
            continue
        info = item.get("volumeInfo")
        if not isinstance(info, dict) or not isinstance(info.get("title"), str) or not info["title"].strip():
            skipped += 1
            continue
        external_id = item.get("id")
        if not isinstance(external_id, str) or not external_id:
            external_id = f"position:{position}"
        foundation.record_source_record(
            snapshot, external_id=external_id, payload=item,
            title=info["title"].strip(), record_type="edition",
        )
        foundation.set_import_checkpoint(run, "records", cursor=str(position), state={"staged": staged + 1})
        staged += 1
    foundation.finish_import_run(run)
    return staged, skipped


def search_google_books(
    query: str,
    api_key: str | None = None,
    language: str = "bn",
    max_results: int = 10,
) -> list[GoogleBookResult]:
    params: dict[str, str | int] = {
        "q": query,
        "maxResults": max_results,
        "printType": "books",
    }
    if api_key:
        params["key"] = api_key
    if language:
        params["langRestrict"] = language

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(GOOGLE_BOOKS_API, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("Google Books API request failed: %s", e)
        return []

    results: list[GoogleBookResult] = []
    for item in data.get("items", []):
        vi = item.get("volumeInfo", {})
        title = vi.get("title", "")
        if not title:
            continue

        authors = vi.get("authors", [])
        publisher = vi.get("publisher")
        published_date = vi.get("publishedDate")
        page_count = vi.get("pageCount")
        description = vi.get("description")
        language = vi.get("language")
        categories = vi.get("categories", [])
        rating = vi.get("averageRating")

        isbn = None
        for ident in vi.get("industryIdentifiers", []):
            if ident.get("type") == "ISBN_13":
                isbn = ident.get("identifier")
                break
            if ident.get("type") == "ISBN_10" and not isbn:
                isbn = ident.get("identifier")

        thumbnails = vi.get("imageLinks", {})
        thumbnail = thumbnails.get("thumbnail") or thumbnails.get("smallThumbnail")

        results.append(GoogleBookResult(
            title=title,
            authors=authors,
            publisher=publisher,
            published_date=published_date,
            isbn=isbn,
            page_count=page_count,
            description=description,
            language=language,
            categories=categories,
            thumbnail=thumbnail,
            google_books_id=item.get("id", ""),
            rating=rating,
        ))

    return results


def enrich_book_from_google(
    db: CatalogueDB,
    api_key: str,
    max_enrichments: int = 500,
    rate_limit_delay: float = 0.1,
) -> int:
    enriched = 0
    unmatched = db.list_books(offset=0, limit=10000)
    books_to_enrich = [b for b in unmatched if not b.google_books_id and not b.isbn]

    for book in books_to_enrich[:max_enrichments]:
        author_names = db.get_book_authors(book.id)  # type: ignore[arg-type]
        author_name = author_names[0].name if author_names else ""

        query = f"intitle:{book.title}"
        if author_name:
            query += f"+inauthor:{author_name}"

        results = search_google_books(
            query=query,
            api_key=api_key,
            language="bn",
            max_results=1,
        )

        if not results:
            query_no_lang = f"intitle:{book.title}"
            if author_name:
                query_no_lang += f"+inauthor:{author_name}"
            results = search_google_books(
                query=query_no_lang,
                api_key=api_key,
                language="",
                max_results=3,
            )

        if not results:
            time.sleep(rate_limit_delay)
            continue

        best = results[0]
        title_sim = _title_similarity(book.title, best.title)
        if title_sim < 0.5:
            time.sleep(rate_limit_delay)
            continue

        updated = False
        if not book.isbn and best.isbn:
            book.isbn = best.isbn
            updated = True
        if not book.description and best.description:
            book.description = best.description[:500]
            updated = True
        if not book.page_count and best.page_count:
            book.page_count = best.page_count
            updated = True
        if not book.publisher and best.publisher:
            book.publisher = best.publisher
            updated = True
        if not book.source_date and best.published_date:
            book.source_date = best.published_date
            updated = True
        if not book.cover_path and best.thumbnail:
            book.cover_path = best.thumbnail
            updated = True
        if best.google_books_id:
            book.google_books_id = best.google_books_id
            updated = True

        if best.categories:
            for cat in best.categories:
                subject_id = db.get_or_create_subject(cat)
                db.add_book_subject(book.id, subject_id)  # type: ignore[arg-type]

        if updated:
            db.update_book(book)
            enriched += 1
            logger.info(
                "Enriched book %d: '%s' with Google Books data",
                book.id, book.title,
            )

        time.sleep(rate_limit_delay)

    logger.info("Google Books enrichment complete: %d books enriched", enriched)
    return enriched


def _title_similarity(title_a: str, title_b: str) -> float:
    a = normalize_bengali(title_a).lower()
    b = normalize_bengali(title_b).lower()
    if a == b:
        return 1.0
    shorter = min(len(a), len(b))
    if shorter == 0:
        return 0.0
    common = sum(1 for ca, cb in zip(a, b) if ca == cb)
    return common / max(len(a), len(b))
