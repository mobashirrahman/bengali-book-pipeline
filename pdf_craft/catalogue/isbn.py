"""Strict ISBN normalization used by the staged catalogue pipeline."""

from __future__ import annotations

import re


def normalize_isbn(value: str) -> str:
    """Return a canonical ISBN-13, or raise ``ValueError`` for invalid input."""
    if not isinstance(value, str):
        raise ValueError("ISBN must be a string")
    compact = re.sub(r"[-\s\u00ad]", "", value).upper()
    if len(compact) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", compact):
            raise ValueError("invalid ISBN-10 characters")
        total = sum((10 - i) * (10 if char == "X" else int(char)) for i, char in enumerate(compact))
        if total % 11:
            raise ValueError("invalid ISBN-10 checksum")
        body = "978" + compact[:9]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(char) for i, char in enumerate(body)) % 10) % 10
        return body + str(check)
    if len(compact) == 13:
        if not compact.isdigit() or not compact.startswith(("978", "979")):
            raise ValueError("invalid ISBN-13 characters")
        total = sum((1 if i % 2 == 0 else 3) * int(char) for i, char in enumerate(compact))
        if total % 10:
            raise ValueError("invalid ISBN-13 checksum")
        return compact
    raise ValueError("ISBN must contain 10 or 13 characters")


def normalize_isbn10(value: str) -> str:
    """Return canonical ISBN-10 (preserving a valid ``X`` check digit)."""
    compact = re.sub(r"[-\s\u00ad]", "", value).upper()
    if len(compact) != 10 or not re.fullmatch(r"\d{9}[\dX]", compact):
        raise ValueError("invalid ISBN-10")
    if sum((10 - i) * (10 if char == "X" else int(char)) for i, char in enumerate(compact)) % 11:
        raise ValueError("invalid ISBN-10 checksum")
    return compact


def is_valid_isbn(value: str) -> bool:
    """Return whether *value* is a valid ISBN-10 or ISBN-13."""
    try:
        normalize_isbn(value)
    except ValueError:
        return False
    return True
