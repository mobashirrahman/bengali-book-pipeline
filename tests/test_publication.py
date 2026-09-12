import json
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pytest
from PIL import Image
from epub_generator import BookMeta

from pdf_craft import PublicationOptions, EpubRenderer
from pdf_craft.common import save_xml
from pdf_craft.extractor.chapter.chapter import encode
from pdf_craft.renderer.epub.validation import validate_publication
from pdf_craft_tool.book_metadata import load_metadata, publication_settings
from tests.extraction_helpers import make_extraction
from tests.test_book_pipeline import chapter


def test_enriched_metadata_cover_title_and_links(tmp_path):
    extraction = make_extraction(tmp_path / "source", with_toc=True)
    save_xml(encode(chapter()), tmp_path / "source/chapters/chapter_1.xml")
    cover = tmp_path / "override.jpg"
    Image.new("RGB", (100, 150), "navy").save(cover)
    output = tmp_path / "book.epub"
    EpubRenderer().render(extraction, output, lan="bn",
                          book_meta=BookMeta(title="বই & <শিরোনাম>", authors=["লেখক"],
                                             editors=["সম্পাদক"], translators=["অনুবাদক"], publisher="প্রকাশক", isbn="9780000000002"),
                          publication=PublicationOptions(cover_path=cover, source_date="১৯২০",
                                                         edition="প্রথম", subjects=["উপন্যাস"],
                                                         identifier="urn:test:book", rights="Unknown"))
    assert validate_publication(output)["status"] == "passed"
    with ZipFile(output) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext(".//{*}identifier") == "urn:test:book"
        assert opf.findtext(".//{*}subject") == "উপন্যাস"
        assert len(opf.findall(".//{*}creator")) == 1
        assert len(opf.findall(".//{*}contributor")) == 2
        assert any(node.text == "ISBN 9780000000002" for node in opf.findall(".//{*}source"))
        assert opf.find(".//{*}date") is None
        assert opf.find(".//{*}item[@properties='cover-image']") is not None
        assert archive.read("OEBPS/assets/cover.png").startswith(b"\x89PNG")
        title = ET.fromstring(archive.read("OEBPS/pdf-craft-title.xhtml"))
        assert title.findtext(".//{*}h1") == "বই & <শিরোনাম>"
        nav = ET.fromstring(archive.read("OEBPS/nav.xhtml"))
        types = {node.get("{http://www.idpf.org/2007/ops}type") for node in nav.iter()}
        assert {"cover", "titlepage", "toc", "bodymatter"} <= types
        assert [node.get("idref") for node in opf.find("{*}spine")][:2] == ["x_cover.xhtml", "pdf-craft-title"]
        assert opf.find(".//{*}itemref[@idref='nav']") is not None
        assert "মেয়েঢিই ঘরে গেল।" in archive.read("OEBPS/Text/part1.xhtml").decode()


def test_metadata_is_manual_preserved_and_image_content_changes_identity(tmp_path):
    source = tmp_path / "book.pdf"
    sidecar = source.with_suffix(".metadata.json")
    original = json.dumps({"title": "বই", "authors": ["লেখক"], "cover": "cover.png"})
    sidecar.write_text(original)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (10, 10), "red").save(cover)
    values = load_metadata(source)
    before = publication_settings(values)
    Image.new("RGB", (10, 10), "blue").save(cover)
    assert before != publication_settings(load_metadata(source))
    assert load_metadata(source, title="override")["title"] == "override"
    assert sidecar.read_text() == original


@pytest.mark.parametrize("payload", [{"authors": "author"}, {"cover_page": True},
                                      {"cover_page": 0}, {"cover": "a", "cover_page": 1},
                                      {"unknown": 1}, []])
