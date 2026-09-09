#!/usr/bin/env python3
"""
Aggregate OCR benchmark results into summary CSV and proxy accuracy scores.

Input files (all in pdf-craft-output/benchmark/ocr-engines/):
  - manifest.json: 20 PDFs, 60 pages total
  - results_tesseract.jsonl: 60 rows
  - results_easyocr.jsonl: 60 rows

Output files:
  - summary.csv: per-page metrics + engine-level aggregates
  - proxy_scores.json: accuracy proxies (no ground truth available)
"""

import json
import csv
import statistics
from pathlib import Path
from typing import Dict, List, Tuple
from difflib import SequenceMatcher
import re


# Bangla Unicode block and common punctuation/digits
BANGLA_BLOCK_START = 0x0980
BANGLA_BLOCK_END = 0x09FF
COMMON_PUNCT_DIGITS = set('.,;:!?()-–—\'"্‍‌')  # ASCII punct/digits, Bangla digits U+09E6–U+09EF
BANGLA_DIGITS = set(chr(i) for i in range(0x09E6, 0x09F0))


def load_jsonl(path: Path) -> List[Dict]:
    """Load JSONL file."""
    data = []
    with open(path) as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    return data


def load_json(path: Path):
    """Load JSON file."""
    with open(path) as f:
        return json.load(f)


def is_bangla_char(char: str) -> bool:
    """Check if character is Bangla or common punctuation/digit."""
    code = ord(char)
    if BANGLA_BLOCK_START <= code <= BANGLA_BLOCK_END:
        return True
    if char in COMMON_PUNCT_DIGITS or char in BANGLA_DIGITS:
        return True
    if char.isdigit():  # ASCII digits
        return True
    return False


def compute_bangla_char_ratio(text: str) -> float:
    """Compute fraction of non-whitespace chars that are Bangla/punct/digits."""
    non_whitespace = [c for c in text if not c.isspace()]
    if not non_whitespace:
        return 0.0
    bangla_chars = sum(1 for c in non_whitespace if is_bangla_char(c))
    return bangla_chars / len(non_whitespace)


def normalize_for_similarity(text: str) -> str:
    """Normalize text for similarity comparison (whitespace-normalized)."""
    return ' '.join(text.split())


def pairwise_similarity(text1: str, text2: str) -> float:
    """Compute normalized text similarity using difflib."""
    norm1 = normalize_for_similarity(text1)
    norm2 = normalize_for_similarity(text2)
    return SequenceMatcher(None, norm1, norm2).ratio()


