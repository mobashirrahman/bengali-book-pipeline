"""Tests for safe staged embedding: round trips, no source mutation, rejection."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from pdf_craft.catalogue import metadata_embed as embed
from pdf_craft.catalogue import metadata_manifest as manifest
from pdf_craft.catalogue.epub_metadata import extract_epub_metadata
from pdf_craft.catalogue.pdf_metadata import extract_pdf_metadata

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="bookid">
  <metadata>
    <dc:title>Old Title</dc:title>
    <dc:creator>Old Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest><item id="t" href="t.xhtml" media-type="application/xhtml+xml"/></manifest>
</package>
"""


def _write_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _write_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/t.xhtml", "<html/>")
    return path


def _manifest_for(paths: list[Path], directory: Path) -> Path:
    entries = {}
    for src in paths:
        entries[str(src)] = manifest.build_entry(src, read_embedded=False)
        # read_embedded=False leaves titles from filenames only; force an
        # accepted value so prepare has something to embed.
        entries[str(src)]["status"] = "ok"
        entries[str(src)]["title"] = f"Title of {src.stem}"
        entries[str(src)]["authors"] = ["Test Author"]
    out = directory / "manifest.json"
    manifest.save_manifest({"version": 1, "entries": entries}, out)
    return out


def test_pdf_info_round_trip(tmp_path: Path) -> None:
    src = _write_pdf(tmp_path / "scan.pdf")
    staged = tmp_path / "staged.pdf"
    embed.embed_pdf_metadata(src, staged, "Shesher Kabita", ["Rabindranath Tagore"])
    result = extract_pdf_metadata(staged)
    assert result["title"] == "Shesher Kabita"
    assert result["authors"] == ["Rabindranath Tagore"]
    # The source is untouched by the write path.
    assert extract_pdf_metadata(src) == {}


def test_epub_opf_round_trip(tmp_path: Path) -> None:
    src = _write_epub(tmp_path / "book.epub")
    staged = tmp_path / "staged.epub"
    embed.embed_epub_metadata(src, staged, "New Title", ["New Author"])
    result = extract_epub_metadata(staged)
    assert result["title"] == "New Title"
    assert result["authors"] == ["New Author"]
    # Untouched fields survive the rewrite.
    assert result["language"] == "en"
    assert extract_epub_metadata(src)["title"] == "Old Title"


def test_prepare_does_not_mutate_sources(tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "Some Book - Some Author.pdf")
    epub = _write_epub(tmp_path / "Other Book - Other Author.epub")
    before = {str(p): manifest.sha256_of(p) for p in (pdf, epub)}
    manifest_path = _manifest_for([pdf, epub], tmp_path)
    report = embed.prepare_plan(
        manifest_path, tmp_path / "staging", plan_path=tmp_path / "plan.json")
    assert report == {"examined": 2, "prepared": 2, "reused": 0,
                      "skipped": 0, "errors": 0}
    assert {str(p): manifest.sha256_of(p) for p in (pdf, epub)} == before
    check = embed.verify_plan(tmp_path / "plan.json")
    assert check["verified"] == 2
    assert check["mismatch"] == 0


def test_apply_rejects_changed_source(tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "Some Book - Some Author.pdf")
    manifest_path = _manifest_for([pdf], tmp_path)
    plan = tmp_path / "plan.json"
    embed.prepare_plan(manifest_path, tmp_path / "staging", plan_path=plan)
    # Simulate an active consumer rewriting the source after prepare.
    # (A blank PdfWriter is byte-deterministic, so append to really change it.)
    pdf.write_bytes(pdf.read_bytes() + b"% changed by another writer\n")
    report = embed.apply_plan(plan)
    assert report["rejected_changed"] == 1
    assert report["applied"] == 0


def test_apply_is_resumable_and_reports_status(tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "Some Book - Some Author.pdf")
    manifest_path = _manifest_for([pdf], tmp_path)
    plan = tmp_path / "plan.json"
    embed.prepare_plan(manifest_path, tmp_path / "staging", plan_path=plan)
    first = embed.apply_plan(plan)
    assert first["applied"] == 1
    assert extract_pdf_metadata(pdf)["title"] == f"Title of {pdf.stem}"
    second = embed.apply_plan(plan)
    assert second["already_applied"] == 1
    assert second["applied"] == 0
    status = embed.status_report(plan)
    assert status["already_applied"] == 1
    assert status["staged_ok"] == 1


