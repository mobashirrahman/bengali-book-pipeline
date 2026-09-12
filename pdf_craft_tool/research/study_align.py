"""Line-to-census-region alignment (P3a, pure Python).

Turns each OCR engine's whole-page output into one text per human census
region so voters can be compared region by region.

Stdlib only at module top level (plus the local ``schema`` package).
"""

from __future__ import annotations

import json
import math
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

_SPACE_BREAKS = frozenset({"SPACE", "SURE_SPACE"})
_EOL_BREAKS = frozenset({"EOL_SURE_SPACE", "LINE_BREAK"})

_TIE_EPS = 1e-9


@dataclass(frozen=True)
class EngineLine:
    """One OCR line in page PNG pixel space."""

    text: str
    bbox: tuple[float, float, float, float]
    conf: Optional[float] = None


@dataclass
class Alignment:
    """Result of aligning engine lines to census regions."""

    region_texts: dict[int, str] = field(default_factory=dict)
    unassigned: list[EngineLine] = field(default_factory=list)
    ambiguous: list[EngineLine] = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        if key == "region_texts":
            return self.region_texts
        if key == "unassigned":
            return self.unassigned
        if key == "ambiguous":
            return self.ambiguous
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in ("region_texts", "unassigned", "ambiguous")

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_texts": dict(self.region_texts),
            "unassigned": list(self.unassigned),
            "ambiguous": list(self.ambiguous),
        }


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        if math.isfinite(result):
            return result
        return None
    return None


def _norm_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return unicodedata.normalize("NFC", text)


def _norm_box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    """Normalise a box so ``x0 <= x1`` and ``y0 <= y1`` (tolerates inversion)."""
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _word_bbox(vertices: Any) -> Optional[tuple[float, float, float, float]]:
    if not isinstance(vertices, list) or not vertices:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for vertex in vertices:
        if not isinstance(vertex, dict):
            continue
        x = vertex.get("x", 0)
        y = vertex.get("y", 0)
        x_num = _num(x)
        y_num = _num(y)
        xs.append(0.0 if x_num is None else x_num)
        ys.append(0.0 if y_num is None else y_num)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _union(bboxes: list[tuple[float, float, float, float]]) -> Optional[tuple[float, float, float, float]]:
    if not bboxes:
        return None
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def _break_of(container: Any) -> str:
    if not isinstance(container, dict):
        return ""
    prop = container.get("property")
    if not isinstance(prop, dict):
        return ""
    brk = prop.get("detectedBreak")
    if not isinstance(brk, dict):
        return ""
    btype = brk.get("type")
    return btype if isinstance(btype, str) else ""


def lines_from_google(raw_json: str) -> list[EngineLine]:
    """Parse a Vision ``images:annotate`` response into lines."""
    try:
        if isinstance(raw_json, (dict, list)):
            data = raw_json
        elif isinstance(raw_json, (bytes, bytearray)):
            data = json.loads(bytes(raw_json).decode("utf-8"))
        elif isinstance(raw_json, str):
            data = json.loads(raw_json)
        else:
            return []
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    try:
        responses = data.get("responses")
        if not isinstance(responses, list) or not responses:
            return []
        first = responses[0]
        if not isinstance(first, dict):
            return []
        fta = first.get("fullTextAnnotation")
        if not isinstance(fta, dict):
            return []
        pages = fta.get("pages")
        if not isinstance(pages, list):
            return []
    except AttributeError:
        return []

    out: list[EngineLine] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            paragraphs = block.get("paragraphs")
            if not isinstance(paragraphs, list):
                continue
            for para in paragraphs:
                if not isinstance(para, dict):
                    continue
                words = para.get("words")
                if not isinstance(words, list):
                    continue
                current_words: list[str] = []
                current_boxes: list[tuple[float, float, float, float]] = []
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    symbols = word.get("symbols")
                    if not isinstance(symbols, list):
                        continue
                    chars: list[str] = []
                    for sym in symbols:
                        if not isinstance(sym, dict):
                            continue
                        t = sym.get("text")
                        if isinstance(t, str):
                            chars.append(t)
                    wtext = "".join(chars)
                    bbox = _word_bbox(word.get("boundingBox", {}).get("vertices")
                                      if isinstance(word.get("boundingBox"), dict) else None)
                    if bbox is None:
                        # Fall back: union of symbol boxes when present.
                        sym_boxes: list[tuple[float, float, float, float]] = []
                        for sym in symbols:
                            if not isinstance(sym, dict):
                                continue
                            bb = sym.get("boundingBox")
                            verts = bb.get("vertices") if isinstance(bb, dict) else None
                            sb = _word_bbox(verts)
                            if sb is not None:
                                sym_boxes.append(sb)
                        bbox = _union(sym_boxes)
                    if wtext:
                        current_words.append(wtext)
                        if bbox is not None:
                            current_boxes.append(bbox)
                    btype = _break_of(word)
                    if not btype and symbols:
                        for sym in reversed(symbols):
                            if isinstance(sym, dict):
                                btype = _break_of(sym)
                                if btype:
                                    break
                    if btype in _EOL_BREAKS:
                        if current_words:
                            merged = _union(current_boxes)
                            if merged is not None:
                                out.append(EngineLine(text=_norm_text(" ".join(current_words)),
                                                      bbox=merged, conf=None))
                        current_words = []
                        current_boxes = []
                    # SPACE / SURE_SPACE / unknown: stay on the same line
                    # (words are space-joined when the line is flushed).
                if current_words:
                    merged = _union(current_boxes)
                    if merged is not None:
                        out.append(EngineLine(text=_norm_text(" ".join(current_words)),
                                              bbox=merged, conf=None))
    return out


