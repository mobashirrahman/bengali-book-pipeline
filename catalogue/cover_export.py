"""Export first-page cover images for every inventoried PDF.

Each cover is rendered from page 0, written content-addressed
(``<sha256>.jpg``) into the output directory, and recorded in
``index.json`` with the truthful method label ``first-page-extraction``.
Files that cannot be rendered keep an explicit ``error`` status instead
of being silently dropped, so the manifest's "every PDF gets a cover or
an error" requirement is checkable.

The export is resumable: entries whose source identity still matches a
recorded ``exported`` row -- and whose output file still exists -- are
skipped.  Non-PDF manifest entries are recorded as ``skipped``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from .metadata_manifest import load_manifest, sha256_of

COVER_EXPORT_VERSION = 1

#: Truthful method label: the image is the rendered first page, not a
#: publisher-supplied cover.
COVER_METHOD = "first-page-extraction"

STATUS_EXPORTED = "exported"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

Renderer = Callable[[str | Path, list[int]], dict[int, Any]]


def _default_renderer(path: str | Path, pages: list[int]) -> dict[int, Any]:
    from .page_fingerprint import render_pages

    return render_pages(path, pages)


def _content_name(sha256: str) -> str:
    return f"{sha256}.jpg"


def export_covers(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    renderer: Renderer | None = None,
    limit: int | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """Render covers for PDF manifest entries into *output_dir*.

    Returns counts ``{examined, exported, skipped, errors}``.  With
    ``dry_run`` nothing is written and ``exported`` reports what *would*
    be rendered.  The renderer is injectable so tests can avoid poppler.
    """
    manifest = load_manifest(manifest_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.json"
    index: dict[str, Any] = {"version": COVER_EXPORT_VERSION, "entries": {}}
    if index_path.exists():
        try:
            with open(index_path, encoding="utf-8") as handle:
                index = json.load(handle)
        except ValueError:
            index = {"version": COVER_EXPORT_VERSION, "entries": {}}
    records = index.setdefault("entries", {})
    render = renderer or _default_renderer

    report = {"examined": 0, "exported": 0, "skipped": 0, "errors": 0}
    examined = 0
    for key in sorted(manifest.get("entries", {})):
        if limit is not None and examined >= limit:
            break
        entry = manifest["entries"][key]
        if not str(key).lower().endswith(".pdf"):
            records[key] = {
                "status": STATUS_SKIPPED, "reason": "not a PDF",
                "method": COVER_METHOD,
            }
            report["skipped"] += 1
            continue
        examined += 1
        report["examined"] += 1
        source_sha = entry.get("sha256")
        previous = records.get(key)
        output_name = _content_name(source_sha) if source_sha else None
        if (
            not overwrite and previous is not None
            and previous.get("status") == STATUS_EXPORTED
            and previous.get("source_sha256") == source_sha
            and output_name is not None
            and (out / output_name).exists()
        ):
            report["skipped"] += 1
            continue
        if dry_run:
            report["exported"] += 1
            continue
        try:
            images = render(key, [0])
            image = images.get(0)
            if image is None:
                raise RuntimeError("renderer produced no first page")
            if not output_name:
                raise RuntimeError("manifest entry has no source sha256")
            digest = hashlib.sha256(image.tobytes()).hexdigest()
            tmp = out / (output_name + ".tmp")
            image.save(tmp, format="JPEG", quality=85)
            os.replace(tmp, out / output_name)
            records[key] = {
                "status": STATUS_EXPORTED, "output": output_name,
                "source_sha256": source_sha, "image_sha256": digest,
                "method": COVER_METHOD,
            }
            report["exported"] += 1
        except Exception as exc:
            records[key] = {
                "status": STATUS_ERROR, "method": COVER_METHOD,
                "source_sha256": source_sha,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
            report["errors"] += 1
    if not dry_run:
        tmp = index_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, index_path)
    return report


def verify_covers(
    manifest_path: str | Path, output_dir: str | Path,
) -> dict[str, int]:
    """Recheck that every PDF has either an existing cover or an error row."""
    manifest = load_manifest(manifest_path)
    out = Path(output_dir)
    index_path = out / "index.json"
    records: dict[str, Any] = {}
    if index_path.exists():
        try:
            records = json.load(open(index_path, encoding="utf-8")).get("entries", {})
        except ValueError:
            records = {}
    report = {"pdfs": 0, "present": 0, "missing_output": 0, "no_record": 0}
    for key, entry in manifest.get("entries", {}).items():
        if not str(key).lower().endswith(".pdf"):
            continue
        report["pdfs"] += 1
        record = records.get(str(key))
        if record is None:
            report["no_record"] += 1
            continue
        if record.get("status") == STATUS_ERROR:
            # An explicit error is a complete outcome, not a gap.
            report["present"] += 1
            continue
        output = record.get("output")
        if (
            record.get("status") == STATUS_EXPORTED
            and record.get("source_sha256") == entry.get("sha256")
            and output is not None and (out / output).exists()
        ):
            report["present"] += 1
        else:
            report["missing_output"] += 1
    return report


__all__ = ["export_covers", "verify_covers", "COVER_METHOD", "sha256_of"]
