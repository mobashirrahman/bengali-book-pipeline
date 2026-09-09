"""Study-level baseline tables and reproducible reports (S6).

Every configured arm appears in :func:`baseline_table` even when it failed.
An arm with zero coverage renders its exact-correction precision as
``"undefined"`` — never ``1.0`` / ``100%``. :func:`reconcile` guards every
table's totals against the expected record count, and
:func:`rebuild_from_records` rebuilds all numeric tables offline from saved
prediction/decision JSONL, embedding the metric version and config hash in
the report header so a later bug fix requires a versioned correction and a
transparent rerun — never silent test-driven tuning.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import metrics, schema, statistics

ARM_STATUSES = ("ok", "failed", "unsupported")
PRIMARY_METRIC = "macro_per_family_cer_strict_nfc"


@dataclass(frozen=True)
class ArmResult:
    arm_id: str
    status: str  # "ok" | "failed" | "unsupported"
    per_family_cer: dict  # family -> strict-NFC macro CER
    coverage: float | None
    accepted: int
    beneficial: int
    neutral: int
    harmful: int
    exact_correction_precision: float | None
    notes: str = ""
    # Per-page failure breakdown. When these are None the arm status is used
    # as an all-or-nothing fallback; supply them so a partly-failed arm is not
    # reported as fully ok.
    pages_attempted: int | None = None
    pages_ok: int | None = None
    pages_failed: int | None = None
    pages_unsupported: int | None = None

    def __post_init__(self):
        if not isinstance(self.arm_id, str) or not self.arm_id:
            raise schema.ContractError("arm_id must be a non-empty string")
        if self.status not in ARM_STATUSES:
            raise schema.ContractError(
                f"unknown arm status {self.status!r}; "
                f"expected one of {list(ARM_STATUSES)}"
            )
        if not isinstance(self.per_family_cer, dict):
            raise schema.ContractError("per_family_cer must be a dict")
        for family, value in self.per_family_cer.items():
            if not isinstance(family, str) or not family:
                raise schema.ContractError("per_family_cer keys must be strings")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise schema.ContractError(
                    f"per_family_cer[{family!r}] must be numeric"
                )
        object.__setattr__(
            self, "per_family_cer", dict(sorted(self.per_family_cer.items()))
        )
        if self.coverage is not None:
            if not isinstance(self.coverage, (int, float)) or isinstance(
                self.coverage, bool
            ):
                raise schema.ContractError("coverage must be numeric or None")
            if not 0 <= self.coverage <= 1:
                raise schema.ContractError("coverage must satisfy 0 <= c <= 1")
        for name in ("accepted", "beneficial", "neutral", "harmful"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise schema.ContractError(f"{name} must be a non-negative int")
        if self.beneficial + self.neutral + self.harmful > self.accepted:
            raise schema.ContractError(
                "beneficial + neutral + harmful must not exceed accepted"
            )
        if self.exact_correction_precision is not None:
            prec = self.exact_correction_precision
            if not isinstance(prec, (int, float)) or isinstance(prec, bool):
                raise schema.ContractError(
                    "exact_correction_precision must be numeric or None"
                )
            if not 0 <= prec <= 1:
                raise schema.ContractError(
                    "exact_correction_precision must satisfy 0 <= p <= 1"
                )
        if not isinstance(self.notes, str):
            raise schema.ContractError("notes must be a string")
        page_fields = (self.pages_attempted, self.pages_ok,
                       self.pages_failed, self.pages_unsupported)
        if any(v is not None for v in page_fields):
            if any(v is None for v in page_fields):
                raise schema.ContractError(
                    "per-page fields must all be set or all be None"
                )
            for name in ("pages_attempted", "pages_ok", "pages_failed",
                         "pages_unsupported"):
                value = getattr(self, name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise schema.ContractError(
                        f"{name} must be a non-negative int"
                    )
            if (self.pages_ok + self.pages_failed + self.pages_unsupported
                    != self.pages_attempted):
                raise schema.ContractError(
                    "pages_ok + pages_failed + pages_unsupported must equal "
                    "pages_attempted"
                )

    def to_dict(self) -> dict:
        return {
            "arm_id": self.arm_id,
            "status": self.status,
            "per_family_cer": dict(self.per_family_cer),
            "coverage": self.coverage,
            "accepted": self.accepted,
            "beneficial": self.beneficial,
            "neutral": self.neutral,
            "harmful": self.harmful,
            "exact_correction_precision": self.exact_correction_precision,
            "notes": self.notes,
            "pages_attempted": self.pages_attempted,
            "pages_ok": self.pages_ok,
            "pages_failed": self.pages_failed,
            "pages_unsupported": self.pages_unsupported,
        }


def _macro_cer(arm: ArmResult):
    if not arm.per_family_cer:
        return None
    return sum(arm.per_family_cer.values()) / len(arm.per_family_cer)


def _precision_cell(arm: ArmResult):
    """None when coverage is 0/None — renders as 'undefined', never 100%."""
    if arm.coverage is None or arm.coverage == 0:
        return None
    if arm.accepted == 0:
        return None
    return arm.exact_correction_precision


def baseline_table(arms: list[ArmResult]) -> dict:
    """One row per configured arm, including failed/unsupported arms."""
    rows = []
    for arm in arms:
        if not isinstance(arm, ArmResult):
            raise schema.ContractError("arms entries must be ArmResult")
        rows.append({
            "arm_id": arm.arm_id,
            "status": arm.status,
            "macro_cer": _macro_cer(arm),
            "coverage": arm.coverage,
            "exact_correction_precision": _precision_cell(arm),
            "accepted": arm.accepted,
            "beneficial": arm.beneficial,
            "neutral": arm.neutral,
            "harmful": arm.harmful,
            "notes": arm.notes,
        })
    rows.sort(key=lambda row: row["arm_id"])
    totals = {
        "arms": len(rows),
        "accepted": sum(row["accepted"] for row in rows),
        "beneficial": sum(row["beneficial"] for row in rows),
        "neutral": sum(row["neutral"] for row in rows),
        "harmful": sum(row["harmful"] for row in rows),
    }
    return {
        "metric": PRIMARY_METRIC,
        "rows": rows,
        "totals": totals,
    }


def primary_contrast(arms: dict[str, ArmResult], *, a: str = "B0",
                     b: str = "B5", iterations: int = 10000,
                     seed: int = 20260909) -> dict:
    """Strict-NFC macro-family CER contrast, B minus A, paired by family."""
    if a not in arms or b not in arms:
        raise schema.ContractError(
            f"primary_contrast needs arms {a!r} and {b!r}; got {sorted(arms)}"
        )
    arm_a, arm_b = arms[a], arms[b]
    families_a = sorted(arm_a.per_family_cer)
    families_b = sorted(arm_b.per_family_cer)
    if families_a != families_b:
        raise schema.ContractError(
            f"primary_contrast refuses mismatched family sets: "
            f"{a} has {families_a}, {b} has {families_b}"
        )
    if not families_a:
        raise schema.ContractError("primary_contrast needs at least one family")
    boot = statistics.paired_bootstrap(
        arm_a.per_family_cer, arm_b.per_family_cer,
        iterations=iterations, seed=seed,
    )
    return {
        "a": a,
        "b": b,
        "metric": PRIMARY_METRIC,
        "n_families": boot["n_families"],
        "point_estimate": boot["point_estimate"],
        "ci_low": boot["ci_low"],
        "ci_high": boot["ci_high"],
        "ci": boot["ci"],
        "iterations": boot["iterations"],
        "resample_unit": boot["resample_unit"],
        "method": boot["method"],
        "secondary_endpoints": {
            a: {
                "coverage": arm_a.coverage,
                "harmful": arm_a.harmful,
                "accepted": arm_a.accepted,
            },
            b: {
                "coverage": arm_b.coverage,
                "harmful": arm_b.harmful,
                "accepted": arm_b.accepted,
            },
        },
    }


def harm_coverage_points(arms: list[ArmResult]) -> list[dict]:
    """One ``(coverage, harmful_rate, arm_id)`` point per arm."""
    points = []
    for arm in arms:
        if not isinstance(arm, ArmResult):
            raise schema.ContractError("arms entries must be ArmResult")
        harmful_rate = (arm.harmful / arm.accepted) if arm.accepted else None
        points.append({
            "arm_id": arm.arm_id,
            "coverage": arm.coverage,
            "harmful_rate": harmful_rate,
        })
    points.sort(key=lambda point: point["arm_id"])
    return points


def failure_denominator_table(arms: list[ArmResult], *,
                              total_pages: int) -> dict:
    """Per-arm page denominators; the denominator is always ``total_pages``."""
    if not isinstance(total_pages, int) or isinstance(total_pages, bool):
        raise schema.ContractError("total_pages must be an int")
    if total_pages < 0:
        raise schema.ContractError("total_pages must be >= 0")
    rows = []
    for arm in arms:
        if not isinstance(arm, ArmResult):
            raise schema.ContractError("arms entries must be ArmResult")
        if arm.pages_attempted is not None:
            # Per-page truth from the records: a partly-failed arm is reported
            # as partly failed, not collapsed to its overall status.
            attempted = arm.pages_attempted
            ok = arm.pages_ok
            failed = arm.pages_failed
            unsupported = arm.pages_unsupported
            granularity = "per_page"
        else:
            attempted = total_pages
            if arm.status == "ok":
                ok, failed, unsupported = total_pages, 0, 0
            elif arm.status == "failed":
                ok, failed, unsupported = 0, total_pages, 0
            else:
                ok, failed, unsupported = 0, 0, total_pages
            granularity = "arm_status_fallback"
        rows.append({
            "arm_id": arm.arm_id,
            "status": arm.status,
            "granularity": granularity,
            "pages_attempted": attempted,
            "pages_ok": ok,
            "pages_failed": failed,
            "pages_unsupported": unsupported,
            "denominator": total_pages,
        })
    rows.sort(key=lambda row: row["arm_id"])
    return {"total_pages": total_pages, "rows": rows}


def _format_precision(value) -> str:
    if value is None:
        return "undefined"
    return f"{value:.3f}"


def _format_float(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def reconcile(report: dict, *, expected_records: int) -> None:
    """Raise :class:`schema.ContractError` on any irreconcilable table."""
    if not isinstance(report, dict):
        raise schema.ContractError("report must be a dict")
    if not isinstance(expected_records, int) or isinstance(expected_records, bool):
        raise schema.ContractError("expected_records must be an int")
    if "baseline" not in report:
        raise schema.ContractError("report has no 'baseline' table")
    baseline = report["baseline"]
    rows = baseline.get("rows", [])
    totals = baseline.get("totals", {})
    for key in ("accepted", "beneficial", "neutral", "harmful"):
        recomputed = sum(row.get(key, 0) for row in rows)
        if totals.get(key) != recomputed:
            raise schema.ContractError(
                f"baseline totals[{key!r}]={totals.get(key)!r} does not "
                f"reconcile to row sum {recomputed}"
            )
    header = report.get("header", {})
    if "expected_records" in header and header["expected_records"] != expected_records:
        raise schema.ContractError(
            f"report header expected_records={header['expected_records']!r} "
            f"!= {expected_records!r}"
        )
    n_records = header.get("n_records")
    if isinstance(n_records, int) and expected_records and n_records != expected_records:
        raise schema.ContractError(
            f"report header n_records={n_records} != expected_records "
            f"{expected_records}"
        )
    failures = report.get("failures")
    if isinstance(failures, dict):
        total_pages = failures.get("total_pages")
        for row in failures.get("rows", []):
            if row.get("denominator") != total_pages:
                raise schema.ContractError(
                    f"arm {row.get('arm_id')!r} denominator does not match "
                    f"the failure table total_pages"
                )
            accounted = (
                row.get("pages_ok", 0) + row.get("pages_failed", 0)
                + row.get("pages_unsupported", 0)
            )
            if accounted != row.get("pages_attempted", accounted):
                raise schema.ContractError(
                    f"arm {row.get('arm_id')!r} page states do not sum "
                    f"to pages_attempted"
                )
            if row.get("pages_attempted", 0) > total_pages:
                raise schema.ContractError(
                    f"arm {row.get('arm_id')!r} attempted more pages than "
                    f"total_pages"
                )
    contrast = report.get("primary_contrast")
    if isinstance(contrast, dict) and "n_families" in contrast:
        if contrast["n_families"] < 1:
            raise schema.ContractError("primary_contrast has no families")


def render_markdown(report: dict) -> str:
    """Render the report deterministically; same input -> same bytes."""
    lines = []
    header = report.get("header", {})
    lines.append(f"# Study report {header.get('study_id', 'unknown')}")
    lines.append("")
    lines.append(f"- metric_version: {header.get('metric_version', 'unknown')}")
    lines.append(f"- config_hash: {header.get('config_hash', 'unknown')}")
    lines.append(
        f"- expected_records: {header.get('expected_records', 'unknown')}"
    )
    lines.append("")
    baseline = report.get("baseline", {})
    lines.append("## Baseline")
    lines.append("")
    lines.append(
        "| arm | status | macro_cer | coverage | exact_precision | "
        "accepted | beneficial | neutral | harmful | notes |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for row in sorted(baseline.get("rows", []), key=lambda r: r["arm_id"]):
        lines.append(
            f"| {row['arm_id']} | {row['status']} | "
            f"{_format_float(row.get('macro_cer'))} | "
            f"{_format_float(row.get('coverage'))} | "
            f"{_format_precision(row.get('exact_correction_precision'))} | "
            f"{row.get('accepted', 0)} | {row.get('beneficial', 0)} | "
            f"{row.get('neutral', 0)} | {row.get('harmful', 0)} | "
            f"{row.get('notes', '')} |"
        )
    totals = baseline.get("totals", {})
    if totals:
        lines.append("")
        lines.append(
            f"Totals: arms={totals.get('arms', 0)} "
            f"accepted={totals.get('accepted', 0)} "
            f"beneficial={totals.get('beneficial', 0)} "
            f"neutral={totals.get('neutral', 0)} "
            f"harmful={totals.get('harmful', 0)}"
        )
    contrast = report.get("primary_contrast")
    if isinstance(contrast, dict) and contrast:
        lines.append("")
        lines.append("## Primary contrast")
        lines.append("")
        lines.append(
            f"- contrast: {contrast.get('b')} minus {contrast.get('a')} "
            f"({contrast.get('metric', PRIMARY_METRIC)})"
        )
        lines.append(f"- n_families: {contrast.get('n_families')}")
        lines.append(
            f"- point_estimate: {_format_float(contrast.get('point_estimate'))}"
        )
        lines.append(
            f"- CI: [{_format_float(contrast.get('ci_low'))}, "
            f"{_format_float(contrast.get('ci_high'))}]"
        )
        for arm_id in sorted(contrast.get("secondary_endpoints", {})):
            endpoint = contrast["secondary_endpoints"][arm_id]
            lines.append(
                f"- {arm_id}: coverage="
                f"{_format_float(endpoint.get('coverage'))} "
                f"harmful={endpoint.get('harmful')} "
                f"accepted={endpoint.get('accepted')}"
            )
    failures = report.get("failures")
    if isinstance(failures, dict) and failures.get("rows"):
        lines.append("")
        lines.append("## Failure denominators")
        lines.append("")
        lines.append(
            "| arm | status | attempted | ok | failed | unsupported | "
            "denominator |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in sorted(failures["rows"], key=lambda r: r["arm_id"]):
            lines.append(
                f"| {row['arm_id']} | {row['status']} | "
                f"{row.get('pages_attempted', 0)} | {row.get('pages_ok', 0)} | "
                f"{row.get('pages_failed', 0)} | "
                f"{row.get('pages_unsupported', 0)} | "
                f"{row.get('denominator', 0)} |"
            )
    points = report.get("harm_coverage")
    if isinstance(points, list) and points:
        lines.append("")
        lines.append("## Harm vs coverage")
        lines.append("")
        for point in sorted(points, key=lambda p: p["arm_id"]):
            lines.append(
                f"- {point['arm_id']}: coverage="
                f"{_format_float(point.get('coverage'))} "
                f"harmful_rate={_format_float(point.get('harmful_rate'))}"
            )
    lines.append("")
    return "\n".join(lines)


def _config_hash(config_path) -> str:
    raw = Path(config_path).read_bytes()
    return hashlib.sha256(raw).hexdigest()


def rebuild_from_records(records_path, *,
                         config: str = "research/configs/final.json") -> dict:
    """Rebuild every numeric table offline from saved prediction JSONL.

    Each JSONL line is one scored record::

        {"arm_id": ..., "family": ..., "reference": ..., "hypothesis": ...,
         "accepted": bool, "beneficial"/"neutral"/"harmful" class via
         "edit_class", "status": "ok"|"failed"|..., "exact": bool}

    The header embeds ``metric_version`` and the config-file hash, so a later
    bug fix requires a versioned correction plus a transparent rerun.
    """
    config_file = Path(config)
    try:
        config_payload = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise schema.ContractError(
            f"cannot load study config {config_file}: {exc}"
        ) from exc
    study_id = config_payload.get("study_id", "unknown")
    expected_records = int(config_payload.get("expected_records", 0))
    total_pages = int(config_payload.get("total_pages", expected_records))
    hash_digest = _config_hash(config_file)

    families: dict[str, dict[str, list[float]]] = {}
    decisions: dict[str, dict[str, int]] = {}
    exact_hits: dict[str, int] = {}
    statuses: dict[str, str] = {}
    page_states: dict[str, dict[str, int]] = {}
    has_decisions: set[str] = set()  # arms with any correction-decision record
    n_records = 0

    _OK = {"ok", ""}
    _UNSUPPORTED = {"unsupported"}
    try:
        raw_lines = Path(records_path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise schema.ContractError(
            f"cannot load records {records_path}: {exc}"
        ) from exc
    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise schema.ContractError(f"bad JSONL record: {exc}") from exc
        arm_id = record.get("arm_id", "")
        family = record.get("family", "")
        reference = record.get("reference", "")
        hypothesis = record.get("hypothesis", "")
        if not arm_id or not family or not reference:
            raise schema.ContractError("records need arm_id/family/reference")
        cer = metrics.character_error_rate(
            reference, hypothesis, policy="nfc_strict"
        )["cer"]
        families.setdefault(arm_id, {}).setdefault(family, []).append(cer)
        bucket = decisions.setdefault(arm_id, {
            "accepted": 0, "beneficial": 0, "neutral": 0, "harmful": 0,
        })
        if "accepted" in record:
            has_decisions.add(arm_id)
        if record.get("accepted", False):
            bucket["accepted"] += 1
            edit_class = record.get("edit_class")
            if edit_class is None:
                raise schema.ContractError(
                    "an accepted correction record must carry an edit_class "
                    "(beneficial/neutral/harmful); a fabricated default is "
                    "not allowed"
                )
            if edit_class not in ("beneficial", "neutral", "harmful"):
                raise schema.ContractError(
                    f"unknown edit_class {edit_class!r}"
                )
            bucket[edit_class] += 1
            if record.get("exact", False):
                exact_hits[arm_id] = exact_hits.get(arm_id, 0) + 1
        page_status = str(record.get("status", "ok"))
        bucket_states = page_states.setdefault(
            arm_id, {"attempted": 0, "ok": 0, "failed": 0, "unsupported": 0}
        )
        bucket_states["attempted"] += 1
        if page_status in _OK:
            bucket_states["ok"] += 1
        elif page_status in _UNSUPPORTED:
            bucket_states["unsupported"] += 1
        else:  # empty/truncated/parse_error/invocation_error/failed/...
            bucket_states["failed"] += 1
        # arm-level status: worst-case summary of the per-page states
        if bucket_states["failed"]:
            statuses[arm_id] = "failed"
        elif bucket_states["unsupported"] and not bucket_states["ok"]:
            statuses[arm_id] = "unsupported"
        else:
            statuses.setdefault(arm_id, "ok")
        n_records += 1

    roster = config_payload.get("arms", sorted(families))
    arm_results: list[ArmResult] = []
    for arm_id in roster:
        per_family = {
            family: sum(values) / len(values)
            for family, values in sorted(families.get(arm_id, {}).items())
        }
        bucket = decisions.get(arm_id, {
            "accepted": 0, "beneficial": 0, "neutral": 0, "harmful": 0,
        })
        attempted = sum(
            len(values) for values in families.get(arm_id, {}).values()
        )
        if arm_id not in has_decisions:
            # No correction decisions in the records for this arm: coverage and
            # the harm endpoints are UNMEASURED, not a measured zero.
            coverage = None
        else:
            coverage = (bucket["accepted"] / attempted) if attempted else 0.0
        precision = None
        if coverage and bucket["accepted"]:
            precision = exact_hits.get(arm_id, 0) / bucket["accepted"]
        states = page_states.get(arm_id)
        arm_results.append(ArmResult(
            arm_id=arm_id,
            status=statuses.get(arm_id, "failed" if arm_id not in families else "ok"),
            per_family_cer=per_family,
            coverage=coverage,
            accepted=bucket["accepted"],
            beneficial=bucket["beneficial"],
            neutral=bucket["neutral"],
            harmful=bucket["harmful"],
            exact_correction_precision=precision,
            pages_attempted=states["attempted"] if states else None,
            pages_ok=states["ok"] if states else None,
            pages_failed=states["failed"] if states else None,
            pages_unsupported=states["unsupported"] if states else None,
        ))

    baseline = baseline_table(arm_results)
    by_id = {arm.arm_id: arm for arm in arm_results}
    contrast_cfg = config_payload.get("primary_contrast", {"a": "B0", "b": "B5"})
    contrast = None
    if contrast_cfg.get("a") in by_id and contrast_cfg.get("b") in by_id:
        left, right = by_id[contrast_cfg["a"]], by_id[contrast_cfg["b"]]
        if left.per_family_cer and (
            sorted(left.per_family_cer) == sorted(right.per_family_cer)
        ):
            contrast = primary_contrast(
                by_id, a=contrast_cfg["a"], b=contrast_cfg["b"]
            )
    report = {
        "header": {
            "study_id": study_id,
            "metric_version": metrics.METRIC_VERSION,
            "config_hash": hash_digest,
            "config": str(config_file),
            "expected_records": expected_records or n_records,
            "n_records": n_records,
        },
        "baseline": baseline,
        "primary_contrast": contrast or {},
        "harm_coverage": harm_coverage_points(arm_results),
        "failures": failure_denominator_table(
            arm_results, total_pages=total_pages or n_records
        ),
    }
    return report
