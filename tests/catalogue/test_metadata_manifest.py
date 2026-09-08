"""Tests for the resumable metadata manifest: idempotence, invalidation, conflicts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfWriter

from pdf_craft.catalogue import metadata_manifest as manifest


def _write_pdf(path: Path, *, title: str | None = None,
               author: str | None = None) -> Path:
    writer = PdfWriter()
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


def _authordir_pdf(root: Path, name: str) -> Path:
    # authordir template "<Title> - <Author>.pdf" yields filename titles.
    path = root / name
    return _write_pdf(path)


def test_manifest_update_is_idempotent(tmp_path: Path) -> None:
    src = _authordir_pdf(tmp_path, "Shesher Kabita - Rabindranath Tagore.pdf")
    out = tmp_path / "manifest.json"
    first = manifest.update_manifest(out, [src], read_embedded=False)
    payload = json.loads(out.read_text(encoding="utf-8"))
    second = manifest.update_manifest(out, [src], read_embedded=False)
    assert first["resolved"] == 1
    assert second == {"examined": 1, "reused": 1, "resolved": 0,
                      "ambiguous": 0, "damaged": 0, "deferred": 0}
    assert json.loads(out.read_text(encoding="utf-8")) == payload
    entry = payload["entries"][str(src)]
    assert entry["status"] == "ok"
    assert entry["title"] == "Shesher Kabita"
    assert entry["authors"] == ["Rabindranath Tagore"]


def test_changed_source_is_reinvalidated(tmp_path: Path) -> None:
    src = _authordir_pdf(tmp_path, "First Title - First Author.pdf")
    out = tmp_path / "manifest.json"
    manifest.update_manifest(out, [src], read_embedded=False)
    # Ensure the mtime actually moves so identity comparison notices.
    time.sleep(0.02)
    src.unlink()
    _authordir_pdf(tmp_path, "Second Title - Second Author.pdf")
    # Same directory, but the path key changed; also rewrite in place to
    # exercise identity mismatch on identical keys.
    renamed = tmp_path / "First Title - First Author.pdf"
    renamed.unlink(missing_ok=True)
    _authordir_pdf(tmp_path, "First Title - First Author.pdf")
    report = manifest.update_manifest(out, [tmp_path / "First Title - First Author.pdf"],
                                      read_embedded=False)
    assert report["reused"] == 0
    assert report["resolved"] == 1


def test_conflicting_filename_and_embedded_is_ambiguous(tmp_path: Path) -> None:
    # Filename says one book, embedded /Info says another: no accepted value.
    src = _write_pdf(tmp_path / "Filename Title - Filename Author.pdf",
                     title="Embedded Title", author="Embedded Author")
    entry = manifest.build_entry(
        src, read_embedded=True,
        embedded_override={"title": "Embedded Title",
                           "authors": ["Embedded Author"]},
    )
    assert entry["status"] == "ambiguous"
    assert entry["title"] is None
    assert entry["authors"] == []
    assert entry["conflict"] is True
    assert "Filename Title" in entry["title_candidates"]
    assert "Embedded Title" in entry["title_candidates"]


def test_title_agreement_survives_author_conflict(tmp_path: Path) -> None:
    # Per-field conservatism: the corroborated title is kept while the
    # contested authors are cleared.
    src = _write_pdf(tmp_path / "Same Title - Filename Author.pdf",
                     title="Same Title", author="Embedded Author")
    entry = manifest.build_entry(src, read_embedded=True)
    assert entry["status"] == "ambiguous"
    assert entry["title"] == "Same Title"
    assert entry["authors"] == []
    assert entry["evidence"]["author_conflict"] is True
    assert entry["evidence"]["title_conflict"] is False


def test_lone_slug_filename_is_empty_not_accepted(tmp_path: Path) -> None:
    # "doc" matches no scraper template (confidence 0.2): laundering it
    # into the catalogue would be a confident wrong match downstream.
    src = _write_pdf(tmp_path / "doc.pdf", title="untitled", author="unknown")
    entry = manifest.build_entry(src, read_embedded=True)
    assert entry["status"] == "empty"
    assert entry["title"] is None
    assert entry["title_candidates"] == ["doc"]


def test_garbled_pdf_without_trusted_name_is_unreadable(tmp_path: Path) -> None:
    src = tmp_path / "qzxw.pdf"
    src.write_bytes(b"not a pdf at all")
    entry = manifest.build_entry(src, read_embedded=True)
    assert entry["status"] == "unreadable"
    assert "embedded_error" in entry["evidence"]


def test_epub_slug_title_conflict_keeps_opf_authors(tmp_path: Path) -> None:
    import zipfile

    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf"
    xmlns:dc="http://purl.org/dc/elements/1.1/" unique-identifier="b">
  <metadata><dc:title>Elating Belating</dc:title><dc:creator>Shamsur Rahman</dc:creator></metadata>
</package>"""
    src = tmp_path / "Elating-belating-Shamsur-Rahoman.epub"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
    entry = manifest.build_entry(src, read_embedded=True)
    # The slug filename matches no template, so it cannot contest the
    # OPF: the authoritative embedded signal is accepted as-is.
    assert entry["status"] == "ok"
    assert entry["title"] == "Elating Belating"
    assert entry["authors"] == ["Shamsur Rahman"]
    assert "Elating-belating-Shamsur-Rahoman" in entry["title_candidates"]