def test_dry_run_prepare_and_apply_write_nothing(tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "Some Book - Some Author.pdf")
    manifest_path = _manifest_for([pdf], tmp_path)
    report = embed.prepare_plan(
        manifest_path, tmp_path / "staging",
        plan_path=tmp_path / "plan.json", dry_run=True)
    assert report["prepared"] == 1
    assert not (tmp_path / "plan.json").exists()
    plan = json.loads((tmp_path / "manifest.json").read_text())  # sanity
    assert plan["entries"]


def test_pdf_embedding_preserves_outlines_and_pages(tmp_path: Path) -> None:
    from pypdf import PdfReader

    src = tmp_path / "outlined.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    writer.add_outline_item("Chapter One", 0)
    writer.add_outline_item("Chapter Two", 1)
    writer.add_metadata({"/Title": "Old Title"})
    with src.open("wb") as stream:
        writer.write(stream)

    staged = tmp_path / "staged.pdf"
    embed.embed_pdf_metadata(src, staged, "New Title", ["New Author"])
    reader = PdfReader(str(staged))
    assert len(reader.pages) == 2
    outline_titles = []

    def collect(items):
        for item in items:
            if isinstance(item, list):
                collect(item)
            else:
                outline_titles.append(item.title)

    collect(reader.outline)
    assert outline_titles == ["Chapter One", "Chapter Two"]
    result = extract_pdf_metadata(staged)
    assert result["title"] == "New Title"
    assert result["authors"] == ["New Author"]
    # The source keeps its old metadata and structure.
    assert extract_pdf_metadata(src)["title"] == "Old Title"


def test_pdf_incremental_output_appends_to_source_bytes(tmp_path: Path) -> None:
    src = _write_pdf(tmp_path / "scan.pdf")
    staged = tmp_path / "staged.pdf"
    embed.embed_pdf_metadata(src, staged, "Shesher Kabita", ["Rabindranath Tagore"])
    source_bytes = src.read_bytes()
    staged_bytes = staged.read_bytes()
    # Incremental, not a rewrite: the source survives byte-identical up
    # front and the update is a small append (title + author objects).
    assert staged_bytes[:len(source_bytes)] == source_bytes
    assert 0 < len(staged_bytes) - len(source_bytes) < 4096
    result = extract_pdf_metadata(staged)
    assert result["title"] == "Shesher Kabita"
    assert result["authors"] == ["Rabindranath Tagore"]


def test_pdf_embed_rejects_encrypted(tmp_path: Path) -> None:
    src = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt(user_password="secret")
    with src.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(ValueError, match="encrypted"):
        embed.embed_pdf_metadata(src, tmp_path / "staged.pdf", "Title", ["Author"])


def test_pdf_embed_rejects_same_source_and_dest(tmp_path: Path) -> None:
    src = _write_pdf(tmp_path / "scan.pdf")
    before = src.read_bytes()
    with pytest.raises(ValueError, match="same file"):
        embed.embed_pdf_metadata(src, src, "Title", ["Author"])
    # A different spelling of the same path is still the same file.
    alias = tmp_path / "sub" / ".." / "scan.pdf"
    with pytest.raises(ValueError, match="same file"):
        embed.embed_pdf_metadata(src, alias, "Title", ["Author"])
    assert src.read_bytes() == before


def test_pdf_embed_preserves_existing_info(tmp_path: Path) -> None:
    from pypdf import PdfReader

    src = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "Old Title", "/Producer": "Scanner 3000",
                         "/Creator": "scan-tool"})
    with src.open("wb") as stream:
        writer.write(stream)
    staged = tmp_path / "staged.pdf"
    embed.embed_pdf_metadata(src, staged, "New Title", ["New Author"])
    info = PdfReader(str(staged)).metadata
    assert info.title == "New Title"
    assert info.author == "New Author"
    assert info.producer == "Scanner 3000"
    assert info.creator == "scan-tool"


