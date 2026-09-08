from __future__ import annotations

import mimetypes
import os
from collections.abc import Callable, Generator, Sequence
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .. import read_repository
from ..database import CatalogueDB
from ..postgres import PostgresCatalogueDB, initialize_postgres, resolve_postgres_dsn
from ..ratings import delete_user_rating, upsert_user_rating
from ..search import get_book_stats, search_books_with_authors

app = FastAPI(title="pdf-craft catalogue", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

_backend_kind: str | None = None
_db_path: Path | None = None
_postgres_dsn: str | None = None
_content_root: Path | None = None
_asset_root: Path | None = None
_auth_resolver: Callable[[Request], str | None] | None = None


def get_db() -> Generator[CatalogueDB | PostgresCatalogueDB, None, None]:
    if _backend_kind == "postgres" and _postgres_dsn is not None:
        db = PostgresCatalogueDB.connect(_postgres_dsn)
    elif _backend_kind == "sqlite" and _db_path is not None:
        db = CatalogueDB.connect(_db_path)
    else:
        raise HTTPException(status_code=503, detail="Database not initialized")
    try:
        yield db
    finally:
        db.close()


def get_sqlite_db() -> Generator[CatalogueDB, None, None]:
    """Dependency for the legacy API, which is intentionally SQLite-only."""
    if _backend_kind != "sqlite" or _db_path is None:
        raise HTTPException(
            status_code=503,
            detail="/v1 requires an SQLite backend; use normalized /v2 endpoints with PostgreSQL",
        )
    db = CatalogueDB.connect(_db_path)
    try:
        yield db
    finally:
        db.close()


def init_app(
    db_path: str | Path | None = None,
    cors_origins: Sequence[str] | None = None,
    *,
    postgres_dsn: str | None = None,
    content_root: str | Path | None = None,
    asset_root: str | Path | None = None,
    auth_resolver: Callable[[Request], str | None] | None = None,
) -> FastAPI:
    """Configure explicit SQLite or PostgreSQL request-scoped connections."""
    global _backend_kind, _db_path, _postgres_dsn, _content_root, _asset_root, _auth_resolver
    if postgres_dsn is None and isinstance(db_path, str) and db_path.startswith(("postgres://", "postgresql://")):
        postgres_dsn = db_path
        db_path = None
    if postgres_dsn is None and db_path is None:
        postgres_dsn = os.environ.get("CATALOGUE_POSTGRES_DSN")
    if postgres_dsn is not None:
        resolved = resolve_postgres_dsn(postgres_dsn)
        initialize_postgres(resolved)
        _backend_kind = "postgres"
        _postgres_dsn = resolved
        _db_path = None
    elif db_path is not None:
        path = Path(db_path)
        initialized = CatalogueDB(path)
        initialized.close()
        _backend_kind = "sqlite"
        _db_path = path
        _postgres_dsn = None
    else:
        raise ValueError(
            "Catalogue API requires an SQLite path or PostgreSQL DSN; no backend fallback is configured"
        )
    configured_root = content_root if content_root is not None else os.environ.get("CATALOGUE_CONTENT_ROOT")
    _content_root = Path(configured_root).expanduser().resolve() if configured_root else None
    configured_asset_root = asset_root if asset_root is not None else os.environ.get("CATALOGUE_ASSET_ROOT")
    _asset_root = Path(configured_asset_root).expanduser().resolve() if configured_asset_root else None
    _auth_resolver = auth_resolver
    if cors_origins is not None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            allow_credentials=False,
        )
    return app


