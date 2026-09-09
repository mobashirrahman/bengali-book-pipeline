"""Transcription and correction metrics for the Bengali-OCR research study (S3).

Reuses low-level edit-distance machinery (`rapidfuzz`) but deliberately does NOT
call `pdf_craft_tool.benchmark.score`: that scorer folds curly quotes and is not
a strict view. Here every text view is explicit and versioned.

Text policies
-------------
``raw``        exact codepoints, nothing folded or collapsed.
``nfc_strict`` Unicode NFC only. No quote folding, no whitespace collapsing.
``nfc_search`` NFC + fold ’‘→' and “”→" + collapse whitespace runs + strip.
               A documented lossy view; never the primary endpoint.

A reference is always required. An absent or empty reference raises
``schema.ContractError`` — it is never silently scored as perfect.
"""

from __future__ import annotations

import unicodedata

from rapidfuzz.distance import Levenshtein

from . import schema

METRIC_VERSION = "s3-1"
TOKEN_VERSION = "lmn-1"
UNICODE_VERSION = unicodedata.unidata_version

try:  # grapheme clustering needs the third-party `regex` module (\X)
    import regex as _regex

    REGEX_VERSION = _regex.__version__
    GRAPHEME_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where regex is absent
    _regex = None
    REGEX_VERSION = ""
    GRAPHEME_AVAILABLE = False

_POLICIES = ("raw", "nfc_strict", "nfc_search")


def apply_policy(text: str, policy: str) -> str:
    if not isinstance(text, str):
        raise schema.ContractError("text must be a string")
    if policy == "raw":
        return text
    if policy == "nfc_strict":
        return unicodedata.normalize("NFC", text)
    if policy == "nfc_search":
        folded = (
            unicodedata.normalize("NFC", text)
            .replace("’", "'")
            .replace("‘", "'")
            .replace("“", '"')
            .replace("”", '"')
        )
        return " ".join(folded.split())
    raise schema.ContractError(
        f"unknown text policy {policy!r}; expected one of {list(_POLICIES)}"
    )


def word_tokens(text: str) -> list[str]:
    """Contiguous runs of letter/mark/number characters (versioned rule)."""
    tokens: list[str] = []
    current: list[str] = []
    for char in text:
        if unicodedata.category(char)[0] in {"L", "M", "N"}:
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def grapheme_clusters(text: str) -> list[str]:
    if not GRAPHEME_AVAILABLE:
        raise RuntimeError("grapheme clustering requires the `regex` module")
    return _regex.findall(r"\X", text)


def _require_reference(reference: str, applied: str) -> None:
    if not isinstance(reference, str) or not reference or not applied:
        raise schema.ContractError(
            "reference text is required for scoring; an absent reference is "
            "never scored as a perfect match"
        )


def character_error_rate(
    reference: str, hypothesis: str, *, policy: str = "nfc_strict"
) -> dict:
    ref = apply_policy(reference, policy)
    hyp = apply_policy(hypothesis, policy)
    _require_reference(reference, ref)
    editops = Levenshtein.editops(ref, hyp)
    ops = {
        "insert": sum(op.tag == "insert" for op in editops),
        "delete": sum(op.tag == "delete" for op in editops),
        "replace": sum(op.tag == "replace" for op in editops),
    }
    edits = len(editops)
    return {
        "policy": policy,
        "metric_version": METRIC_VERSION,
        "unicode_version": UNICODE_VERSION,
        "token_version": TOKEN_VERSION,
        "reference_chars": len(ref),
        "hypothesis_chars": len(hyp),
        "edits": edits,
        "operations": ops,
        "cer": edits / len(ref),
    }


