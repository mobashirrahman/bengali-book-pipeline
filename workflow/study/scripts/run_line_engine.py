#!/usr/bin/env python3
"""Shared line-extraction helpers for the study voter runners (stdlib only).

Pure functions (unit-tested in tests/research/test_line_engine.py) plus thin
per-engine entry points used by run_*_lines.py in this directory:

* :func:`parse_tesseract_tsv` -- TSV text -> ``[{text, bbox, conf}]`` lines.
* :func:`easyocr_to_lines` -- EasyOCR detail=1 results -> lines.
* :func:`surya_to_lines` -- Surya recognition page -> lines.
* :func:`surya_page_lines` -- detection boxes + recognition -> lines mapped
  back onto the detected boxes by IoU (full-page fallback when nothing is
  detected).
* :func:`load_done_ids` / :func:`write_row` -- resume-safe JSONL output.
* :func:`run_tesseract` / :func:`run_easyocr` / :func:`run_surya` --
  ``(lines, error, seconds)`` with missing-file guards and a worker-thread
  timeout (a timed-out engine call is recorded, never raised).

Line dicts are ``{"text": str, "bbox": [x0, y0, x1, y1], "conf": float}``
in page PNG pixel space. No model calls happen at import time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

_TAG = re.compile(r"<[^>]+>")
_TIE_EPS = 1e-9


# ---------------------------------------------------------------------------
# Tesseract TSV
# ---------------------------------------------------------------------------

def parse_tesseract_tsv(text: str) -> list:
    """Group Tesseract TSV word rows (level 5) into line dicts."""
    rows = (text or "").splitlines()
    if len(rows) < 2:
        return []
    grouped: dict = {}
    for raw in rows[1:]:
        cols = raw.split("\t")
        if len(cols) < 12:
            continue
        try:
            level = int(cols[0])
        except ValueError:
            continue
        if level != 5:
            continue
        word = cols[11].strip() if len(cols) > 11 else ""
        if not word:
            continue
        try:
            key = (int(cols[2]), int(cols[3]), int(cols[4]))
            left, top = int(cols[6]), int(cols[7])
            width, height = int(cols[8]), int(cols[9])
            conf = float(cols[10])
        except ValueError:
            continue
        if min(width, height) < 0:
            continue
        grouped.setdefault(key, []).append({
            "text": word, "left": left, "top": top,
            "width": width, "height": height, "conf": conf,
        })
    lines = []
    for key in sorted(grouped):
        words = grouped[key]
        text = " ".join(word["text"] for word in words).strip()
        if not text:
            continue
        confs = [word["conf"] for word in words if word["conf"] >= 0]
        lines.append({
            "text": text,
            "bbox": [min(w["left"] for w in words),
                     min(w["top"] for w in words),
                     max(w["left"] + w["width"] for w in words),
                     max(w["top"] + w["height"] for w in words)],
            "conf": sum(confs) / len(confs) if confs else 0.0,
        })
    return lines


# ---------------------------------------------------------------------------
# EasyOCR
# ---------------------------------------------------------------------------

def _quad_to_bbox(quad) -> list:
    xs = [float(point[0]) for point in quad]
    ys = [float(point[1]) for point in quad]
    return [min(xs), min(ys), max(xs), max(ys)]


def easyocr_to_lines(results) -> list:
    """EasyOCR detail=1 ``[(polygon, text, conf)]`` -> line dicts."""
    lines = []
    for entry in results or []:
        try:
            quad, text, conf = entry
        except (TypeError, ValueError):
            continue
        text = str(text).strip() if text is not None else ""
        if not text:
            continue
        try:
            bbox = _quad_to_bbox(quad)
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        lines.append({"text": text, "bbox": bbox, "conf": conf})
    return lines


# ---------------------------------------------------------------------------
# Geometry + Surya
# ---------------------------------------------------------------------------

def _bbox_iou(a, b) -> float:
    """Intersection over union of two ``[x0, y0, x1, y1]`` boxes."""
    try:
        ax0, ay0, ax1, ay1 = (float(v) for v in a)
        bx0, by0, bx1, by1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return 0.0
    inter_w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    inter_h = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    union = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0) \
        + max(0.0, bx1 - bx0) * max(0.0, by1 - by0) - inter
    if union <= 0:
        return 0.0
    return inter / union


def _norm_poly(poly):
    """Accept nested ``[[x, y], ...]`` or flat ``[x0, y0, ...]`` polygons."""
    if poly is None:
        return None
    pts = list(poly)
    if pts and isinstance(pts[0], (list, tuple)):
        return pts
    flat = list(pts)
    if len(flat) < 4 or len(flat) % 2:
        return None
    return [[flat[i], flat[i + 1]] for i in range(0, len(flat), 2)]


def _strip_tags(html) -> str:
    if not isinstance(html, str):
        return ""
    return _TAG.sub("", html).strip()


def surya_to_lines(page_result) -> list:
    """Surya recognition page (``.blocks``) -> line dicts in reading order."""
    blocks = getattr(page_result, "blocks", None) or []
    ordered = sorted(blocks, key=lambda b: (
        getattr(b, "reading_order", None) is None,
        getattr(b, "reading_order", 0) or 0,
    ))
    lines = []
    for block in ordered:
        if getattr(block, "skipped", False) or getattr(block, "error", False):
            continue
        text = _strip_tags(getattr(block, "html", ""))
        if not text:
            continue
        points = _norm_poly(getattr(block, "polygon", None))
        if points is None:
            fallback = getattr(block, "bbox", None)
            if isinstance(fallback, (list, tuple)) and len(fallback) == 4:
                try:
                    x0, y0, x1, y1 = (float(v) for v in fallback)
                    points = [[x0, y0], [x1, y1]]
                except (TypeError, ValueError):
                    continue
            else:
                continue
        try:
            xs = [float(p[0]) for p in points]
            ys = [float(p[1]) for p in points]
            conf = float(getattr(block, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        lines.append({"text": text,
                      "bbox": [min(xs), min(ys), max(xs), max(ys)],
                      "conf": conf})
    return lines


def surya_page_lines(image, detector, recognizer, layout_box_cls,
                     layout_result_cls) -> list:
    """Detect lines, recognise them, map blocks back onto detected boxes.

    ``detector([image])`` returns ``[{.bboxes: [.bbox, ...], .image_bbox}]``;
    ``recognizer([image], [layout], full_page=False)`` returns
    ``[{.blocks}]``. Each detected box takes the highest-IoU unmatched
    recognition line (boxes with no match are dropped, never emitted empty).
    With no detected boxes the recognizer runs once in full-page fallback.
    """
    width, height = image.size
    det_pages = detector([image]) if detector is not None else []
    det_boxes = []
    if det_pages:
        for box in getattr(det_pages[0], "bboxes", None) or []:
            bbox = getattr(box, "bbox", None)
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                try:
                    det_boxes.append([float(v) for v in bbox])
                except (TypeError, ValueError):
                    continue
    if not det_boxes:
        rec_pages = recognizer([image])
        if not rec_pages:
            return []
        return surya_to_lines(rec_pages[0])
    layout = layout_result_cls(
        bboxes=[layout_box_cls(polygon=list(box), label="Text",
                               raw_label="Text", position=i)
                for i, box in enumerate(det_boxes)],
        image_bbox=[0, 0, width, height],
    )
    rec_pages = recognizer([image], [layout], full_page=False)
    if not rec_pages:
        return []
    rec_lines = surya_to_lines(rec_pages[0])
    used = [False] * len(rec_lines)
    lines = []
    for box in det_boxes:
        best, best_iou = -1, _TIE_EPS
        for i, line in enumerate(rec_lines):
            if used[i]:
                continue
            iou = _bbox_iou(box, line["bbox"])
            if iou > best_iou:
                best, best_iou = i, iou
        if best < 0:
            continue
        used[best] = True
        lines.append({"text": rec_lines[best]["text"], "bbox": list(box),
                      "conf": rec_lines[best]["conf"]})
    return lines


# ---------------------------------------------------------------------------
# Resume-safe JSONL output
# ---------------------------------------------------------------------------

def load_done_ids(path) -> set:
    """Ids already present in a JSONL output file (missing file -> empty)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return set()
    done = set()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id"):
            done.add(row["id"])
    return done