def _content_path(document: dict, content_root: Path) -> Path | None:
    """Resolve a catalogue path while keeping it below the approved root."""
    root = content_root.resolve()
    paths = [document.get("source_path")]
    paths.extend(location.get("source_path") for location in document.get("locations", []))
    for raw_path in paths:
        if not raw_path:
            continue
        candidate = Path(str(raw_path))
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return 0, size - 1
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid range")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("invalid range")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start, end = max(size - suffix, 0), size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or end < start:
                raise ValueError
            end = min(end, size - 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid range") from exc
    if size <= 0 or start >= size:
        raise ValueError("invalid range")
    return start, end


def _content_response(
    document: dict,
    path: Path,
    request: Request,
    *,
    download: bool,
    head: bool,
    nosniff: bool = False,
) -> Response:
    size = path.stat().st_size
    try:
        start, end = _byte_range(request.headers.get("range"), size)
    except ValueError:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    assert start is not None and end is not None
    length = end - start + 1
    etag_value = document.get("sha256") or f"asset-{document.get('id', 'content')}-{path.stat().st_mtime_ns}"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{size}" if request.headers.get("range") else "",
        "ETag": f'"{etag_value}"',
        "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
    }
    if nosniff:
        headers["X-Content-Type-Options"] = "nosniff"
    if not headers["Content-Range"]:
        del headers["Content-Range"]
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.name)}"
    media_type = document.get("media_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if request.headers.get("if-none-match") == headers["ETag"] and not request.headers.get("range"):
        return Response(status_code=304, headers={"ETag": headers["ETag"], "Accept-Ranges": "bytes"})
    if head:
        return Response(status_code=206 if request.headers.get("range") else 200, media_type=media_type, headers=headers)

    def iterator() -> Generator[bytes, None, None]:
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(), status_code=206 if request.headers.get("range") else 200,
        media_type=media_type, headers=headers,
    )


# ── Pydantic models ────────────────────────────────────────────────

class BookResponse(BaseModel):
    id: int
    title: str
    title_sort: str | None = None
    description: str | None = None
    language: str | None = None
    publisher: str | None = None
    isbn: str | None = None
    edition: str | None = None
    source_date: str | None = None
    series: str | None = None
    series_index: float | None = None
    page_count: int | None = None
    cover_path: str | None = None
    rokomari_url: str | None = None
    google_books_id: str | None = None
    open_library_key: str | None = None


class AuthorResponse(BaseModel):
    id: int
    name: str
    name_sort: str | None = None


class BookListResponse(BaseModel):
    items: list[BookResponse]
    total: int
    offset: int
    limit: int


class BookWithAuthorsResponse(BaseModel):
    book: BookResponse
    authors: list[AuthorResponse]
    subjects: list[str]


class BookSearchResponse(BaseModel):
    items: list[BookWithAuthorsResponse]
    total: int
    query: str


class StatsResponse(BaseModel):
    total_books: int
    total_authors: int
    total_subjects: int
    total_languages: int
    total_files: int


class ReadingListCreate(BaseModel):
    user_id: int
    name: str


class ReadingListResponse(BaseModel):
    id: int
    user_id: int
    name: str


class BookmarkCreate(BaseModel):
    user_id: int
    book_id: int
    position: str
    note: str | None = None


class ReviewCreate(BaseModel):
    user_id: int
    book_id: int
    rating: int
    text: str | None = None


class ProgressUpdate(BaseModel):
    user_id: int
    book_id: int
    position: str


class NormalizedAsset(BaseModel):
    id: int
    edition_id: int | None = None
    document_id: int | None = None
    asset_type: str
    storage_uri: str
    source_url: str | None = None
    sha256: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    attribution: str | None = None
    rights: str | None = None
    is_selected: int = 0
    status: str = "candidate"
    verification_status: str = "candidate"
    retrieved_at: str | None = None
    metadata_json: str = "{}"


class NormalizedWorkResponse(BaseModel):
    id: int
    title: str
    subtitle: str | None = None
    sort_title: str | None = None
    language: str | None = None
    description: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    identifiers: list[dict]
    editions: list[dict]
    sources: list[str]
    ratings: dict


class NormalizedEditionResponse(BaseModel):
    id: int
    work_id: int | None = None
    title: str
    subtitle: str | None = None
    publisher: str | None = None
    publication_date: str | None = None
    edition_statement: str | None = None
    language: str | None = None
    description: str | None = None
    page_count: int | None = None
    work: dict | None = None
    people: list[dict]
    identifiers: list[dict]
    assets: list[dict]
    sources: list[str]
    documents: list[dict]