def word_error_rate(
    reference: str, hypothesis: str, *, policy: str = "nfc_strict"
) -> dict:
    ref = apply_policy(reference, policy)
    hyp = apply_policy(hypothesis, policy)
    _require_reference(reference, ref)
    ref_tokens = word_tokens(ref)
    hyp_tokens = word_tokens(hyp)
    if not ref_tokens:
        raise schema.ContractError("reference has no word tokens under this policy")
    distance = Levenshtein.distance(ref_tokens, hyp_tokens)
    return {
        "policy": policy,
        "token_version": TOKEN_VERSION,
        "metric_version": METRIC_VERSION,
        "reference_words": len(ref_tokens),
        "hypothesis_words": len(hyp_tokens),
        "word_edits": distance,
        "wer": distance / len(ref_tokens),
    }


def grapheme_error_rate(
    reference: str, hypothesis: str, *, policy: str = "nfc_strict"
) -> dict:
    if not GRAPHEME_AVAILABLE:
        return {
            "available": False,
            "reason": "the `regex` module is unavailable; the grapheme metric "
            "is pending an isolated dependency decision",
        }
    ref = apply_policy(reference, policy)
    hyp = apply_policy(hypothesis, policy)
    _require_reference(reference, ref)
    ref_clusters = grapheme_clusters(ref)
    hyp_clusters = grapheme_clusters(hyp)
    edits = len(Levenshtein.editops(ref_clusters, hyp_clusters))
    return {
        "available": True,
        "policy": policy,
        "unicode_version": UNICODE_VERSION,
        "regex_version": REGEX_VERSION,
        "reference_graphemes": len(ref_clusters),
        "hypothesis_graphemes": len(hyp_clusters),
        "edits": edits,
        "ger": edits / len(ref_clusters) if ref_clusters else 0.0,
    }


def score_prediction(pred, gold_text: str, *, policy: str = "nfc_strict") -> dict:
    """Score one prediction against its gold page text.

    ``pred`` may be a ``schema.Prediction`` or a plain dict. A falsy
    ``gold_text`` raises rather than scoring a perfect match.
    """
    if isinstance(pred, schema.Prediction):
        parsed = pred.parsed_text
        failure_state = pred.failure_state
    elif isinstance(pred, dict):
        schema.assert_no_gold_fields(pred, context="score_prediction")
        parsed = pred.get("parsed_text", "")
        failure_state = pred.get("failure_state", schema.FailureState.OK.value)
    else:
        raise schema.ContractError("pred must be a schema.Prediction or a dict")
    if not isinstance(gold_text, str) or not gold_text.strip():
        raise schema.ContractError("gold_text is required to score a prediction")
    result = {
        "policy": policy,
        "failure_state": failure_state,
        "character": character_error_rate(gold_text, parsed, policy=policy),
        "word": word_error_rate(gold_text, parsed, policy=policy),
        "grapheme": grapheme_error_rate(gold_text, parsed, policy=policy),
    }
    return result


# ---------------------------------------------------------------------------
# Omission / reading order.
# ---------------------------------------------------------------------------

def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - Levenshtein.distance(a, b) / max(len(a), len(b))


def omission_report(
    census_lines: list[str],
    predicted_lines: list[str],
    *,
    policy: str = "nfc_strict",
    match_threshold: float = 0.30,
) -> dict:
    """Greedy-align census lines to predicted lines; count the gaps.

    A census line the prediction never produced is ``omitted``; a predicted line
    that matches no census line is ``spurious``. The census is the independent
    full-page enumeration, so an OCR that dropped a line yields ``omitted >= 1``.
    """
    census = [apply_policy(line, policy) for line in census_lines]
    predicted = [apply_policy(line, policy) for line in predicted_lines]
    used: set[int] = set()
    matched = 0
    omitted_index: list[int] = []
    for c_index, c_line in enumerate(census):
        best_ratio = 0.0
        best_j = -1
        for j, p_line in enumerate(predicted):
            if j in used:
                continue
            score = _ratio(c_line, p_line)
            if score > best_ratio:
                best_ratio = score
                best_j = j
        if best_j >= 0 and best_ratio >= 1.0 - match_threshold:
            used.add(best_j)
            matched += 1
        else:
            omitted_index.append(c_index)
    return {
        "census_text_regions": len(census),
        "matched": matched,
        "omitted_lines": len(omitted_index),
        "spurious_lines": len(predicted) - len(used),
        "omitted_index": omitted_index,
        "denominator": len(census),
        "policy": policy,
    }