def write_row(path, row: dict) -> None:
    """Append one JSON row (creating parent dirs), flushing immediately."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Per-engine entry points: (lines, error, seconds), never raise
# ---------------------------------------------------------------------------

def _guarded(image: str, timeout_s: float, work) -> tuple:
    """Run ``work() -> lines`` on a worker thread with a timeout."""
    start = time.monotonic()
    if not image or not Path(image).is_file():
        return [], f"missing-file:{image}", round(time.monotonic() - start, 3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work)
        try:
            lines = future.result(timeout=timeout_s)
        except FuturesTimeoutError:
            return [], "timeout", round(timeout_s, 3)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            return [], f"{type(exc).__name__}:{str(exc)[:120]}", \
                round(time.monotonic() - start, 3)
    return lines or [], None, round(time.monotonic() - start, 3)


def run_tesseract(image: str, tesseract_bin: str, tessdata_dir: str,
                  lang: str, oem: str, psm: str, timeout_s: float) -> tuple:
    """Tesseract TSV -> grouped lines."""
    def work():
        result = subprocess.run(
            [tesseract_bin, image, "stdout", "-l", lang,
             "--tessdata-dir", tessdata_dir,
             "--oem", str(oem), "--psm", str(psm), "tsv"],
            capture_output=True, text=True, timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(
                f"exit_{result.returncode}:{result.stderr.strip()[:100]}")
        return parse_tesseract_tsv(result.stdout)
    return _guarded(image, timeout_s + 30.0, work)


def run_easyocr(image: str, reader, timeout_s: float) -> tuple:
    """EasyOCR ``reader.readtext(detail=1)`` -> lines (reader owned by caller)."""
    canvas = int(os.environ.get("EASYOCR_CANVAS_SIZE", "2560"))
    mag = float(os.environ.get("EASYOCR_MAG_RATIO", "1.0"))

    def work():
        return easyocr_to_lines(reader.readtext(
            image, detail=1, canvas_size=canvas, mag_ratio=mag))
    return _guarded(image, timeout_s + 30.0, work)


def run_surya(image: str, detector, recognizer, timeout_s: float) -> tuple:
    """Surya detection+recognition -> lines mapped onto detected boxes."""
    from PIL import Image  # noqa: PLC0415 - heavy env only

    def work():
        with Image.open(image) as handle:
            rgb = handle.convert("RGB")
        try:
            from surya.layout.schema import (  # noqa: PLC0415
                LayoutBox, LayoutResult)
        except ImportError:
            LayoutBox = LayoutResult = None
        if detector is None or LayoutBox is None or LayoutResult is None:
            pages = recognizer([rgb])
            return surya_to_lines(pages[0]) if pages else []
        return surya_page_lines(rgb, detector, recognizer,
                                LayoutBox, LayoutResult)
    return _guarded(image, timeout_s + 30.0, work)