def test_invalid_metadata_rejected(tmp_path, payload):
    source = tmp_path / "book.pdf"
    source.with_suffix(".metadata.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_metadata(source)


def test_missing_metadata_does_not_invent_authors(tmp_path):
    assert load_metadata(tmp_path / "book.pdf") == {"title": "book"}
    with pytest.raises(FileNotFoundError):
        load_metadata(tmp_path / "book.pdf", metadata_path=tmp_path / "missing.json")


def test_publish_saved_extraction_without_models(tmp_path, monkeypatch):
    from pdf_craft_tool.cli import _parser
    from tests.test_book_pipeline import _TestEncoding
    monkeypatch.setattr("pdf_craft.renderer.markdown.bundle.get_encoding", lambda _: _TestEncoding())
    extraction = make_extraction(tmp_path / "source", with_toc=True, language="bn")
    save_xml(encode(chapter()), tmp_path / "source/chapters/chapter_1.xml")
    package = tmp_path / "saved.pcex"
    extraction.export(package)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"identity-only: PDF is not opened without a cover_page override")
    output = tmp_path / "output"
    args = _parser().parse_args(["publish", str(package), "--source", str(source), "--output-dir", str(output)])
    assert args.handler(args) == 0
    assert json.loads((output / "validation.json").read_text())["status"] == "passed"
    assert (output / "book.epub.sha256").exists()
    assert json.loads((output / "metadata.json").read_text())["metadata"]["title"] == "book"
    with pytest.raises(FileExistsError):
        args.handler(args)