def test_corroborated_signals_are_accepted(tmp_path: Path) -> None:
    # Same title/author in filename and embedded: accepted, provenance kept.
    src = _write_pdf(tmp_path / "Shesher Kabita - Rabindranath Tagore.pdf",
                     title="Shesher Kabita", author="Rabindranath Tagore")
    entry = manifest.build_entry(src, read_embedded=True)
    assert entry["status"] == "ok"
    assert entry["title"] == "Shesher Kabita"
    assert entry["authors"] == ["Rabindranath Tagore"]
    assert entry["evidence"]["filename_titles"] == ["Shesher Kabita"]
    assert entry["evidence"]["embedded_titles"] == ["Shesher Kabita"]


def test_cover_reading_supplements_placeholder_filename(tmp_path: Path) -> None:
    # Placeholder scraper names carry nothing; a lone cover signal is accepted.
    directory = tmp_path / "data" / "incoming-scraped" / "banglabookshelf" / "books"
    directory.mkdir(parents=True)
    src = _write_pdf(directory / "Unknown Author - banglabookshelf - a3bf2028730e.pdf")
    cover = SimpleNamespace(title_candidates=("Cover Title",),
                            author_candidates=("Cover Author",))
    entry = manifest.build_entry(src, read_embedded=False, cover_reading=cover)
    assert entry["status"] == "ok"
    assert entry["title"] == "Cover Title"
    assert entry["authors"] == ["Cover Author"]


def test_missing_file_is_unreadable_not_empty(tmp_path: Path) -> None:
    entry = manifest.build_entry(tmp_path / "nothing-here.pdf", read_embedded=False)
    assert entry["status"] == "unreadable"


def test_unsupported_extension_is_damaged_not_empty(tmp_path: Path) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("hello")
    entry = manifest.build_entry(text)
    assert entry["status"] == "unsupported"


def test_summarize_buckets_statuses(tmp_path: Path) -> None:
    src = _authordir_pdf(tmp_path, "Shesher Kabita - Rabindranath Tagore.pdf")
    out = tmp_path / "manifest.json"
    manifest.update_manifest(out, [src], read_embedded=False)
    data = manifest.load_manifest(out)
    data["entries"]["missing.pdf"] = {"status": "unreadable"}
    summary = manifest.summarize(data)
    assert summary["completed"] == 1
    assert summary["damaged"] == 1


