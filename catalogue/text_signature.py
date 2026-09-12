"""Content signatures for text-bearing documents.

The page-image fingerprints in :mod:`page_fingerprint` can only compare two
PDFs, because an EPUB has no pages to render.  Yet the collection was scraped
from several sites, and the same book is routinely present once as a scanned
PDF and once as an EPUB.  Nothing in the visual pipeline can see that pair.

This module closes that gap with a MinHash over the extracted text, which is
format-blind: whatever produced the bytes, two files that contain the same book
share the same shingles.  It applies to every EPUB and to the minority of PDFs
that carry a real text layer (roughly 14% of them -- the rest are pure scans and
are only reachable through page images or OCR).

Character n-grams are used rather than word n-grams deliberately.  Text layers
in this collection range from born-digital to OCR output, and OCR corrupts
individual characters.  A single bad character destroys ``_SHINGLE_SIZE``
character shingles out of tens of thousands, but would destroy an entire word
n-gram out of only a few thousand -- proportionally a much larger loss.  The
character form degrades gracefully where the word form falls off a cliff.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Version tag stored alongside every cached text-signature row.  A cached row
# is reused only when both the file's SHA-256 and this version match, so any
# change to shingling, permutations, or extraction windows must bump it.
TEXT_SIGNATURE_ALGO_VERSION = "char9-minhash128-epub-pdf3-24-v1"

# Nine characters is long enough to be specific in both Bengali and English
# without being so long that one OCR error invalidates a whole sentence.
_SHINGLE_SIZE = 9

# 128 permutations put the standard error of the Jaccard estimate near 1/sqrt(128)
# ~= 0.09, which is comfortably tighter than the gap between the duplicate and
# non-duplicate populations we need to separate.
NUM_PERM = 128

# 16 bands of 8 rows.  A pair is a candidate when any band matches exactly, so
# the detection probability is 1-(1-s^8)^16: ~4% at Jaccard 0.4, ~48% at 0.7,
# ~97% at 0.85.  That is the intended shape -- aggressive above 0.8, quiet below.
BANDS = 16

# Mersenne prime; universal hashing modulo a prime keeps the permutations
# independent without needing 128 real permutation tables.
_PRIME = (1 << 61) - 1

# Below this many shingles the estimate is dominated by sampling noise, and a
# short front-matter page can look like any other short front-matter page.
MIN_SHINGLES = 500

# Enough text to identify a book several times over; the cost of the MinHash is
# linear in this number, so the cap is what keeps a full-corpus pass tractable.
_MAX_CHARS = 60_000

# PDF front matter is exactly the boilerplate that page-image dedupe already had
# to reject: scraper banners, blank leaves, title pages in a house style.  Body
# text starts later.
_PDF_FIRST_PAGE = 3
_PDF_LAST_PAGE = 24

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class TextSignature:
    document_id: int
    signature: tuple[int, ...]
    shingle_count: int

    @property
    def usable(self) -> bool:
        return self.shingle_count >= MIN_SHINGLES


def normalize_for_shingles(text: str) -> str:
    """Reduce text to the part that survives a change of format.

    Case, whitespace layout and punctuation all differ between a PDF text layer
    and the EPUB of the same book without the words differing at all, so none of
    them may take part in the signature.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = "".join(
        char for char in text
        if char.isalnum() or char.isspace()
    )
    return _WHITESPACE.sub(" ", text).strip().lower()


def extract_pdf_text(path: str | Path, *, timeout: int = 60) -> str:
    """Read a PDF's text layer with poppler.

    ``pdftotext`` is used rather than pypdf because pypdf raises
    ``DependencyError`` on the AES-encrypted files in this collection -- 10% of
    it -- while poppler reads them without complaint.
    """
    try:
        result = subprocess.run(
            [
                "pdftotext", "-q",
                "-f", str(_PDF_FIRST_PAGE), "-l", str(_PDF_LAST_PAGE),
                str(path), "-",
            ],
            capture_output=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.decode("utf-8", errors="replace")[:_MAX_CHARS]


def extract_epub_text(path: str | Path, *, max_chars: int = _MAX_CHARS) -> str:
    """Concatenate an EPUB's XHTML content, tags stripped."""
    chunks: list[str] = []
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name for name in archive.namelist()
                if name.lower().endswith((".xhtml", ".html", ".htm"))
            ]
            for name in sorted(names):
                if total >= max_chars:
                    break
                try:
                    raw = archive.read(name).decode("utf-8", errors="replace")
                except (KeyError, zipfile.BadZipFile):
                    continue
                stripped = _TAG.sub(" ", raw)
                chunks.append(stripped)
                total += len(stripped)
    except (zipfile.BadZipFile, OSError):
        return ""
    return "".join(chunks)[:max_chars]