def test_selected_cover_page_and_provenance(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from pdf_craft_tool.book_metadata import prepare_publication
    document = Mock()
    document.render_page.return_value = Image.new("RGB", (80, 120), "white")
    monkeypatch.setattr("pdf_craft.pdf.handler.DefaultPDFHandler.open", lambda *_: document)
    _, options = prepare_publication({"title": "Book", "cover_page": 3}, tmp_path / "book.pdf", "abc", tmp_path)
    document.render_page.assert_called_once_with(3, 150)
    document.close.assert_called_once()
    assert options.cover_path.exists()
    assert json.loads((tmp_path / "metadata.json").read_text())["cover_origin"] == "PDF page 3"


def test_validator_rejects_missing_resources(tmp_path):
    from zipfile import ZIP_STORED
    path = tmp_path / "broken.epub"
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("page.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="missing.png"/></body></html>')
    with pytest.raises(ValueError, match="Missing EPUB resource"):
        validate_publication(path)


def test_publication_queue_revises_metadata_without_touching_ocr_queue(tmp_path, monkeypatch):
    from pdf_craft_tool.cluster_queue import BookQueue
    from pdf_craft_tool.publication_queue import PublicationQueue
    from pdf_craft_tool.book import file_hash
    from tests.test_book_pipeline import _TestEncoding
    monkeypatch.setattr("pdf_craft.renderer.markdown.bundle.get_encoding", lambda _: _TestEncoding())
    extraction = make_extraction(tmp_path / "extraction", with_toc=True, language="bn")
    save_xml(encode(chapter()), tmp_path / "extraction/chapters/chapter_1.xml")
    package = tmp_path / "saved.pcex"
    extraction.export(package)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"source identity")
    cluster = BookQueue(tmp_path / "queue.sqlite3")
    result = json.dumps({"raw": str(package), "proofreading": "not run"})
    with cluster.db:
        cluster.db.execute("INSERT INTO jobs(id,sha256,profile,source,title,state,result) VALUES(?,?,?,?,?,'done',?)",
                           ("job1", file_hash(source), "original-profile", str(source), "Book", result))
    queue = PublicationQueue(tmp_path, tmp_path / "publications", settle_seconds=0)
    assert queue.scan()["published"] == 1
    first = json.loads((queue.root / "records/job1.json").read_text())
    assert queue.scan()["unchanged"] == 1
    sidecar = queue.root / "metadata/job1.json"
    sidecar.write_text(json.dumps({"title": "Corrected title", "authors": ["Verified author"]}))
    assert queue.scan()["published"] == 1
    second = json.loads((queue.root / "records/job1.json").read_text())
    assert second["output"] != first["output"]
    assert __import__("pathlib").Path(first["output"]).exists()
    assert tuple(cluster.db.execute("SELECT state,profile,result FROM jobs").fetchone()) == ("done", "original-profile", result)
    # A bad override fails this publication, not the OCR job or entire watcher.
    sidecar.write_text('{"authors": "wrong type"}')
    assert queue.scan()["errors"] == 1
    assert json.loads((queue.root / "records/job1.json").read_text())["status"] == "error"
    cluster.close()


def test_mathml_property_declared_in_opf(tmp_path):
    # epub-generator omits the mathml property on head.xhtml even when it
    # contains MathML (live EPUBCheck OPF-014 failures); _enrich must add it.
    from pdf_craft.extractor.chapter.chapter import (
        BlockLayout,
        Chapter,
        InlineExpression,
        ParagraphLayout,
    )
    from pdf_craft.expression import ExpressionKind

    extraction = make_extraction(tmp_path / "source", with_toc=True)
    head = Chapter(None, 0, [ParagraphLayout("text", -1, [BlockLayout(1, 0, (5, 5, 90, 90), ["ভূমিকা"])])])
    head.layouts[0].blocks[0].content.append(InlineExpression(ExpressionKind.INLINE_DOLLAR, "y^2"))
    save_xml(encode(head), tmp_path / "source/chapters/chapter_0.xml")
    save_xml(encode(chapter()), tmp_path / "source/chapters/chapter_1.xml")
    output = tmp_path / "book.epub"
    EpubRenderer().render(extraction, output, lan="bn",
                          book_meta=BookMeta(title="গণিত"),
                          publication=PublicationOptions(identifier="urn:test:mathml"))
    with ZipFile(output) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        head_item = opf.find(".//{*}item[@href='Text/head.xhtml']")
        assert head_item is not None
        assert b"Math/MathML" in archive.read("OEBPS/Text/head.xhtml")
        assert "mathml" in (head_item.get("properties") or "").split()


def test_changed_source_is_superseded_not_error(tmp_path, monkeypatch):
    from pdf_craft_tool.cluster_queue import BookQueue
    from pdf_craft_tool.publication_queue import PublicationQueue
    from pdf_craft_tool.book import file_hash
    from tests.test_book_pipeline import _TestEncoding
    monkeypatch.setattr("pdf_craft.renderer.markdown.bundle.get_encoding", lambda _: _TestEncoding())
    extraction = make_extraction(tmp_path / "extraction", with_toc=True, language="bn")
    save_xml(encode(chapter()), tmp_path / "extraction/chapters/chapter_1.xml")
    package = tmp_path / "saved.pcex"
    extraction.export(package)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"original source")
    cluster = BookQueue(tmp_path / "queue.sqlite3")
    result = json.dumps({"raw": str(package), "proofreading": "not run"})
    with cluster.db:
        cluster.db.execute("INSERT INTO jobs(id,sha256,profile,source,title,state,result) VALUES(?,?,?,?,?,'done',?)",
                           ("job1", file_hash(source), "profile", str(source), "Book", result))
    queue = PublicationQueue(tmp_path, tmp_path / "publications", settle_seconds=0)
    assert queue.scan()["published"] == 1
    source.write_bytes(b"replacement PDF bytes")
    counts = queue.scan()
    assert counts["superseded"] == 1
    assert counts["errors"] == 0
    record = json.loads((queue.root / "records/job1.json").read_text())
    assert record["status"] == "superseded"
    cluster.close()


def test_overlong_sidecar_name_treated_as_absent(tmp_path, monkeypatch):
    from pdf_craft_tool.book_metadata import load_metadata
    from pdf_craft_tool.publication_queue import _exists

    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setattr("pathlib.Path.exists", lambda self: (_ for _ in ()).throw(OSError(36, "File name too long")))
    assert _exists(source.with_suffix(".metadata.json")) is False
    assert load_metadata(source) == {"title": "book"}