def _azure_read_result(data: Any) -> Optional[dict]:
    if isinstance(data, dict):
        rr = data.get("readResult")
        if isinstance(rr, dict):
            return rr
        if "blocks" in data and isinstance(data.get("blocks"), list):
            return data
    return None


def lines_from_azure(raw_json: str) -> list[EngineLine]:
    """Parse an Image Analysis 4.0 result into lines."""
    try:
        if isinstance(raw_json, (dict, list)):
            data = raw_json
        elif isinstance(raw_json, (bytes, bytearray)):
            data = json.loads(bytes(raw_json).decode("utf-8"))
        elif isinstance(raw_json, str):
            data = json.loads(raw_json)
        else:
            return []
    except (ValueError, UnicodeDecodeError):
        return []
    rr = _azure_read_result(data)
    if rr is None:
        return []
    blocks = rr.get("blocks")
    if not isinstance(blocks, list):
        return []
    out: list[EngineLine] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        lines = block.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, dict):
                continue
            text = line.get("text")
            if not isinstance(text, str):
                continue
            polygon = line.get("boundingPolygon")
            if not isinstance(polygon, list) or not polygon:
                continue
            xs: list[float] = []
            ys: list[float] = []
            for pt in polygon:
                if not isinstance(pt, dict):
                    continue
                x = _num(pt.get("x", 0))
                y = _num(pt.get("y", 0))
                xs.append(0.0 if x is None else x)
                ys.append(0.0 if y is None else y)
            if not xs:
                continue
            bbox = (min(xs), min(ys), max(xs), max(ys))
            confs: list[float] = []
            words = line.get("words")
            if isinstance(words, list):
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    c = _num(word.get("confidence"))
                    if c is not None:
                        confs.append(c)
            conf = sum(confs) / len(confs) if confs else None
            out.append(EngineLine(text=_norm_text(text), bbox=bbox, conf=conf))
    return out


