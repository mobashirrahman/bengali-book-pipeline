"""Reproducible reference scoring. References are never inputs to OCR or LLMs."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import unicodedata
from zipfile import ZipFile


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.in_body = True

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False

    def handle_data(self, data):
        if self.in_body and data.strip():
            self.parts.append(data.strip())


def reference_text(path: Path, items: list[str]) -> str:
    with ZipFile(path) as archive:
        parts = []
        for item in items:
            parser = _HTMLText()
            parser.feed(archive.read(item).decode("utf-8"))
            parts.append(" ".join(parser.parts))
        return " ".join(parts)


def extraction_text(path: Path) -> str:
    from pdf_craft import PDFCraftExtraction
    from pdf_craft.extractor.chapter import create_chapters_reader, ParagraphLayout
    from pdf_craft.markdown.paragraph import flatten

    extraction = PDFCraftExtraction.open(path)
    with extraction._materialize() as paths:
        return "\n".join(
            "".join(item for block in layout.blocks for item in flatten(block.content) if isinstance(item, str))
            for chapter in create_chapters_reader(paths.chapters)()
            for layout in chapter.layouts if isinstance(layout, ParagraphLayout))


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).replace("’", "'").replace("‘", "'").split())


def words(text: str) -> list[str]:
    result, current = [], []
    for char in text:
        if unicodedata.category(char)[0] in {"L", "M", "N"}:
            current.append(char)
        elif current:
            result.append("".join(current))
            current = []
    if current:
        result.append("".join(current))
    return result


def score(reference: str, hypothesis: str) -> dict:
    try:
        from rapidfuzz.distance import Levenshtein
    except ImportError as error:
        raise RuntimeError("Benchmark scoring requires optional rapidfuzz: pip install rapidfuzz") from error
    reference, hypothesis = normalize(reference), normalize(hypothesis)
    reference_words, hypothesis_words = words(reference), words(hypothesis)
    if not reference or not reference_words:
        raise ValueError("Reference must contain text")
    edits = Levenshtein.editops(reference, hypothesis)
    char_counts = {kind: sum(edit.tag == kind for edit in edits) for kind in ("insert", "delete", "replace")}
    word_edits = Levenshtein.distance(reference_words, hypothesis_words)
    return {"reference_chars": len(reference), "hypothesis_chars": len(hypothesis),
            "reference_words": len(reference_words), "hypothesis_words": len(hypothesis_words),
            "character_edits": len(edits), "character_operations": char_counts,
            "word_edits": word_edits, "cer": len(edits) / len(reference),
            "wer": word_edits / len(reference_words)}


def main() -> None:
    from .book import file_hash, write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--epub-items", nargs="+", required=True, help="explicit reference XHTML files in reading order")
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--proofread", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = reference_text(args.reference, args.epub_items)
    result = {"reference_sha256": file_hash(args.reference), "epub_items": args.epub_items,
              "normalization": "NFC, curly apostrophes folded, whitespace collapsed; L/M/N word tokens",
              "caveat": "EPUB is an imperfect reference; scores are agreement, not verified transcription accuracy.",
              "raw": {"sha256": file_hash(args.raw), **score(reference, extraction_text(args.raw))}}
    if args.proofread:
        result["proofread"] = {"sha256": file_hash(args.proofread), **score(reference, extraction_text(args.proofread))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
