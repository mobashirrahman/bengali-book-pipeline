#!/usr/bin/env python3
"""
EasyOCR benchmark script for Bengali text extraction.
Processes manifest pages and records OCR results with timing.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

try:
    import easyocr
except ImportError:
    print("Error: easyocr not installed. Install with: pip install easyocr")
    sys.exit(1)


def load_manifest(manifest_path):
    """Load manifest JSON file."""
    with open(manifest_path, 'r') as f:
        return json.load(f)


def process_page(reader, doc_id, page_index, image_path, repo_root):
    """
    Process a single page with EasyOCR.
    Returns (doc_id, page_index, seconds, text, confidence, error).
    """
    full_path = repo_root / image_path

    if not full_path.exists():
        return (doc_id, page_index, 0.0, "", 0.0, f"File not found: {image_path}")

    start_time = time.monotonic()
    try:
        result = reader.readtext(str(full_path), detail=1)
        elapsed = time.monotonic() - start_time

        if not result:
            return (doc_id, page_index, elapsed, "", 0.0, None)

        # Extract text and confidence
        texts = []
        confidences = []
        for bbox, text, conf in result:
            texts.append(text)
            confidences.append(conf)

        full_text = " ".join(texts)
        mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        return (doc_id, page_index, elapsed, full_text, mean_confidence, None)

    except FuturesTimeoutError:
        elapsed = time.monotonic() - start_time
        return (doc_id, page_index, elapsed, "", 0.0, "timeout")
    except Exception as e:
        elapsed = time.monotonic() - start_time
        return (doc_id, page_index, elapsed, "", 0.0, str(type(e).__name__) + ": " + str(e)[:50])


def main():
    repo_root = Path(__file__).parent.parent.parent
    manifest_path = repo_root / "pdf-craft-output/benchmark/ocr-engines/manifest.json"
    output_path = repo_root / "pdf-craft-output/benchmark/ocr-engines/results_easyocr.jsonl"

    print(f"Loading manifest from: {manifest_path}")
    manifest = load_manifest(manifest_path)

    # Collect all pages to process
    all_pages = []
    for doc in manifest:
        doc_id = doc["doc_id"]
        for page in doc["pages"]:
            all_pages.append((doc_id, page["index"], page["image_path"]))

    print(f"Found {len(all_pages)} pages across {len(manifest)} documents")

    # Initialize EasyOCR reader once (before timing)
    print("Initializing EasyOCR reader (Bengali)...")
    reader = easyocr.Reader(['bn'], gpu=True)
    print("Reader initialized. Starting benchmark...")

    results = []
    timings = []
    errors = []

    # Process pages with timeout
    with ThreadPoolExecutor(max_workers=1) as executor:
        for i, (doc_id, page_index, image_path) in enumerate(all_pages, 1):
            future = executor.submit(process_page, reader, doc_id, page_index, image_path, repo_root)

            try:
                doc_id_res, page_idx, elapsed, text, confidence, error = future.result(timeout=60)
            except FuturesTimeoutError:
                print(f"  [{i}/{len(all_pages)}] Page {page_index} (doc {doc_id}): TIMEOUT")
                doc_id_res, page_idx, elapsed, text, confidence, error = doc_id, page_index, 60.0, "", 0.0, "timeout"
                errors.append(("timeout", doc_id, page_index))

            if error:
                print(f"  [{i}/{len(all_pages)}] Page {page_index} (doc {doc_id}): ERROR - {error}")
                errors.append((error, doc_id, page_index))
            else:
                timings.append(elapsed)
                print(f"  [{i}/{len(all_pages)}] Page {page_index} (doc {doc_id}): {elapsed:.2f}s, conf={confidence:.3f}")

            result_obj = {
                "doc_id": doc_id_res,
                "page_index": page_idx,
                "engine": "easyocr",
                "seconds": elapsed,
                "text": text,
                "confidence": confidence,
                "error": error
            }
            results.append(result_obj)

    # Write results to JSONL
    print(f"\nWriting {len(results)} results to {output_path}")
    with open(output_path, 'w') as f:
        for result in results:
            f.write(json.dumps(result) + '\n')

    # Report statistics
    print("\n=== BENCHMARK SUMMARY ===")
    print(f"Total pages processed: {len(all_pages)}")
    print(f"Successful pages: {len(timings)}")
    print(f"Errored/timed out: {len(errors)}")

    if timings:
        mean_time = sum(timings) / len(timings)
        median_time = sorted(timings)[len(timings) // 2]
        print(f"Mean time per page: {mean_time:.2f}s")
        print(f"Median time per page: {median_time:.2f}s")
        print(f"Min/max: {min(timings):.2f}s / {max(timings):.2f}s")

    # Compute mean confidence for successful pages
    successful_results = [r for r in results if r["error"] is None]
    if successful_results:
        mean_conf = sum(r["confidence"] for r in successful_results) / len(successful_results)
        print(f"Mean confidence: {mean_conf:.4f}")

    if errors:
        print(f"\nErrors breakdown:")
        error_types = {}
        for error_type, doc_id, page_idx in errors:
            if error_type not in error_types:
                error_types[error_type] = []
            error_types[error_type].append(f"doc {doc_id}, page {page_idx}")
        for error_type, examples in error_types.items():
            print(f"  {error_type}: {len(examples)} occurrences")

    # Show sample text snippets
    print("\n=== SAMPLE TEXT SNIPPETS ===")
    for i, result in enumerate(successful_results[:3]):
        if result["text"]:
            snippet = result["text"][:100].replace('\n', ' ')
            print(f"Sample {i+1} (doc {result['doc_id']}, page {result['page_index']}): {snippet}")

    print("\nResults written to:", output_path)


if __name__ == "__main__":
    main()