def reading_order_errors(
    gold_order: list[int], predicted_order: list[int]
) -> dict:
    """Kendall-tau distance on the common items plus missing/extra counts."""
    gold = list(gold_order)
    predicted = list(predicted_order)
    common = [item for item in gold if item in set(predicted)]
    position = {item: index for index, item in enumerate(predicted)}
    inversions = 0
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            if position[common[i]] > position[common[j]]:
                inversions += 1
    return {
        "inversions": inversions,
        "missing_from_prediction": len([i for i in gold if i not in set(predicted)]),
        "extra_in_prediction": len([i for i in predicted if i not in set(gold)]),
        "common_items": len(common),
    }


# ---------------------------------------------------------------------------
# Edit accounting (before / after).
# ---------------------------------------------------------------------------

def classify_edit(
    reference: str, before_text: str, after_text: str, *, policy: str = "nfc_strict"
) -> str:
    ref = apply_policy(reference, policy)
    before = apply_policy(before_text, policy)
    after = apply_policy(after_text, policy)
    d0 = Levenshtein.distance(before, ref)
    d1 = Levenshtein.distance(after, ref)
    if d1 < d0:
        return "beneficial"
    if d1 > d0:
        return "harmful"
    return "neutral"


def edit_accounting(units: list[dict], *, policy: str = "nfc_strict") -> dict:
    """Classify accepted edits and report the safety-relevant fractions.

    ``units``: ``[{"reference", "before", "after", "accepted": bool}]``.
    """
    beneficial = neutral = harmful = accepted = exact = damage = 0
    for unit in units:
        reference = unit["reference"]
        before = unit["before"]
        after = unit["after"]
        is_accepted = bool(unit.get("accepted", False))
        if is_accepted:
            accepted += 1
            kind = classify_edit(reference, before, after, policy=policy)
            if kind == "beneficial":
                beneficial += 1
            elif kind == "harmful":
                harmful += 1
            else:
                neutral += 1
            if apply_policy(after, policy) == apply_policy(reference, policy):
                exact += 1
            if apply_policy(before, policy) == apply_policy(reference, policy) and (
                apply_policy(after, policy) != apply_policy(reference, policy)
            ):
                damage += 1
    total = len(units)
    return {
        "policy": policy,
        "units": total,
        "accepted": accepted,
        "beneficial": beneficial,
        "neutral": neutral,
        "harmful": harmful,
        "beneficial_edit_fraction": (beneficial / accepted) if accepted else None,
        "exact_correction_precision": (exact / accepted) if accepted else None,
        "damage_to_initially_correct": damage,
        "coverage": (accepted / total) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Sensitive-span preservation.
# ---------------------------------------------------------------------------

_SENSITIVE_KINDS = ("name", "numeral", "historical_spelling")


def sensitive_span_preservation(
    spans: list[dict], hypothesis: str, *, policy: str = "nfc_strict"
) -> dict:
    hyp = apply_policy(hypothesis, policy)
    per_kind: dict[str, dict] = {}
    checked = preserved = 0
    for span in spans:
        kind = span.get("kind", "other")
        text = apply_policy(span["text"], policy)
        bucket = per_kind.setdefault(kind, {"checked": 0, "preserved": 0})
        bucket["checked"] += 1
        checked += 1
        if text and text in hyp:
            bucket["preserved"] += 1
            preserved += 1
    for bucket in per_kind.values():
        bucket["rate"] = (
            bucket["preserved"] / bucket["checked"] if bucket["checked"] else None
        )
    return {
        "policy": policy,
        "checked": checked,
        "preserved": preserved,
        "rate": (preserved / checked) if checked else None,
        "per_kind": per_kind,
    }