def lines_from_records(rows: list[dict]) -> list[EngineLine]:
    """Parse local-engine ``{"text", "bbox", "conf"}`` rows into lines."""
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except ValueError:
            return []
    if isinstance(rows, (bytes, bytearray)):
        try:
            rows = json.loads(bytes(rows).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
    if not isinstance(rows, list):
        return []
    out: list[EngineLine] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if not isinstance(text, str):
            continue
        bbox_raw = row.get("bbox")
        if not isinstance(bbox_raw, (list, tuple)) or len(bbox_raw) != 4:
            continue
        coords = [_num(v) for v in bbox_raw]
        if any(c is None for c in coords):
            continue
        assert all(c is not None for c in coords)
        bbox = _norm_box(float(coords[0]), float(coords[1]),  # type: ignore[arg-type]
                         float(coords[2]), float(coords[3]))  # type: ignore[arg-type]
        conf: Optional[float] = None
        if "conf" in row and row.get("conf") is not None:
            conf = _num(row.get("conf"))
            # Non-numeric conf degrades to None rather than dropping the line.
        out.append(EngineLine(text=_norm_text(text), bbox=bbox, conf=conf))
    return out


def _region_boxes(entries: Any) -> list[tuple[int, tuple[float, float, float, float]]]:
    if not isinstance(entries, list):
        return []
    regions: list[tuple[int, tuple[float, float, float, float]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("region_index")
        if isinstance(idx, bool):
            continue
        if not isinstance(idx, int):
            try:
                idx = int(idx)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        geo = entry.get("geometry")
        if not isinstance(geo, dict):
            continue
        coords = [_num(geo.get(k)) for k in ("x0", "y0", "x1", "y1")]
        if any(c is None for c in coords):
            continue
        assert all(c is not None for c in coords)
        regions.append((idx, _norm_box(float(coords[0]), float(coords[1]),  # type: ignore[arg-type]
                                       float(coords[2]), float(coords[3]))))  # type: ignore[arg-type]
    regions.sort(key=lambda item: item[0])
    return regions


def _intersection_area(a: tuple[float, float, float, float],
                       b: tuple[float, float, float, float]) -> float:
    ix0 = max(a[0], b[0])
    iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2])
    iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _line_area(bbox: tuple[float, float, float, float]) -> float:
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    if w <= 0 or h <= 0:
        return 0.0
    return w * h


def _order_region_lines(lines: list[EngineLine]) -> list[list[EngineLine]]:
    """Group region lines into rows, top-to-bottom, items left-to-right.

    Rows are computed ONCE from the centre-y sorted list so ordering and
    rendering always agree: lines whose centre-y differs from the row's
    first line by ``< 0.5 * median line height`` share a row.
    """
    if not lines:
        return []
    if len(lines) == 1:
        return [list(lines)]
    heights = sorted(max(0.0, ln.bbox[3] - ln.bbox[1]) for ln in lines)
    median_h = statistics.median(heights)
    keyed = sorted(
        lines,
        key=lambda ln: (
            (ln.bbox[1] + ln.bbox[3]) / 2.0,
            (ln.bbox[0] + ln.bbox[2]) / 2.0,
            ln.bbox[0], ln.bbox[1], ln.bbox[2], ln.bbox[3], ln.text,
        ),
    )
    if median_h <= 0:
        return [[ln] for ln in keyed]
    threshold = 0.5 * median_h
    rows: list[list[EngineLine]] = []
    row_ref: Optional[float] = None
    for ln in keyed:
        cy = (ln.bbox[1] + ln.bbox[3]) / 2.0
        if not rows or (row_ref is not None and abs(cy - row_ref) >= threshold):
            rows.append([ln])
            row_ref = cy
        else:
            assert row_ref is not None
            rows[-1].append(ln)
    for row in rows:
        row.sort(key=lambda ln: (
            (ln.bbox[0] + ln.bbox[2]) / 2.0,
            ln.bbox[0], ln.bbox[1], ln.bbox[2], ln.bbox[3], ln.text,
        ))
    return rows


def align(lines: Any, entries: Any, *, min_overlap: float = 0.5) -> Alignment:
    """Assign engine lines to census regions.

    A line belongs to the region holding the largest share of the line bbox
    area; it is assigned only when that share is ``>= min_overlap``.  Exact
    ties (within 1e-9) are recorded in :attr:`Alignment.ambiguous` and
    deterministically assigned to the lowest ``region_index``.
    """
    try:
        threshold = float(min_overlap)
    except (TypeError, ValueError):
        threshold = 0.5
    if not math.isfinite(threshold):
        threshold = 0.5
    regions = _region_boxes(entries)
    buckets: dict[int, list[EngineLine]] = {idx: [] for idx, _ in regions}
    boxes = {idx: box for idx, box in regions}
    ordered_indices = sorted(boxes)
    unassigned: list[EngineLine] = []
    ambiguous: list[EngineLine] = []
    safe_lines = lines if isinstance(lines, list) else []
    for line in safe_lines:
        if not isinstance(line, EngineLine):
            continue
        area = _line_area(line.bbox)
        if area <= 0:
            unassigned.append(line)
            continue
        shares: list[tuple[int, float]] = []
        for idx in ordered_indices:
            inter = _intersection_area(line.bbox, boxes[idx])
            shares.append((idx, inter / area))
        best_share = max(s for _, s in shares)
        if best_share < threshold:
            unassigned.append(line)
            continue
        tied = sorted(idx for idx, s in shares if abs(s - best_share) <= _TIE_EPS)
        chosen = tied[0]
        if len(tied) > 1:
            ambiguous.append(line)
        buckets[chosen].append(line)
    region_texts: dict[int, str] = {}
    for idx in ordered_indices:
        rows = _order_region_lines(buckets[idx])
        rendered = "\n".join(" ".join(ln.text for ln in row) for row in rows if row)
        region_texts[idx] = _norm_text(rendered)
    return Alignment(region_texts=region_texts, unassigned=unassigned, ambiguous=ambiguous)


def align_page(engine: str, raw: Any, entries: Any) -> Alignment:
    """Parse ``raw`` for ``engine`` and align its lines to ``entries``."""
    if engine == "google":
        payload = raw if isinstance(raw, str) else json.dumps(raw) if isinstance(
            raw, (dict, list)) else raw
        lines = lines_from_google(payload)  # type: ignore[arg-type]
    elif engine == "azure":
        payload = raw if isinstance(raw, str) else json.dumps(raw) if isinstance(
            raw, (dict, list)) else raw
        lines = lines_from_azure(payload)  # type: ignore[arg-type]
    elif engine == "records":
        lines = lines_from_records(raw)  # type: ignore[arg-type]
    else:
        raise ValueError(f"unknown engine {engine!r}; expected 'google', 'azure' or 'records'")
    return align(lines, entries)
