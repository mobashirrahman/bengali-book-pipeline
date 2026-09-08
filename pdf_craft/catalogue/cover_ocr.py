"""Read title and author off a scanned book cover.

86% of this collection is scanned images with no text layer, and the embedded
``/Info`` dictionary is usually a scanner or download-site watermark, so for most
documents the cover is the only place the real title and author appear.

**Why EasyOCR rather than Tesseract.** Both were run over the same rendered
covers.  Tesseract read clean typographic covers well but returned noise on
decorative ones, because its page segmentation assumes document-like text flow.
EasyOCR's CRAFT stage detects text *regions* first and recognises them
individually, which is what a cover needs.  On the five-document trial Tesseract
resolved two covers and EasyOCR resolved five, recovering a Tagore *Sanchayita*
and a Manik Bandyopadhyay title that Tesseract turned into gibberish.

**Why text height matters.** Book covers are typographically ranked: the title is
set largest, the author smaller.  The detector returns a bounding box per region,
so box height orders the candidates by design intent.  That ordering is a
*prior*, not a decision -- the caller confirms roles against the catalogue,
because plenty of covers put a series name or publisher in large type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .page_fingerprint import render_pages

# Watermarks the scraping sites burn into the page image itself.  Measured on
# real covers: `wwwamarboicom` and `banglabooksin` were both read by the
# detector at high confidence, and would otherwise become title candidates.
_WATERMARK = re.compile(
    r"(www\.?[a-z0-9-]+\.?(com|net|org|info)|amarboi|banglabooks|banglabookshelf"
    r"|bengaliebook|allbanglaboi|granthagara|pdfporo|boierpdf|ebook)",
    re.IGNORECASE,
)
# Below this the recogniser is guessing; measured noise on decorative covers sat
# under 0.30 while genuine title text ran 0.47-0.90.
_MIN_CONFIDENCE = 0.30
_COVER_DPI = 200  # Higher than the fingerprint pass: recognition needs the detail.


@dataclass(frozen=True)
class CoverTextRegion:
    text: str
    confidence: float
    height: float      # bounding-box height in pixels; the typographic rank
    page_index: int
    top: float = 0.0
    left: float = 0.0
    right: float = 0.0


def merge_lines(regions: list[CoverTextRegion], page_index: int) -> list[CoverTextRegion]:
    """Rejoin a title that the detector split across visual lines.

    CRAFT returns one region per line, so a cover setting `আমার / অবিশ্বাস` on
    two lines yields two regions.  Ranking them separately hands the caller the
    fragment `অবিশ্বাস` instead of the real title, which then cannot match the
    catalogue.  Lines belong to the same block when they are set at a similar
    size, sit close together vertically, and overlap horizontally -- the same
    cues a reader uses.
    """
    page = sorted(
        (region for region in regions if region.page_index == page_index),
        key=lambda region: region.top,
    )
    merged: list[CoverTextRegion] = []
    for region in page:
        if merged:
            previous = merged[-1]
            gap = region.top - previous.top
            # 0.75, not a looser ratio: on a real cover the title's own lines
            # are set at nearly one size (measured 126 and 156 px, ratio 0.81)
            # while the author sits distinctly smaller (78 px, ratio 0.62
            # against the title).  A 0.6 threshold swallowed the author into the
            # title on the first live run.
            similar_size = min(region.height, previous.height) >= 0.75 * max(region.height, previous.height)
            close = 0 <= gap <= 1.8 * max(previous.height, 1.0)
            overlaps = min(region.right, previous.right) > max(region.left, previous.left)
            if similar_size and close and overlaps:
                merged[-1] = CoverTextRegion(
                    text=f"{previous.text} {region.text}",
                    confidence=min(previous.confidence, region.confidence),
                    height=max(previous.height, region.height),
                    page_index=page_index,
                    top=previous.top,
                    left=min(previous.left, region.left),
                    right=max(previous.right, region.right),
                )
                continue
        merged.append(region)
    merged.sort(key=lambda region: (-region.height, -region.confidence))
    return merged


@dataclass(frozen=True)
class CoverReading:
    regions: tuple[CoverTextRegion, ...]      # tallest first
    title_candidates: tuple[str, ...]
    author_candidates: tuple[str, ...]


def _clean(text: str) -> str:
    return " ".join(text.split()).strip(" .,:;-–—_|/\\")


def _is_noise(text: str) -> bool:
    if len(text) < 2:
        return True
    if _WATERMARK.search(text):
        return True
    # Pure punctuation or digits carry no bibliographic value on a cover.
    return not any(character.isalpha() for character in text)


def extract_cover_text(
    reader: Any,
    pdf_path: str | Path,
    page_indexes: tuple[int, ...] = (0, 1),
) -> CoverReading:
    """Render the cover pages and read their text regions.

    ``reader`` is an ``easyocr.Reader``; it is injected rather than constructed
    here because loading the Bengali model costs seconds of GPU time and must be
    amortised across the whole corpus, not paid per document.
    """
    images = render_pages(pdf_path, list(page_indexes), dpi=_COVER_DPI)
    regions: list[CoverTextRegion] = []
    for index in sorted(images):
        import numpy as np

        try:
            found = reader.readtext(np.asarray(images[index]), detail=1, paragraph=False)
        except Exception:
            continue  # A single unreadable page must not fail the document.
        for box, text, confidence in found:
            cleaned = _clean(str(text))
            if confidence < _MIN_CONFIDENCE or _is_noise(cleaned):
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            regions.append(CoverTextRegion(
                cleaned, float(confidence), max(ys) - min(ys), index,
                top=min(ys), left=min(xs), right=max(xs),
            ))

    # The front cover alone decides the ranked candidates.  Page 2 is usually the
    # title or copyright page, valuable for publisher and date but typographically
    # flat, so it would pollute a height-based ranking.
    front = merge_lines(regions, page_indexes[0])

    # Tallest first: on a cover, size encodes bibliographic rank.  The title is
    # the largest block and the author is normally the next distinct one, so the
    # two lists are offset rather than identical -- but they deliberately
    # overlap, because the caller decides the roles by looking each candidate up
    # in the catalogue rather than trusting typography alone.
    regions.sort(key=lambda region: (-region.height, -region.confidence))
    return CoverReading(
        regions=tuple(regions),
        title_candidates=tuple(region.text for region in front[:3]),
        author_candidates=tuple(region.text for region in front[1:4]),
    )


def build_reader(gpu: bool = True) -> Any:
    """Construct the shared EasyOCR reader from the local model cache.

    ``download_enabled=False`` is deliberate: the cluster nodes have no outbound
    network, so a missing model must fail loudly here rather than hang there.
    """
    import easyocr

    return easyocr.Reader(
        ["bn", "en"], gpu=gpu,
        model_storage_directory="/scratch/pdf-craft/models-cache/easyocr",
        download_enabled=False, verbose=False,
    )
