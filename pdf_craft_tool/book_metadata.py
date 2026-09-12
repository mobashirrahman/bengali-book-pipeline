"""Editable, explicitly sourced publication metadata; no guessed authors or dates."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from epub_generator import BookMeta
from pdf_craft import PublicationOptions


_TEXT = {"title", "description", "publisher", "isbn", "source_date", "edition", "rights", "identifier", "cover"}
_LISTS = {"authors", "editors", "translators", "subjects"}


def load_metadata(source: Path, *, metadata_path=None, title=None, authors=None):
    """Read adjacent book.metadata.json, never overwrite it. CLI values win."""
    path = Path(metadata_path) if metadata_path else source.with_suffix(".metadata.json")
    try:
        present = path.exists()
    except OSError:
        present = False
    values = json.loads(path.read_text(encoding="utf-8")) if present else {}
    if metadata_path and not present:
        raise FileNotFoundError(path)
    if not isinstance(values, dict) or set(values) - (_TEXT | _LISTS | {"cover_page", "evidence", "structure"}):
        raise ValueError("Metadata must be an object containing documented fields only")
    if "structure" in values and not isinstance(values["structure"], list):
        raise ValueError("structure must be a list of source-block overrides")
    for key in _TEXT:
        if key in values and not isinstance(values[key], str):
            raise ValueError(f"Metadata {key} must be text")
    for key in _LISTS:
        if key in values and (not isinstance(values[key], list) or
                              not all(isinstance(item, str) and item.strip() for item in values[key])):
            raise ValueError(f"Metadata {key} must be a list of nonempty strings")
    if "cover_page" in values and (type(values["cover_page"]) is not int or values["cover_page"] < 1):
        raise ValueError("cover_page must be a positive 1-based PDF page number")
    if values.get("cover") and values.get("cover_page"):
        raise ValueError("Choose either cover or cover_page")
    if title:
        values["title"] = title
    values.setdefault("title", source.stem)
    if authors:
        values["authors"] = authors
    if values.get("cover"):
        values["cover"] = str((path.parent / values["cover"]).resolve())
    return values


def publication_settings(values):
    """Content identity, including replacement image bytes, not just its filename."""
    result = dict(values)
    if values.get("cover"):
        with Path(values["cover"]).open("rb") as stream:
            result["cover_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def prepare_publication(values, source, source_id, directory):
    """Normalize external covers to PNG or render a user-selected original page."""
    cover = None
    if values.get("cover"):
        from PIL import Image, ImageOps
        cover = directory / "cover.png"
        with Image.open(values["cover"]) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.thumbnail((2400, 3200))
            normalized.save(cover)
    elif values.get("cover_page"):
        from pdf_craft.pdf.handler import DefaultPDFHandler
        document = DefaultPDFHandler().open(source)
        try:
            image = document.render_page(values["cover_page"], 150)
            try:
                cover = directory / "cover.png"
                image.save(cover)
            finally:
                image.close()
        finally:
            document.close()
    meta = BookMeta(**{key: values[key] for key in
                      ("title", "description", "publisher", "isbn", "authors", "editors", "translators") if key in values})
    options = PublicationOptions(**{key: values[key] for key in
                                  ("source_date", "edition", "subjects", "rights") if key in values},
                                 identifier=values.get("identifier") or f"urn:sha256:{source_id}",
                                 source_identifier=f"urn:sha256:{source_id}", cover_path=cover)
    record = {"metadata": values, "source_sha256": source_id,
              "cover_origin": "manual image" if values.get("cover") else
                              f"PDF page {values['cover_page']}" if values.get("cover_page") else "extraction cover, if available",
              "publication": {key: value for key, value in asdict(options).items() if key != "cover_path"}}
    (directory / "metadata.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta, options
