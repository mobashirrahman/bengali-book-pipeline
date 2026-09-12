#!/usr/bin/env python3
"""
Benchmark Tesseract OCR (Bengali) over pages in manifest.
"""

import json
import os
import subprocess
import time
import sys
from pathlib import Path

TESSERACT_BIN = Path(os.environ.get("TESSERACT_BIN", "tesseract"))
TESSDATA_DIR = Path("pdf-craft-output/benchmark/ocr-engines/tessdata")
MANIFEST_PATH = Path("pdf-craft-output/benchmark/ocr-engines/manifest.json")
OUTPUT_PATH = Path("pdf-craft-output/benchmark/ocr-engines/results_tesseract.jsonl")

def run_ocr_page(image_path: str) -> dict:
    """Run tesseract on a single page image."""
    start = time.monotonic()
    error = None
    text = ""

    try:
        result = subprocess.run(
            [
                str(TESSERACT_BIN),
                image_path,
                "stdout",
                "-l", "ben",
                "--tessdata-dir", str(TESSDATA_DIR),
            ],
            capture_output=True,
            timeout=60,
            text=True,
        )

        if result.returncode != 0:
            error = f"exit_code_{result.returncode}"
        else:
            text = result.stdout

    except subprocess.TimeoutExpired:
        error = "timeout"
    except Exception as e:
        error = f"{type(e).__name__}"

    elapsed = time.monotonic() - start

    return {
        "seconds": elapsed,
        "text": text,
        "error": error,
    }

def main():
    # Load manifest
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    # Process each page
    results = []
    for doc in manifest:
        doc_id = doc["doc_id"]
        for page in doc["pages"]:
            page_index = page["index"]
            image_path = page["image_path"]

            # Run OCR
            ocr_result = run_ocr_page(image_path)

            # Build result record
            record = {
                "doc_id": doc_id,
                "page_index": page_index,
                "engine": "tesseract",
                "seconds": ocr_result["seconds"],
                "text": ocr_result["text"],
                "error": ocr_result["error"],
            }
            results.append(record)

    # Write JSONL output
    with open(OUTPUT_PATH, "w") as f:
        for record in results:
            f.write(json.dumps(record) + "\n")

    # Report summary
    total = len(results)
    errors = sum(1 for r in results if r["error"])
    times = [r["seconds"] for r in results if not r["error"]]

    print(f"Processed {total} pages")
    if errors:
        print(f"Errors: {errors}")
    if times:
        print(f"Mean time: {sum(times) / len(times):.2f}s")
        print(f"Median time: {sorted(times)[len(times) // 2]:.2f}s")

    # Print a couple of example texts
    texts_with_content = [r for r in results if r["text"] and not r["error"]]
    if texts_with_content:
        print("\nExample text snippets:")
        for i, r in enumerate(texts_with_content[:2]):
            snippet = r["text"][:100].replace("\n", " ")
            print(f"  [{r['doc_id']}/p{r['page_index']}] {snippet}...")

if __name__ == "__main__":
    main()
