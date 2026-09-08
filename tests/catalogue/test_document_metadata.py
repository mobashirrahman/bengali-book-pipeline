"""Tests for metadata fusion.

The precedence asserted here is measured, not assumed: on this corpus the
filename template is right more often than the embedded /Info dictionary,
because /Info is usually written by the scanning tool or the download site.
"""
from __future__ import annotations

from types import SimpleNamespace

from pdf_craft.catalogue.document_metadata import build_document_metadata

_BENGALIEBOOK = (
    "data/incoming-scraped/bengaliebook/books/"
    "Unknown Author - অন্যদিন-হুমায়ূন আহমেদ (Anyadin by Humayun Ahmed).pdf"
)
_LINK_LIST = (
    "data/incoming-scraped/books/"
    "Unknown Author - ৪২৫টি বাংলা বইয়ের pdf ডাউনলোড লিঙ্ক - 97f09e329d0a.pdf"
)
_PLACEHOLDER = (
    "data/incoming-scraped/banglabookshelf/books/"
    "Unknown Author - banglabookshelf - a3bf2028730e.pdf"
)


def _build(path, **kwargs):
    kwargs.setdefault("read_embedded", False)
    return build_document_metadata(path, "application/pdf", **kwargs)


def test_filename_yields_both_scripts():
    # This template carries title and author in Bengali and romanized form, so
    # both must survive as candidates for the matcher to try.
    result = _build(_BENGALIEBOOK)
    assert result["title"] == "অন্যদিন"
    assert "Anyadin" in result["title_candidates"]
    assert result["authors"] == ["হুমায়ূন আহমেদ", "Humayun Ahmed"]
    assert result["_extraction"]["signals"] == ["filename"]


def test_link_list_is_recorded_as_not_a_book():
    # 791 documents are aggregates or fragments.  Recording that is more useful
    # than scraping a title off the filename.
    result = _build(_LINK_LIST)
    assert result["_extraction"]["status"] == "not_a_book"
    assert "title" not in result and "authors" not in result


def test_placeholder_filename_yields_nothing():
    # Deliberate: an absent title lets resolution fall back to path inference,
    # whereas a wrong one produces a confident wrong catalogue match.
    result = _build(_PLACEHOLDER)
    assert "title" not in result
    assert result["_extraction"]["status"] == "empty"


def test_cover_ocr_supplements_a_placeholder_filename():
    # Cover OCR is the only signal for the 2,441 placeholder-named documents.
    cover = SimpleNamespace(
        title_candidates=("আমার অবিশ্বাস",), author_candidates=("হুমায়ুন আজাদ",),
    )
    result = _build(_PLACEHOLDER, cover_reading=cover)
    assert result["title"] == "আমার অবিশ্বাস"
    assert result["authors"] == ["হুমায়ুন আজাদ"]
    assert "cover_ocr" in result["_extraction"]["signals"]


def test_filename_outranks_cover_ocr():
    # OCR garbles Bengali print; a matched rigid template does not.
    cover = SimpleNamespace(title_candidates=("অনদিন",), author_candidates=())
    result = _build(_BENGALIEBOOK, cover_reading=cover)
    assert result["title"] == "অন্যদিন"
    assert "অনদিন" in result["title_candidates"]  # kept as an alternate


def test_signals_are_fused_not_cascaded():
    # A document can have a usable filename title and a usable cover author;
    # taking only the first signal would lose the second.
    cover = SimpleNamespace(title_candidates=(), author_candidates=("Cover Author",))
    result = _build(_BENGALIEBOOK, cover_reading=cover)
    assert result["title"] == "অন্যদিন"
    assert "Cover Author" in result["authors"]


def test_duplicate_candidates_are_collapsed():
    cover = SimpleNamespace(title_candidates=("অন্যদিন",), author_candidates=("হুমায়ূন আহমেদ",))
    result = _build(_BENGALIEBOOK, cover_reading=cover)
    assert result["title_candidates"].count("অন্যদিন") == 1
    assert result["authors"].count("হুমায়ূন আহমেদ") == 1


def test_extraction_block_marks_the_row_as_attempted():
    # A row still holding '{}' has never been tried; one carrying _extraction
    # has, which is what makes the backfill resumable.
    assert "_extraction" in _build(_PLACEHOLDER)


def test_epub_opf_outranks_the_filename_slug():
    """EPUB precedence is inverted relative to PDF, and deliberately so.

    An EPUB's OPF carries real dc:title metadata, while its filename is usually
    a romanized slug: the OPF gives `এলাটিং বেলাটিং` where the filename offers
    only `Elating-belating-Shamsur-Rahoman`. A PDF's /Info is the opposite --
    written by the scanning tool far more often than by a publisher -- so it
    ranks below a matched scraper template.
    """
    from types import SimpleNamespace
    import pdf_craft.catalogue.document_metadata as module

    def fake_epub(path):
        return {"title": "এলাটিং বেলাটিং", "authors": ["শামসুর রাহমান"]}

    original = module.extract_epub_metadata
    module.extract_epub_metadata = fake_epub
    try:
        result = build_document_metadata(
            "data/ই-পাব/Elating-belating-Shamsur-Rahoman.epub",
            "application/epub+zip",
            read_embedded=True,
        )
    finally:
        module.extract_epub_metadata = original
    assert result["title"] == "এলাটিং বেলাটিং"
    assert result["authors"][0] == "শামসুর রাহমান"
    assert result["_extraction"]["precedence"] == "embedded_first"


def test_pdf_keeps_filename_precedence():
    result = _build(_BENGALIEBOOK)
    assert result["_extraction"]["precedence"] == "filename_first"


def test_format_named_directory_is_not_an_author():
    # `ই-পাব` is Bengali for "e-pub"; it was written as the author of every
    # EPUB stored under it.
    from pdf_craft.catalogue.filename_parser import is_probably_person_name

    assert not is_probably_person_name("ই-পাব")
    assert not is_probably_person_name("epub-staging")
