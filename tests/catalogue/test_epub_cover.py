"""Tests for catalogue.epub_cover.extract_epub_cover.

Small hand-built EPUBs via zipfile.ZipFile, no real book files.  The module is
read-only and must never raise: every failure mode returns None.
"""

from __future__ import annotations

import zipfile

from pdf_craft.catalogue.epub_cover import extract_epub_cover

PNG_BYTES = b"\x89PNG\r\n\x1a\ncover-bytes"
JPEG_BYTES = b"\xff\xd8\xffcover-bytes"


def make_epub(path, members: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, "application/epub+zip")
        for name, data in members.items():
            archive.writestr(name, data)
    return path


EPUB3_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>T</dc:title>
</metadata>
<manifest>
<item id="cover" href="cover.png" properties="cover-image" media-type="image/png"/>
<item id="ch" href="chapter.xhtml" media-type="application/xhtml+xml"/>
</manifest>
</package>
"""

EPUB2_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>T</dc:title>
<meta name="cover" content="cover-id"/>
</metadata>
<manifest>
<item id="cover-id" href="cover.jpg" media-type="image/jpeg"/>
<item id="ch" href="chapter.xhtml" media-type="application/xhtml+xml"/>
</manifest>
</package>
"""

NO_COVER_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>T</dc:title>
</metadata>
<manifest>
<item id="pic" href="illustration.png" media-type="image/png"/>
<item id="ch" href="chapter.xhtml" media-type="application/xhtml+xml"/>
</manifest>
</package>
"""


def test_epub3_cover_image_property_is_found(tmp_path):
    path = make_epub(
        tmp_path / "book.epub",
        {"content.opf": EPUB3_OPF.encode(), "cover.png": PNG_BYTES},
    )
    assert extract_epub_cover(path) == (PNG_BYTES, "image/png")


def test_epub2_meta_cover_is_found(tmp_path):
    path = make_epub(
        tmp_path / "book.epub",
        {"content.opf": EPUB2_OPF.encode(), "cover.jpg": JPEG_BYTES},
    )
    assert extract_epub_cover(path) == (JPEG_BYTES, "image/jpeg")


def test_epub_without_a_declared_cover_returns_none(tmp_path):
    path = make_epub(
        tmp_path / "book.epub",
        {"content.opf": NO_COVER_OPF.encode(), "illustration.png": PNG_BYTES},
    )
    assert extract_epub_cover(path) is None


def test_non_zip_file_returns_none(tmp_path):
    path = tmp_path / "broken.epub"
    path.write_bytes(b"definitely not a zip")
    assert extract_epub_cover(path) is None


def test_zip_without_an_opf_returns_none(tmp_path):
    path = make_epub(tmp_path / "book.epub", {"chapter.xhtml": b"<html/>"})
    assert extract_epub_cover(path) is None


def test_dangling_href_returns_none(tmp_path):
    path = make_epub(
        tmp_path / "book.epub",
        {"content.opf": EPUB3_OPF.encode()},
    )
    assert extract_epub_cover(path) is None


def test_href_is_resolved_against_the_opf_directory(tmp_path):
    opf = EPUB3_OPF.replace('href="cover.png"', 'href="images/cover.jpg"')
    path = make_epub(
        tmp_path / "book.epub",
        {
            "OEBPS/content.opf": opf.encode(),
            "OEBPS/images/cover.jpg": JPEG_BYTES,
        },
    )
    assert extract_epub_cover(path) == (JPEG_BYTES, "image/jpeg")
