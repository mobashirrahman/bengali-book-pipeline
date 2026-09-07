from __future__ import annotations

from pdf_craft.catalogue.matching import (
    fuzzy_match_score,
    find_best_match,
    infer_author_from_path,
    infer_title_from_path,
    match_book,
    normalize_bengali,
)


class TestNormalizeBengali:
    def test_basic_normalization(self) -> None:
        result = normalize_bengali("  শেষের কবিতা  ")
        assert result == "শেষের কবিতা"

    def test_removes_honorifics(self) -> None:
        result = normalize_bengali("রবীন্দ্রনাথ ঠাকুর শ্রী")
        assert "শ্রী" not in result

    def test_collapses_whitespace(self) -> None:
        result = normalize_bengali("  গোরা   বই  ")
        assert "  " not in result


class TestFuzzyMatchScore:
    def test_identical_strings(self) -> None:
        score = fuzzy_match_score("শেষের কবিতা", "শেষের কবিতা")
        assert score == 1.0

    def test_similar_strings(self) -> None:
        score = fuzzy_match_score("শেষেরকবিতা", "শেষের কবিতা")
        assert score > 0.8

    def test_different_strings(self) -> None:
        score = fuzzy_match_score("গোরা", "নৌকাডুবি")
        assert score < 0.3

    def test_empty_strings(self) -> None:
        assert fuzzy_match_score("", "test") == 0.0
        assert fuzzy_match_score("test", "") == 0.0


class TestMatchBook:
    def test_exact_match(self) -> None:
        result = match_book(
            query_title="শেষের কবিতা",
            query_author="রবীন্দ্রনাথ ঠাকুর",
            candidate_title="শেষের কবিতা",
            candidate_author="রবীন্দ্রনাথ ঠাকুর",
        )
        assert result is not None
        assert result.score > 0.9

    def test_fuzzy_title_match(self) -> None:
        result = match_book(
            query_title="শেষেরকবিতা",
            query_author="রবীন্দ্রনাথ ঠাকুর",
            candidate_title="শেষের কবিতা",
            candidate_author="রবীন্দ্রনাথ ঠাকুর",
        )
        assert result is not None
        assert result.score > 0.7

    def test_no_match(self) -> None:
        result = match_book(
            query_title="গোরা",
            query_author="বঙ্কিমচন্দ্র",
            candidate_title="নৌকাডুবি",
            candidate_author="রবীন্দ্রনাথ",
        )
        assert result is None


class TestFindBestMatch:
    def test_finds_best(self) -> None:
        candidates = [
            ("নৌকাডুবি", "রবীন্দ্রনাথ ঠাকুর", "rokomari"),
            ("শেষের কবিতা", "রবীন্দ্রনাথ ঠাকুর", "rokomari"),
            ("গোরা", "বঙ্কিমচন্দ্র", "rokomari"),
        ]
        result = find_best_match(
            query_title="শেষের কবিতা",
            query_author="রবীন্দ্রনাথ ঠাকুর",
            candidates=candidates,
        )
        assert result is not None
        assert "শেষের কবিতা" in result.matched_title

    def test_returns_none_when_no_match(self) -> None:
        candidates = [
            ("Physics 101", "Newton", "other"),
        ]
        result = find_best_match(
            query_title="গোরা",
            query_author="বঙ্কিমচন্দ্র",
            candidates=candidates,
        )
        assert result is None


class TestPathInference:
    def test_infer_author_from_path(self) -> None:
        assert infer_author_from_path("রবীন্দ্রনাথ ঠাকুর/শেষের কবিতা.pdf") == "রবীন্দ্রনাথ ঠাকুর"
        assert infer_author_from_path("data/বঙ্কিমচন্দ্র/কপালকুণ্ডলা.pdf") == "বঙ্কিমচন্দ্র"

    def test_infer_title_from_path(self) -> None:
        assert infer_title_from_path("রবীন্দ্রনাথ ঠাকুর/শেষের কবিতা.pdf") == "শেষের কবিতা"
        assert infer_title_from_path("author/book - subtitle.pdf") == "book"
