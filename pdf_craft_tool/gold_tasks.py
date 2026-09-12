"""Frozen line-level gold tasks for Bengali OCR adjudication.

Each task is one Tesseract text line (grouped from ``page_*.json`` word boxes)
with an optional Qwen-derived line draft. The line crop image is the ground
truth source; both drafts are untrusted suggestions. Human-verified text plus
provenance is what later becomes the benchmark reference and ``tesstrain``
``.gt.txt`` material.

Discovery input is one finished ``book`` work directory (``ocr/<hash>/`` and
``proofread/<hash>/audit/`` stages) so page geometry, word confidences and
paragraph-level Qwen proposals stay consistent. Nothing here runs OCR or calls
a model; it only freezes line tasks deterministically.
"""

from __future__ import annotations

import hashlib
import json
import random
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GOLD_VERSION = 1
DEFAULT_CAP = 1000
DEFAULT_DPI = 300
LOW_WORD_CONFIDENCE = 65.0


@dataclass(frozen=True)
class GoldDataset:
    """A stable, read-only collection of line tasks."""

    path: Path
    entries: tuple[dict[str, Any], ...]
    cap: int

    @classmethod
    def open(cls, path: Path) -> GoldDataset:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("version") != GOLD_VERSION or not isinstance(
            payload.get("entries"), list
        ):
            raise ValueError(f"Unsupported gold manifest: {path}")
        entries = tuple(dict(entry) for entry in payload["entries"])
        if any(not isinstance(entry.get("id"), str) for entry in entries):
            raise ValueError(f"Gold manifest has an entry without an id: {path}")
        return cls(
            path=Path(path), entries=entries, cap=int(payload.get("cap", len(entries)))
        )

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return next((entry for entry in self.entries if entry["id"] == entry_id), None)


def normalize_line(text: str) -> str:
    """Collapse whitespace and join punctuation like the Tesseract adapter."""
    text = unicodedata.normalize("NFC", text)
    for mark in "।,;:!?":
        text = text.replace(f" {mark}", mark)
    return " ".join(text.split())


def group_words_into_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group Tesseract TSV-level word dicts into line dicts.

    Expected word keys: block, paragraph, line, left, top, width, height,
    confidence, text. Returns lines with text, bbox, confidence and a
    suspicious flag for low-confidence words.
    """
    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for word in words:
        try:
            key = (int(word["block"]), int(word["paragraph"]), int(word["line"]))
            text = str(word["text"]).strip()
            if not text:
                continue
            left, top = int(word["left"]), int(word["top"])
            width, height = int(word["width"]), int(word["height"])
            confidence = float(word["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(left, top) < 0 or min(width, height) <= 0:
            continue
        grouped.setdefault(key, []).append(
            {**word, "text": text, "confidence": confidence,
             "left": left, "top": top, "width": width, "height": height}
        )
    lines = []
    for key in sorted(grouped):
        group = grouped[key]
        text = normalize_line(" ".join(word["text"] for word in group))
        if not text:
            continue
        left = min(word["left"] for word in group)
        top = min(word["top"] for word in group)
        right = max(word["left"] + word["width"] for word in group)
        bottom = max(word["top"] + word["height"] for word in group)
        weights = [max(1, len(word["text"])) for word in group]
        confidence = sum(
            max(0.0, word["confidence"]) * weight
            for word, weight in zip(group, weights)
        ) / sum(weights)
        lines.append(
            {
                "key": key,
                "text": text,
                "bbox": [left, top, right, bottom],
                "confidence": round(confidence, 2),
                "min_confidence": round(min(word["confidence"] for word in group), 2),
                "suspicious": any(
                    word["confidence"] < LOW_WORD_CONFIDENCE for word in group
                ),
            }
        )
    return lines


def derive_qwen_line(
    line_text: str, audits: list[dict[str, Any]]
) -> tuple[str, bool, dict[str, Any] | None]:
    """Derive a line-level Qwen draft from paragraph-level edit decisions.

    Returns ``(text, derived, parent)`` where ``derived`` is True only when at
    least one paragraph word edit applied cleanly inside this line. Otherwise
    the Tesseract line is returned unchanged so the UI never overstates what
    Qwen actually proposed at this span.
    """
    for audit in audits:
        decisions = audit.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            continue
        candidate = line_text
        applied = False
        for edit in decisions:
            if not isinstance(edit, dict):
                continue
            before, after = edit.get("before"), edit.get("after")
            if not isinstance(before, str) or not isinstance(after, str):
                continue
            if not before or candidate.count(before) != 1:
                continue
            start = candidate.index(before)
            end = start + len(before)
            if (start and _word_char(candidate[start - 1])) or (
                end < len(candidate) and _word_char(candidate[end])
            ):
                continue
            candidate = candidate[:start] + after + candidate[end:]
            applied = True
        if applied:
            return candidate, True, audit
    return line_text, False, None


def _word_char(char: str) -> bool:
    return unicodedata.category(char)[0] in {"L", "M", "N"} or char == "_"


def _contains(outer: list[int], inner: list[int]) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def parent_audit(
    line_bbox: list[int], audits: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the paragraph audit whose bbox contains this line, if any."""
    for audit in audits:
        bbox = audit.get("bbox")
        if (
            isinstance(bbox, (list, tuple))
            and len(bbox) == 4
            and all(isinstance(v, (int, float)) for v in bbox)
            and _contains([int(v) for v in bbox], line_bbox)
        ):
            return audit
    return None


