"""Paired, family-level uncertainty for the Bengali-OCR research study (S3).

Confidence intervals resample **whole work/edition families**, never
per-character. Zero observed harms never license a safety claim: the harm
report always returns ``safety_claim_supported = False`` and reports event
counts.
"""

from __future__ import annotations

import random

from . import schema

RESAMPLE_UNIT = "work_edition_family"


def _resample_indices(n: int, rng: random.Random) -> list[int]:
    """Draw ``n`` family indices with replacement (a whole-family bootstrap)."""
    return [rng.randrange(n) for _ in range(n)]


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise schema.ContractError("cannot take a percentile of no samples")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def macro_cer(per_family_cer: dict[str, float]) -> float:
    if not per_family_cer:
        raise schema.ContractError("per_family_cer must not be empty")
    return sum(per_family_cer.values()) / len(per_family_cer)


def paired_bootstrap(
    family_scores_a: dict[str, float],
    family_scores_b: dict[str, float],
    *,
    iterations: int = 10000,
    seed: int = 20260909,
    ci: float = 0.95,
) -> dict:
    """Bootstrap the macro mean of ``b - a`` over families (b and a paired)."""
    families = sorted(family_scores_a)
    if families != sorted(family_scores_b):
        raise schema.ContractError(
            "paired_bootstrap requires the identical family set on both systems"
        )
    if not families:
        raise schema.ContractError("paired_bootstrap needs at least one family")
    if not 0 < ci < 1:
        raise schema.ContractError("ci must satisfy 0 < ci < 1")
    diffs = [family_scores_b[f] - family_scores_a[f] for f in families]
    point = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    n = len(families)
    samples: list[float] = []
    for _ in range(iterations):
        picks = _resample_indices(n, rng)
        samples.append(sum(diffs[i] for i in picks) / n)
    tail = (1 - ci) / 2
    return {
        "n_families": n,
        "point_estimate": point,
        "ci_low": _percentile(samples, tail),
        "ci_high": _percentile(samples, 1 - tail),
        "ci": ci,
        "iterations": iterations,
        "resample_unit": RESAMPLE_UNIT,
        "method": "paired_bootstrap",
    }


def harm_rate_report(
    family_harm_counts: dict[str, int],
    *,
    iterations: int = 10000,
    seed: int = 20260909,
    ci: float = 0.95,
) -> dict:
    """Report harmful-edit event counts and the family-level harm fraction.

    Never asserts safety. Bootstrap CIs cannot establish a very low harm risk
    from zero observed harms, so ``safety_claim_supported`` is always ``False``.
    """
    families = sorted(family_harm_counts)
    if not families:
        raise schema.ContractError("harm_rate_report needs at least one family")
    for value in family_harm_counts.values():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise schema.ContractError("harm counts must be non-negative ints")
    any_harm = [1.0 if family_harm_counts[f] > 0 else 0.0 for f in families]
    total_events = sum(family_harm_counts.values())
    families_with_harm = int(sum(any_harm))
    n = len(families)
    rng = random.Random(seed)
    fractions: list[float] = []
    for _ in range(iterations):
        picks = _resample_indices(n, rng)
        fractions.append(sum(any_harm[i] for i in picks) / n)
    tail = (1 - ci) / 2
    return {
        "n_families": n,
        "total_harm_events": total_events,
        "families_with_any_harm": families_with_harm,
        "fraction_families_with_harm": families_with_harm / n,
        "ci_low": _percentile(fractions, tail),
        "ci_high": _percentile(fractions, 1 - tail),
        "ci": ci,
        "zero_observed_harms": total_events == 0,
        "safety_claim_supported": False,
        "note": (
            "bootstrap CIs cannot establish a very low harm risk from zero "
            "observed harms; report event counts and family incidence honestly"
        ),
    }


def paired_effect_table(rows: list[dict]) -> dict:
    """Reconcile a per-family a/b table to its macro means and macro difference."""
    if not rows:
        raise schema.ContractError("paired_effect_table needs at least one row")
    per_family = {}
    for row in rows:
        family = row["family"]
        if family in per_family:
            raise schema.ContractError(f"duplicate family in table: {family!r}")
        per_family[family] = {
            "a": float(row["a"]),
            "b": float(row["b"]),
            "diff": float(row["b"]) - float(row["a"]),
        }
    macro_a = sum(v["a"] for v in per_family.values()) / len(per_family)
    macro_b = sum(v["b"] for v in per_family.values()) / len(per_family)
    return {
        "n_families": len(per_family),
        "macro_a": macro_a,
        "macro_b": macro_b,
        "macro_diff": macro_b - macro_a,
        "per_family": per_family,
    }
