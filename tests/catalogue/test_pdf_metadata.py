"""Tests for conservative PDF metadata extraction (no guessing)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter

from catalogue import pdf_metadata as module
from catalogue.pdf_metadata import PdfMetadataError, extract_pdf_metadata

VALID_ISBN13 = "9780306406157"
VALID_ISBN13_HYPHENATED = "978-0-306-40615-7"
INVALID_ISBN13 = "9780306406158"


def _write_pdf(path: Path, *, title: str | None = None,
               author: str | None = None, pages: int = 1) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    metadata = {}
    if title is not None:
        metadata["/Title"] = title
    if author is not None:
        metadata["/Author"] = author
    if metadata:
        writer.add_metadata(metadata)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _stub_reader(monkeypatch: pytest.MonkeyPatch, *, metadata: Any = None,
                 pages: list[Any] | None = None, xmp: Any = None) -> None:
    """Replace the module's ``PdfReader`` with a stub holding fixed data."""
    current = {"metadata": metadata, "pages": pages
               if pages is not None else [SimpleNamespace(extract_text=lambda: "")],
               "xmp": xmp}

    def factory(stream: Any) -> Any:
        return SimpleNamespace(is_encrypted=False, metadata=current["metadata"],
                               pages=list(current["pages"]), xmp_metadata=current["xmp"])

    monkeypatch.setattr(module, "PdfReader", factory)


def _text_pages(texts: list[str]) -> list[Any]:
    return [SimpleNamespace(extract_text=lambda text=text: text) for text in texts]


def test_info_title_and_author_are_extracted(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "scan001.pdf", title="Shesher Kabita",
                      author="Rabindranath Tagore")
    assert extract_pdf_metadata(path) == {
        "title": "Shesher Kabita",
        "authors": ["Rabindranath Tagore"],
        "provenance": {"title": "pdf:info:/Title", "authors": "pdf:info:/Author"},
    }


def test_title_matching_filename_stem_is_omitted(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "mytitle.pdf", title="mytitle",
                      author="Good Author")
    result = extract_pdf_metadata(path)
    assert "title" not in result
    assert result["authors"] == ["Good Author"]


def test_generic_title_placeholders_are_omitted(tmp_path: Path) -> None:
    for junk in ("untitled", "Microsoft Word - report.doc"):
        path = _write_pdf(tmp_path / "doc.pdf", title=junk)
        assert extract_pdf_metadata(path) == {}, junk


def test_unknown_author_is_omitted(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "doc.pdf", title="Real Title", author="unknown")
    result = extract_pdf_metadata(path)
    assert "authors" not in result
    assert result["title"] == "Real Title"


def test_single_comma_author_stays_one_name(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "doc.pdf", author="Tagore, Rabindranath")
    assert extract_pdf_metadata(path)["authors"] == ["Tagore, Rabindranath"]


def test_semicolon_authors_split_into_two(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "doc.pdf", author="A. Khan; B. Rahman")
    assert extract_pdf_metadata(path)["authors"] == ["A. Khan", "B. Rahman"]


def test_valid_isbn13_in_page_text_is_extracted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = _write_pdf(tmp_path / "doc.pdf")
    _stub_reader(monkeypatch, pages=_text_pages(["no isbn here", f"ISBN {VALID_ISBN13}"]))
    result = extract_pdf_metadata(path)
    assert result["isbns"] == [VALID_ISBN13]
    assert result["provenance"] == {"isbns": "pdf:text:page2"}


def test_invalid_check_digit_isbn_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = _write_pdf(tmp_path / "doc.pdf")
    _stub_reader(monkeypatch, pages=_text_pages([f"ISBN {INVALID_ISBN13}"]))
    assert extract_pdf_metadata(path) == {}


def test_hyphenated_and_spaced_isbns_normalize_to_bare_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = _write_pdf(tmp_path / "doc.pdf")
    _stub_reader(monkeypatch, pages=_text_pages([f"ISBN {VALID_ISBN13_HYPHENATED}"]))
    hyphenated = extract_pdf_metadata(path)
    _stub_reader(monkeypatch, pages=_text_pages(["978 0 306 40615 7"]))
    spaced = extract_pdf_metadata(path)
    assert hyphenated["isbns"] == [VALID_ISBN13]
    assert spaced["isbns"] == [VALID_ISBN13]


def test_max_text_pages_is_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "doc.pdf")
    _stub_reader(monkeypatch, pages=_text_pages(["", "", f"ISBN {VALID_ISBN13}"]))
    assert extract_pdf_metadata(path, max_text_pages=2) == {}
    assert extract_pdf_metadata(path, max_text_pages=3)["isbns"] == [VALID_ISBN13]


def test_encrypted_or_corrupt_pdf_raises_dedicated_error(tmp_path: Path) -> None:
    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt(user_password="secret")
    with encrypted.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(PdfMetadataError):
        extract_pdf_metadata(encrypted)
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf at all")
    with pytest.raises(PdfMetadataError):
        extract_pdf_metadata(corrupt)


def test_pdf_without_metadata_returns_empty_dict(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "doc.pdf")
    assert extract_pdf_metadata(path) in ({}, {"provenance": {}})


def test_xmp_fills_fields_missing_from_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = _write_pdf(tmp_path / "doc.pdf")
    xmp = SimpleNamespace(dc_title={"x-default": "XMP Title"},
                          dc_creator=["XMP Author"])
    _stub_reader(monkeypatch, xmp=xmp)
    result = extract_pdf_metadata(path)
    assert result["title"] == "XMP Title"
    assert result["authors"] == ["XMP Author"]
    assert result["provenance"] == {
        "title": "pdf:xmp:dc:title", "authors": "pdf:xmp:dc:creator",
    }
