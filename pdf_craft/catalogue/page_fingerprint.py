"""One render pass per document, shared by dedupe, OCR and cover extraction.

Rendering a scanned PDF page is by far the most expensive step in the metadata
pipeline, and three separate consumers need it, so they share a single pass:

* **Dedupe.** Byte-level dedupe finds nothing in this collection -- all 22,717
  documents have distinct sha256, because the same book scraped from two sites
  differs in scan, crop and compression.  Filenames do not help either: 2,453 of
  them are scraper placeholders such as ``Bangla eBooks pdf``.  Duplicates are
  therefore only visible in page content.
* **Metadata OCR.** 86% of the collection is scanned images with no text layer,
  so title and author have to be read off the rendered cover.
* **Covers** for the frontend.

``dhash`` is used rather than an average hash because it compares adjacent pixel
gradients, which survive the rescaling and re-compression that separate two
scans of one book.  Measured on this corpus: the same book rendered at 110/150/
200 DPI, at JPEG quality 60/85, and with a 2% margin crop, stays within Hamming
distance 5, while distinct books sit at 27-39.  ``DUPLICATE_THRESHOLD`` is set
at 10 to sit inside that gap with margin at both ends.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

# Version tag stored alongside every cached fingerprint row.  A cached row is
# reused only when both the file's SHA-256 and this version match, so any
# change to the render settings, hash, or page selection below must bump it --
# otherwise a rerun would silently trust fingerprints it can no longer
# interpret.
FINGERPRINT_ALGO_VERSION = "dhash110-samples5-v1"

# Comfortably above the observed same-book maximum (5) and far below the
# observed different-book minimum (27).
DUPLICATE_THRESHOLD = 10

# A page whose pixel standard deviation falls below this carries almost no ink
# -- a blank leaf or an even grey scan.  Such pages hash alike no matter which
# book they came from, so they are excluded rather than allowed to manufacture
# false duplicates.
_MIN_PAGE_STDDEV = 12.0

_RENDER_DPI = 110  # Enough for a stable hash; OCR re-renders its pages higher.


@dataclass(frozen=True)
class PageFingerprint:
    page_index: int   # 0-based
    dhash: int        # 64-bit perceptual hash
    stddev: float     # ink proxy; low means near-blank
    informative: bool


def dhash(image: Image.Image, size: int = 8) -> int:
    """64-bit difference hash built from horizontal pixel gradients."""
    grey = image.convert("L").resize((size + 1, size), Image.LANCZOS)
    pixels = np.asarray(grey, dtype=np.int16)
    bits = (pixels[:, 1:] > pixels[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming(left: int, right: int) -> int:
    return int(left ^ right).bit_count()


def sample_page_indexes(page_count: int, samples: int = 5) -> list[int]:
    """Pick pages to fingerprint.

    The first two pages carry the cover and title page, which is what dedupe and
    metadata both care about.  The rest are spread through the body so that two
    books sharing a generic cover -- a plain library plate, for instance -- are
    still told apart by their interiors.
    """
    if page_count <= 0:
        return []
    chosen = [0, 1]
    for fraction in (0.25, 0.50, 0.75):
        chosen.append(int(page_count * fraction))
    unique = sorted({index for index in chosen if 0 <= index < page_count})
    return unique[:samples]


def render_pages(pdf_path: str | Path, page_indexes: list[int], dpi: int = _RENDER_DPI) -> dict[int, Image.Image]:
    """Render selected pages with poppler.

    ``pdftoppm`` is used instead of a Python binding because it is already a
    dependency of the cluster pipeline and, being a separate process, contains
    the segfaults that malformed scans routinely trigger.
    """
    images: dict[int, Image.Image] = {}
    path = Path(pdf_path)
    with tempfile.TemporaryDirectory(prefix="pdf-craft-fp-") as directory:
        for index in page_indexes:
            prefix = Path(directory) / f"p{index}"
            page = index + 1  # pdftoppm counts from 1
            try:
                subprocess.run(
                    ["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi),
                     "-jpeg", "-jpegopt", "quality=80", str(path), str(prefix)],
                    check=True, capture_output=True, timeout=120,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                continue  # One unreadable page must not abandon the document.
            rendered = sorted(Path(directory).glob(f"p{index}*.jpg"))
            if rendered:
                try:
                    with Image.open(rendered[0]) as opened:
                        # Kept in RGB: EasyOCR's CRAFT detector is trained on
                        # three-channel input and loses text regions on a
                        # single-channel image.  The fingerprint path converts
                        # to greyscale itself, so nothing downstream needs it
                        # done here.
                        images[index] = opened.convert("RGB").copy()
                except OSError:
                    continue
    return images


def fingerprint_document(pdf_path: str | Path, page_count: int, samples: int = 5) -> list[PageFingerprint]:
    indexes = sample_page_indexes(page_count, samples)
    rendered = render_pages(pdf_path, indexes)
    results: list[PageFingerprint] = []
    for index in indexes:
        image = rendered.get(index)
        if image is None:
            continue
        deviation = float(np.asarray(image.convert("L"), dtype=np.float32).std())
        results.append(PageFingerprint(
            page_index=index,
            dhash=dhash(image),
            stddev=deviation,
            informative=deviation >= _MIN_PAGE_STDDEV,
        ))
    return results


def find_duplicate_pairs(
    hashes: dict[int, int], threshold: int = DUPLICATE_THRESHOLD
) -> list[tuple[int, int, int]]:
    """All-pairs near-duplicate search over ``{document_id: dhash}``.

    Done as a vectorised bit-count rather than a Python loop: the collection is
    ~22k documents, so the 258M pair comparisons are seconds of numpy but
    minutes of interpreter.  Returns ``(left_id, right_id, distance)``.
    """
    if len(hashes) < 2:
        return []
    ids = np.fromiter(hashes.keys(), dtype=np.int64, count=len(hashes))
    values = np.fromiter(hashes.values(), dtype=np.uint64, count=len(hashes))
    packed = values.view(np.uint8).reshape(len(values), 8)
    pairs: list[tuple[int, int, int]] = []
    block = 512
    for start in range(0, len(values), block):
        stop = min(start + block, len(values))
        # XOR this block against every later document, then popcount the bytes.
        xor = np.bitwise_xor(packed[start:stop, None, :], packed[None, start:, :])
        distances = np.unpackbits(xor, axis=2).sum(axis=2)
        rows, cols = np.nonzero(distances <= threshold)
        for row, col in zip(rows, cols):
            left, right = start + int(row), start + int(col)
            if left >= right:
                continue  # Skip self-pairs and the mirrored half.
            pairs.append((int(ids[left]), int(ids[right]), int(distances[row, col])))
    return pairs


# A document this short is a sample, fragment or link-list rather than a book.
# Measured: a 6-page bengaliebook excerpt paired with twelve unrelated titles
# because its few sampled pages were nearly blank, so short documents are held
# out of dedupe entirely rather than allowed to generate noise.
_MIN_DEDUPE_PAGES = 12

# Two scans of one book keep almost the same page count; the observed genuine
# duplicate matched exactly (82 and 82), while every false positive differed
# wildly (524 vs 21, 318 vs 524).  Blocking on page count first is nearly free
# and removes most of the noise before any hash is compared.
_PAGE_COUNT_TOLERANCE = 0.02


@dataclass(frozen=True)
class DuplicatePair:
    left_id: int
    right_id: int
    pages_agreeing: int
    page_count_delta: int


def find_duplicate_documents(
    fingerprints: dict[int, list[PageFingerprint]],
    page_counts: dict[int, int],
    *,
    threshold: int = DUPLICATE_THRESHOLD,
    min_pages_agreeing: int = 2,
) -> list[DuplicatePair]:
    """Group documents that are the same book scanned or repackaged twice.

    Three guards, each added because real data defeated the previous design:

    1. **Boilerplate rejection.** The scraper sites prepend a branded banner page
       to every file, so page 0 is identical across all books from one site --
       309 documents shared a single granthagara template hash.  A page hash seen
       in three or more documents is a template, not content, and is dropped.
    2. **Page-count blocking.** See ``_PAGE_COUNT_TOLERANCE`` above.
    3. **Agreement.** One matching interior page is coincidence; two or more,
       between documents of the same length, is a duplicate.
    """
    frequency: dict[int, int] = {}
    for pages in fingerprints.values():
        for page in pages:
            frequency[page.dhash] = frequency.get(page.dhash, 0) + 1
    boilerplate = {value for value, count in frequency.items() if count >= 3}

    usable: dict[int, list[int]] = {}
    for document_id, pages in fingerprints.items():
        if page_counts.get(document_id, 0) < _MIN_DEDUPE_PAGES:
            continue
        interior = [
            page.dhash for page in pages
            if page.page_index > 0 and page.informative and page.dhash not in boilerplate
        ]
        if interior:
            usable[document_id] = interior

    # Sorting by page count turns the page-count guard into a stopping rule
    # instead of a filter: once the candidate is longer than the tolerance
    # allows, so is every candidate after it.  The comparison set is the same as
    # a full scan would produce, but 20,970 documents no longer cost 220M pair
    # tests -- only the handful within each length band are ever compared.
    ids = sorted(usable, key=lambda doc_id: (page_counts[doc_id], doc_id))
    pairs: list[DuplicatePair] = []
    for position, left in enumerate(ids):
        left_pages = page_counts[left]
        allowance = max(1, int(left_pages * _PAGE_COUNT_TOLERANCE))
        for right in ids[position + 1:]:
            delta = page_counts[right] - left_pages
            if delta > allowance:
                break
            agreeing = sum(
                1 for a in usable[left]
                if any(hamming(a, b) <= threshold for b in usable[right])
            )
            if agreeing >= min_pages_agreeing:
                low, high = (left, right) if left <= right else (right, left)
                pairs.append(DuplicatePair(low, high, agreeing, delta))
    pairs.sort(key=lambda pair: (-pair.pages_agreeing, pair.page_count_delta))
    return pairs


# dhash produces an unsigned 64-bit value, but SQLite's INTEGER is signed
# 64-bit, so any hash with the top bit set overflows on insert.  Storage
# therefore round-trips through a signed representation; the bit pattern -- the
# only thing Hamming distance depends on -- is preserved exactly.
_SIGN_BIT = 1 << 63
_UINT64 = 1 << 64


def to_signed64(value: int) -> int:
    """Map an unsigned 64-bit hash into SQLite's signed INTEGER range."""
    return value - _UINT64 if value >= _SIGN_BIT else value


def from_signed64(value: int) -> int:
    """Inverse of :func:`to_signed64`."""
    return value + _UINT64 if value < 0 else value


def page_count(pdf_path: str | Path) -> int:
    """Page count via poppler, falling back to pypdf.

    ``pdfinfo`` is tried first because pypdf raises ``DependencyError`` on
    AES-encrypted files -- 416 of the first 4,250 documents fingerprinted, 10%
    of the corpus -- while poppler reads and renders exactly those files without
    complaint.  Since the rendering step already depends on poppler, taking the
    page count from it as well removes a dependency rather than adding one.
    """
    try:
        completed = subprocess.run(
            ["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=60, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass
    else:
        for line in completed.stdout.splitlines():
            if line.startswith("Pages:"):
                try:
                    return int(line.split(":", 1)[1].strip())
                except ValueError:
                    break
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0
