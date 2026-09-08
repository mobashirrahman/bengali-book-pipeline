"""Tests for the format-blind text signatures (PDF text layer <-> EPUB)."""
from __future__ import annotations

import random
import string
from pathlib import Path

from pdf_craft.catalogue.text_signature import (
    BANDS,
    MIN_SHINGLES,
    TEXT_SIGNATURE_ALGO_VERSION,
    TextSignature,
    band_keys,
    estimate_jaccard,
    extract_epub_text,
    extract_pdf_text,
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


# --- Spec: normalization, MinHash quality, lookup and extraction ---------------


def _random_word(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase)
                   for _ in range(rng.randint(4, 9)))


def _prose(seed: int, words: int = 2500) -> str:
    """Long prose over a large word space, so unrelated texts stay apart."""
    rng = random.Random(seed)
    return " ".join(_random_word(rng) for _ in range(words))


def _mutated(text: str, seed: int, fraction: float) -> str:
    rng = random.Random(seed)
    words = text.split()
    victims = set(rng.sample(range(len(words)),
                             int(len(words) * fraction)))
    return " ".join(
        word if index not in victims else _random_word(rng)
        for index, word in enumerate(words)
    )


def _prose_signature(document_id: int, text: str) -> TextSignature:
    hashes = shingle_hashes(text)
    assert int(hashes.size) >= MIN_SHINGLES
    return TextSignature(document_id, minhash(hashes), int(hashes.size))


def test_normalize_collapses_case_punctuation_whitespace_and_unicode() -> None:
    assert (normalize_for_shingles(" Hello,\tWORLD!\nNew  line... ")
            == "hello world new line")
    assert (normalize_for_shingles("CAFÉ — tales!")
            == normalize_for_shingles("café tales"))
    assert normalize_for_shingles("école") == normalize_for_shingles("école")


def test_shingles_of_too_short_text_are_empty() -> None:
    assert shingle_hashes("hi").size == 0


def test_minhash_of_a_set_with_itself_estimates_one() -> None:
    signature = _prose_signature(1, _prose(501)).signature
    assert estimate_jaccard(signature, signature) == 1.0


def test_near_duplicate_texts_estimate_high() -> None:
    original = _prose(502)
    changed = _mutated(original, seed=503, fraction=0.10)
    score = estimate_jaccard(_prose_signature(1, original).signature,
                             _prose_signature(2, changed).signature)
    assert score > 0.6


def test_unrelated_texts_estimate_low() -> None:
    score = estimate_jaccard(_prose_signature(1, _prose(504)).signature,
                             _prose_signature(2, _prose(505)).signature)
    assert score < 0.2


def test_signatures_are_deterministic_across_calls() -> None:
    text = _prose(506)
    assert minhash(shingle_hashes(text)) == minhash(shingle_hashes(text))


def test_band_keys_count_and_identity() -> None:
    signature = _prose_signature(1, _prose(507)).signature
    assert len(band_keys(signature)) == BANDS
    assert band_keys(signature) == band_keys(signature)


def test_find_text_duplicates_reports_only_the_near_identical_pair() -> None:
    body = _prose(508)
    signatures = {
        1: _prose_signature(1, body),
        2: _prose_signature(2, body),
        3: _prose_signature(3, _prose(509)),
    }
    pairs = find_text_duplicates(signatures)
    assert [(left, right) for left, right, _ in pairs] == [(1, 2)]
    assert pairs[0][2] >= 0.75


def test_find_text_duplicates_skips_short_signatures() -> None:
    tiny_hashes = shingle_hashes("too short")
    tiny = TextSignature(1, minhash(tiny_hashes), int(tiny_hashes.size))
    assert tiny.usable is False
    full = _prose_signature(2, _prose(510))
    assert find_text_duplicates({1: tiny, 2: full}) == []


def test_epub_extract_returns_empty_for_an_invalid_zip(tmp_path: Path) -> None:
    junk = tmp_path / "junk.epub"
    junk.write_bytes(b"this is not a zip file at all")
    assert extract_epub_text(junk) == ""


def test_pdf_extract_returns_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert extract_pdf_text(tmp_path / "does-not-exist.pdf") == ""
