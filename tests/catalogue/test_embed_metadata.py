"""Tests for writing metadata back into the user's own book files.

Most of these are safety tests rather than feature tests.  This module is the
only part of the catalogue that modifies the collection in place, so what
matters most is not that a successful write works but that an unsuccessful one
leaves the file exactly as it found it.
"""

from __future__ import annotations

import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from pdf_craft.catalogue.embed_metadata import (
    FAILED,
    SKIPPED,
    WRITTEN,
    embed_document,
    embed_epub,
    embed_pdf,
)

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Old</dc:title>
<dc:creator>Old Author</dc:creator>
</metadata>
</package>
"""


def make_pdf(path, pages: int = 2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(200, 200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def make_epub(path, opf: str = OPF):
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, "application/epub+zip")
        archive.writestr("content.opf", opf)
        archive.writestr("chapter.xhtml", "<html><body>x</body></html>")
    return path


# --- PDF ---------------------------------------------------------------------


def test_pdf_title_and_author_are_readable_after_writing(tmp_path):
    path = make_pdf(tmp_path / "book.pdf")
    result = embed_pdf(path, title="চাঁদের অমাবস্যা", authors=["সৈয়দ ওয়ালীউল্লাহ্"])
    assert result.status == WRITTEN
    metadata = PdfReader(str(path)).metadata
    assert metadata["/Title"] == "চাঁদের অমাবস্যা"
    assert metadata["/Author"] == "সৈয়দ ওয়ালীউল্লাহ্"


def test_pdf_pages_survive_the_write(tmp_path):
    path = make_pdf(tmp_path / "book.pdf", pages=2)
    embed_pdf(path, title="T")
    assert len(PdfReader(str(path)).pages) == 2


def test_pdf_write_appends_and_keeps_the_original_bytes(tmp_path):
    # The prefix check is the real guarantee that page content was never
    # re-encoded: an incremental update only ever adds to the end of the file.
    path = make_pdf(tmp_path / "book.pdf")
    original = path.read_bytes()
    embed_pdf(path, title="T")
    written = path.read_bytes()
    assert len(written) > len(original)
    assert written.startswith(original)


def test_pdf_dry_run_changes_nothing(tmp_path):
    path = make_pdf(tmp_path / "book.pdf")
    original = path.read_bytes()
    result = embed_pdf(path, title="T", dry_run=True)
    assert result.status == SKIPPED
    assert result.sha256_before
    assert path.read_bytes() == original


def test_pdf_with_nothing_to_write_is_skipped(tmp_path):
    path = make_pdf(tmp_path / "book.pdf")
    original = path.read_bytes()
    assert embed_pdf(path, title="", authors=[]).status == SKIPPED
    assert path.read_bytes() == original


def test_missing_pdf_fails_without_raising(tmp_path):
    assert embed_pdf(tmp_path / "absent.pdf", title="T").status == FAILED


def test_corrupt_pdf_is_refused_without_touching_the_file(tmp_path):
    # Guards the collection: an unreadable file must never be overwritten by a
    # half-written replacement.
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf")
    result = embed_pdf(path, title="T")
    assert result.status == FAILED
    assert path.read_bytes() == b"not a pdf"


def test_failed_write_leaves_no_temporary_file_behind(tmp_path):
    # A stray .tmp beside a book would end up ingested as a new document.
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf")
    embed_pdf(path, title="T")
    assert list(tmp_path.glob("*.tmp")) == []


def test_multiple_pdf_authors_are_joined(tmp_path):
    path = make_pdf(tmp_path / "book.pdf")
    embed_pdf(path, title="T", authors=["A One", "B Two"])
    assert PdfReader(str(path)).metadata["/Author"] == "A One, B Two"


# --- EPUB --------------------------------------------------------------------


def read_opf(path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("content.opf").decode("utf-8")


def test_epub_title_is_replaced_not_duplicated(tmp_path):
    path = make_epub(tmp_path / "book.epub")
    assert embed_epub(path, title="New Title").status == WRITTEN
    opf = read_opf(path)
    assert opf.count("<dc:title") == 1
    assert "<dc:title>New Title</dc:title>" in opf
    assert "<dc:title>Old</dc:title>" not in opf


def test_epub_creators_are_replaced_wholesale(tmp_path):
    # A partial overwrite would leave a mixture of old and new attributions.
    path = make_epub(tmp_path / "book.epub")
    embed_epub(path, title="T", authors=["One", "Two"])
    opf = read_opf(path)
    assert opf.count("<dc:creator>") == 2
    assert "Old Author" not in opf


def test_epub_mimetype_stays_first_and_uncompressed(tmp_path):
    # Readers reject an EPUB whose mimetype entry is moved or deflated.
    path = make_epub(tmp_path / "book.epub")
    embed_epub(path, title="T")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
    assert entries[0].filename == "mimetype"
    assert entries[0].compress_type == zipfile.ZIP_STORED


def test_epub_keeps_every_entry_and_stays_valid(tmp_path):
    path = make_epub(tmp_path / "book.epub")
    embed_epub(path, title="T")
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == 3


def test_epub_title_is_xml_escaped(tmp_path):
    path = make_epub(tmp_path / "book.epub")
    embed_epub(path, title="Tom & <Jerry>")
    opf = read_opf(path)
    assert "Tom &amp; &lt;Jerry&gt;" in opf


def test_non_zip_epub_is_refused_without_touching_the_file(tmp_path):
    path = tmp_path / "broken.epub"
    path.write_bytes(b"definitely not a zip")
    assert embed_epub(path, title="T").status == FAILED
    assert path.read_bytes() == b"definitely not a zip"


def test_epub_without_an_opf_is_refused(tmp_path):
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("chapter.xhtml", "<html/>")
    original = path.read_bytes()
    assert embed_epub(path, title="T").status == FAILED
    assert path.read_bytes() == original


# --- Dispatch ----------------------------------------------------------------


@pytest.mark.parametrize(
    "media_type, suffix",
    [("application/epub+zip", ".epub"), ("application/pdf", ".pdf")],
)
def test_embed_document_routes_on_media_type(tmp_path, media_type, suffix):
    path = tmp_path / f"book{suffix}"
    if suffix == ".epub":
        make_epub(path)
    else:
        make_pdf(path)
    assert embed_document(path, media_type, title="T").status == WRITTEN
