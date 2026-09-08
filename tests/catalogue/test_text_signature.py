"""Tests for the format-blind text signatures (PDF text layer <-> EPUB)."""
from __future__ import annotations

import random

from pdf_craft.catalogue.text_signature import (
    TEXT_SIGNATURE_ALGO_VERSION,
    TextSignature,
    band_keys,
    estimate_jaccard,
    find_text_duplicates,
    minhash,
    normalize_for_shingles,
    shingle_hashes,
)


def _body(seed: int, words: int = 3000) -> str:
    """Long varied prose: thousands of distinct character shingles.

    (A repeated single sentence would collapse to a handful of unique
    shingles and be -- correctly -- judged unusable.)
    """
    rng = random.Random(seed)
    vocabulary = [f"word{index}" for index in range(400)]
    return " ".join(rng.choice(vocabulary) for _ in range(words))


def _signature(document_id: int, text: str) -> TextSignature:
    hashes = shingle_hashes(text)
    return TextSignature(document_id, minhash(hashes), int(hashes.size))


def test_normalize_ignores_case_punctuation_and_layout() -> None:
    assert (normalize_for_shingles(" Hello,\tWORLD!\nNew  line... ")
            == "hello world new line")


def test_same_text_signs_identically() -> None:
    text = _body(11)
    assert _signature(1, text).signature == _signature(2, text).signature
    assert estimate_jaccard(_signature(1, text).signature,
                            _signature(2, text).signature) == 1.0


def test_different_books_do_not_match() -> None:
    score = estimate_jaccard(_signature(1, _body(21)).signature,
                             _signature(2, _body(22)).signature)
    assert score < 0.5


def test_short_text_is_not_usable() -> None:
    signature = _signature(1, "too short")
    assert signature.usable is False


def test_find_text_duplicates_reports_only_pairs_above_threshold() -> None:
    body = _body(31)
    signatures = {
        1: _signature(1, body),
        2: _signature(2, body),
        3: _signature(3, _body(32)),
    }
    assert all(signature.usable for signature in signatures.values())
    pairs = find_text_duplicates(signatures, threshold=0.75)
    assert [(left, right) for left, right, _ in pairs] == [(1, 2)]
    assert pairs[0][2] >= 0.75


def test_band_keys_are_deterministic_per_position() -> None:
    signature = _signature(1, _body(41)).signature
    first, second = band_keys(signature), band_keys(signature)
    assert first == second
    assert len(first) == 16
    assert len({key.split(":")[0] for key in first}) == 16


def test_algorithm_version_is_pinned() -> None:
    assert isinstance(TEXT_SIGNATURE_ALGO_VERSION, str)
    assert TEXT_SIGNATURE_ALGO_VERSION
