"""External Bengali eval splits: BL REID pages + Mozhi word images.

These are fixed third-party references for benchmarking OCR engines against
each other. They complement (never replace) the in-domain gold labels from
``gold_store`` exports.

Layout on disk (all under git-ignored ``pdf-craft-output/external-eval/``)::

    reid/           # BL REID2019: page TIFFs + PAGE XML ground truth (public domain)
    mozhi/test/     # Mozhi-Bengali test: images/ + test_gt.txt (CC BY 4.0)
    mozhi/val/      # Mozhi-Bengali val split

Scoring reuses :func:`pdf_craft_tool.benchmark.score` (NFC, CER/WER), so
external numbers are directly comparable with gold-split numbers.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .benchmark import score


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _equiv_text(element: ET.Element) -> str:
    for equiv in element:
        if _local(equiv.tag) != "TextEquiv":
            continue
        for child in equiv:
            if _local(child.tag) == "Unicode" and child.text:
                return unicodedata.normalize("NFC", child.text).strip()
    return ""


def parse_page_xml(path: Path) -> list[str]:
    """Return reference text lines from a PAGE XML file in reading order.

    Handles both line-level (TextRegion/TextLine/TextEquiv) and region-level
    (TextRegion/TextEquiv, as in most BL REID pages) transcriptions.
    """
    root = ET.parse(path).getroot()
    lines: list[str] = []
    for region in root.iter():
        if _local(region.tag) != "TextRegion":
            continue
        line_texts = [
            text
            for line in region
            if _local(line.tag) == "TextLine"
            for text in [_equiv_text(line)]
            if text
        ]
        if line_texts:
            lines.extend(line_texts)
        else:
            region_text = _equiv_text(region)
            if region_text:
                lines.extend(
                    part.strip() for part in region_text.splitlines() if part.strip()
                )
    return lines


def reid_references(reid_dir: Path) -> list[dict[str, Any]]:
    """Collect ``{id, image, reference}`` for every REID page with GT."""
    reid_dir = Path(reid_dir)
    items = []
    for xml_path in sorted(reid_dir.rglob("*.xml")):
        try:
            lines = parse_page_xml(xml_path)
        except (OSError, ET.ParseError):
            continue
        if not lines:
            continue
        stem = xml_path.stem
        image = next(
            (
                candidate
                for suffix in (".tif", ".tiff", ".png", ".jpg")
                if (candidate := xml_path.with_suffix(suffix)).is_file()
            ),
            None,
        )
        items.append(
            {
                "id": f"reid:{stem}",
                "xml": str(xml_path),
                "image": str(image) if image else "",
                "reference": "\n".join(lines),
                "nlines": len(lines),
            }
        )
    return items


def load_mozhi_gt(gt_path: Path) -> dict[str, str]:
    """Parse a Mozhi ``*_gt.txt`` file (image name + TAB + transcription)."""
    references: dict[str, str] = {}
    for raw in Path(gt_path).read_text(encoding="utf-8").splitlines():
        if "\t" not in raw:
            continue
        name, text = raw.split("\t", 1)
        name, text = name.strip(), unicodedata.normalize("NFC", text.strip())
        if name and text:
            references[name] = text
    return references


def mozhi_references(mozhi_dir: Path, split: str = "test") -> list[dict[str, Any]]:
    """Collect ``{id, image, reference}`` for one Mozhi split."""
    mozhi_dir = Path(mozhi_dir)
    candidates = sorted(mozhi_dir.glob(f"*{split}*gt.txt")) or sorted(
        mozhi_dir.glob("*.txt")
    )
    if not candidates:
        raise ValueError(f"No Mozhi ground-truth file in {mozhi_dir}")
    references = load_mozhi_gt(candidates[0])
    items = []
    for name in sorted(references):
        image = mozhi_dir / "images" / name
        if not image.is_file():
            image = mozhi_dir / name
        items.append(
            {
                "id": f"mozhi-{split}:{name}",
                "image": str(image) if image.is_file() else "",
                "reference": references[name],
            }
        )
    return items


def score_hypotheses(
    items: list[dict[str, Any]], hypotheses: dict[str, str]
) -> dict[str, Any]:
    """Score hypothesis texts against references; missing counts as empty."""
    scored, missing = [], []
    for item in items:
        hypothesis = hypotheses.get(item["id"], "")
        if item["id"] not in hypotheses:
            missing.append(item["id"])
        try:
            result = score(item["reference"], hypothesis or " ")
        except ValueError:
            continue
        scored.append({"id": item["id"], **result})
    if not scored:
        raise ValueError("No scorable items")
    total_ref_chars = sum(row["reference_chars"] for row in scored)
    total_ref_words = sum(row["reference_words"] for row in scored)
    total_char_edits = sum(row["character_edits"] for row in scored)
    total_word_edits = sum(row["word_edits"] for row in scored)
    return {
        "n": len(scored),
        "missing": len(missing),
        "cer": total_char_edits / total_ref_chars,
        "wer": total_word_edits / total_ref_words,
        "items": scored,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reid-dir", type=Path)
    parser.add_argument("--mozhi-dir", type=Path)
    parser.add_argument("--mozhi-split", default="test")
    parser.add_argument(
        "--hypotheses", type=Path,
        help="JSONL with {id, text} per line; omit to just list references",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    items: list[dict[str, Any]] = []
    if args.reid_dir is not None:
        items.extend(reid_references(args.reid_dir))
    if args.mozhi_dir is not None:
        items.extend(mozhi_references(args.mozhi_dir, args.mozhi_split))
    if not items:
        raise SystemExit("Provide --reid-dir and/or --mozhi-dir")
    if args.hypotheses is None:
        print(json.dumps(
            {"items": len(items),
             "ids": [item["id"] for item in items[:5]],
             "note": "pass --hypotheses JSONL to score"}, ensure_ascii=False, indent=2))
        return 0
    hypotheses = {}
    for raw in args.hypotheses.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            row = json.loads(raw)
            hypotheses[row["id"]] = row.get("text", "")
    result = {"split": [item["id"].split(":")[0] for item in items[:1]],
              **score_hypotheses(items, hypotheses)}
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
