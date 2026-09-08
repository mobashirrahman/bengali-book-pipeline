"""Bengali/Unicode text normalization for catalogue matching.

Why this module exists (measured on the live 194,060-work catalogue):
- 40,856 of 194,060 work titles (21%) are NOT NFC-normalized, so byte-wise
  exact matching silently misses identical-looking titles.
- 10,954 of 76,596 people names (14%) are NOT NFC-normalized.
- 1,158 titles and 538 names contain zero-width characters
  (U+200B/C/D, U+00AD), usually pasted in from web/library sources.

Headline failure: the person ``হুমায়ুন আজাদ`` stores য় as precomposed
U+09DF, while another source writes U+09AF U+09BC (য + nukta). The two render
IDENTICALLY but never compare equal, so a correct lookup returns nothing.
NFC composes both to U+09DF, fixing the lookup.

All functions here are pure (no DB, no network, no filesystem access) and
never return None: empty/whitespace-only input yields ``""``.

Stdlib only (``unicodedata``, ``re``).
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width / formatting characters stripped by normalize_text. These carry
# no visible content yet break exact matching (1,158 titles + 538 names in
# the live catalogue carry U+200B/C/D or U+00AD alone).
_ZERO_WIDTH_CHARS = frozenset(
    [
        "\u200b",  # ZERO WIDTH SPACE
        "\u200c",  # ZERO WIDTH NON-JOINER
        "\u200d",  # ZERO WIDTH JOINER
        "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
        "\u00ad",  # SOFT HYPHEN
        "\u202a",  # LEFT-TO-RIGHT EMBEDDING
        "\u202b",  # RIGHT-TO-LEFT EMBEDDING
        "\u202c",  # POP DIRECTIONAL FORMATTING
        "\u202d",  # LEFT-TO-RIGHT OVERRIDE
        "\u202e",  # RIGHT-TO-LEFT OVERRIDE
    ]
)

# Bengali digits U+09E6-U+09EF -> ASCII 0-9, so that e.g. ``রচনাবলী ০২``
# and ``রচনাবলী 02`` produce the same key.
_BENGALI_DIGITS = "০১২৩৪৫৬৭৮৯"
_DIGIT_TRANSLATION = str.maketrans(_BENGALI_DIGITS, "0123456789")

_BENGALI_BLOCK_START = "\u0980"
_BENGALI_BLOCK_END = "\u09ff"


def _is_key_char(char: str) -> bool:
    """Return True for characters kept verbatim by :func:`normalize_text`.

    ``str.isalnum()`` is False for Bengali vowel signs and nukta (they are
    combining marks), so the whole Bengali block U+0980-U+09FF is kept
    explicitly -- otherwise normalization would shred every Bengali word.
    """
    return char.isalnum() or (_BENGALI_BLOCK_START <= char <= _BENGALI_BLOCK_END)


def normalize_text(value: str) -> str:
    """Aggressive key for exact-match lookup and fuzzy scoring.

    Steps, in order: NFC -> strip zero-width/formatting chars -> convert
    Bengali digits to ASCII -> casefold -> replace each run of
    non-alphanumeric characters with a single space -> collapse whitespace.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFC", value)
    text = "".join(char for char in text if char not in _ZERO_WIDTH_CHARS)
    text = text.translate(_DIGIT_TRANSLATION)
    text = text.casefold()
    text = "".join(char if _is_key_char(char) else " " for char in text)
    return " ".join(text.split())


def repair_mojibake(value: str) -> str:
    """Repair UTF-8 bytes that were decoded as Latin-1.

    309 catalogue files carry such damage, e.g.
    ``à¦¸à§à¦² à¦®à¦¾à¦à¦¨à§à¦à§à¦¨`` for Bengali text. The repair is only
    attempted when the string actually looks like mojibake -- it contains
    characters in U+00C0-U+00FF AND no Bengali characters -- so legitimate
    accented Latin names such as ``Émile Zola`` are never mangled (their
    bytes are not valid     UTF-8, and the attempt safely returns the input).
    """
    if not value or not value.strip():
        # Whitespace-only input carries no content; the module-wide contract
        # is that empty input yields "" for every function here.
        return ""
    has_latin1_high = any("\u00c0" <= char <= "\u00ff" for char in value)
    has_bengali = any(
        _BENGALI_BLOCK_START <= char <= _BENGALI_BLOCK_END for char in value
    )
    if not (has_latin1_high and not has_bengali):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