def test_pdf_embed_publishes_atomically_without_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pypdf

    src = _write_pdf(tmp_path / "scan.pdf")
    before = src.read_bytes()
    staged = tmp_path / "staging" / "staged.pdf"
    embed.embed_pdf_metadata(src, staged, "Title", ["Author"])
    assert staged.is_file()
    assert list(tmp_path.glob("**/*.tmp")) == []

    def _fail(self, stream):
        raise OSError("disk on fire")

    monkeypatch.setattr(pypdf.PdfWriter, "write", _fail)
    with pytest.raises(OSError, match="disk on fire"):
        embed.embed_pdf_metadata(src, tmp_path / "staging" / "other.pdf", "T", ["A"])
    # Neither a partial destination nor the temp file may survive.
    assert not (tmp_path / "staging" / "other.pdf").exists()
    assert list(tmp_path.glob("**/*.tmp")) == []
    assert src.read_bytes() == before  # source untouched


def test_epub_rewrite_stays_valid(tmp_path: Path) -> None:
    src = tmp_path / "book.epub"
    with zipfile.ZipFile(src, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        opf_info = zipfile.ZipInfo("OEBPS/content.opf")
        opf_info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(opf_info, OPF)
        archive.writestr("OEBPS/t.xhtml", "<html/>")
    with zipfile.ZipFile(src) as before:
        before_infos = {i.filename: i.compress_type for i in before.infolist()}
        before_blobs = {n: before.read(n) for n in before.namelist()}

    staged = tmp_path / "staged.epub"
    embed.embed_epub_metadata(src, staged, "New Title", ["New Author"])
    with zipfile.ZipFile(staged) as after:
        names = after.namelist()
        infos = {i.filename: i for i in after.infolist()}
        # mimetype first and uncompressed, per the EPUB spec.
        assert names[0] == "mimetype"
        assert infos["mimetype"].compress_type == zipfile.ZIP_STORED
        assert after.read("mimetype") == b"application/epub+zip"
        # Relative order and compression of every other member preserved.
        assert [n for n in names if n != "mimetype"] == [
            n for n in before_blobs if n != "mimetype"]
        for name, compress in before_infos.items():
            if name in ("mimetype", "OEBPS/content.opf"):
                continue
            assert infos[name].compress_type == compress
            assert after.read(name) == before_blobs[name]
    result = extract_epub_metadata(staged)
    assert result["title"] == "New Title"
    assert result["authors"] == ["New Author"]


def test_same_sha_sources_get_distinct_staged_copies(tmp_path: Path) -> None:
    import shutil

    # Byte-identical files under different paths with conflicting
    # accepted metadata (the bio10 corpus has 259 such SHA groups):
    # each record must stage, verify and read back independently.
    first = _write_pdf(tmp_path / "First Book - First Author.pdf")
    second = tmp_path / "Second Book - Second Author.pdf"
    shutil.copy(first, second)
    assert manifest.sha256_of(first) == manifest.sha256_of(second)
    manifest_path = _manifest_for([first, second], tmp_path)
    plan_file = tmp_path / "plan.json"
    report = embed.prepare_plan(
        manifest_path, tmp_path / "staging", plan_path=plan_file)
    assert report == {"examined": 2, "prepared": 2, "reused": 0,
                      "skipped": 0, "errors": 0}
    with open(plan_file, encoding="utf-8") as handle:
        plan = json.load(handle)
    staged = [plan["entries"][str(p)]["staged"] for p in (first, second)]
    assert staged[0] != staged[1]
    assert plan["entries"][str(first)]["source_sha256"] == manifest.sha256_of(first)
    assert plan["entries"][str(second)]["source_sha256"] == manifest.sha256_of(second)
    check = embed.verify_plan(plan_file)
    assert check == {"checked": 2, "verified": 2, "mismatch": 0,
                     "missing": 0, "details": {}}
    assert embed.read_embedded(staged[0])["title"] == f"Title of {first.stem}"
    assert embed.read_embedded(staged[1])["title"] == f"Title of {second.stem}"