def test_deferred_paths_are_recorded_and_resumable(tmp_path: Path) -> None:
    active = _authordir_pdf(tmp_path, "Active Book - Active Author.pdf")
    idle = _authordir_pdf(tmp_path, "Idle Book - Idle Author.pdf")
    out = tmp_path / "manifest.json"
    report = manifest.update_manifest(
        out, [active, idle], read_embedded=False, deferred={str(active)})
    assert report["deferred"] == 1
    assert report["resolved"] == 2
    data = manifest.load_manifest(out)
    assert data["entries"][str(active)]["status"] == "deferred"
    assert data["entries"][str(idle)]["status"] == "ok"
    # A deferred entry with no accepted values carries no metadata.
    assert data["entries"][str(active)]["title"] is None
    summary = manifest.summarize(data)
    assert summary["deferred"] == 1
    assert summary["completed"] == 1
    # Resume keeps the deferral without re-resolving.
    second = manifest.update_manifest(
        out, [active, idle], read_embedded=False, deferred={str(active)})
    assert second["reused"] == 2
    assert second["deferred"] == 1
    assert manifest.load_manifest(out) == data
    # Lifting the deferral resolves the entry normally.
    third = manifest.update_manifest(out, [active, idle], read_embedded=False)
    assert third["resolved"] == 1
    assert manifest.load_manifest(out)["entries"][str(active)]["status"] == "ok"


def test_defer_list_file_round_trip(tmp_path: Path) -> None:
    active = _authordir_pdf(tmp_path, "Active Book - Active Author.pdf")
    defer_file = tmp_path / "defer.txt"
    defer_file.write_text(f"# active consumers\n{active}\n\n")
    assert manifest.load_path_list(defer_file) == {str(active)}


def test_cover_index_feeds_three_signal_resolution(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "incoming-scraped" / "banglabookshelf" / "books"
    directory.mkdir(parents=True)
    src = _write_pdf(directory / "Unknown Author - banglabookshelf - a3bf2028730e.pdf")
    index_file = tmp_path / "covers.json"
    index_file.write_text(json.dumps({
        str(src): {"title_candidates": ["Cover Title"],
                   "author_candidates": ["Cover Author"]},
    }), encoding="utf-8")
    out = tmp_path / "manifest.json"
    readings = manifest.load_cover_index(index_file)
    report = manifest.update_manifest(
        out, [src], read_embedded=False, cover_readings=readings)
    assert report["resolved"] == 1
    entry = manifest.load_manifest(out)["entries"][str(src)]
    assert entry["status"] == "ok"
    assert entry["title"] == "Cover Title"
    assert entry["authors"] == ["Cover Author"]
    assert "cover_ocr" in entry["evidence"]["signals"]


def test_manifest_cli_wires_defer_list_and_cover_index(tmp_path: Path, capsys) -> None:
    import argparse

    from pdf_craft.catalogue.cli import cmd_metadata_manifest

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    active = corpus / "Active Book - Active Author.pdf"
    _write_pdf(active)
    slug = corpus / "plain-slug.pdf"
    _write_pdf(slug)
    defer_file = tmp_path / "defer.txt"
    defer_file.write_text(str(active) + "\n")
    index_file = tmp_path / "covers.json"
    index_file.write_text(json.dumps({
        str(slug): {"title_candidates": ["Slug Cover Title"],
                    "author_candidates": []},
    }), encoding="utf-8")
    out = tmp_path / "manifest.json"
    cmd_metadata_manifest(argparse.Namespace(
        data=str(corpus), out=str(out), extensions=["pdf"], force=False,
        no_embedded=True, limit=None, dry_run=False,
        defer_list=str(defer_file), cover_index=str(index_file),
    ))
    stdout = capsys.readouterr().out
    assert "deferred=1" in stdout
    data = manifest.load_manifest(out)
    assert data["entries"][str(active)]["status"] == "deferred"
    # "plain-slug" is an untrusted lone filename; the cover index rescues it.
    assert data["entries"][str(slug)]["title"] == "Slug Cover Title"
