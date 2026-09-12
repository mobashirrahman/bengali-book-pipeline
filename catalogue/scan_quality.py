"""Choose the better scan when one Bengali book was scraped twice.

Two scraper sites carry the same Bengali titles, each with its own scan, crop
and DPI, and both PDFs lack a text layer, so filenames, hashes and metadata
all fail to tell the scans apart.  The only difference the user cares about
is which scan survives OCR with fewer errors, and Tesseract's per-word
confidence on a few interior pages is the cheapest direct measure of that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .page_fingerprint import render_pages, sample_page_indexes

# Three interior pages balance evidence against cost: one page can be an
# illustration or a blank leaf, while rendering every page at OCR resolution
# would repeat the most expensive step of the pipeline for no extra signal.
SAMPLE_PAGES: int = 3

# A mean over fewer words than this is dominated by a single lucky line, so
# such pages are reported as unmeasurable rather than allowed to outvote
# genuinely measured ones.
MIN_WORDS: int = 20

# The cluster worker ships its own Tesseract build, which must win over any
# system binary so that local runs and cluster runs score scans identically.
_WORKER_TESSERACT = "/scratch/pdf-craft-worker-mdra00001/tesseract/bin/tesseract"

# The best-accuracy Bengali trained data lives outside the worker image, so it
# is picked up only where that cache directory has actually been provisioned.
_TESSDATA_DIR = "/scratch/pdf-craft/models-cache/tesseract-best"


def find_tesseract() -> str | None:
    """Locate the Tesseract binary, preferring the cluster worker build."""
    if os.path.isfile(_WORKER_TESSERACT) and os.access(_WORKER_TESSERACT, os.X_OK):
        return _WORKER_TESSERACT
    return shutil.which("tesseract")


# Resolved once at import because every page of every document would
# otherwise repeat the same filesystem probes.
TESSERACT_BINARY: str | None = find_tesseract()


@dataclass(frozen=True)
class ScanQuality:
    document_id: int
    mean_confidence: float | None  # 0-100; None when unmeasurable.
    word_count: int
    pages_measured: int


def _tesseract_env() -> dict[str, str] | None:
    # Inheriting the environment when the cache is absent keeps developer
    # machines working with their own TESSDATA_PREFIX or system defaults.
    if Path(_TESSDATA_DIR).is_dir():
        return {**os.environ, "TESSDATA_PREFIX": _TESSDATA_DIR}
    return None


def page_confidence(
    image_path: str | Path, *, lang: str = "ben", timeout: int = 120
) -> tuple[float | None, int]:
    """Mean Tesseract word confidence for one rendered page image."""
    if not TESSERACT_BINARY:
        return (None, 0)
    try:
        completed = subprocess.run(
            # TSV output is requested with -c rather than the "tsv" config
            # file: the trimmed tessdata cache used for best-accuracy Bengali
            # recognition ships only language data, no configs/ directory, so
            # the config file can't be found and tesseract silently falls
            # back to plain text -- wrong output at exit code 0, no error.
            [TESSERACT_BINARY, str(image_path), "stdout", "-l", lang, "--psm", "3",
             "-c", "tessedit_create_tsv=1"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_tesseract_env(),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        # A hung or unrunnable OCR process carries no signal about the scan,
        # so the page counts as unmeasured rather than as low quality.
        return (None, 0)
    lines = (completed.stdout or "").splitlines()
    if not lines:
        return (None, 0)
    header = lines[0].split("\t")
    try:
        conf_index = header.index("conf")
        text_index = header.index("text")
    except ValueError:
        return (None, 0)
    confidences: list[float] = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) <= max(conf_index, text_index):
            continue
        try:
            confidence = float(fields[conf_index])
        except ValueError:
            continue
        # Tesseract marks block/line rows with conf -1 and emits empty text
        # for glyphs it segments but cannot read; neither is a word scored.
        if confidence < 0 or not fields[text_index].strip():
            continue
        confidences.append(confidence)
    if len(confidences) < MIN_WORDS:
        return (None, 0)
    return (sum(confidences) / len(confidences), len(confidences))


def measure_document(
    document_id: int, pdf_path: str | Path, page_count: int, *, lang: str = "ben"
) -> ScanQuality:
    """Score a scan by word-count-weighted mean confidence over interior pages."""
    try:
        if page_count <= 2:
            return ScanQuality(document_id, None, 0, 0)
        # sample_page_indexes always leads with the cover and banner page,
        # which carry scraper branding rather than body text, so extra indexes
        # are requested and the covers are dropped to leave SAMPLE_PAGES of
        # interior evidence.
        candidates = sample_page_indexes(page_count, SAMPLE_PAGES + 2)
        indexes = [index for index in candidates if index >= 2][:SAMPLE_PAGES]
        if not indexes:
            return ScanQuality(document_id, None, 0, 0)
        # Tesseract needs well above the 110 DPI the fingerprint pass uses;
        # 300 DPI is its documented sweet spot for small Bengali type.
        rendered = render_pages(pdf_path, indexes, dpi=300)
        if not rendered:
            return ScanQuality(document_id, None, 0, 0)
        weighted_sum = 0.0
        total_words = 0
        measured = 0
        with tempfile.TemporaryDirectory(prefix="pdf-craft-sq-") as directory:
            for index, image in rendered.items():
                location = Path(directory) / f"p{index}.png"
                image.save(location, format="PNG")
                confidence, words = page_confidence(location, lang=lang)
                if confidence is None:
                    continue
                # Weighting by word count treats each recognised word as one
                # vote, so a dense body page outweighs a sparse frontispiece.
                weighted_sum += confidence * words
                total_words += words
                measured += 1
        if measured == 0 or total_words == 0:
            return ScanQuality(document_id, None, 0, 0)
        return ScanQuality(document_id, weighted_sum / total_words, total_words, measured)
    except Exception:
        # Quality scoring must never break cataloguing: callers compare two
        # scans and treat an unmeasurable one as simply losing the contest.
        return ScanQuality(document_id, None, 0, 0)