class NormalizedDocumentResponse(BaseModel):
    id: int
    sha256: str
    source_path: str
    file_size: int
    media_type: str
    metadata_json: str
    matches: list[dict]
    assets: list[dict]
    artifacts: list[dict]
    locations: list[dict]


class NormalizedSearchResponse(BaseModel):
    items: list[dict]
    next: str | None = None


class NormalizedStatsResponse(BaseModel):
    works: int
    editions: int
    people: int
    identifiers: int
    local_documents: int
    document_matches: int
    assets: int
    artifacts: int
    readable_works: int
    source_coverage: list[dict]


class CommunityRatingResponse(BaseModel):
    average: float | None = None
    count: int
    user_rating: int | None = None


class ExternalRatingResponse(BaseModel):
    provider: str
    rating_value: float | None = None
    scale_max: float
    rating_count: int | None = None
    review_count: int | None = None
    source_url: str | None = None
    status: str
    reason: str | None = None


class WorkRatingsResponse(BaseModel):
    work_id: int
    community: CommunityRatingResponse
    external: list[ExternalRatingResponse]


class UserRatingUpdate(BaseModel):
    rating: int = Field(..., ge=1, le=5)


# Normalized v2 uses a fresh connection for every request, leaving the database
# implementation replaceable while keeping the rating write boundary explicit.
@app.get("/v2/health")
def normalized_health(db: CatalogueDB | PostgresCatalogueDB = Depends(get_db)) -> dict[str, str]:
    db.conn.execute("SELECT 1")
    return {"status": "ok", "backend": "postgres" if isinstance(db, PostgresCatalogueDB) else "sqlite"}


@app.get("/v2/stats", response_model=NormalizedStatsResponse)
def normalized_stats(db: CatalogueDB | PostgresCatalogueDB = Depends(get_db)) -> NormalizedStatsResponse:
    return NormalizedStatsResponse(**read_repository.stats(db.conn))


@app.get("/v2/search", response_model=NormalizedSearchResponse)
def normalized_search(
    q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100),
    after: str | None = Query(None), has_documents: bool = Query(False),
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> NormalizedSearchResponse:
    # has_documents is additive and defaults off, so existing callers keep the
    # unfiltered result set; readers pass it to search only what they can open.
    try:
        return NormalizedSearchResponse(**read_repository.search(db.conn, q, limit, after, has_documents))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v2/works", response_model=list[NormalizedWorkResponse])
