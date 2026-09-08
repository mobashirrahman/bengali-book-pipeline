"""Per-source filename parsing for the scraped Bengali PDF library.

These files were scraped from 12 sites, each with a RIGID naming template, so
this module does template matching, not general NLP.

CRITICAL precision rule: never guess. Returning nothing is a correct, useful
answer. A wrong title gets matched against a ~194,060-work catalogue and
produces a confident wrong match, while an omission falls back to other
evidence. Precision over recall.

Stdlib only. Pure function: takes a path STRING, returns a dataclass. No DB,
no network, no filesystem reads.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = ["ParsedFilename", "parse_filename"]


@dataclass(frozen=True)
class ParsedFilename:
    titles: tuple[str, ...]  # candidate titles, best first
    authors: tuple[str, ...]  # candidate author names, best first
    source: str  # scraper key, e.g. "granthagara", or "authordir"
    volume: str | None = None  # e.g. "2" from [ভাগ-২২] / [Pt. 23] / (25)
    edition: str | None = None  # e.g. "5" from [সংস্করণ-৫] / [Ed. 5]
    year: str | None = None  # e.g. "1989" from [১৯৮৯]
    is_not_a_book: bool = False  # link lists, fragments
    confidence: float = 0.0  # 0.0-1.0
    directory_author: str | None = None  # parent dir, UNVERIFIED -- see below


# Directory names that are demonstrably not people.  Measured counts from the
# live collection: `epub-staging` labelled 1,626 documents, `মাসুদ রানা সিরিজ`
# 309, `ওয়েস্টার্ন বই - সেবা প্রকাশনী` 120, `বিবিধ` 103.  Catalogue membership
# cannot be the test on its own, because genuinely famous authors are missing
# from the catalogue -- `মুহম্মদ জাফর ইকবাল` labels 234 documents and is a real
# person -- so a structural test runs first and catalogue lookup only promotes
# confidence afterwards.
_NON_PERSON_DIRECTORIES = frozenset({
    "epub-staging", "others", "বিবিধ", "উপন্যাস", "সমকালীন উপন্যাস", "গল্প",
    "কবিতা", "প্রবন্ধ", "ইতিহাস", "বাংলাদেশের ইতিহাস", "হিন্দু ধর্ম", "ইসলাম",
    "মহাভারত", "রামায়ণ", "ম্যাগাজিন", "পত্রিকা", "অনুবাদ", "সংকলন",
    "books", "data", "pdf", "misc", "unsorted", "new", "temp", "download",
    # Format-named staging folders.  `ই-পাব` is Bengali for "e-pub" and was
    # written as the author of every EPUB in it.
    "ই-পাব", "ইপাব", "epub", "epubs", "e-pub", "ebook", "ebooks", "ই-বুক",
})
# Tokens that mark a collection, imprint or format rather than a person.
_NON_PERSON_MARKERS = (
    "সিরিজ", "প্রকাশনী", "রচনাবলী", "সমগ্র", "সংকলন", "কমিকস", "পত্রিকা",
    "series", "comics", "publications", "publishers", "collection", "magazine",
    "staging", "archive", "vol.", "volume",
)


def is_probably_person_name(value: str) -> bool:
    """Reject directory names that name a series, genre, publisher or folder.

    Deliberately structural rather than catalogue-based.  Testing membership in
    `catalogue_people` alone would discard real authors the catalogue happens to
    lack, which is common here: the catalogue is a current-market retailer while
    the collection is largely classic and out-of-print.
    """
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) < 2:
        return False
    lowered = cleaned.casefold()
    if lowered in _NON_PERSON_DIRECTORIES:
        return False
    if any(marker in lowered for marker in _NON_PERSON_MARKERS):
        return False
    # A directory holding a separator is a description, not a name --
    # `ওয়েস্টার্ন বই - সেবা প্রকাশনী` is a genre plus its publisher.
    if " - " in cleaned or "_" in cleaned:
        return False
    # Folder-ish names carry no spaces and no Bengali; a real name has one or
    # the other.
    return " " in cleaned or any("\u0980" <= ch <= "\u09ff" for ch in cleaned)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BN_DIGITS = "০১২৩৪৫৬৭৮৯"
_BN_TO_ASCII = str.maketrans(_BN_DIGITS, "0123456789")
_DIGIT = "[০-৯0-9]"  # explicit class: \d would also match here, but be obvious


def _ascii_digits(value: str) -> str:
    """Convert Bengali digits to ASCII (measured: corpus uses both)."""
    return value.translate(_BN_TO_ASCII)


_WS_RE = re.compile(r"\s+")
_STRIP_ENDS = " \t\u00a0-_–—:;,."

# Trailing scraper uniqueness suffix, e.g. "Title - a1b2c3d4e5f6".
_HEX_SUFFIX_RE = re.compile(r"\s+-\s+[0-9a-fA-F]{12}\s*$")
# Amarbooks field marker (also the explicit-marker confidence signal).
_WAITING_RE = re.compile(re.escape("is waiting to be download!!!"), re.IGNORECASE)
# Literal scraper placeholder: NOT a real author value, never emit it.
_UNKNOWN_AUTHOR_RE = re.compile(r"^\s*unknown author\s*-\s*", re.IGNORECASE)

# Download boilerplate stripped before parsing (case-insensitive). Longest
# first so "Bengali Book PDF Download" is removed whole instead of leaving
# "Bengali Book" behind via the shorter "pdf download" pattern.
_BOILERPLATE_PHRASES = (
    "বাংলা বই পিডিএফ ডাউনলোড",
    "Bengali Book PDF Download",
    "Bangla Book Pdf",
    "Bangla PDF",
    "pdf download",
)
_BOILERPLATE_RES = tuple(
    re.compile(re.escape(p), re.IGNORECASE) for p in _BOILERPLATE_PHRASES
)

# " by " / " By " anchor used by bengaliebook and allbanglaboi.
_BY_RE = re.compile(r"\s+by\s+", re.IGNORECASE)


def _tidy(text: str) -> str:
    """Collapse whitespace; keep end punctuation (parens matter to parsers)."""
    return _WS_RE.sub(" ", text).strip()


def _clean_part(text: str) -> str:
    """Final cleanup for one title/author candidate."""
    return _tidy(text).strip(_STRIP_ENDS)


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        value = _clean_part(value)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def _repair_mojibake(value: str) -> str:
    """Repair UTF-8 bytes mis-decoded as Latin-1 (measured: banglabook source).

    Leaves the value untouched when the round-trip raises, so already-correct
    text (e.g. real Bengali, which is not Latin-1 encodable) is never damaged.
    """
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _source_key(source_path: str) -> str:
    """`data/incoming-scraped/<key>/books/<file>` gives `<key>`, else authordir."""
    parts = re.split(r"[\\/]", source_path)
    if "incoming-scraped" in parts:
        idx = parts.index("incoming-scraped")
        if idx + 1 < len(parts) and parts[idx + 1]:
            return parts[idx + 1]
    return "authordir"


def _strip_boilerplate(stem: str) -> str:
    """Apply Task 1 removals shared by all sources."""
    text = _HEX_SUFFIX_RE.sub("", stem)
    text = _WAITING_RE.sub(" ", text)
    text = _UNKNOWN_AUTHOR_RE.sub("", text)
    for pattern in _BOILERPLATE_RES:
        text = pattern.sub(" ", text)
    return _tidy(text).strip(_STRIP_ENDS)


# ---------------------------------------------------------------------------
# Task 6 -- non-book detection
# ---------------------------------------------------------------------------

_FRAGMENT_EXACT = frozenset(
    {"preliminary page", "title page", "appendix", "index", "contents", "cover"}
)
# e.g. "Chapter 12", "chapter_3", "CHAPTER-IV" fragments.
_CHAPTER_RE = re.compile(r"^chapter[\s_\d\-p০-৯]*$", re.IGNORECASE)
# Link-list aggregates, e.g. "৫০টি বইয়ের ডাউনলোড লিঙ্ক" (measured in corpus).
_LINK_COUNT_RE = re.compile(r"[০-৯0-9]+\s*টি.*(?:লিঙ্ক|ডাউনলোড লিঙ্ক)")


def _is_not_a_book(stem: str) -> bool:
    lowered = stem.strip().lower()
    if not lowered:
        return False
    if lowered in _FRAGMENT_EXACT:
        return True
    if _CHAPTER_RE.match(lowered):
        return True
    if _LINK_COUNT_RE.search(stem):
        return True
    if "১০০+" in stem or "100+" in stem:  # measured aggregate-list marker
        return True
    if "বইয়ের ডাউনলোড লিঙ্ক" in lowered:
        return True
    return "বইয়ের pdf ডাউনলোড লিঙ্ক" in lowered


# ---------------------------------------------------------------------------
# Task 4 -- volume / edition / year
# ---------------------------------------------------------------------------

_EDITION_BN_RE = re.compile(r"\[\s*সংস্করণ\s*[-–—: ]*\s*(" + _DIGIT + r"+)\s*\]")
_EDITION_EN_RE = re.compile(r"\[\s*Eds?\.?\s*(" + _DIGIT + r"+)\s*\]", re.IGNORECASE)
_VOLUME_BN_RE = re.compile(r"\[\s*ভাগ\s*[-–—: ]*\s*(" + _DIGIT + r"+)\s*\]")
_VOLUME_PT_RE = re.compile(r"\[\s*Pts?\.?\s*(" + _DIGIT + r"+)\s*\]", re.IGNORECASE)
_YEAR_RE = re.compile(r"\[\s*(" + _DIGIT + r"{4})\s*\]\s*$")
_PORBO_RE = re.compile(r"পর্ব\s*(" + _DIGIT + r"+)")
_PAREN_TRAIL_RE = re.compile(r"\(\s*(" + _DIGIT + r"+)\s*\)\s*$")
_PAREN_LEAD_RE = re.compile(r"^\s*\(\s*(" + _DIGIT + r"+)\s*\)\s*")
# Leading "৯। " / "01. " style markers (measured: authordir filenames).
_LEAD_NUM_RE = re.compile(r"^\s*(" + _DIGIT + r"+)\s*[।.]\s*")


def _extract_volume_edition_year(
    text: str,
) -> tuple[str, str | None, str | None, str | None]:
    """Pull volume/edition/year markers out of the text; return the remainder."""
    volume: str | None = None
    edition: str | None = None
    year: str | None = None

    def take(pattern: re.Pattern[str]) -> str | None:
        nonlocal text
        match = pattern.search(text)
        if match is None:
            return None
        text = _tidy(text[: match.start()] + " " + text[match.end() :])
        return _ascii_digits(match.group(1))

    edition = take(_EDITION_BN_RE) or take(_EDITION_EN_RE)
    volume = take(_VOLUME_BN_RE) or take(_VOLUME_PT_RE)

    year_match = _YEAR_RE.search(text)
    if year_match is not None:
        candidate = _ascii_digits(year_match.group(1))
        # 4-digit bracket is only a year inside a plausible range; otherwise it
        # may be an opaque serial, so leave it alone (precision over recall).
        if candidate.isdigit() and 1800 <= int(candidate) <= 2030:
            year = candidate
            text = _tidy(text[: year_match.start()] + " " + text[year_match.end() :])

    if volume is None:
        volume = take(_PORBO_RE)  # e.g. "যৌনতার ইতিহাস পর্ব ০২"
    if volume is None:
        trailing = _PAREN_TRAIL_RE.search(text)
        # Digits-only parens are a volume; "(Anyadin by ...)" style parens used
        # by bengaliebook contain text, so they never match here.
        if trailing is not None:
            volume = _ascii_digits(trailing.group(1))
            text = _tidy(text[: trailing.start()] + " " + text[trailing.end() :])
    if volume is None:
        leading = _PAREN_LEAD_RE.match(text)
        if leading is not None:
            volume = _ascii_digits(leading.group(1))
            text = _tidy(text[leading.end() :])
    if volume is None:
        lead_num = _LEAD_NUM_RE.match(text)
        if lead_num is not None:
            volume = _ascii_digits(lead_num.group(1))
            text = _tidy(text[lead_num.end() :])

    return _tidy(text).strip(_STRIP_ENDS), volume, edition, year


# ---------------------------------------------------------------------------
# Task 3 -- Bengali role markers (any source)
# ---------------------------------------------------------------------------

# "রচনা/রচিত" (composed by), "সম্পাদনা/সম্পাদিত" (edited by),
# "অনুবাদ/অনূদিত" (translated by), "লিখেছেন" (written by). The dataclass has
# no role field, so editor/translator credits resolve to author candidates;
# the caller validates them against the catalogue.
_ROLE_MARKERS = (
    "লিখেছেন",
    "সম্পাদিত",
    "সম্পাদনা",
    "অনূদিত",
    "অনুবাদ",
    "রচিত",
    "রচনা",
)
_ROLE_BOUNDARY = set(" \t\u00a0-_–—:;,.()[]\"'“”‘’/")


def _split_role_marker(title: str) -> tuple[str, str] | None:
    """Split "… <marker> <author>"; None when no marker with word boundaries."""
    best: tuple[int, str, str] | None = None
    for marker in _ROLE_MARKERS:
        start = 0
        while True:
            idx = title.find(marker, start)
            if idx == -1:
                break
            end = idx + len(marker)
            # Require boundaries so mid-word occurrences never split (precision).
            before_ok = (
                idx == 0 or title[idx - 1].isspace() or title[idx - 1] in _ROLE_BOUNDARY
            )
            after_ok = (
                end >= len(title)
                or title[end].isspace()
                or title[end] in _ROLE_BOUNDARY
            )
            before = _clean_part(title[:idx])
            author = _clean_part(title[end:])
            if (
                before
                and author
                and before_ok
                and after_ok
                and (best is None or idx >= best[0])
            ):
                best = (idx, before, author)
            start = idx + 1
    if best is None:
        return None
    return best[1], best[2]


# ---------------------------------------------------------------------------
# Task 2 -- per-source templates
# ---------------------------------------------------------------------------

# Sources whose placeholder filenames carry no title/author at all (measured:
# pure "Bangla eBooks pdf - <hash>" style names); these need OCR.
_PLACEHOLDER_SOURCES = frozenset({"banglabooks_in", "banglabookshelf"})


def _parse_granthagara(work: str) -> tuple[list[str], list[str], float]:
    """Bilingual title, NO author (5,349 measured files).

    After boilerplate stripping, `<bn> বাংলা বই পিডিএফ ডাউনলোড_ <en> Bengali
    Book PDF Download` collapses to `<bn>_ <en>`; the `_` is the separator.
    """
    if not work:
        return [], [], 0.0
    if "_" in work:
        titles = [_clean_part(part) for part in work.split("_")]
        titles = [title for title in titles if title]
        return titles, [], 0.8 if titles else 0.0
    return [work], [], 0.2


def _parse_bengaliebook(work: str) -> tuple[list[str], list[str], float]:
    """`<bn title>-<bn author> (<en title> by <en author>)` (1,944 files).

    The Bengali part splits on the LAST `-` before `(`; the space before `-`
    is inconsistent in the corpus, so plain rsplit (then strip) is used.
    """
    if not work:
        return [], [], 0.0
    match = re.search(r"\(([^()]*)\)\s*$", work)
    if match is None:
        return [work], [], 0.2
    bengali = _clean_part(work[: match.start()].strip().rstrip("-").strip())
    english = match.group(1).strip()
    en_parts = _BY_RE.split(english, maxsplit=1)
    en_title = _clean_part(en_parts[0])
    en_author = _clean_part(en_parts[1]) if len(en_parts) == 2 else ""
    bn_title, bn_author = bengali, ""
    if "-" in bengali:
        left, right = bengali.rsplit("-", 1)
        # A trailing "-" with nothing after it is punctuation, not a split.
        if _clean_part(right):
            bn_title, bn_author = _clean_part(left), _clean_part(right)
        else:
            bn_title = _clean_part(left)
    titles = [t for t in (bn_title, en_title) if t]
    authors = [a for a in (bn_author, en_author) if a]
    if not titles and not authors:
        return [], [], 0.0
    # " by " is an explicit author marker -> highest confidence.
    return titles, authors, 1.0 if len(en_parts) == 2 else 0.8


def _parse_amarbooks(
    work: str, had_waiting_marker: bool
) -> tuple[list[str], list[str], float]:
    """Boilerplate marks which field is which (1,960 files).

    `<Author> is waiting to be download!!! - <Title>` (author first), or
    `Unknown Author - <Title> is waiting...` (title only; the placeholder is
    already stripped by _strip_boilerplate).
    """
    if not work:
        return [], [], 0.0
    if not had_waiting_marker:
        return [work], [], 0.2
    if " - " in work:
        # Author-first split; only the FIRST " - " separates the fields.
        author, title = work.split(" - ", 1)
        author, title = _clean_part(author), _clean_part(title)
        return ([title] if title else [], [author] if author else [], 1.0)
    return [work], [], 1.0


def _parse_author_first(work: str) -> tuple[list[str], list[str], float]:
    """`<Author> - <Title>`, AUTHOR FIRST.

    Shared by rarebooksociety (1,041 files, English) and amarboi (858 files,
    Bengali). Titles here often contain ` - ` and ` _ ` themselves, so split
    on the FIRST ` - ` only.
    """
    if not work:
        return [], [], 0.0
    if " - " in work:
        author, title = work.split(" - ", 1)
        author, title = _clean_part(author), _clean_part(title)
        titles = [title] if title else []
        authors = [author] if author else []
        if not titles and not authors:
            return [], [], 0.0
        return titles, authors, 0.8
    return [work], [], 0.2


def _parse_allbanglaboi(work: str) -> tuple[list[str], list[str], float]:
    """Multi-segment, order varies (1,018 files).

    ` by `/` By ` is the anchor when present; otherwise over-emit ALL ` - `
    segments as title candidates with no author (over-emitting titles is
    fine; the catalogue match disambiguates).
    """
    if not work:
        return [], [], 0.0
    match = _BY_RE.search(work)
    if match is not None:
        before = _clean_part(work[: match.start()])
        segments = [_clean_part(s) for s in work[match.end() :].split(" - ")]
        segments = [s for s in segments if s]
        if not segments:
            return ([before] if before else []), [], 0.2
        authors = [segments[0]]
        titles = ([before] if before else []) + segments[1:]
        if not titles and not authors:
            return [], [], 0.0
        return titles, authors, 1.0
    titles = [_clean_part(s) for s in work.split(" - ")]
    titles = [t for t in titles if t]
    return titles, [], 0.2 if titles else 0.0


def _parse_authordir(work: str) -> tuple[list[str], list[str], float]:
    """`<Title> - <Author>`, TITLE FIRST (opposite of amarboi!).

    The containing directory is appended as an author candidate LAST by the
    caller, since many dirs are series/genres, not people.
    """
    if not work:
        return [], [], 0.0
    if " - " in work:
        # Title-first: the author is the LAST segment.
        title, author = work.rsplit(" - ", 1)
        title, author = _clean_part(title), _clean_part(author)
        titles = [title] if title else []
        authors = [author] if author else []
        if not titles and not authors:
            return [], [], 0.0
        return titles, authors, 0.5
    return [work], [], 0.2


def _parse_title_only(work: str) -> tuple[list[str], list[str], float]:
    """Mixed sources with no usable structure: books (1,522), pdfporo (15)."""
    if not work:
        return [], [], 0.0
    return [work], [], 0.2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_filename(source_path: str) -> ParsedFilename:
    """Parse a document's `source_path` into title/author candidates."""
    source = _source_key(source_path)
    base = re.split(r"[\\/]", source_path)[-1]
    stem, _ext = os.path.splitext(base)
    stem = stem.strip()

    if source == "banglabook":
        # MOJIBAKE source (309 files): UTF-8 bytes decoded as Latin-1.
        stem = _repair_mojibake(stem)

    had_waiting_marker = _WAITING_RE.search(stem) is not None
    work = _strip_boilerplate(stem)

    if _is_not_a_book(work):
        return ParsedFilename(
            titles=(),
            authors=(),
            source=source,
            volume=None,
            edition=None,
            year=None,
            is_not_a_book=True,
            confidence=1.0,
        )

    work, volume, edition, year = _extract_volume_edition_year(work)

    if source in _PLACEHOLDER_SOURCES:
        # Pure placeholders ("Bangla eBooks pdf - <hash>"): nothing usable,
        # these need OCR. Unconditional: never invent titles from hashes.
        return ParsedFilename(
            titles=(),
            authors=(),
            source=source,
            volume=None,
            edition=None,
            year=None,
            is_not_a_book=False,
            confidence=0.0,
        )

    if not work:
        return ParsedFilename(
            titles=(),
            authors=(),
            source=source,
            volume=volume,
            edition=edition,
            year=year,
            is_not_a_book=False,
            confidence=0.0,
        )

    if source == "granthagara":
        titles, authors, confidence = _parse_granthagara(work)
    elif source == "bengaliebook":
        titles, authors, confidence = _parse_bengaliebook(work)
    elif source == "amarbooks":
        titles, authors, confidence = _parse_amarbooks(work, had_waiting_marker)
    elif source in ("rarebooksociety", "amarboi"):
        titles, authors, confidence = _parse_author_first(work)
    elif source == "allbanglaboi":
        titles, authors, confidence = _parse_allbanglaboi(work)
    elif source == "banglabook":
        # Repaired above; shape is `Unknown Author - <Title>` (placeholder
        # already stripped), so what remains is a bare title.
        titles, authors, confidence = _parse_title_only(work)
    elif source in ("books", "pdfporo"):
        titles, authors, confidence = _parse_title_only(work)
    elif source == "authordir":
        titles, authors, confidence = _parse_authordir(work)
    else:
        # Unknown incoming-scraped key: no verified template, bare title only.
        titles, authors, confidence = _parse_title_only(work)

    # Task 3 post-pass (any source): a Bengali role marker moves trailing
    # text from the title into the authors, like ` by ` does in English.
    marker_authors: list[str] = []
    fixed_titles: list[str] = []
    for title in titles:
        split = _split_role_marker(title)
        if split is not None:
            before, author = split
            fixed_titles.append(before)
            marker_authors.append(author)
        else:
            fixed_titles.append(title)
    if marker_authors:
        titles = fixed_titles
        authors = [*marker_authors, *authors]
        confidence = 1.0

    directory_author: str | None = None
    if source == "authordir":
        # Containing directory LAST: often a series/genre, not a person
        # (measured examples: `মাসুদ রানা সিরিজ`, `বিবিধ`, `উপন্যাস`,
        # `epub-staging`); the caller validates it against the catalogue.
        parent = os.path.basename(os.path.dirname(source_path.replace("\\", "/")))
        parent = _clean_part(parent)
        if parent and is_probably_person_name(parent):
            directory_author = parent
            if parent not in authors:
                authors = [*authors, parent]

    final_titles = _dedupe(list(titles))
    final_authors = _dedupe(list(authors))
    if not final_titles and not final_authors:
        confidence = 0.0

    return ParsedFilename(
        titles=final_titles,
        authors=final_authors,
        source=source,
        volume=volume,
        edition=edition,
        year=year,
        is_not_a_book=False,
        confidence=confidence,
        directory_author=directory_author,
    )


