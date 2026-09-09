#!/usr/bin/env python3
"""
PaddleOCR-VL smoke test - process first 3 pages with short timeout to verify GPU works.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

try:
    from paddleocr import PaddleOCR
except ImportError:
    print("Error: paddleocr not installed.")
    sys.exit(1)


def load_manifest(manifest_path):
    """Load manifest JSON file."""
    with open(manifest_path, 'r') as f:
        return json.load(f)


def process_page(ocr, doc_id, page_index, image_path, repo_root):
    """Process a single page with PaddleOCR-VL."""
    full_path = repo_root / image_path

    if not full_path.exists():
        return (doc_id, page_index, 0.0, "", f"File not found: {image_path}")

    start_time = time.monotonic()
    try:
        result = ocr.ocr(str(full_path), cls=True)
        elapsed = time.monotonic() - start_time

        if not result or not result[0]:
            return (doc_id, page_index, elapsed, "", None)

        # Extract text
        texts = []
        for line in result:
            for word_info in line:
                if word_info and len(word_info) > 0:
                    text = word_info[1][0] if isinstance(word_info[1], (list, tuple)) else str(word_info[1])
                    texts.append(text)

        full_text = " ".join(texts)
        return (doc_id, page_index, elapsed, full_text, None)

    except FuturesTimeoutError:
        elapsed = time.monotonic() - start_time
        return (doc_id, page_index, elapsed, "", "timeout")
    except Exception as e:
        elapsed = time.monotonic() - start_time
        return (doc_id, page_index, elapsed, "", str(type(e).__name__) + ": " + str(e)[:80])


def main():
    repo_root = Path(__file__).parent.parent.parent
    manifest_path = repo_root / "pdf-craft-output/benchmark/ocr-engines/manifest.json"

    print(f"Loading manifest from: {manifest_path}")
    manifest = load_manifest(manifest_path)

    # Collect first 3 pages (one from each of first 3 docs, or however they're distributed)
    all_pages = []
    for doc in manifest:
        doc_id = doc["doc_id"]
        for page in doc["pages"][:1]:  # Just first page from each doc
            all_pages.append((doc_id, page["index"], page["image_path"]))
            if len(all_pages) >= 3:
                break
        if len(all_pages) >= 3:
            break

    print(f"Smoke test: processing {len(all_pages)} pages")

    # Initialize PaddleOCR-VL
    print("Initializing PaddleOCR-VL reader (Bengali)...")
    start_init = time.monotonic()
    ocr = PaddleOCR(use_angle_cls=True, lang='ch', use_gpu=True)
    init_elapsed = time.monotonic() - start_init
    print(f"Reader initialized in {init_elapsed:.2f}s. Starting smoke test...")

    results = []
    timings = []

    # Smoke test with 90s timeout per page
    timeout_per_page = 90
    with ThreadPoolExecutor(max_workers=1) as executor:
        for i, (doc_id, page_index, image_path) in enumerate(all_pages, 1):
            future = executor.submit(process_page, ocr, doc_id, page_index, image_path, repo_root)

            try:
                doc_id_res, page_idx, elapsed, text, error = future.result(timeout=timeout_per_page)
            except FuturesTimeoutError:
                print(f"  [{i}/{len(all_pages)}] Page {page_index} (doc {doc_id}): TIMEOUT")
                doc_id_res, page_idx, elapsed, text, error = doc_id, page_index, timeout_per_page, "", "timeout"

            if error:
                print(f"  [{i}/{len(all_pages)}] Page {page_index} (doc {doc_id}): ERROR - {error}")
                return 1
            else:
                timings.append(elapsed)
                snippet = text[:50].replace('\n', ' ') if text else "(empty)"
                print(f"  [{i}/{len(all_pages)}] Page {page_index} (doc {doc_id}): {elapsed:.2f}s - {snippet}")

            results.append({
                "doc_id": doc_id_res,
                "page_index": page_idx,
                "seconds": elapsed,
                "text": text,
                "error": error
            })

    # Report
    print("\n=== SMOKE TEST RESULTS ===")
    print(f"Initialization time: {init_elapsed:.2f}s")
    print(f"Pages processed: {len(all_pages)}")
    if timings:
        mean_time = sum(timings) / len(timings)
        print(f"Mean time per page: {mean_time:.2f}s")
        print(f"Min/max: {min(timings):.2f}s / {max(timings):.2f}s")
        print("\nSmoke test PASSED - GPU is working and performance is acceptable.")
        return 0
    else:
        print("Smoke test FAILED - no successful pages processed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
