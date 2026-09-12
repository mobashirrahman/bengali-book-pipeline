from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

_BENGALI_VOWEL_SIGN_PATTERN = re.compile(
    r"[\u09BE\u09BF\u09C0\u09C1\u09C2\u09C3\u09C4\u09C7\u09C8"
    r"\u09CB\u09CC\u09CD\u09D7]"
)

_BENGALI_NUKTA_PATTERN = re.compile(r"\u09BC")

_COMMON_HONORIFICS = re.compile(
    r"(শ্রী|মহোদয়|বাবু|দা\.|দাদা|দিদা|খান|সাহেb|সাহেb|মহাশয়)"
)


def normalize_bengali(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = _BENGALI_NUKTA_PATTERN.sub("", text)
    text = _COMMON_HONORIFICS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def title_sort_key(title: str) -> str:
    normalized = normalize_bengali(title)
    normalized = normalized.lower()
    return normalized


@dataclass
class MatchResult:
    score: float
    title_score: float
    author_score: float
    matched_title: str
    matched_author: str
    source: str


def fuzzy_match_score(query: str, candidate: str) -> float:
    q = normalize_bengali(query)
    c = normalize_bengali(candidate)
    if not q or not c:
        return 0.0
    ratio = fuzz.token_sort_ratio(q, c) / 100.0
    partial = fuzz.partial_ratio(q, c) / 100.0
    return max(ratio, partial)


def match_book(
    query_title: str,
    query_author: str,
    candidate_title: str,
    candidate_author: str,
    source: str = "",
    title_weight: float = 0.6,
    author_weight: float = 0.4,
    title_threshold: float = 0.55,
    author_threshold: float = 0.50,
) -> MatchResult | None:
    title_score = fuzzy_match_score(query_title, candidate_title)
    author_score = fuzzy_match_score(query_author, candidate_author) if query_author and candidate_author else 0.0

    if title_score < title_threshold:
        return None
    if query_author and candidate_author and author_score < author_threshold:
        return None

    combined = title_weight * title_score + author_weight * author_score

    return MatchResult(
        score=combined,
        title_score=title_score,
        author_score=author_score,
        matched_title=candidate_title,
        matched_author=candidate_author,
        source=source,
    )


def find_best_match(
    query_title: str,
    query_author: str,
    candidates: list[tuple[str, str, str]],
    title_weight: float = 0.6,
    author_weight: float = 0.4,
    combined_threshold: float = 0.50,
) -> MatchResult | None:
    best: MatchResult | None = None
    for cand_title, cand_author, source in candidates:
        result = match_book(
            query_title=query_title,
            query_author=query_author,
            candidate_title=cand_title,
            candidate_author=cand_author,
            source=source,
            title_weight=title_weight,
            author_weight=author_weight,
        )
        if result is not None and (best is None or result.score > best.score):
            best = result
    if best is not None and best.score < combined_threshold:
        return None
    return best


def infer_author_from_path(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 2:
        author_dir = parts[-2]
        if not author_dir.startswith("."):
            return author_dir
    return ""


def infer_title_from_path(path: str) -> str:
    filename = path.replace("\\", "/").split("/")[-1]
    stem = filename.rsplit(".", 1)[0]
    stem = re.sub(r"\s*[-–—]\s*[^-–—]+$", "", stem)
    return stem