# Trailing "(...)" role/disambiguation markers measured in the live people
# table, e.g. ``(Translator)``, ``(Editor)``, ``(২)``, ``(2)``.
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")

# Leading honorifics measured in the live people table, e.g.
# ``শ্রী স্বপনকুমার``, ``ড. ইসরাইল খান``,
# ``বিচারপতি মুহম্মদ হাবিবুর রহমান``, ``Professor Samiran Kumar Saha``.
# These are matched as whole leading TOKENS only: ``শ্রীকান্ত`` must survive
# intact, so substring stripping is forbidden.
_BENGALI_HONORIFICS = frozenset(
    ["শ্রী", "শ্রীমতী", "ড.", "ডঃ", "ডাঃ", "অধ্যাপক", "বিচারপতি"]
)
_ENGLISH_HONORIFICS = frozenset(["dr", "prof", "professor", "sri", "shri"])


def _strip_leading_honorifics(value: str) -> str:
    tokens = value.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _BENGALI_HONORIFICS:
            index += 1
            continue
        folded = token.rstrip(".").casefold()
        if folded in _ENGLISH_HONORIFICS and folded.isascii():
            index += 1
            continue
        break
    return " ".join(tokens[index:])


def normalize_name(value: str) -> str:
    """``normalize_text`` plus person-name handling.

    - Rewrites ``Last, First`` to ``First Last`` when the value has exactly
      one comma with non-empty text on both sides (real library-scan form:
      ``Chakraborty, Pranabesh``).
    - Strips trailing parenthetical role/disambiguation markers.
    - Strips leading honorifics as whole tokens only.
    """
    if not value:
        return ""
    text = value
    while True:
        stripped = _TRAILING_PARENTHETICAL.sub("", text)
        if stripped == text:
            break
        text = stripped
    if text.count(",") == 1:
        left, right = text.split(",", 1)
        if left.strip() and right.strip():
            text = right.strip() + " " + left.strip()
    text = _strip_leading_honorifics(text.strip())
    return normalize_text(text)


# Character pairs Bengali OCR routinely confuses, measured from real
# tesseract output on this corpus: the true title
# ``দেবতা অনুরাগী রবীন্দ্রনাথ`` was read as
# ``দেবতী অনমুরাণী ববীন্দ্রনাথ`` (া/ী, ু/ূ, গ/ণ, র/ব swaps).
# Each group folds to its first member. Note া and ী are deliberately NOT
# folded together (too distinct); া folds only with ৗ (AU LENGTH MARK).
# ব belongs to two visual groups, so ভ and র both fold to ব.
_FOLD_GROUPS = [
    ("ি", "ী"),  # i-kar lengths (also dropped as vowel signs below)
    ("ু", "ূ"),  # u-kar lengths (also dropped as vowel signs below)
    ("া", "ৗ"),  # aa length marks only -- never া/ী
    ("ব", "ভ"),  # ba/bha
    ("ব", "র"),  # ra/ba -- visually close in many print faces
    ("ন", "ণ"),  # na/nna
    ("স", "শ", "ষ"),  # the three sibilants
    ("জ", "য"),  # ja/ya
]

_FOLD_MAP: dict[str, str] = {}
for _group in _FOLD_GROUPS:
    for _member in _group[1:]:
        _FOLD_MAP.setdefault(_member, _group[0])

# Vowel-sign marks dropped entirely as a last-resort key. This range also
# covers the ি/ী and ু/ূ pairs above, making their folding redundant but
# harmless -- the explicit groups above document the measured confusions.
_VOWEL_SIGNS = frozenset(chr(code) for code in range(0x09BE, 0x09CC + 1))


def fold_bengali(value: str) -> str:
    """Extra folding for OCR-error tolerance (secondary index ONLY).

    LOSSY: produces false collisions by design (e.g. স/শ/ষ merge, all
    vowel signs vanish). Callers must use this only to generate candidates
    that are then re-scored with :func:`normalize_text`; never for display.
    """
    if not value:
        return ""
    key = normalize_text(value)
    key = "".join(_FOLD_MAP.get(char, char) for char in key)
    return "".join(char for char in key if char not in _VOWEL_SIGNS)