def discover_entries(
    *,
    ocr_dir: Path,
    audit_dir: Path | None,
    source: str,
    source_sha256: str,
    title: str,
    dpi: int = DEFAULT_DPI,
    cap: int = DEFAULT_CAP,
    sample: str = "mixed",
    seed: int = 7,
) -> list[dict[str, Any]]:
    """Build deterministic line entries from OCR word JSON + audit JSON."""
    if cap < 1:
        raise ValueError("Gold cap must be positive")
    if sample not in ("all", "mixed", "random", "suspicious"):
        raise ValueError("sample must be all, mixed, random or suspicious")
    ocr_dir = Path(ocr_dir)
    page_paths = sorted(ocr_dir.glob("page_*.json"), key=lambda p: p.name)
    if not page_paths:
        raise ValueError(f"No page_*.json diagnostics in {ocr_dir}")
    audits_by_page: dict[int, list[dict[str, Any]]] = {}
    if audit_dir is not None and Path(audit_dir).is_dir():
        for path in sorted(Path(audit_dir).glob("p*.json"), key=lambda p: p.name):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or not isinstance(
                record.get("original"), str
            ):
                continue
            page = record.get("page")
            if not isinstance(page, int) or page < 1:
                continue
            audits_by_page.setdefault(page, []).append(record)
    candidates: list[dict[str, Any]] = []
    for page_path in page_paths:
        try:
            page_number = int(page_path.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        try:
            diagnostics = json.loads(page_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        words = diagnostics.get("words")
        if not isinstance(words, list):
            continue
        audits = audits_by_page.get(page_number, [])
        for line in group_words_into_lines(words):
            parent = parent_audit(line["bbox"], audits)
            if parent is not None:
                qwen_text, derived, _ = derive_qwen_line(line["text"], [parent])
                context_original = parent.get("original", "")
                context_proposed = parent.get("proposed", "")
                model = str(parent.get("model", ""))
            else:
                qwen_text, derived = line["text"], False
                context_original, context_proposed, model = "", "", ""
            entry_id = hashlib.sha256(
                f"{source_sha256}\0{page_number}\0{line['bbox']}\0{line['text']}".encode()
            ).hexdigest()[:32]
            candidates.append(
                {
                    "id": entry_id,
                    "title": title,
                    "source": source,
                    "source_sha256": source_sha256,
                    "page": page_number,
                    "bbox": line["bbox"],
                    "dpi": dpi,
                    "tesseract": line["text"],
                    "qwen": qwen_text,
                    "qwen_derived": derived,
                    "context_original": context_original
                    if isinstance(context_original, str)
                    else "",
                    "context_proposed": context_proposed
                    if isinstance(context_proposed, str)
                    else "",
                    "model": model,
                    "line_confidence": line["confidence"],
                    "suspicious": bool(line["suspicious"]),
                    "line_key": list(line["key"]),
                    "dataset_version": f"gold-{GOLD_VERSION}",
                }
            )
    candidates.sort(
        key=lambda e: (e["page"], tuple(e["bbox"]), e["tesseract"], e["id"])
    )
    if sample == "all":
        return candidates[:cap]
    rng = random.Random(seed)
    suspicious = [e for e in candidates if e["suspicious"]]
    clean = [e for e in candidates if not e["suspicious"]]
    rng.shuffle(suspicious)
    rng.shuffle(clean)
    if sample == "suspicious":
        ordered = suspicious + clean
    elif sample == "random":
        ordered = suspicious + clean
        rng.shuffle(ordered)
    else:  # mixed: half suspicious, half clean when both pools are available
        suspicious_quota = min(len(suspicious), cap // 2)
        clean_quota = min(len(clean), cap - suspicious_quota)
        # If one pool is smaller, let the other pool use the released slots.
        suspicious_quota = min(len(suspicious), cap - clean_quota)
        ordered = suspicious[:suspicious_quota] + clean[:clean_quota]
        rng.shuffle(ordered)
    return ordered[:cap]


def _latest_stage(root: Path, kind: str) -> Path | None:
    base = Path(root) / kind
    if not base.is_dir():
        return None
    stages = [p for p in base.iterdir() if p.is_dir()]
    if not stages:
        return None
    return max(stages, key=lambda p: p.stat().st_mtime)


def create_manifest(
    book_root: Path,
    manifest_path: Path,
    *,
    source: Path | None = None,
    cap: int = DEFAULT_CAP,
    sample: str = "mixed",
    seed: int = 7,
) -> GoldDataset:
    """Freeze line tasks from one finished ``book`` work directory."""
    book_root = Path(book_root).resolve()
    ocr_stage = _latest_stage(book_root, "ocr")
    if ocr_stage is None:
        raise ValueError(f"No ocr/<hash> stage under {book_root}")
    ocr_dir = ocr_stage / "analysis" / "ocr"
    proof_stage = _latest_stage(book_root, "proofread")
    audit_dir = proof_stage / "audit" if proof_stage is not None else None
    summary_path = book_root / "run-summary.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            summary = {}
    resolved_source = Path(
        source or summary.get("source") or ""
    )
    source_sha256 = str(summary.get("source_sha256") or "")
    if not source_sha256 and resolved_source.is_file():
        import hashlib as _hashlib

        with resolved_source.open("rb") as stream:
            source_sha256 = _hashlib.file_digest(stream, "sha256").hexdigest()
    if not resolved_source.name or not source_sha256:
        raise ValueError(
            "Cannot determine source PDF and sha256; pass --source explicitly"
        )
    title = str(resolved_source.stem)
    entries = discover_entries(
        ocr_dir=ocr_dir,
        audit_dir=audit_dir if audit_dir is not None and audit_dir.is_dir() else None,
        source=str(resolved_source),
        source_sha256=source_sha256,
        title=title,
        cap=cap,
        sample=sample,
        seed=seed,
    )
    payload = {
        "version": GOLD_VERSION,
        "cap": cap,
        "sample": sample,
        "seed": seed,
        "book_root": str(book_root),
        "entries": entries,
    }
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return GoldDataset(path=manifest_path, entries=tuple(entries), cap=cap)


def load_or_create_manifest(
    book_root: Path,
    *,
    manifest_path: Path | None = None,
    source: Path | None = None,
    cap: int = DEFAULT_CAP,
    sample: str = "mixed",
    seed: int = 7,
) -> GoldDataset:
    """Reuse a frozen manifest; discovery never rewrites an existing session."""
    book_root = Path(book_root).resolve()
    path = Path(manifest_path or book_root / "gold" / "manifest.json")
    if path.exists():
        return GoldDataset.open(path)
    return create_manifest(
        book_root, path, source=source, cap=cap, sample=sample, seed=seed
    )
