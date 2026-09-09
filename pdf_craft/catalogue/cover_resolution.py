"""Turn ranked cover-OCR candidates into a title and an author.

Cover OCR produces a list of text blocks ordered by how large they are printed.
Typography says which block matters most, but it cannot say which is the title
and which is the author -- both are set large, and on many Bengali covers the
author is set larger than the title.  Nothing in the image resolves that.

The catalogue does.  A block that matches a known person is an author; a block
that matches a known title is a title.  The roles are read off the catalogue
rather than guessed from layout, so a resolved pair is a claim the collection
can already corroborate rather than an OCR guess.

Where a block matches both -- names and titles overlap, because a biography is
routinely titled with its subject's name -- the person reading wins.  There are
68,154 distinct people keys against 174,524 title keys, so a name is the more
distinctive of the two matches, and the string that lost is then free to be
claimed as the title by the next-best candidate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .text_normalize import normalize_name, normalize_text

# Below this, a "title" is a word like ভূমিকা (introduction) or গল্প (story)
# printed as a section heading, which collides with hundreds of real books.
MIN_TITLE_KEY = 8

# rapidfuzz ``ratio`` on normalized keys.  88 keeps ordinary OCR damage -- a
# dropped matra, a split conjunct -- while rejecting a different book by the
# same author, which typically scores in the 60s.
DEFAULT_MIN_SCORE = 88.0


@dataclass(frozen=True)
class ResolvedCover:
    document_id: int
    title: str | None = None
    authors: tuple[str, ...] = ()
    title_score: float = 0.0
    author_score: float = 0.0
    method: str = "none"  # 'exact', 'fuzzy' or 'none'

    @property
    def resolved(self) -> bool:
        return bool(self.title or self.authors)


class CatalogueIndex:
    """Normalized lookup over catalogue people and titles.

    Built once and reused: the exact maps answer most queries for free, and the
    key lists back a vectorized fuzzy pass for the rest.
    """

    def __init__(self, conn) -> None:
        self.people: dict[str, str] = {}
        for (name,) in conn.execute(
            "SELECT name FROM catalogue_people WHERE name IS NOT NULL"
        ):
            key = normalize_name(name)
            if key:
                self.people.setdefault(key, name)
        self.titles: dict[str, str] = {}
        for (title,) in conn.execute(
            "SELECT title FROM catalogue_editions WHERE title IS NOT NULL"
        ):
            key = normalize_text(title)
            if key and len(key) >= MIN_TITLE_KEY:
                self.titles.setdefault(key, title)
        self.person_keys = list(self.people)
        self.title_keys = list(self.titles)


def _best_fuzzy(
    keys: list[str], haystack: list[str], min_score: float
) -> dict[str, tuple[str, float]]:
    """Best match per key, computed for every key in one vectorized pass.

    ``cdist`` over the whole candidate set at once is what makes this
    affordable: 200 keys against 68,154 takes 0.1s, so the entire corpus costs
    seconds rather than a per-document round trip.
    """
    if not keys or not haystack:
        return {}
    import numpy as np
    from rapidfuzz import fuzz, process

    matrix = process.cdist(
        keys, haystack, scorer=fuzz.ratio, score_cutoff=min_score, workers=-1
    )
    best: dict[str, tuple[str, float]] = {}
    for row, key in enumerate(keys):
        column = int(np.argmax(matrix[row]))
        score = float(matrix[row][column])
        if score >= min_score:
            best[key] = (haystack[column], score)
    return best


def resolve_covers(
    readings: dict[int, list[str]],
    index: CatalogueIndex,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict[int, ResolvedCover]:
    """Assign a title and authors to each document's ranked candidate blocks.

    ``readings`` maps a document id to its candidate strings, most prominent
    first; that order is the tie-breaker, because a cover sets what matters
    largest.
    """
    # Normalize every candidate once, then resolve exact hits before paying for
    # any fuzzy work -- most covers are answered by the exact maps alone.
    name_keys: dict[str, str] = {}
    title_keys: dict[str, str] = {}
    for candidates in readings.values():
        for candidate in candidates:
            name_keys.setdefault(candidate, normalize_name(candidate))
            title_keys.setdefault(candidate, normalize_text(candidate))

    unresolved_names = sorted({
        key for key in name_keys.values() if key and key not in index.people
    })
    unresolved_titles = sorted({
        key for key in title_keys.values()
        if len(key) >= MIN_TITLE_KEY and key not in index.titles
    })
    fuzzy_people = _best_fuzzy(unresolved_names, index.person_keys, min_score)
    fuzzy_titles = _best_fuzzy(unresolved_titles, index.title_keys, min_score)

    resolved: dict[int, ResolvedCover] = {}
    for document_id, candidates in readings.items():
        authors: list[str] = []
        author_score = 0.0
        claimed: set[str] = set()
        exact = False

        for candidate in candidates:
            key = name_keys[candidate]
            if not key:
                continue
            if key in index.people:
                authors.append(index.people[key])
                claimed.add(candidate)
                author_score = max(author_score, 100.0)
                exact = True
            elif key in fuzzy_people:
                name, score = fuzzy_people[key]
                authors.append(index.people[name])
                claimed.add(candidate)
                author_score = max(author_score, score)

        title: str | None = None
        title_score = 0.0
        for candidate in candidates:
            # A block already read as an author cannot also be the title; this
            # is what stops a biography's subject being recorded as both.
            if candidate in claimed:
                continue
            key = title_keys[candidate]
            if len(key) < MIN_TITLE_KEY:
                continue
            if key in index.titles:
                title, title_score, exact = index.titles[key], 100.0, True
                break
            if key in fuzzy_titles:
                matched, score = fuzzy_titles[key]
                title, title_score = index.titles[matched], score
                break

        if title or authors:
            resolved[document_id] = ResolvedCover(
                document_id=document_id,
                title=title,
                # Order is preserved but duplicates are not: the same name is
                # often detected on both the cover and the spine.
                authors=tuple(dict.fromkeys(authors)),
                title_score=title_score,
                author_score=author_score,
                method="exact" if exact else "fuzzy",
            )
    return resolved
