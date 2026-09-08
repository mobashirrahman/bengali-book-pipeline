"""Tests for the shared render pass and near-duplicate detection.

The thresholds asserted here were calibrated against the live corpus, not
chosen: the same book rendered at 110/150/200 DPI and JPEG quality 60/85 stayed
within Hamming distance 5, while distinct books sat at 27-39.
"""
from __future__ import annotations

import random

from PIL import Image

from pdf_craft.catalogue.page_fingerprint import (
    DUPLICATE_THRESHOLD, PageFingerprint, dhash, find_duplicate_documents,
    from_signed64, hamming, sample_page_indexes, to_signed64,
)


def _pages(*hashes: int, start: int = 1) -> list[PageFingerprint]:
    return [PageFingerprint(start + offset, value, 50.0, True) for offset, value in enumerate(hashes)]


def test_sample_covers_front_and_interior():
    # Front matter carries the title; interior pages separate books that share
    # a generic cover such as a library plate.
    indexes = sample_page_indexes(908)
    assert indexes[:2] == [0, 1]
    assert len(indexes) == 5
    assert max(indexes) < 908


def test_sample_handles_tiny_and_empty_documents():
    assert sample_page_indexes(1) == [0]
    assert sample_page_indexes(0) == []


def test_dhash_survives_rescaling():
    # A re-scrape differs in resolution; the hash must not.
    random.seed(7)
    image = Image.new("L", (400, 600))
    image.putdata([random.randint(0, 255) for _ in range(400 * 600)])
    smaller = image.resize((200, 300), Image.LANCZOS)
    assert hamming(dhash(image), dhash(smaller)) <= DUPLICATE_THRESHOLD


def test_signed64_round_trip_covers_the_high_bit():
    # SQLite INTEGER is signed, so hashes with the top bit set overflow unless
    # they are mapped first.  This failed on the first full corpus run.
    for value in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1):
        assert from_signed64(to_signed64(value)) == value
        assert -(1 << 63) <= to_signed64(value) < (1 << 63)


def test_duplicate_requires_two_agreeing_pages():
    random.seed(3)
    a, b, c = (random.getrandbits(64) for _ in range(3))
    fingerprints = {
        1: _pages(a, b),
        2: _pages(a ^ 0b11, b ^ 0b1),   # same book, minor scan differences
        3: _pages(c, c ^ 0b101),        # unrelated book
    }
    counts = {1: 82, 2: 82, 3: 82}
    pairs = find_duplicate_documents(fingerprints, counts)
    assert [(pair.left_id, pair.right_id) for pair in pairs] == [(1, 2)]


def test_page_count_blocking_rejects_mismatched_lengths():
    # A real false positive: a 524-page book matched a 21-page one on a single
    # shared page.  Page count is the cheapest way to rule that out.
    random.seed(4)
    shared_a, shared_b = random.getrandbits(64), random.getrandbits(64)
    fingerprints = {1: _pages(shared_a, shared_b), 2: _pages(shared_a, shared_b)}
    assert find_duplicate_documents(fingerprints, {1: 524, 2: 21}) == []
    assert find_duplicate_documents(fingerprints, {1: 82, 2: 82})


def test_short_documents_are_excluded():
    # A 6-page excerpt paired with twelve unrelated titles because its few
    # sampled pages were nearly blank.
    random.seed(5)
    shared = random.getrandbits(64)
    fingerprints = {1: _pages(shared, shared ^ 1), 2: _pages(shared, shared ^ 1)}
    assert find_duplicate_documents(fingerprints, {1: 6, 2: 6}) == []


def test_site_boilerplate_pages_are_ignored():
    # Scraper sites prepend a branded banner to every file; one granthagara
    # template hash appeared in 309 documents.
    random.seed(6)
    template = random.getrandbits(64)
    unique = [random.getrandbits(64) for _ in range(3)]
    fingerprints = {
        index + 1: _pages(template, unique[index]) for index in range(3)
    }
    counts = {1: 90, 2: 90, 3: 90}
    # The template is shared by three documents, so it is rejected, and each
    # document's remaining page is unique -- no pair reaches two agreements.
    assert find_duplicate_documents(fingerprints, counts) == []


def test_uninformative_pages_are_ignored():
    blank = PageFingerprint(1, 12345, 2.0, False)
    real = PageFingerprint(2, 999, 60.0, True)
    fingerprints = {1: [blank, real], 2: [blank, real]}
    # Only one informative page agrees, which is below the required two.
    assert find_duplicate_documents(fingerprints, {1: 90, 2: 90}) == []
