"""External-dataset evaluation stubs for the Bengali-OCR research study (S6).

No downloads, no network. Real external datasets (REID2019, Mozhi) are NOT
fetched here; tests use tiny synthetic PAGE-XML / reference fixtures.

Two scoring modes::

    native              use the published native evaluator for the dataset
                        version (REID / Mozhi metric code). There is none
                        registered by default, so this mode raises
                        :class:`NativeEvaluatorUnavailable`. It NEVER falls
                        back to the legacy scorer.
    common_text_metrics score with the study's versioned CER/WER
                        (``research.metrics``) under a fully specified
                        normalization dict. The result is labelled
                        ``common_text_metrics`` and carries
                        ``comparable_to_published_native = False``.

Reference-import helpers in :mod:`pdf_craft_tool.external_eval` may be reused
where the on-disk format matches (PAGE XML); its legacy scorer
(:func:`external_eval.score_hypotheses`, built on ``benchmark.score``) is the
repo's normalised CER/WER, NOT a published native metric, and is never called
from the ``native`` path.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import metrics, schema

EXTERNAL_MODES = ("native", "common_text_metrics")


class NativeEvaluatorUnavailable(RuntimeError):
    """No versioned published evaluator is registered for this dataset."""


@dataclass(frozen=True)
class ExternalRef:
    ref_id: str
    split: str
    text: str
    source: str
    license: str

    def __post_init__(self):
        for name in ("ref_id", "split", "text", "source", "license"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise schema.ContractError(
                    f"ExternalRef.{name} must be a non-empty string, "
                    f"got {value!r}"
                )

    def to_dict(self) -> dict:
        return {
            "ref_id": self.ref_id,
            "split": self.split,
            "text": self.text,
            "source": self.source,
            "license": self.license,
        }


# Registry of published native evaluators: (dataset, version) -> callable.
_NATIVE_REGISTRY: dict[tuple[str, str], object] = {}


def register_native_evaluator(dataset, version, fn) -> None:
    """Register the versioned published evaluator for one dataset version."""
    if not isinstance(dataset, str) or not dataset:
        raise schema.ContractError("dataset must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise schema.ContractError("version must be a non-empty string")
    if not callable(fn):
        raise schema.ContractError("native evaluator must be callable")
    _NATIVE_REGISTRY[(dataset, version)] = fn


def _native_lookup(dataset, version):
    try:
        return _NATIVE_REGISTRY[(dataset, version)]
    except KeyError:
        raise NativeEvaluatorUnavailable(
            f"no published native evaluator registered for "
            f"dataset={dataset!r} version={version!r}; refusing to fall back "
            f"to the legacy scorer"
        ) from None


def import_reference_fixture(path, *, dataset, version) -> list[ExternalRef]:
    """Import a reference fixture, preserving IDs and splits exactly.

    Supported fixture layouts (all local files, never downloaded):

    * a single PAGE-XML file (``*.xml``) — parsed with
      :func:`pdf_craft_tool.external_eval.parse_page_xml`; one
      :class:`ExternalRef` per non-empty line, ``ref_id`` of the form
      ``{dataset}:{stem}:L{index:04d}``, split ``"test"``;
    * a directory of PAGE-XML files — same per-file rule;
    * a JSONL file with one ``{ref_id/id, split, text/reference, source?,
      license?, overlap_note?}`` object per line;
    * a JSON file holding either such a list or
      ``{"records": [...], "license": ..., "overlap_note": ...}``.

    Reference IDs and splits are kept byte-for-byte. The dataset, version,
    license and any overlap note are recorded on each ref's ``source``
    payload (JSON, canonical key order).
    """
    from pdf_craft_tool import external_eval  # reference-import helpers only

    if not isinstance(dataset, str) or not dataset:
        raise schema.ContractError("dataset must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise schema.ContractError("version must be a non-empty string")
    target = Path(path)
    if not target.exists():
        raise schema.ContractError(f"reference fixture not found: {target}")

    refs: list[ExternalRef] = []

    def _make(ref_id, split, text, license, overlap_note=""):
        if not isinstance(ref_id, str) or not ref_id:
            raise schema.ContractError("reference id must be preserved; got empty")
        if not isinstance(split, str) or not split:
            raise schema.ContractError("split must be preserved; got empty")
        if not isinstance(text, str) or not text:
            raise schema.ContractError(f"reference {ref_id!r} has empty text")
        source = schema.canonical_json({
            "dataset": dataset,
            "version": version,
            "overlap_note": overlap_note or "",
        })
        return ExternalRef(
            ref_id=ref_id, split=split, text=text,
            source=source, license=license or "unknown",
        )

    if target.is_dir():
        xml_files = sorted(target.rglob("*.xml"))
        if not xml_files:
            raise schema.ContractError(
                f"no PAGE-XML fixtures under directory {target}"
            )
        for xml_path in xml_files:
            try:
                lines = external_eval.parse_page_xml(xml_path)
            except (OSError, ET.ParseError) as exc:
                raise schema.ContractError(
                    f"cannot parse PAGE-XML fixture {xml_path}: {exc}"
                ) from exc
            for index, line in enumerate(lines):
                if line.strip():
                    refs.append(_make(
                        f"{dataset}:{xml_path.stem}:L{index:04d}",
                        "test", line, "unknown",
                    ))
        if not refs:
            raise schema.ContractError(f"no references parsed under {target}")
        return refs

    if target.suffix.lower() == ".xml":
        try:
            lines = external_eval.parse_page_xml(target)
        except (OSError, ET.ParseError) as exc:
            raise schema.ContractError(
                f"cannot parse PAGE-XML fixture {target}: {exc}"
            ) from exc
        for index, line in enumerate(lines):
            if line.strip():
                refs.append(_make(
                    f"{dataset}:{target.stem}:L{index:04d}",
                    "test", line, "unknown",
                ))
        if not refs:
            raise schema.ContractError(f"no references parsed in {target}")
        return refs

    if target.suffix.lower() == ".json":
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise schema.ContractError(
                f"cannot load JSON fixture {target}: {exc}"
            ) from exc
        if isinstance(payload, dict):
            top_license = payload.get("license", "unknown")
            overlap = payload.get("overlap_note", "")
            records = payload.get("records")
            if not isinstance(records, list):
                raise schema.ContractError(
                    "JSON fixture dict must hold a 'records' list"
                )
        elif isinstance(payload, list):
            top_license = "unknown"
            overlap = ""
            records = payload
        else:
            raise schema.ContractError("JSON fixture must be a list or a dict")
        for record in records:
            if not isinstance(record, dict):
                raise schema.ContractError("fixture records must be dicts")
            ref_id = record.get("ref_id", record.get("id", ""))
            split = record.get("split", "")
            text = record.get("text", record.get("reference", ""))
            refs.append(_make(
                ref_id, split, text,
                record.get("license", top_license),
                record.get("overlap_note", overlap),
            ))
        if not refs:
            raise schema.ContractError(f"no references parsed in {target}")
        return refs

    # JSONL (also the default for unknown suffixes).
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise schema.ContractError(
            f"cannot load fixture {target}: {exc}"
        ) from exc
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise schema.ContractError(
                f"bad JSONL line in {target}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise schema.ContractError("fixture records must be dicts")
        ref_id = record.get("ref_id", record.get("id", ""))
        split = record.get("split", "")
        text = record.get("text", record.get("reference", ""))
        refs.append(_make(
            ref_id, split, text,
            record.get("license", "unknown"),
            record.get("overlap_note", ""),
        ))
    if not refs:
        raise schema.ContractError(f"no references parsed in {target}")
    return refs


def _granularity(ref: ExternalRef) -> str:
    """Classify a ref as word-level or page-level from its id/source."""
    haystack = f"{ref.ref_id}\n{ref.source}".lower()
    if "word" in haystack and "page" not in haystack:
        return "word"
    if "page" in haystack and "word" not in haystack:
        return "page"
    # REID fixtures are page-level, Mozhi fixtures word-level.
    lowered_id = ref.ref_id.lower()
    if lowered_id.startswith("mozhi"):
        return "word"
    if lowered_id.startswith("reid"):
        return "page"
    return "page"


def _check_single_granularity(refs: list[ExternalRef]) -> str:
    levels = {_granularity(ref) for ref in refs}
    if len(levels) > 1:
        raise schema.ContractError(
            "word-level and page-level scores must never be pooled under one "
            f"metric key; mixed granularities: {sorted(levels)}"
        )
    return next(iter(levels))


def _default_normalization() -> dict:
    return {
        "policy": "nfc_strict",
        "metric_version": metrics.METRIC_VERSION,
        "token_version": metrics.TOKEN_VERSION,
        "unicode_version": metrics.UNICODE_VERSION,
    }


def evaluate_external(hypotheses: dict[str, str], refs: list[ExternalRef], *,
                      mode: str, normalization: dict | None = None,
                      dataset: str | None = None,
                      version: str | None = None) -> dict:
    """Score external hypotheses in one of :data:`EXTERNAL_MODES`.

    ``native`` dispatches to the registered published evaluator for
    ``(dataset, version)`` — inferred from the refs' ``source`` payload when
    not passed explicitly — and raises :class:`NativeEvaluatorUnavailable`
    when none is registered. It never touches the legacy scorer.

    ``common_text_metrics`` scores with ``metrics.character_error_rate`` /
    ``word_error_rate`` under a fully specified normalization dict, which is
    recorded verbatim in the result.
    """
    if mode not in EXTERNAL_MODES:
        raise schema.ContractError(
            f"unknown external mode {mode!r}; expected one of {list(EXTERNAL_MODES)}"
        )
    if not isinstance(hypotheses, dict):
        raise schema.ContractError("hypotheses must be a dict")
    refs = list(refs)
    if not refs:
        raise schema.ContractError("evaluate_external needs at least one ref")
    for ref in refs:
        if not isinstance(ref, ExternalRef):
            raise schema.ContractError("refs entries must be ExternalRef")
    granularity = _check_single_granularity(refs)

    if mode == "native":
        resolved_dataset = dataset
        resolved_version = version
        if resolved_dataset is None or resolved_version is None:
            try:
                payload = json.loads(refs[0].source)
                resolved_dataset = resolved_dataset or payload.get("dataset", "")
                resolved_version = resolved_version or payload.get("version", "")
            except ValueError:
                resolved_dataset, resolved_version = "", ""
        fn = _native_lookup(resolved_dataset or "", resolved_version or "")
        scored = fn(hypotheses, refs)
        return {
            "mode": "native",
            "dataset": resolved_dataset,
            "version": resolved_version,
            "granularity": granularity,
            "comparable_to_published_native": True,
            "native_result": scored,
        }

    # common_text_metrics: fully specified normalization, recorded verbatim.
    norm = dict(normalization) if normalization is not None else _default_normalization()
    if not isinstance(norm, dict) or "policy" not in norm:
        raise schema.ContractError(
            "normalization must be a dict holding at least a 'policy' key"
        )
    policy = norm["policy"]
    per_item: list[dict] = []
    total_edits = total_ref_chars = 0
    total_word_edits = total_ref_words = 0
    missing = 0
    for ref in refs:
        hyp = hypotheses.get(ref.ref_id, "")
        if ref.ref_id not in hypotheses:
            missing += 1
        char = metrics.character_error_rate(ref.text, hyp, policy=policy)
        word = metrics.word_error_rate(ref.text, hyp, policy=policy)
        total_edits += char["edits"]
        total_ref_chars += char["reference_chars"]
        total_word_edits += word["word_edits"]
        total_ref_words += word["reference_words"]
        per_item.append({
            "ref_id": ref.ref_id,
            "cer": char["cer"],
            "wer": word["wer"],
        })
    return {
        "mode": "common_text_metrics",
        "granularity": granularity,
        "comparable_to_published_native": False,
        "normalization": norm,
        "n": len(refs),
        "missing": missing,
        "cer": (total_edits / total_ref_chars) if total_ref_chars else 0.0,
        "wer": (total_word_edits / total_ref_words) if total_ref_words else 0.0,
        "items": per_item,
    }
