from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pdf_craft.catalogue.epub_metadata import EpubMetadataError, extract_epub_metadata

ISBN_13 = "9780131103627"
ISBN_13_DASHED = "978-0-13-110362-7"
ISBN_13_OTHER = "9780306406157"
UUID = "urn:uuid:12345678-1234-5678-1234-567812345678"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _write_epub(path: Path, opf: str, *, container: str = CONTAINER) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
    return path


def test_epub2_scheme_isbn_extracts_title_authors_isbns(tmp_path: Path) -> None:
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:opf="http://www.idpf.org/2007/opf" unique-identifier="bookid">
  <metadata>
    <dc:title>The C Programming Language</dc:title>
    <dc:creator opf:role="aut">Brian Kernighan</dc:creator>
    <dc:creator opf:role="aut">Dennis Ritchie</dc:creator>
    <dc:identifier opf:scheme="ISBN">{ISBN_13_DASHED}</dc:identifier>
    <dc:identifier opf:scheme="UUID">{UUID}</dc:identifier>
    <dc:language>en</dc:language>
    <dc:publisher>Prentice Hall</dc:publisher>
    <dc:date>1988-04-01</dc:date>
  </metadata>
</package>
"""
    result = extract_epub_metadata(_write_epub(tmp_path / "book.epub", opf))
    assert result["title"] == "The C Programming Language"
    assert result["authors"] == ["Brian Kernighan", "Dennis Ritchie"]
    assert result["isbns"] == [ISBN_13]
    assert result["language"] == "en"
    assert result["publisher"] == "Prentice Hall"
    assert result["date"] == "1988-04-01"


def test_epub3_refines_roles_keep_only_aut(tmp_path: Path) -> None:
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="3.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:title>Sample Book</dc:title>
    <dc:creator id="c1">Author One</dc:creator>
    <dc:creator id="c2">Editor Person</dc:creator>
    <dc:creator id="c3">Translator Person</dc:creator>
    <meta refines="#c1" property="role">aut</meta>
    <meta refines="#c2" property="role">edt</meta>
    <meta refines="#c3" property="role">trl</meta>
    <dc:identifier>{ISBN_13_OTHER}</dc:identifier>
  </metadata>
</package>
"""
    result = extract_epub_metadata(_write_epub(tmp_path / "book.epub", opf))
    assert result["authors"] == ["Author One"]


def test_creators_without_roles_all_returned_in_order(tmp_path: Path) -> None:
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:title>Joint Work</dc:title>
    <dc:creator>First Writer</dc:creator>
    <dc:creator>Second Writer</dc:creator>
    <dc:creator>Third Writer</dc:creator>
  </metadata>
</package>
"""
    result = extract_epub_metadata(_write_epub(tmp_path / "book.epub", opf))
    assert result["authors"] == ["First Writer", "Second Writer", "Third Writer"]


def test_uuid_identifier_never_becomes_isbn(tmp_path: Path) -> None:
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:title>UUID Book</dc:title>
    <dc:identifier>{UUID}</dc:identifier>
    <dc:identifier>random-id-42</dc:identifier>
  </metadata>
</package>
"""
    result = extract_epub_metadata(_write_epub(tmp_path / "book.epub", opf))
    assert "isbns" not in result
    assert result["title"] == "UUID Book"


def test_urn_isbn_identifier_extracted_and_normalized(tmp_path: Path) -> None:
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:title>URN Book</dc:title>
    <dc:identifier>urn:isbn:{ISBN_13_DASHED}</dc:identifier>
  </metadata>
</package>
"""
    result = extract_epub_metadata(_write_epub(tmp_path / "book.epub", opf))
    assert result["isbns"] == [ISBN_13]


def test_missing_title_omits_key_without_exception(tmp_path: Path) -> None:
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:creator>Lonely Author</dc:creator>
    <dc:identifier>{ISBN_13_OTHER}</dc:identifier>
  </metadata>
</package>
"""
    result = extract_epub_metadata(_write_epub(tmp_path / "book.epub", opf))
    assert "title" not in result
    assert result["authors"] == ["Lonely Author"]
    assert result["isbns"] == [ISBN_13_OTHER]


def test_corrupt_zip_raises_epub_metadata_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not a zip file")
    with pytest.raises(EpubMetadataError):
        extract_epub_metadata(bad)


def test_opf_with_dtd_raises_without_expansion(tmp_path: Path) -> None:
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE package [
  <!ENTITY boom "this must never expand">
]>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:title>&boom;</dc:title>
  </metadata>
</package>
"""
    with pytest.raises(EpubMetadataError):
        extract_epub_metadata(_write_epub(tmp_path / "evil.epub", opf))


def test_prefixed_and_default_namespaces_both_parse(tmp_path: Path) -> None:
    prefixed = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:title>Prefixed Title</dc:title>
    <dc:creator>Prefixed Author</dc:creator>
    <dc:identifier>{ISBN_13_OTHER}</dc:identifier>
  </metadata>
</package>
"""
    default_ns = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">
  <metadata xmlns="http://purl.org/dc/elements/1.1/">
    <title>Default Title</title>
    <creator>Default Author</creator>
    <identifier>{ISBN_13_OTHER}</identifier>
  </metadata>
</package>
"""
    first = extract_epub_metadata(_write_epub(tmp_path / "a.epub", prefixed))
    second = extract_epub_metadata(_write_epub(tmp_path / "b.epub", default_ns))
    assert (first["title"], first["authors"]) == ("Prefixed Title", ["Prefixed Author"])
    assert (second["title"], second["authors"]) == ("Default Title", ["Default Author"])
    assert first["isbns"] == second["isbns"] == [ISBN_13_OTHER]


def test_provenance_reports_each_field_source(tmp_path: Path) -> None:
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:opf="http://www.idpf.org/2007/opf" unique-identifier="bookid">
  <metadata>
    <dc:title>Provenance Book</dc:title>
    <dc:creator opf:role="aut">Known Author</dc:creator>
    <dc:identifier opf:scheme="ISBN">{ISBN_13_DASHED}</dc:identifier>
    <dc:language>bn</dc:language>
    <dc:publisher>Sample Press</dc:publisher>
    <dc:date>2020</dc:date>
  </metadata>
</package>
"""
    result = extract_epub_metadata(_write_epub(tmp_path / "book.epub", opf))
    assert result["provenance"] == {
        "title": "opf:dc:title",
        "authors": "opf:dc:creator",
        "isbns": "opf:dc:identifier",
        "language": "opf:dc:language",
        "publisher": "opf:dc:publisher",
        "date": "opf:dc:date",
    }
