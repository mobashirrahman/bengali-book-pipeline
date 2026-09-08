"""Tests for resumable cover export: skip/resume, errors, dry-run."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter

from pdf_craft.catalogue import cover_export
from pdf_craft.catalogue import metadata_manifest as manifest


def _write_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _manifest_with(*names: str, root: Path) -> Path:
    entries = {}
    for name in names:
        src = root / name
        if name.endswith(".pdf"):
            _write_pdf(src)
        else:
            src.write_text("not a pdf")
        entries[str(src)] = manifest.build_entry(src, read_embedded=False)
    out = root / "manifest.json"
    manifest.save_manifest({"version": 1, "entries": entries}, out)
    return out


def _renderer(images: dict[str, Image.Image], *, fail: set[str] | None = None):
    def render(path, pages):
        key = str(path)
        if fail and key in fail:
            raise RuntimeError("poppler exploded")
        return {0: images[key]}
    return render


def test_export_skip_and_resume(tmp_path: Path) -> None:
    src = _write_pdf(tmp_path / "Shesher Kabita - Rabindranath Tagore.pdf")
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake")
    manifest_path = _manifest_with(
        "Shesher Kabita - Rabindranath Tagore.pdf", root=tmp_path)
    # Rebuild manifest including the epub entry manually.
    data = json.loads((tmp_path / "manifest.json").read_text())
    data["entries"][str(epub)] = manifest.build_entry(epub, read_embedded=False)
    manifest.save_manifest(data, tmp_path / "manifest.json")

    images = {str(src): Image.new("RGB", (8, 10), "white")}
    out = tmp_path / "covers"
    first = cover_export.export_covers(
        tmp_path / "manifest.json", out, renderer=_renderer(images))
    assert first["exported"] == 1
    assert first["skipped"] == 1  # the epub
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    record = index["entries"][str(src)]
    assert record["status"] == "exported"
    assert record["method"] == cover_export.COVER_METHOD
    assert (out / record["output"]).exists()

    # Second run resumes: nothing re-rendered.
    calls: list[str] = []

    def counting(path, pages):
        calls.append(str(path))
        return {0: images[str(path)]}

    second = cover_export.export_covers(
        tmp_path / "manifest.json", out, renderer=counting)
    assert second == {"examined": 1, "exported": 0, "skipped": 2, "errors": 0}
    assert calls == []


def test_renderer_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    src = _write_pdf(tmp_path / "Some Book - Some Author.pdf")
    manifest_path = _manifest_with("Some Book - Some Author.pdf", root=tmp_path)
    images = {str(src): Image.new("RGB", (8, 10), "white")}
    out = tmp_path / "covers"
    report = cover_export.export_covers(
        manifest_path, out, renderer=_renderer(images, fail={str(src)}))
    assert report == {"examined": 1, "exported": 0, "skipped": 0, "errors": 1}
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert index["entries"][str(src)]["status"] == "error"
    # verify_covers treats an explicit error as a complete outcome.
    check = cover_export.verify_covers(manifest_path, out)
    assert check == {"pdfs": 1, "present": 1, "missing_output": 0, "no_record": 0}


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = _write_pdf(tmp_path / "Some Book - Some Author.pdf")
    manifest_path = _manifest_with("Some Book - Some Author.pdf", root=tmp_path)
    out = tmp_path / "covers"
    report = cover_export.export_covers(
        manifest_path, out, renderer=_renderer({str(src): None}), dry_run=True)
    assert report["exported"] == 1
    assert not (out / "index.json").exists()