def normalized_works(
    limit: int = Query(20, ge=1, le=100), after: int | None = Query(None, ge=0),
    has_documents: bool = Query(False),
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> list[NormalizedWorkResponse]:
    return [NormalizedWorkResponse(**row) for row in read_repository.list_works(db.conn, limit, after, has_documents)]


@app.get("/v2/works/{work_id}", response_model=NormalizedWorkResponse)
def normalized_work(work_id: int, db: CatalogueDB | PostgresCatalogueDB = Depends(get_db)) -> NormalizedWorkResponse:
    row = read_repository.get_work(db.conn, work_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Work not found")
    return NormalizedWorkResponse(**row)


def _current_subject(request: Request) -> str:
    if _auth_resolver is None:
        raise HTTPException(status_code=401, detail="Authentication is not configured")
    try:
        subject = _auth_resolver(request)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Authentication failed") from exc
    if subject is None or not str(subject).strip():
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(subject).strip()


@app.get("/v2/works/{work_id}/ratings", response_model=WorkRatingsResponse)
def normalized_work_ratings(
    work_id: int,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> WorkRatingsResponse:
    if not read_repository.work_exists(db.conn, work_id):
        raise HTTPException(status_code=404, detail="Work not found")
    subject: str | None = None
    if _auth_resolver is not None:
        try:
            resolved = _auth_resolver(request)
            subject = str(resolved).strip() if resolved is not None and str(resolved).strip() else None
        except Exception:  # noqa: BLE001 - auth adapters must not break public reads
            subject = None
    return WorkRatingsResponse(**read_repository.get_ratings(db.conn, work_id, subject))


@app.put("/v2/works/{work_id}/rating", response_model=WorkRatingsResponse)
def put_work_rating(
    work_id: int,
    data: UserRatingUpdate,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> WorkRatingsResponse:
    subject = _current_subject(request)
    if not read_repository.work_exists(db.conn, work_id):
        raise HTTPException(status_code=404, detail="Work not found")
    try:
        upsert_user_rating(db.conn, work_id, subject, data.rating)
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    return WorkRatingsResponse(**read_repository.get_ratings(db.conn, work_id, subject))


@app.delete("/v2/works/{work_id}/rating", response_model=WorkRatingsResponse)
def remove_work_rating(
    work_id: int,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> WorkRatingsResponse:
    subject = _current_subject(request)
    if not read_repository.work_exists(db.conn, work_id):
        raise HTTPException(status_code=404, detail="Work not found")
    try:
        delete_user_rating(db.conn, work_id, subject)
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    return WorkRatingsResponse(**read_repository.get_ratings(db.conn, work_id, subject))


@app.get("/v2/editions/{edition_id}", response_model=NormalizedEditionResponse)
def normalized_edition(edition_id: int, db: CatalogueDB | PostgresCatalogueDB = Depends(get_db)) -> NormalizedEditionResponse:
    row = read_repository.get_edition(db.conn, edition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Edition not found")
    return NormalizedEditionResponse(**row)


@app.get("/v2/documents/{document_id}", response_model=NormalizedDocumentResponse)
def normalized_document(document_id: int, db: CatalogueDB | PostgresCatalogueDB = Depends(get_db)) -> NormalizedDocumentResponse:
    row = read_repository.get_document(db.conn, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return NormalizedDocumentResponse(**row)


def _serve_document_content(
    document_id: int,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB,
    *,
    download: bool,
) -> Response:
    if _content_root is None:
        raise HTTPException(status_code=503, detail="Document content delivery is not configured")
    row = read_repository.get_document(db.conn, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    path = _content_path(row, _content_root)
    if path is None:
        raise HTTPException(status_code=404, detail="Document content is unavailable")
    return _content_response(row, path, request, download=download, head=request.method == "HEAD")


@app.api_route("/v2/documents/{document_id}/content", methods=["GET", "HEAD"])
def document_content(
    document_id: int,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> Response:
    return _serve_document_content(document_id, request, db, download=False)


@app.api_route("/v2/documents/{document_id}/content.epub", methods=["GET", "HEAD"])
def document_content_epub(
    document_id: int,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> Response:
    return _serve_document_content(document_id, request, db, download=False)


@app.api_route("/v2/documents/{document_id}/download", methods=["GET", "HEAD"])
def document_download(
    document_id: int,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> Response:
    return _serve_document_content(document_id, request, db, download=True)


@app.get("/v2/assets/{asset_id}", response_model=NormalizedAsset)
def normalized_asset(asset_id: int, db: CatalogueDB | PostgresCatalogueDB = Depends(get_db)) -> NormalizedAsset:
    row = read_repository.get_asset(db.conn, asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return NormalizedAsset(**row)


_RASTER_MIME_TYPES = frozenset({
    "image/bmp", "image/gif", "image/jpeg", "image/png", "image/tiff", "image/webp",
})


def _asset_content_path(asset: dict, asset_root: Path) -> tuple[Path, str] | None:
    if not asset.get("is_selected") or asset.get("asset_type") != "cover":
        return None
    storage_uri = asset.get("storage_uri")
    if not isinstance(storage_uri, str) or not storage_uri or storage_uri.startswith("remote:"):
        return None
    mime_type = str(asset.get("mime_type") or mimetypes.guess_type(storage_uri)[0] or "").lower()
    if mime_type not in _RASTER_MIME_TYPES:
        return None
    try:
        root = asset_root.resolve()
        candidate = Path(storage_uri)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved, mime_type


@app.api_route("/v2/assets/{asset_id}/content", methods=["GET", "HEAD"])
def asset_content(
    asset_id: int,
    request: Request,
    db: CatalogueDB | PostgresCatalogueDB = Depends(get_db),
) -> Response:
    if _asset_root is None:
        raise HTTPException(status_code=503, detail="Asset content delivery is not configured")
    asset = read_repository.get_asset(db.conn, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    resolved = _asset_content_path(asset, _asset_root)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Asset content is unavailable")
    path, mime_type = resolved
    response_asset = {**asset, "media_type": mime_type}
    return _content_response(
        response_asset,
        path,
        request,
        download=False,
        head=request.method == "HEAD",
        nosniff=True,
    )


# ── Endpoints ──────────────────────────────────────────────────────

@app.get("/v1/stats", response_model=StatsResponse)
def stats(db: CatalogueDB = Depends(get_sqlite_db)) -> StatsResponse:
    s = get_book_stats(db)
    return StatsResponse(**s)  # type: ignore[arg-type]


@app.get("/v1/books", response_model=BookListResponse)
def list_books(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    author_id: int | None = Query(None),
    language: str | None = Query(None),
    db: CatalogueDB = Depends(get_sqlite_db),
) -> BookListResponse:
    books = db.list_books(offset=offset, limit=limit, author_id=author_id, language=language)
    total = db.count_books(author_id=author_id, language=language)
    return BookListResponse(
        items=[BookResponse(id=b.id, title=b.title, **{k: v for k, v in b.__dict__.items() if k not in ("id", "title")}) for b in books],
        total=total,
        offset=offset,
        limit=limit,
    )


@app.get("/v1/books/search", response_model=BookSearchResponse)
def search_books_endpoint(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: CatalogueDB = Depends(get_sqlite_db),
) -> BookSearchResponse:
    results = search_books_with_authors(db, q, limit=limit, offset=offset)
    items = []
    for book, authors in results:
        subjects = [s.name for s in db.get_book_subjects(book.id)]  # type: ignore[arg-type]
        items.append(BookWithAuthorsResponse(
            book=BookResponse(
                id=book.id,
                title=book.title,
                title_sort=book.title_sort,
                description=book.description,
                language=book.language,
                publisher=book.publisher,
                isbn=book.isbn,
                edition=book.edition,
                source_date=book.source_date,
                series=book.series,
                series_index=book.series_index,
                page_count=book.page_count,
                cover_path=book.cover_path,
                rokomari_url=book.rokomari_url,
                google_books_id=book.google_books_id,
                open_library_key=book.open_library_key,
            ),
            authors=[AuthorResponse(id=a.id, name=a.name, name_sort=a.name_sort) for a in authors],
            subjects=subjects,
        ))
    return BookSearchResponse(items=items, total=len(items), query=q)


@app.get("/v1/books/{book_id}", response_model=BookWithAuthorsResponse)
def get_book(
    book_id: int,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> BookWithAuthorsResponse:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    authors = db.get_book_authors(book_id)
    subjects = [s.name for s in db.get_book_subjects(book_id)]
    return BookWithAuthorsResponse(
        book=BookResponse(
            id=book.id,
            title=book.title,
            title_sort=book.title_sort,
            description=book.description,
            language=book.language,
            publisher=book.publisher,
            isbn=book.isbn,
            edition=book.edition,
            source_date=book.source_date,
            series=book.series,
            series_index=book.series_index,
            page_count=book.page_count,
            cover_path=book.cover_path,
            rokomari_url=book.rokomari_url,
            google_books_id=book.google_books_id,
            open_library_key=book.open_library_key,
        ),
        authors=[AuthorResponse(id=a.id, name=a.name, name_sort=a.name_sort) for a in authors],
        subjects=subjects,
    )


@app.get("/v1/authors", response_model=list[AuthorResponse])
def list_authors(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: CatalogueDB = Depends(get_sqlite_db),
) -> list[AuthorResponse]:
    authors = db.list_authors(offset=offset, limit=limit)
    return [AuthorResponse(id=a.id, name=a.name, name_sort=a.name_sort) for a in authors]


@app.get("/v1/authors/{author_id}")
def get_author(
    author_id: int,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> dict:
    author = db.get_author(author_id)
    if not author:
        raise HTTPException(status_code=404, detail="Author not found")
    books = db.get_author_books(author_id)
    return {
        "author": AuthorResponse(id=author.id, name=author.name, name_sort=author.name_sort),
        "books": [BookResponse(id=b.id, title=b.title, **{k: v for k, v in b.__dict__.items() if k not in ("id", "title")}) for b in books],
    }


@app.post("/v1/reading-lists", response_model=ReadingListResponse)
def create_reading_list(
    data: ReadingListCreate,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> ReadingListResponse:
    from ..models import ReadingList
    rl = ReadingList(user_id=data.user_id, name=data.name)
    rl_id = db.create_reading_list(rl)
    return ReadingListResponse(id=rl_id, user_id=data.user_id, name=data.name)


@app.get("/v1/reading-lists/{list_id}/books", response_model=list[BookResponse])
def get_reading_list_books(
    list_id: int,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> list[BookResponse]:
    books = db.get_reading_list_books(list_id)
    return [BookResponse(id=b.id, title=b.title, **{k: v for k, v in b.__dict__.items() if k not in ("id", "title")}) for b in books]


@app.post("/v1/reading-lists/{list_id}/books")
def add_book_to_list(
    list_id: int,
    book_id: int = Query(...),
    db: CatalogueDB = Depends(get_sqlite_db),
) -> dict:
    db.add_book_to_reading_list(list_id, book_id)
    return {"status": "added"}


@app.post("/v1/bookmarks")
def create_bookmark(
    data: BookmarkCreate,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> dict:
    from ..models import Bookmark
    bm = Bookmark(user_id=data.user_id, book_id=data.book_id, position=data.position, note=data.note)
    bm_id = db.create_bookmark(bm)
    return {"id": bm_id}


@app.get("/v1/books/{book_id}/bookmarks")
def get_book_bookmarks(
    book_id: int,
    user_id: int = Query(...),
    db: CatalogueDB = Depends(get_sqlite_db),
) -> list[dict]:
    bookmarks = db.get_user_bookmarks(user_id, book_id)
    return [{"id": b.id, "position": b.position, "note": b.note, "created_at": b.created_at} for b in bookmarks]


@app.post("/v1/reviews")
def upsert_review(
    data: ReviewCreate,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> dict:
    from ..models import Review
    rev = Review(user_id=data.user_id, book_id=data.book_id, rating=data.rating, text=data.text)
    rev_id = db.upsert_review(rev)
    return {"id": rev_id}


@app.get("/v1/books/{book_id}/reviews")
def get_book_reviews(
    book_id: int,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> list[dict]:
    reviews = db.get_book_reviews(book_id)
    return [{"id": r.id, "user_id": r.user_id, "rating": r.rating, "text": r.text, "created_at": r.created_at} for r in reviews]


@app.post("/v1/progress")
def update_progress(
    data: ProgressUpdate,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> dict:
    from ..models import ReadingProgress
    prog = ReadingProgress(user_id=data.user_id, book_id=data.book_id, position=data.position)
    db.upsert_reading_progress(prog)
    return {"status": "updated"}


@app.get("/v1/progress/{user_id}/{book_id}")
def get_progress(
    user_id: int,
    book_id: int,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> dict | None:
    prog = db.get_reading_progress(user_id, book_id)
    if not prog:
        return None
    return {"position": prog.position, "updated_at": prog.updated_at}


@app.post("/v1/favorites/{user_id}/{book_id}")
def toggle_favorite(
    user_id: int,
    book_id: int,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> dict:
    added = db.toggle_favorite(user_id, book_id)
    return {"favorited": added}


@app.get("/v1/favorites/{user_id}", response_model=list[BookResponse])
def get_favorites(
    user_id: int,
    db: CatalogueDB = Depends(get_sqlite_db),
) -> list[BookResponse]:
    books = db.get_user_favorites(user_id)
    return [BookResponse(id=b.id, title=b.title, **{k: v for k, v in b.__dict__.items() if k not in ("id", "title")}) for b in books]
