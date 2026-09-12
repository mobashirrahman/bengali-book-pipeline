"""Best-effort PDF metadata extraction for the staged catalogue pipeline.

The matcher in :mod:`pdf_craft.catalogue.resolution` falls back to guessing
from file paths when ``metadata_json`` is empty, so this module recovers a
small, high-confidence ``title`` / ``authors`` / ``isbns`` dict from embedded
PDF metadata.  Scanner and converter output is full of junk ``/Title`` values
(``untitled``, ``Microsoft Word - ...``, filenames), so anything that looks
like a placeholder is omitted: an empty dict is a correct result, while a
wrong title would mislead the matcher into a false candidate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .isbn import normalize_isbn

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
# A 13-digit run is tried before a 10-digit one at each position; overlaps
# between the two are resolved by span so a hyphenated ISBN-13 does not also
# yield a bogus ISBN-10 sliced from its middle digits.
_ISBN13_RE = re.compile(r"(?<!\d)(?:\d[\s\-\u00ad]?){13}(?!\d)")
_ISBN10_RE = re.compile(r"(?<!\d)(?:\d[\s\-\u00ad]?){9}[\dXx](?!\d)", re.IGNORECASE)
_TITLE_PLACEHOLDERS = frozenset({
    "document", "new document", "microsoft word", "word document", "print",
    "scan", "scanned", "scanned document", "book", "book1", "pdf",
    "document1", "doc1", "test", "test document", "default", "title",
})
_UNTITLED_RE = re.compile(r"untitled[\s\-_#\d]*", re.IGNORECASE)
_AUTHOR_PLACEHOLDERS = frozenset({
    "unknown", "unknown author", "administrator", "admin", "user", "users",
    "owner", "anonymous", "none", "n/a", "na", "null", "author", "authors",
    "no author",
})
_TITLE_EXTENSIONS = (".pdf", ".doc", ".docx", ".indd")


class PdfMetadataError(ValueError):
    """Raised when a PDF cannot be read for metadata extraction."""


def extract_pdf_metadata(path: str | Path, *, max_text_pages: int = 5) -> dict[str, object]:
    """Return ``title`` / ``authors`` / ``isbns`` recovered from *path*.

    Sources, in priority order, are the ``/Info`` dictionary, XMP metadata,
    and ISBNs validated from the embedded text of the first
    *max_text_pages* pages.  Untrusted input yields either a conservative
    dict or :class:`PdfMetadataError`; junk placeholders are omitted, never
    returned.  The file is only ever opened for reading.
    """
    file_path = Path(path)
    try:
        with file_path.open("rb") as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise PdfMetadataError(f"encrypted PDF: {file_path}")
            if len(reader.pages) == 0:
                raise PdfMetadataError(f"PDF has no pages: {file_path}")
            info_title = _info_field(reader, "title")
            info_author = _info_field(reader, "author")
            xmp = _safe_xmp(reader)
            title, title_origin = _first_valid_title(file_path, info_title, xmp)
            authors, author_origin = _first_valid_authors(info_author, xmp)
            isbns, isbn_origin = _text_isbns(reader, max_text_pages)
    except PdfMetadataError:
        raise
    except (PdfReadError, ValueError, TypeError, OSError, EOFError,
            RecursionError, MemoryError) as exc:
        # One dedicated type for every malformed/truncated input: callers
        # loop over tens of thousands of untrusted files and must not see
        # bare pypdf internals.
        raise PdfMetadataError(f"unreadable PDF {file_path}: {exc}") from exc
    result: dict[str, object] = {}
    provenance: dict[str, str] = {}
    if title is not None:
        result["title"] = title
        provenance["title"] = title_origin
    if authors:
        result["authors"] = authors
        provenance["authors"] = author_origin
    if isbns:
        result["isbns"] = isbns
        provenance["isbns"] = isbn_origin
    if provenance:
        result["provenance"] = provenance
    return result


def _info_field(reader: PdfReader, name: str) -> str | None:
    try:
        value = getattr(reader.metadata, name, None)
    except (PdfReadError, ValueError, TypeError, AttributeError):
        return None
    text = str(value).strip() if value is not None else ""
    return text or None


def _safe_xmp(reader: PdfReader) -> Any | None:
    try:
        return reader.xmp_metadata
    except (PdfReadError, ValueError, TypeError, AttributeError):
        # XMP is a fallback source; a broken XMP packet must not fail the
        # whole extraction when /Info already gave us what we need.
        return None


def _first_valid_title(
    file_path: Path, info_title: str | None, xmp: Any | None,
) -> tuple[str | None, str]:
    if info_title is not None and not _is_junk_title(info_title, file_path):
        return info_title.strip(), "pdf:info:/Title"
    xmp_title = _xmp_title(xmp)
    if xmp_title is not None and not _is_junk_title(xmp_title, file_path):
        return xmp_title.strip(), "pdf:xmp:dc:title"
    return None, ""


def _first_valid_authors(
    info_author: str | None, xmp: Any | None,
) -> tuple[list[str], str]:
    if info_author is not None:
        authors = _split_authors(info_author)
        if authors:
            return authors, "pdf:info:/Author"
    for creator in _xmp_creators(xmp):
        authors = _split_authors(creator)
        if authors:
            return authors, "pdf:xmp:dc:creator"
    return [], ""


def _xmp_title(xmp: Any | None) -> str | None:
    if xmp is None:
        return None
    try:
        raw = xmp.dc_title
    except (PdfReadError, ValueError, TypeError, AttributeError):
        return None
    if isinstance(raw, dict):
        candidates = [raw.get("x-default"), *raw.values()]
    elif isinstance(raw, (list, tuple)):
        candidates = list(raw)
    else:
        candidates = [raw]
    for candidate in candidates:
        text = str(candidate).strip() if candidate is not None else ""
        if text:
            return text
    return None


def _xmp_creators(xmp: Any | None) -> list[str]:
    if xmp is None:
        return []
    try:
        raw = xmp.dc_creator
    except (PdfReadError, ValueError, TypeError, AttributeError):
        return []
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(value).strip() for value in values if value and str(value).strip()]


def _is_junk_title(value: str, file_path: Path) -> bool:
    text = value.strip()
    if not text:
        return True
    lowered = text.lower()
    # Never launder a filename into metadata: converters routinely copy the
    # file name into /Title.
    if lowered in {file_path.name.lower(), file_path.stem.lower()}:
        return True
    if "/" in text or "\\" in text or lowered.endswith(_TITLE_EXTENSIONS):
        return True
    if lowered in _TITLE_PLACEHOLDERS or _UNTITLED_RE.fullmatch(text):
        return True
    if lowered.startswith("microsoft word"):
        return True
    return _is_numeric_or_uuid(text)


def _is_junk_author(value: str) -> bool:
    text = value.strip()
    if not text:
        return True
    if text.lower() in _AUTHOR_PLACEHOLDERS:
        return True
    return _is_numeric_or_uuid(text)


def _is_numeric_or_uuid(text: str) -> bool:
    compact = re.sub(r"[\s\-._]", "", text)
    if compact and compact.isdigit():
        return True
    return bool(_UUID_RE.fullmatch(text.strip()))


def _split_authors(value: str) -> list[str]:
    """Split an ``/Author`` string without breaking ``Surname, Given``."""
    authors: list[str] = []
    for semicolon_part in value.split(";"):
        for and_part in re.split(r"\s+and\s+", semicolon_part, flags=re.IGNORECASE):
            piece = and_part.strip()
            if not piece:
                continue
            if piece.count(",") == 1:
                # A single comma with no other separator is far more likely
                # one "Surname, Given" name than two authors.
                candidates = [piece]
            else:
                candidates = piece.split(",")
            for candidate in candidates:
                name = " ".join(candidate.split())
                if name and not _is_junk_author(name):
                    authors.append(name)
    return authors


def _text_isbns(reader: PdfReader, max_text_pages: int) -> tuple[list[str], str]:
    if max_text_pages <= 0:
        return [], ""
    found: list[str] = []
    seen: set[str] = set()
    origin = ""
    for index, page in enumerate(reader.pages[:max_text_pages]):
        try:
            text = page.extract_text()
        except (PdfReadError, ValueError, TypeError, AttributeError):
            # One unreadable page must not fail the whole file; later pages
            # may still hold a valid ISBN.
            continue
        if not text:
            continue
        for candidate in _isbn_candidates(text):
            try:
                normalized = normalize_isbn(candidate)
            except ValueError:
                continue
            if normalized not in seen:
                seen.add(normalized)
                found.append(normalized)
                if not origin:
                    origin = f"pdf:text:page{index + 1}"
    return found, origin


def _isbn_candidates(text: str) -> list[str]:
    # Prefer longer spans so a hyphenated ISBN-13 suppresses the ISBN-10
    # that could otherwise be sliced from its middle digits.
    spans = [(m.start(), m.end(), m.group(0)) for m in _ISBN13_RE.finditer(text)]
    taken = list(spans)
    for match in _ISBN10_RE.finditer(text):
        span = (match.start(), match.end())
        if any(span[0] < end and start < span[1] for start, end, _ in taken):
            continue
        taken.append((span[0], span[1], match.group(0)))
        spans.append((span[0], span[1], match.group(0)))
    spans.sort()
    return [raw for _, _, raw in spans]