def main():
    base_dir = Path('/scratch/pdf-craft/pdf-craft-output/benchmark/ocr-engines')

    # Load data
    manifest = load_json(base_dir / 'manifest.json')
    tesseract_results = load_jsonl(base_dir / 'results_tesseract.jsonl')
    easyocr_results = load_jsonl(base_dir / 'results_easyocr.jsonl')

    # Index results by (doc_id, page_index)
    tes_index = {(r['doc_id'], r['page_index']): r for r in tesseract_results}
    easy_index = {(r['doc_id'], r['page_index']): r for r in easyocr_results}

    # Prepare summary CSV data
    summary_rows = []

    # Per-page rows
    for engine_name, results_index in [('tesseract', tes_index), ('easyocr', easy_index)]:
        for (doc_id, page_index), result in results_index.items():
            row = {
                'engine': engine_name,
                'doc_id': doc_id,
                'page_index': page_index,
                'seconds': result['seconds'],
                'char_count': len(result['text']),
                'error': result.get('error'),
            }
            summary_rows.append(row)

    # Engine-level aggregates
    for engine_name, results_index in [('tesseract', tes_index), ('easyocr', easy_index)]:
        results = list(results_index.values())
        seconds_list = [r['seconds'] for r in results if r['seconds'] is not None]
        error_count = sum(1 for r in results if r.get('error'))
        error_rate = error_count / len(results) if results else 0.0

        row = {
            'engine': engine_name,
            'doc_id': '',
            'page_index': '',
            'seconds': '',
            'char_count': '',
            'error': '',
            'mean_seconds': statistics.mean(seconds_list) if seconds_list else None,
            'median_seconds': statistics.median(seconds_list) if seconds_list else None,
            'p95_seconds': statistics.quantiles(seconds_list, n=20)[18] if len(seconds_list) >= 20 else None,
            'error_count': error_count,
            'error_rate': error_rate,
        }
        summary_rows.append(row)

    # Write summary CSV
    csv_path = base_dir / 'summary.csv'
    fieldnames = ['engine', 'doc_id', 'page_index', 'seconds', 'char_count', 'error',
                  'mean_seconds', 'median_seconds', 'p95_seconds', 'error_count', 'error_rate']
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"Wrote {csv_path}")

    # Compute proxy scores
    proxy_scores = {}

    # 1. bangla_char_ratio per engine
    bangla_ratios = {}
    for engine_name, results_index in [('tesseract', tes_index), ('easyocr', easy_index)]:
        results = list(results_index.values())
        ratios = [compute_bangla_char_ratio(r['text']) for r in results]
        bangla_ratios[engine_name] = statistics.mean(ratios) if ratios else 0.0

    proxy_scores['bangla_char_ratio'] = {
        'note': 'Fraction of non-whitespace characters that fall in Bangla Unicode block (U+0980–U+09FF) or common punctuation/digits. Higher = more plausible Bangla text.',
        'tesseract': bangla_ratios['tesseract'],
        'easyocr': bangla_ratios['easyocr'],
    }

    # 2. pairwise_similarity between Tesseract and EasyOCR
    similarities = []
    page_similarities = []
    for key in tes_index:
        if key in easy_index:
            tes_text = tes_index[key]['text']
            easy_text = easy_index[key]['text']
            sim = pairwise_similarity(tes_text, easy_text)
            similarities.append(sim)
            page_similarities.append((key[0], key[1], sim))

    # Sort to find lowest and highest agreement
    page_similarities_sorted = sorted(page_similarities, key=lambda x: x[2])
    lowest_5 = page_similarities_sorted[:5]
    highest_5 = page_similarities_sorted[-5:]

    proxy_scores['pairwise_similarity'] = {
        'note': 'Normalized text similarity between Tesseract and EasyOCR output for the same page, using difflib.SequenceMatcher (0-1, whitespace-normalized). Higher = more agreement.',
        'mean': statistics.mean(similarities) if similarities else 0.0,
        'lowest_agreement_pages': [
            {'doc_id': doc_id, 'page_index': page_idx, 'ratio': ratio}
            for doc_id, page_idx, ratio in lowest_5
        ],
        'highest_agreement_pages': [
            {'doc_id': doc_id, 'page_index': page_idx, 'ratio': ratio}
            for doc_id, page_idx, ratio in highest_5
        ],
    }

    # 3. mean_confidence from EasyOCR
    easyocr_confidences = []
    for r in easyocr_results:
        if r.get('confidence') is not None:
            easyocr_confidences.append(r['confidence'])

    proxy_scores['mean_confidence'] = {
        'note': 'EasyOCR self-reported mean confidence per page, then averaged. Tesseract has no comparable native confidence score.',
        'easyocr': statistics.mean(easyocr_confidences) if easyocr_confidences else None,
        'tesseract': None,  # Not available
    }

    # Write proxy scores JSON
    json_path = base_dir / 'proxy_scores.json'
    with open(json_path, 'w') as f:
        json.dump(proxy_scores, f, indent=2)

    print(f"Wrote {json_path}")

    # Report summary
    print("\n=== OCR Benchmark Summary ===\n")

    print("Engine-level timing:")
    for engine_name, results_index in [('tesseract', tes_index), ('easyocr', easy_index)]:
        results = list(results_index.values())
        seconds_list = [r['seconds'] for r in results if r['seconds'] is not None]
        if seconds_list:
            print(f"  {engine_name}:")
            print(f"    Mean: {statistics.mean(seconds_list):.4f}s")
            print(f"    Median: {statistics.median(seconds_list):.4f}s")

    print("\nBangla character ratio (accuracy proxy):")
    for engine in ['tesseract', 'easyocr']:
        print(f"  {engine}: {bangla_ratios[engine]:.4f}")

    print(f"\nPairwise similarity (Tesseract vs EasyOCR): {statistics.mean(similarities):.4f}")

    print("\nLowest-agreement page (for spot-check):")
    if lowest_5:
        doc_id, page_idx, ratio = lowest_5[0]
        print(f"  doc_id={doc_id}, page_index={page_idx}, similarity={ratio:.4f}")


if __name__ == '__main__':
    main()
