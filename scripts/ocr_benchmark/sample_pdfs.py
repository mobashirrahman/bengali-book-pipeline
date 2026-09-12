#!/usr/bin/env python3
"""Sample 20 random Bangla PDFs and render selected pages for OCR benchmark.

Creates:
- pdf-craft-output/benchmark/ocr-engines/pages/{doc_id}/p{index}.png for each sampled page
- pdf-craft-output/benchmark/ocr-engines/manifest.json with metadata
"""

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional
import random

from catalogue.page_fingerprint import render_pages


def get_page_count(pdf_path: Path) -> Optional[int]:
    """Get page count via pdfinfo or return None on failure."""
    try:
        result = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                try:
                    return int(line.split(":", 1)[1].strip())
                except ValueError:
                    break
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def select_page_indexes(page_count: int) -> list[int]:
    """Select 3 page indexes: 15%, 50%, 85%, clamped to [0, n-1]."""
    if page_count <= 0:
        return []

    indexes = [
        int(page_count * 0.15),
        page_count // 2,
        int(page_count * 0.85),
    ]
    # Clamp to [0, n-1]
    indexes = [max(0, min(page_count - 1, idx)) for idx in indexes]
    # De-duplicate while preserving order
    seen = set()
    result = []
    for idx in indexes:
        if idx not in seen:
            seen.add(idx)
            result.append(idx)
    return result


def main():
    repo_root = Path(__file__).parent.parent.parent
    db_path = repo_root / "pdf-craft-output" / "catalogue" / "catalogue.db"

    # Query database
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, source_path, file_size, metadata_json FROM catalogue_local_documents "
        "WHERE media_type='application/pdf'"
    )
    rows = cursor.fetchall()
    conn.close()

    print(f"Loaded {len(rows)} PDF records from database")

    # Shuffle deterministically
    rng = random.Random(42)
    rng.shuffle(rows)

    # Sample 20 PDFs
    sampled_pdfs = []
    skipped_pdfs = {}

    for doc_id, source_path, file_size, metadata_json in rows:
        if len(sampled_pdfs) >= 20:
            break

        # Check file_size > 0
        if file_size <= 0:
            skipped_pdfs[doc_id] = f"file_size={file_size}"
            continue

        # Check file exists
        pdf_full_path = repo_root / source_path
        if not pdf_full_path.exists():
            skipped_pdfs[doc_id] = f"file not found at {source_path}"
            continue

        # Get page count
        page_count = get_page_count(pdf_full_path)
        if page_count is None or page_count <= 0:
            skipped_pdfs[doc_id] = f"pdfinfo failed or page_count={page_count}"
            continue

        # Select page indexes
        page_indexes = select_page_indexes(page_count)
        if not page_indexes:
            skipped_pdfs[doc_id] = "no valid page indexes"
            continue

        # Render pages
        try:
            rendered = render_pages(str(pdf_full_path), page_indexes, dpi=300)
        except Exception as e:
            skipped_pdfs[doc_id] = f"render_pages failed: {e}"
            continue

        if not rendered:
            skipped_pdfs[doc_id] = "render_pages returned no images"
            continue

        # Save images
        output_dir = repo_root / "pdf-craft-output" / "benchmark" / "ocr-engines" / "pages" / str(doc_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        saved_pages = []
        for page_index in page_indexes:
            image = rendered.get(page_index)
            if image is None:
                continue

            image_path = output_dir / f"p{page_index}.png"
            image.save(image_path, "PNG")

            # Store relative path from repo root
            rel_path = image_path.relative_to(repo_root)
            saved_pages.append({
                "index": page_index,
                "image_path": str(rel_path),
            })

        if not saved_pages:
            skipped_pdfs[doc_id] = "no pages could be saved"
            continue

        # Parse metadata
        try:
            metadata = json.loads(metadata_json)
            title = metadata.get("title")
        except (json.JSONDecodeError, TypeError):
            title = None

        sampled_pdfs.append({
            "doc_id": doc_id,
            "source_path": source_path,
            "title": title,
            "pages": saved_pages,
        })

        print(f"Sampled PDF {len(sampled_pdfs)}/20: doc_id={doc_id}, title={title}, pages={[p['index'] for p in saved_pages]}")

    # Write manifest
    manifest_path = repo_root / "pdf-craft-output" / "benchmark" / "ocr-engines" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    with open(manifest_path, "w") as f:
        json.dump(sampled_pdfs, f, indent=2)

    print(f"\nManifest written to {manifest_path}")
    print(f"Successfully sampled {len(sampled_pdfs)} PDFs")

    if skipped_pdfs:
        print(f"\nSkipped {len(skipped_pdfs)} PDFs:")
        for doc_id, reason in sorted(skipped_pdfs.items())[:10]:
            print(f"  doc_id={doc_id}: {reason}")
        if len(skipped_pdfs) > 10:
            print(f"  ... and {len(skipped_pdfs) - 10} more")

    # Count total PNGs
    pages_dir = repo_root / "pdf-craft-output" / "benchmark" / "ocr-engines" / "pages"
    png_count = len(list(pages_dir.glob("**/p*.png")))
    print(f"\nTotal PNG files: {png_count}")

    # Verify manifest
    with open(manifest_path) as f:
        manifest = json.load(f)

    total_pages_in_manifest = sum(len(entry["pages"]) for entry in manifest)
    print(f"Total pages in manifest: {total_pages_in_manifest}")

    # Verify all referenced files exist
    missing_files = []
    for entry in manifest:
        for page in entry["pages"]:
            image_path = repo_root / page["image_path"]
            if not image_path.exists():
                missing_files.append(page["image_path"])

    if missing_files:
        print(f"\nERROR: {len(missing_files)} referenced PNG files do not exist:")
        for path in missing_files[:10]:
            print(f"  {path}")
    else:
        print("All referenced PNG files exist.")


if __name__ == "__main__":
    main()
