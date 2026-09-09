"""Bengali-OCR research study record contracts (S0).

Immutable dataclasses with strict validation. Later stages (corpus audit,
annotation, metrics) import this schema unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import hashlib
import json
import platform
import subprocess
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path

SCHEMA_VERSION = 1
MANIFEST_VERSION = 1

GOLD_FREE_FORBIDDEN_KEYS = frozenset({
    "gold",
    "gold_text",
    "gold_page",
    "verified_text",
    "reference",
    "reference_text",
    "diplomatic_text",
    "target_text",
    "adjudicated_text",
    "annotators",
    "adjudicator",
    "reading_order_gold",
})


class ContractError(ValueError):
    """Raised by every record-contract validation failure."""


class _CoercibleEnum(enum.Enum):
    """String-valued enum with a strict ``coerce`` classmethod."""

    @classmethod
    def coerce(cls, value):
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            valid = sorted(m.value for m in cls)
            raise ContractError(
                f"unknown {cls.__name__} value {value!r}; "
                f"expected one of {valid}"
            ) from None


class Split(_CoercibleEnum):
    TRAIN = "train"
    CALIBRATION = "calibration"
    TEST = "test"
    PILOT = "pilot"
    CHALLENGE = "challenge"


class PageNumberConvention(_CoercibleEnum):
    PDF_INDEX = "pdf_index"
    PRINTED_FOLIO = "printed_folio"
    IMAGE_SEQUENCE = "image_sequence"


class RightsBasis(_CoercibleEnum):
    UNKNOWN = "unknown"
    PUBLIC_DOMAIN = "public_domain"
    LICENSED = "licensed"
    OWNER_PERMISSION = "owner_permission"
    EXCLUDED = "excluded"


class GoldStatus(_CoercibleEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    CONFLICT = "conflict"
    ADJUDICATED = "adjudicated"
    FINAL = "final"
    FLAGGED = "flagged"
    PROVISIONAL = "provisional"


class FailureState(_CoercibleEnum):
    OK = "ok"
    EMPTY = "empty"
    TRUNCATED = "truncated"
    PARSE_ERROR = "parse_error"
    INVOCATION_ERROR = "invocation_error"
    UNSUPPORTED = "unsupported"


class DecisionAction(_CoercibleEnum):
    ACCEPT = "accept"
    ABSTAIN = "abstain"
    REJECT = "reject"


def canonical_json(obj) -> str:
    """Serialise ``obj`` to canonical JSON (sorted keys, compact)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plain(obj):
    if isinstance(obj, enum.Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def record_hash(obj) -> str:
    """SHA-256 hex of the canonical JSON of ``obj`` (key order independent)."""
    return hashlib.sha256(canonical_json(_plain(obj)).encode("utf-8")).hexdigest()


def _is_sha256(s) -> bool:
    return (
        isinstance(s, str)
        and len(s) == 64
        and all(c in "0123456789abcdef" for c in s)
    )


def assert_no_gold_fields(mapping, *, context="record") -> None:
    """Raise if any key in ``mapping`` (recursively) is a gold-only field."""
    stack = [mapping]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in GOLD_FREE_FORBIDDEN_KEYS:
                    raise ContractError(
                        f"gold-only field {key!r} forbidden in {context}"
                    )
                stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


# ---------------------------------------------------------------------------
# Small validation helpers (all failures raise ContractError).
# ---------------------------------------------------------------------------

def _require_str(name, value, *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContractError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _require_sha(name, value):
    if not _is_sha256(value):
        raise ContractError(f"{name} must be a 64-char lowercase sha256 hex string")
    return value


def _require_int(name, value, *, minimum=None):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{name} must be an int, got {value!r}")
    if minimum is not None and value < minimum:
        raise ContractError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def _require_number(name, value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError(f"{name} must be numeric, got {value!r}")
    return value


def _require_bool(name, value):
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be a bool, got {value!r}")
    return value


def _as_tuple(name, value):
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise ContractError(f"{name} must be a list or tuple, got {value!r}")


def _check_unknown(label, data, known):
    unknown = set(data) - known
    if unknown:
        raise ContractError(f"unknown keys for {label}: {sorted(unknown)}")


def _coerced_value(enum_cls, name, value):
    return enum_cls.coerce(value).value


# ---------------------------------------------------------------------------
# Records.
# ---------------------------------------------------------------------------

_GEOMETRY_KEYS = frozenset({
    "x0", "y0", "x1", "y1", "unit", "render_scale", "orientation",
})


@dataclass(frozen=True)
class Geometry:
    x0: int
    y0: int
    x1: int
    y1: int
    unit: str = "px"
    render_scale: float = 1.0
    orientation: int = 0

    def __post_init__(self):
        _require_int("x0", self.x0, minimum=0)
        _require_int("y0", self.y0, minimum=0)
        _require_int("x1", self.x1, minimum=0)
        _require_int("y1", self.y1, minimum=0)
        if not self.x1 > self.x0:
            raise ContractError(f"x1 ({self.x1}) must be > x0 ({self.x0})")
        if not self.y1 > self.y0:
            raise ContractError(f"y1 ({self.y1}) must be > y0 ({self.y0})")
        if self.unit not in ("px", "pt"):
            raise ContractError(f"unit must be 'px' or 'pt', got {self.unit!r}")
        _require_number("render_scale", self.render_scale)
        if not self.render_scale > 0:
            raise ContractError(
                f"render_scale must be > 0, got {self.render_scale!r}"
            )
        if self.orientation not in (0, 90, 180, 270):
            raise ContractError(
                f"orientation must be one of 0/90/180/270, got {self.orientation!r}"
            )

    def to_dict(self) -> dict:
        return {
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
            "unit": self.unit,
            "render_scale": self.render_scale,
            "orientation": self.orientation,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Geometry":
        if not isinstance(data, dict):
            raise ContractError(f"Geometry must be built from a dict, got {type(data)}")
        _check_unknown("Geometry", data, _GEOMETRY_KEYS)
        try:
            return cls(
                x0=data["x0"],
                y0=data["y0"],
                x1=data["x1"],
                y1=data["y1"],
                unit=data.get("unit", "px"),
                render_scale=data.get("render_scale", 1.0),
                orientation=data.get("orientation", 0),
            )
        except KeyError as exc:
            raise ContractError(f"Geometry missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid Geometry: {exc}") from exc


_SOURCE_PAGE_KEYS = frozenset({
    "source_sha256", "work_id", "edition_id", "overlap_group", "page_number",
    "page_number_convention", "width", "height", "image_sha256",
    "original_location", "rights_basis", "rights_evidence",
})


@dataclass(frozen=True)
class SourcePage:
    source_sha256: str
    work_id: str
    edition_id: str
    overlap_group: str
    page_number: int
    page_number_convention: str
    width: int
    height: int
    image_sha256: str
    original_location: str
    rights_basis: str
    rights_evidence: str

    def __post_init__(self):
        _require_sha("source_sha256", self.source_sha256)
        _require_str("work_id", self.work_id)
        _require_str("edition_id", self.edition_id)
        if not isinstance(self.overlap_group, str):
            raise ContractError("overlap_group must be a string")
        _require_int("page_number", self.page_number, minimum=1)
        object.__setattr__(
            self, "page_number_convention",
            _coerced_value(PageNumberConvention, "page_number_convention",
                           self.page_number_convention),
        )
        _require_int("width", self.width, minimum=1)
        _require_int("height", self.height, minimum=1)
        _require_sha("image_sha256", self.image_sha256)
        _require_str("original_location", self.original_location)
        object.__setattr__(
            self, "rights_basis",
            _coerced_value(RightsBasis, "rights_basis", self.rights_basis),
        )
        if not isinstance(self.rights_evidence, str):
            raise ContractError("rights_evidence must be a string")

    @property
    def page_id(self) -> str:
        return record_hash({
            "source_sha256": self.source_sha256,
            "page_number": self.page_number,
            "page_number_convention": self.page_number_convention,
        })

    def to_dict(self) -> dict:
        return {
            "source_sha256": self.source_sha256,
            "work_id": self.work_id,
            "edition_id": self.edition_id,
            "overlap_group": self.overlap_group,
            "page_number": self.page_number,
            "page_number_convention": self.page_number_convention,
            "width": self.width,
            "height": self.height,
            "image_sha256": self.image_sha256,
            "original_location": self.original_location,
            "rights_basis": self.rights_basis,
            "rights_evidence": self.rights_evidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourcePage":
        if not isinstance(data, dict):
            raise ContractError(f"SourcePage must be built from a dict, got {type(data)}")
        _check_unknown("SourcePage", data, _SOURCE_PAGE_KEYS)
        try:
            return cls(**{k: data[k] for k in _SOURCE_PAGE_KEYS if k in data})
        except KeyError as exc:
            raise ContractError(f"SourcePage missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid SourcePage: {exc}") from exc


_SAMPLE_PAGE_KEYS = frozenset({
    "page_id", "source_sha256", "work_id", "edition_id", "overlap_group",
    "split", "stratum", "selection_probability", "seed", "manifest_version",
    "schema_version", "file_hashes", "legacy_gold_ids",
})


@dataclass(frozen=True)
class SamplePage:
    page_id: str
    source_sha256: str
    work_id: str
    edition_id: str
    overlap_group: str
    split: str
    stratum: str
    selection_probability: float
    seed: int
    manifest_version: int
    schema_version: int
    file_hashes: dict
    legacy_gold_ids: tuple = ()

    def __post_init__(self):
        _require_sha("page_id", self.page_id)
        _require_sha("source_sha256", self.source_sha256)
        _require_str("work_id", self.work_id)
        _require_str("edition_id", self.edition_id)
        if not isinstance(self.overlap_group, str):
            raise ContractError("overlap_group must be a string")
        object.__setattr__(self, "split",
                           _coerced_value(Split, "split", self.split))
        _require_str("stratum", self.stratum)
        _require_number("selection_probability", self.selection_probability)
        if not 0 < self.selection_probability <= 1:
            raise ContractError(
                "selection_probability must satisfy 0 < p <= 1, "
                f"got {self.selection_probability!r}"
            )
        _require_int("seed", self.seed, minimum=0)
        if self.manifest_version != MANIFEST_VERSION:
            raise ContractError(
                f"manifest_version must be {MANIFEST_VERSION}, "
                f"got {self.manifest_version!r}"
            )
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError(
                f"schema_version must be {SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        if not isinstance(self.file_hashes, dict):
            raise ContractError("file_hashes must be a dict")
        for key, value in self.file_hashes.items():
            if not isinstance(key, str) or not key:
                raise ContractError(f"file_hashes keys must be non-empty strings")
            _require_sha(f"file_hashes[{key!r}]", value)
        object.__setattr__(self, "file_hashes", dict(sorted(self.file_hashes.items())))
        object.__setattr__(self, "legacy_gold_ids",
                           _as_tuple("legacy_gold_ids", self.legacy_gold_ids))
        for entry in self.legacy_gold_ids:
            _require_str("legacy_gold_ids entry", entry)

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "source_sha256": self.source_sha256,
            "work_id": self.work_id,
            "edition_id": self.edition_id,
            "overlap_group": self.overlap_group,
            "split": self.split,
            "stratum": self.stratum,
            "selection_probability": self.selection_probability,
            "seed": self.seed,
            "manifest_version": self.manifest_version,
            "schema_version": self.schema_version,
            "file_hashes": dict(self.file_hashes),
            "legacy_gold_ids": list(self.legacy_gold_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SamplePage":
        if not isinstance(data, dict):
            raise ContractError(f"SamplePage must be built from a dict, got {type(data)}")
        _check_unknown("SamplePage", data, _SAMPLE_PAGE_KEYS)
        try:
            kwargs = {k: data[k] for k in _SAMPLE_PAGE_KEYS if k in data}
            if "legacy_gold_ids" not in kwargs:
                kwargs["legacy_gold_ids"] = ()
            return cls(**kwargs)
        except KeyError as exc:
            raise ContractError(f"SamplePage missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid SamplePage: {exc}") from exc


_GOLD_LINE_KEYS = frozenset({"line_index", "geometry", "text", "unreadable"})


@dataclass(frozen=True)
class GoldLine:
    line_index: int
    geometry: Geometry
    text: str
    unreadable: bool

    def __post_init__(self):
        _require_int("line_index", self.line_index, minimum=0)
        geometry = self.geometry
        if isinstance(geometry, dict):
            geometry = Geometry.from_dict(geometry)
        if not isinstance(geometry, Geometry):
            raise ContractError(
                f"geometry must be a Geometry, got {self.geometry!r}"
            )
        object.__setattr__(self, "geometry", geometry)
        if not isinstance(self.text, str):
            raise ContractError("text must be a string")
        _require_bool("unreadable", self.unreadable)
        if self.unreadable and self.text != "":
            raise ContractError("unreadable lines must have empty text")
        if not self.unreadable and not self.text:
            raise ContractError("readable lines must have non-empty text")

    def to_dict(self) -> dict:
        return {
            "line_index": self.line_index,
            "geometry": self.geometry.to_dict(),
            "text": self.text,
            "unreadable": self.unreadable,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GoldLine":
        if not isinstance(data, dict):
            raise ContractError(f"GoldLine must be built from a dict, got {type(data)}")
        _check_unknown("GoldLine", data, _GOLD_LINE_KEYS)
        try:
            geometry = data["geometry"]
            if isinstance(geometry, dict):
                geometry = Geometry.from_dict(geometry)
            return cls(
                line_index=data["line_index"],
                geometry=geometry,
                text=data["text"],
                unreadable=data["unreadable"],
            )
        except KeyError as exc:
            raise ContractError(f"GoldLine missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid GoldLine: {exc}") from exc


_GOLD_PAGE_KEYS = frozenset({
    "page_id", "source_sha256", "image_sha256", "lines", "reading_order",
    "unreadable_regions", "annotator_ids", "revisions", "adjudicator_id",
    "status", "guideline_version", "disagreement_rate",
})


@dataclass(frozen=True)
class GoldPage:
    page_id: str
    source_sha256: str
    image_sha256: str
    lines: tuple
    reading_order: tuple
    annotator_ids: tuple
    adjudicator_id: str
    status: str
    guideline_version: str
    disagreement_rate: float = None
    unreadable_regions: tuple = ()
    revisions: tuple = ()

    def __post_init__(self):
        _require_sha("page_id", self.page_id)
        _require_sha("source_sha256", self.source_sha256)
        _require_sha("image_sha256", self.image_sha256)
        object.__setattr__(self, "lines", _as_tuple("lines", self.lines))
        parsed_lines = []
        for line in self.lines:
            if isinstance(line, dict):
                line = GoldLine.from_dict(line)
            if not isinstance(line, GoldLine):
                raise ContractError(f"lines entries must be GoldLine, got {line!r}")
            parsed_lines.append(line)
        object.__setattr__(self, "lines", tuple(parsed_lines))
        indices = [line.line_index for line in self.lines]
        if len(set(indices)) != len(indices):
            raise ContractError("GoldLine line_index values must be unique")
        if set(indices) != set(range(len(self.lines))):
            raise ContractError(
                "GoldLine line_index values must equal {0..len-1}, "
                f"got {sorted(indices)}"
            )
        object.__setattr__(self, "reading_order",
                           _as_tuple("reading_order", self.reading_order))
        for entry in self.reading_order:
            _require_int("reading_order entry", entry, minimum=0)
        if sorted(self.reading_order) != list(range(len(self.lines))):
            raise ContractError("reading_order must be a permutation of the line indices")
        object.__setattr__(self, "unreadable_regions",
                           _as_tuple("unreadable_regions", self.unreadable_regions))
        for region in self.unreadable_regions:
            if not isinstance(region, dict):
                raise ContractError("unreadable_regions entries must be dicts")
        object.__setattr__(self, "annotator_ids",
                           _as_tuple("annotator_ids", self.annotator_ids))
        for annotator in self.annotator_ids:
            _require_str("annotator_ids entry", annotator)
        object.__setattr__(self, "revisions", _as_tuple("revisions", self.revisions))
        for revision in self.revisions:
            if not isinstance(revision, dict):
                raise ContractError("revisions entries must be dicts")
        if not isinstance(self.adjudicator_id, str):
            raise ContractError("adjudicator_id must be a string")
        object.__setattr__(self, "status",
                           _coerced_value(GoldStatus, "status", self.status))
        _require_str("guideline_version", self.guideline_version)
        if self.disagreement_rate is not None:
            _require_number("disagreement_rate", self.disagreement_rate)
            if not 0 <= self.disagreement_rate <= 1:
                raise ContractError(
                    "disagreement_rate must satisfy 0 <= r <= 1, "
                    f"got {self.disagreement_rate!r}"
                )
        distinct_annotators = len(set(self.annotator_ids))
        if self.status == GoldStatus.FINAL.value and distinct_annotators < 2:
            raise ContractError("status 'final' requires >= 2 distinct annotator_ids")
        if self.status == GoldStatus.ADJUDICATED.value and distinct_annotators < 2:
            raise ContractError("status 'adjudicated' requires >= 2 distinct annotator_ids")
        if self.status in (GoldStatus.ADJUDICATED.value, GoldStatus.FINAL.value):
            if not self.adjudicator_id:
                raise ContractError(
                    f"status {self.status!r} requires a non-empty adjudicator_id"
                )

    def is_final_export_eligible(self) -> bool:
        return (
            self.status == GoldStatus.FINAL.value
            and bool(self.adjudicator_id)
            and len(set(self.annotator_ids)) >= 2
        )

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "source_sha256": self.source_sha256,
            "image_sha256": self.image_sha256,
            "lines": [line.to_dict() for line in self.lines],
            "reading_order": list(self.reading_order),
            "unreadable_regions": list(self.unreadable_regions),
            "annotator_ids": list(self.annotator_ids),
            "revisions": list(self.revisions),
            "adjudicator_id": self.adjudicator_id,
            "status": self.status,
            "guideline_version": self.guideline_version,
            "disagreement_rate": self.disagreement_rate,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GoldPage":
        if not isinstance(data, dict):
            raise ContractError(f"GoldPage must be built from a dict, got {type(data)}")
        _check_unknown("GoldPage", data, _GOLD_PAGE_KEYS)
        try:
            kwargs = {k: data[k] for k in _GOLD_PAGE_KEYS if k in data}
            if "unreadable_regions" not in kwargs:
                kwargs["unreadable_regions"] = ()
            if "revisions" not in kwargs:
                kwargs["revisions"] = ()
            if "disagreement_rate" not in kwargs:
                kwargs["disagreement_rate"] = None
            return cls(**kwargs)
        except KeyError as exc:
            raise ContractError(f"GoldPage missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid GoldPage: {exc}") from exc


_PREDICTION_KEYS = frozenset({
    "page_id", "system_id", "config_hash", "model_id", "prompt_hash",
    "crop_hash", "raw_output", "parsed_text", "failure_state", "timing_ms",
    "resource",
})


@dataclass(frozen=True)
class Prediction:
    page_id: str
    system_id: str
    config_hash: str
    model_id: str
    prompt_hash: str
    crop_hash: str
    raw_output: str
    parsed_text: str
    failure_state: str
    timing_ms: int
    resource: dict

    def __post_init__(self):
        _require_sha("page_id", self.page_id)
        _require_str("system_id", self.system_id)
        _require_str("config_hash", self.config_hash)
        _require_str("model_id", self.model_id)
        _require_str("prompt_hash", self.prompt_hash)
        _require_str("crop_hash", self.crop_hash)
        if not isinstance(self.raw_output, str):
            raise ContractError("raw_output must be a string")
        if not isinstance(self.parsed_text, str):
            raise ContractError("parsed_text must be a string")
        object.__setattr__(self, "failure_state",
                           _coerced_value(FailureState, "failure_state",
                                          self.failure_state))
        _require_int("timing_ms", self.timing_ms, minimum=0)
        if not isinstance(self.resource, dict):
            raise ContractError("resource must be a dict")
        for key, value in self.resource.items():
            if not isinstance(key, str) or not key:
                raise ContractError("resource keys must be non-empty strings")
            _require_number(f"resource[{key!r}]", value)

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "system_id": self.system_id,
            "config_hash": self.config_hash,
            "model_id": self.model_id,
            "prompt_hash": self.prompt_hash,
            "crop_hash": self.crop_hash,
            "raw_output": self.raw_output,
            "parsed_text": self.parsed_text,
            "failure_state": self.failure_state,
            "timing_ms": self.timing_ms,
            "resource": dict(self.resource),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Prediction":
        if not isinstance(data, dict):
            raise ContractError(f"Prediction must be built from a dict, got {type(data)}")
        assert_no_gold_fields(data, context="Prediction")
        _check_unknown("Prediction", data, _PREDICTION_KEYS)
        try:
            return cls(**{k: data[k] for k in _PREDICTION_KEYS if k in data})
        except KeyError as exc:
            raise ContractError(f"Prediction missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid Prediction: {exc}") from exc


_CANDIDATE_KEYS = frozenset({
    "candidate_id", "page_id", "base", "start", "end", "before", "after",
    "proposer_id", "evidence_refs", "proposal_bank_hash",
})


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    page_id: str
    base: str
    start: int
    end: int
    before: str
    after: str
    proposer_id: str
    proposal_bank_hash: str
    evidence_refs: tuple = ()

    def __post_init__(self):
        _require_str("candidate_id", self.candidate_id)
        _require_sha("page_id", self.page_id)
        _require_str("base", self.base)
        _require_int("start", self.start, minimum=0)
        _require_int("end", self.end, minimum=0)
        if self.end < self.start:
            raise ContractError(
                f"end ({self.end}) must be >= start ({self.start})"
            )
        if not isinstance(self.before, str):
            raise ContractError("before must be a string")
        if not isinstance(self.after, str):
            raise ContractError("after must be a string")
        if self.start == self.end:
            if self.before != "":
                raise ContractError(
                    "pure insertions (start == end) must have empty 'before'"
                )
        elif not self.before:
            raise ContractError(
                "non-insertion edits (start < end) must have non-empty 'before'"
            )
        _require_str("proposer_id", self.proposer_id)
        _require_str("proposal_bank_hash", self.proposal_bank_hash)
        object.__setattr__(self, "evidence_refs",
                           _as_tuple("evidence_refs", self.evidence_refs))
        for ref in self.evidence_refs:
            _require_str("evidence_refs entry", ref)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "page_id": self.page_id,
            "base": self.base,
            "start": self.start,
            "end": self.end,
            "before": self.before,
            "after": self.after,
            "proposer_id": self.proposer_id,
            "evidence_refs": list(self.evidence_refs),
            "proposal_bank_hash": self.proposal_bank_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate":
        if not isinstance(data, dict):
            raise ContractError(f"Candidate must be built from a dict, got {type(data)}")
        assert_no_gold_fields(data, context="Candidate")
        _check_unknown("Candidate", data, _CANDIDATE_KEYS)
        try:
            kwargs = {k: data[k] for k in _CANDIDATE_KEYS if k in data}
            if "evidence_refs" not in kwargs:
                kwargs["evidence_refs"] = ()
            return cls(**kwargs)
        except KeyError as exc:
            raise ContractError(f"Candidate missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid Candidate: {exc}") from exc


_DECISION_KEYS = frozenset({
    "candidate_id", "gate_id", "gate_version", "threshold", "action",
    "reason", "evidence", "original_retained",
})


@dataclass(frozen=True)
class Decision:
    candidate_id: str
    gate_id: str
    gate_version: str
    threshold: float
    action: str
    reason: str
    original_retained: bool
    evidence: tuple = ()

    def __post_init__(self):
        _require_str("candidate_id", self.candidate_id)
        _require_str("gate_id", self.gate_id)
        _require_str("gate_version", self.gate_version)
        if self.threshold is not None:
            _require_number("threshold", self.threshold)
        object.__setattr__(self, "action",
                           _coerced_value(DecisionAction, "action", self.action))
        _require_str("reason", self.reason)
        _require_bool("original_retained", self.original_retained)
        if self.action != DecisionAction.ACCEPT.value and not self.original_retained:
            raise ContractError(
                "original_retained must be True unless action is 'accept'"
            )
        object.__setattr__(self, "evidence", _as_tuple("evidence", self.evidence))
        for item in self.evidence:
            _require_str("evidence entry", item)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "gate_id": self.gate_id,
            "gate_version": self.gate_version,
            "threshold": self.threshold,
            "action": self.action,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "original_retained": self.original_retained,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        if not isinstance(data, dict):
            raise ContractError(f"Decision must be built from a dict, got {type(data)}")
        _check_unknown("Decision", data, _DECISION_KEYS)
        try:
            kwargs = {k: data[k] for k in _DECISION_KEYS if k in data}
            if "evidence" not in kwargs:
                kwargs["evidence"] = ()
            return cls(**kwargs)
        except KeyError as exc:
            raise ContractError(f"Decision missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid Decision: {exc}") from exc


_EVALUATION_KEYS = frozenset({
    "prediction_hash", "gold_hash", "manifest_hash", "metric_version",
    "tokenization_version", "unicode_version", "denominators", "per_family",
    "statistical_protocol",
})


@dataclass(frozen=True)
class Evaluation:
    prediction_hash: str
    gold_hash: str
    manifest_hash: str
    metric_version: str
    tokenization_version: str
    unicode_version: str
    denominators: dict
    per_family: dict
    statistical_protocol: dict

    def __post_init__(self):
        _require_sha("prediction_hash", self.prediction_hash)
        _require_sha("gold_hash", self.gold_hash)
        _require_sha("manifest_hash", self.manifest_hash)
        _require_str("metric_version", self.metric_version)
        _require_str("tokenization_version", self.tokenization_version)
        _require_str("unicode_version", self.unicode_version)
        if not isinstance(self.denominators, dict):
            raise ContractError("denominators must be a dict")
        for key, value in self.denominators.items():
            if not isinstance(key, str) or not key:
                raise ContractError("denominators keys must be non-empty strings")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractError(
                    f"denominators[{key!r}] must be a non-negative int"
                )
        if not isinstance(self.per_family, dict):
            raise ContractError("per_family must be a dict")
        for key, value in self.per_family.items():
            if not isinstance(key, str) or not key:
                raise ContractError("per_family keys must be non-empty strings")
            if not isinstance(value, dict):
                raise ContractError(f"per_family[{key!r}] must be a dict")
        if not isinstance(self.statistical_protocol, dict) or not self.statistical_protocol:
            raise ContractError("statistical_protocol must be a non-empty dict")

    def to_dict(self) -> dict:
        return {
            "prediction_hash": self.prediction_hash,
            "gold_hash": self.gold_hash,
            "manifest_hash": self.manifest_hash,
            "metric_version": self.metric_version,
            "tokenization_version": self.tokenization_version,
            "unicode_version": self.unicode_version,
            "denominators": dict(self.denominators),
            "per_family": {k: dict(v) for k, v in self.per_family.items()},
            "statistical_protocol": dict(self.statistical_protocol),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Evaluation":
        if not isinstance(data, dict):
            raise ContractError(f"Evaluation must be built from a dict, got {type(data)}")
        _check_unknown("Evaluation", data, _EVALUATION_KEYS)
        try:
            return cls(**{k: data[k] for k in _EVALUATION_KEYS if k in data})
        except KeyError as exc:
            raise ContractError(f"Evaluation missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid Evaluation: {exc}") from exc


# ---------------------------------------------------------------------------
# Manifest.
# ---------------------------------------------------------------------------

_MANIFEST_KEYS = frozenset({
    "kind", "manifest_version", "schema_version", "created_utc",
    "software_baseline", "records", "manifest_hash",
})


def software_baseline() -> dict:
    """Capture environment identity (git state, interpreter, key packages)."""
    root = Path(__file__).resolve().parents[2]

    def _run_text(args):
        try:
            result = subprocess.run(
                args, cwd=root, capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return ""
            return result.stdout.strip()
        except Exception:
            return ""

    def _run_bytes(args):
        try:
            result = subprocess.run(
                args, cwd=root, capture_output=True, timeout=15
            )
            if result.returncode != 0:
                return b""
            return result.stdout or b""
        except Exception:
            return b""

    commit = _run_text(["git", "rev-parse", "HEAD"])
    branch = _run_text(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    status_bytes = _run_bytes(["git", "-c", "core.abbrev=no", "status", "--porcelain=v1"])

    packages = {}
    for name in ("rapidfuzz", "regex", "Pillow"):
        try:
            packages[name] = importlib_metadata.version(name)
        except Exception:
            packages[name] = ""

    return {
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": bool(status_bytes.strip()),
        "git_status_sha256": hashlib.sha256(status_bytes).hexdigest(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
    }


_default_baseline_fn = software_baseline


@dataclass(frozen=True)
class Manifest:
    kind: str
    manifest_version: int
    schema_version: int
    created_utc: str
    software_baseline: dict
    records: tuple
    manifest_hash: str

    def __post_init__(self):
        _require_str("kind", self.kind)
        if self.manifest_version != MANIFEST_VERSION:
            raise ContractError(
                f"manifest_version must be {MANIFEST_VERSION}, "
                f"got {self.manifest_version!r}"
            )
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError(
                f"schema_version must be {SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        _require_str("created_utc", self.created_utc)
        if not isinstance(self.software_baseline, dict):
            raise ContractError("software_baseline must be a dict")
        object.__setattr__(self, "records", _as_tuple("records", self.records))
        for record in self.records:
            if not isinstance(record, dict):
                raise ContractError("records entries must be dicts")
        _require_sha("manifest_hash", self.manifest_hash)

    @staticmethod
    def _compute_hash(kind, manifest_version, schema_version, created_utc,
                      baseline, records) -> str:
        return record_hash({
            "kind": kind,
            "manifest_version": manifest_version,
            "schema_version": schema_version,
            "created_utc": created_utc,
            "software_baseline": baseline,
            "records": list(records),
        })

    @classmethod
    def build(cls, kind, records, *, software_baseline=None) -> "Manifest":
        _require_str("kind", kind)
        if isinstance(records, (dict, str, bytes)):
            raise ContractError("records must be a list or tuple of dicts")
        try:
            recs = tuple(records)
        except TypeError as exc:
            raise ContractError(f"records must be a list or tuple of dicts: {exc}") from exc
        for record in recs:
            if not isinstance(record, dict):
                raise ContractError("records entries must be dicts")
        # Defense in depth: an inference-side manifest must never carry gold /
        # reference text, even when its records were assembled from raw dicts
        # rather than through Prediction/Candidate.from_dict.
        if any(token in kind.lower()
               for token in ("inference", "prediction", "candidate")):
            for record in recs:
                assert_no_gold_fields(record, context=f"{kind} manifest record")
        baseline = dict(software_baseline) if software_baseline is not None else _default_baseline_fn()
        if not isinstance(baseline, dict):
            raise ContractError("software_baseline must be a dict")
        created = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        digest = cls._compute_hash(
            kind, MANIFEST_VERSION, SCHEMA_VERSION, created, baseline, recs
        )
        return cls(kind, MANIFEST_VERSION, SCHEMA_VERSION, created, baseline, recs, digest)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "manifest_version": self.manifest_version,
            "schema_version": self.schema_version,
            "created_utc": self.created_utc,
            "software_baseline": dict(self.software_baseline),
            "records": list(self.records),
            "manifest_hash": self.manifest_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        if not isinstance(data, dict):
            raise ContractError(f"Manifest must be built from a dict, got {type(data)}")
        _check_unknown("Manifest", data, _MANIFEST_KEYS)
        try:
            manifest = cls(
                kind=data["kind"],
                manifest_version=data["manifest_version"],
                schema_version=data["schema_version"],
                created_utc=data["created_utc"],
                software_baseline=data["software_baseline"],
                records=tuple(data["records"]),
                manifest_hash=data["manifest_hash"],
            )
        except KeyError as exc:
            raise ContractError(f"Manifest missing required key {exc}") from exc
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid Manifest: {exc}") from exc
        expected = cls._compute_hash(
            manifest.kind, manifest.manifest_version, manifest.schema_version,
            manifest.created_utc, manifest.software_baseline, manifest.records,
        )
        if expected != manifest.manifest_hash:
            raise ContractError("manifest_hash does not match manifest contents")
        return manifest

    def write(self, path) -> Path:
        """Write this manifest immutably: refuse to overwrite differing content."""
        target = Path(path)
        if target.exists():
            existing = Manifest.load(target)
            if existing.manifest_hash != self.manifest_hash:
                raise ContractError(f"manifest is immutable: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path) -> "Manifest":
        target = Path(path)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except ContractError:
            raise
        except (OSError, ValueError) as exc:
            raise ContractError(f"cannot load manifest {target}: {exc}") from exc
        return cls.from_dict(data)