def extract_text(path: str | Path, media_type: str) -> str:
    if media_type == "application/epub+zip":
        return extract_epub_text(path)
    return extract_pdf_text(path)


def shingle_hashes(text: str, size: int = _SHINGLE_SIZE) -> np.ndarray:
    """Hash every character n-gram of the normalized text to a uint64."""
    normalized = normalize_for_shingles(text)
    if len(normalized) < size:
        return np.empty(0, dtype=np.uint64)
    encoded = np.frombuffer(normalized.encode("utf-32-le"), dtype=np.uint32)
    # A rolling polynomial hash over the code points: cheap, vectorized, and
    # good enough because MinHash only needs the values to be well spread.
    windows = np.lib.stride_tricks.sliding_window_view(encoded, size)
    weights = np.power(np.uint64(1099511628211), np.arange(size, dtype=np.uint64))
    hashes = (windows.astype(np.uint64) * weights).sum(axis=1, dtype=np.uint64)
    return np.unique(hashes)


def _permutations(num_perm: int = NUM_PERM) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic (a, b) coefficient pairs for universal hashing.

    Seeded from a constant so that signatures computed in different runs, or on
    different machines, remain comparable.
    """
    rng = np.random.default_rng(0x9E3779B97F4A7C15)
    a = rng.integers(1, _PRIME, size=num_perm, dtype=np.uint64)
    b = rng.integers(0, _PRIME, size=num_perm, dtype=np.uint64)
    return a, b


_A, _B = _permutations()


def minhash(hashes: np.ndarray, num_perm: int = NUM_PERM) -> tuple[int, ...]:
    """Reduce a shingle set to a fixed-width signature."""
    if hashes.size == 0:
        return tuple([0] * num_perm)
    # Modulo a prime under uint64 arithmetic; the product is taken in Python's
    # arbitrary precision only where it would overflow, via the split below.
    values = hashes.astype(np.uint64) % np.uint64(_PRIME)
    permuted = (
        (values[:, None] * _A[None, :num_perm]) % np.uint64(_PRIME)
        + _B[None, :num_perm]
    ) % np.uint64(_PRIME)
    return tuple(int(value) for value in permuted.min(axis=0))


def estimate_jaccard(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Fraction of signature positions that agree -- an unbiased Jaccard estimate."""
    if not left or not right or len(left) != len(right):
        return 0.0
    agreeing = sum(1 for a, b in zip(left, right) if a == b)
    return agreeing / len(left)


def band_keys(signature: tuple[int, ...], bands: int = BANDS) -> list[str]:
    """Split a signature into band digests for candidate lookup.

    Comparing every pair of 4,600 signatures is 10.6M comparisons of 128 ints;
    banding replaces that with a dictionary lookup, so only pairs that already
    agree on eight consecutive positions are ever scored.
    """
    if not signature:
        return []
    rows = len(signature) // bands
    keys: list[str] = []
    for band in range(bands):
        chunk = signature[band * rows:(band + 1) * rows]
        digest = hashlib.blake2b(
            b"".join(value.to_bytes(8, "big") for value in chunk),
            digest_size=8,
        ).hexdigest()
        keys.append(f"{band}:{digest}")
    return keys


def signature_document(document_id: int, path: str | Path, media_type: str) -> TextSignature:
    hashes = shingle_hashes(extract_text(path, media_type))
    return TextSignature(document_id, minhash(hashes), int(hashes.size))


def find_text_duplicates(
    signatures: dict[int, TextSignature],
    *,
    threshold: float = 0.75,
) -> list[tuple[int, int, float]]:
    """Return (left, right, jaccard) for every pair above ``threshold``.

    Candidates come from LSH banding, so this is linear in the corpus rather
    than quadratic; the exact estimate is then computed only for the candidates.
    """
    buckets: dict[str, list[int]] = {}
    for signature in signatures.values():
        if not signature.usable:
            continue
        for key in band_keys(signature.signature):
            buckets.setdefault(key, []).append(signature.document_id)

    candidates: set[tuple[int, int]] = set()
    for members in buckets.values():
        # A band shared by a large number of documents is boilerplate -- a common
        # publisher front matter, not a shared book.  The same reasoning that
        # rejects template pages in the visual pipeline applies here.
        if len(members) > 50:
            continue
        for position, left in enumerate(sorted(members)):
            for right in sorted(members)[position + 1:]:
                candidates.add((left, right))

    pairs: list[tuple[int, int, float]] = []
    for left, right in candidates:
        score = estimate_jaccard(
            signatures[left].signature, signatures[right].signature
        )
        if score >= threshold:
            pairs.append((left, right, score))
    pairs.sort(key=lambda pair: -pair[2])
    return pairs