# Bengali sentence terminators used as a title/author separator in this corpus:
# `অনুর পাঠশালা ।। মাহমুদুল হক`.
_DANDA_SPLIT = re.compile(r"\s*(?:।।|॥|।\s*।)\s*")


def parse_embedded_title(value: str) -> ParsedFilename:
    """Split a title string that is really a filename in disguise.

    1,630 EPUBs in this collection carry a numeric filename (`281.epub`) and an
    OPF ``dc:title`` holding the *original PDF filename* -- scraper boilerplate
    and all, including a literal ``Unknown Author -`` prefix on 332 of them and
    ``is waiting to be download!!!`` on 138.  They were produced by converting
    the scraped PDFs, so the title carries the author that ``dc:creator`` is
    missing.  Measured: 70% split on ` - `, plus ` by `, the Bengali danda and
    trailing parentheses, for 72% of the authorless EPUBs.

    Title-first ordering, matching the observed data
    (`শ্রেষ্ঠ কবিতা - শফিকুল ইসলাম`).  The trailing segment becomes an author only
    if it looks like a person, so `বাংলা গল্প-বিচিত্রা` does not donate its second
    half to the author field.
    """
    # These titles are filenames, so they keep the extension: real values seen
    # include `শেক্সপীয়র রচনাবলী ।। পৃথ্বীরাজ সেন.pdf` and
    # `ca$hvertising ( PDFDrive.com ).epub`.  Stripping it first also unblocks
    # the separator split, which the trailing `.pdf` was defeating.
    raw = re.sub(r"\.(pdf|epub|mobi|azw3?|djvu|txt)\s*$", "", value or "", flags=re.IGNORECASE)
    # Download sites stamp their domain into the name, usually parenthesised:
    # `ca$hvertising ( PDFDrive.com )`.  A parenthetical that is just a domain
    # carries no bibliographic content.
    raw = re.sub(r"[\(\[]\s*[\w-]+\.(com|net|org|info|io|co)\s*[\)\]]", " ", raw, flags=re.IGNORECASE)
    cleaned = _clean_part(_strip_boilerplate(raw))
    if not cleaned:
        return ParsedFilename((), (), "embedded_title", confidence=0.0)

    cleaned, volume, edition, year = _extract_volume_edition_year(cleaned)
    title, author = cleaned, ""

    # An explicit ` by ` marker is unambiguous, so it is tried first.
    by_match = re.split(r"\s+[Bb][Yy]\s+", cleaned, maxsplit=1)
    if len(by_match) == 2 and by_match[0].strip() and by_match[1].strip():
        title, author = by_match[0], by_match[1]
    elif _DANDA_SPLIT.search(cleaned):
        parts = [part for part in _DANDA_SPLIT.split(cleaned) if part.strip()]
        if len(parts) >= 2:
            title, author = " ".join(parts[:-1]), parts[-1]
    elif re.search(r"\s[-–—]\s", cleaned):
        segments = re.split(r"\s[-–—]\s", cleaned)
        if len(segments) >= 2:
            title, author = " - ".join(segments[:-1]), segments[-1]
    else:
        trailing = re.search(r"^(.*?)\s*\(([^()]{4,})\)\s*$", cleaned)
        if trailing:
            title, author = trailing.group(1), trailing.group(2)

    title = _clean_part(title)
    author = _clean_part(author)
    # A separator does not prove the tail is a person; many titles simply
    # contain a dash.  Reuse the structural test rather than trusting position.
    if author and not is_probably_person_name(author):
        title, author = cleaned, ""

    return ParsedFilename(
        titles=(title,) if title else (),
        authors=(author,) if author else (),
        source="embedded_title",
        volume=volume,
        edition=edition,
        year=year,
        confidence=0.7 if author else 0.4,
    )
