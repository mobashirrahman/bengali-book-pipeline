"""Study configuration for the Bengali-OCR research pilot (S0)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .schema import SCHEMA_VERSION, ContractError

_CONFIG_KEYS = frozenset({
    "study_id",
    "schema_version",
    "draft",
    "primary_endpoint",
    "baseline_candidates",
    "sampling",
    "split_allocation",
    "uncertainty",
    "calibration_screen",
})

_ENDPOINT_KEYS = frozenset({"name", "contrast", "unit", "direction", "budget"})

_ENDPOINT_DIRECTIONS = frozenset({"lower_is_better", "higher_is_better"})


def _is_baseline_id(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) >= 2
        and value[0] == "B"
        and value[1:].isdigit()
        and value[1:].isascii()
    )


@dataclass(frozen=True)
class StudyConfig:
    study_id: str
    schema_version: int
    draft: bool
    primary_endpoint: dict
    baseline_candidates: tuple
    sampling: dict
    split_allocation: dict
    uncertainty: dict
    calibration_screen: dict

    def __post_init__(self):
        if not isinstance(self.study_id, str) or not self.study_id:
            raise ContractError("study_id must be a non-empty string")
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError(
                f"schema_version must be {SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        if not isinstance(self.draft, bool):
            raise ContractError(f"draft must be a bool, got {self.draft!r}")
        if not isinstance(self.primary_endpoint, dict):
            raise ContractError("primary_endpoint must be a dict")
        missing = _ENDPOINT_KEYS - set(self.primary_endpoint)
        if missing:
            raise ContractError(
                f"primary_endpoint missing keys: {sorted(missing)}"
            )
        for key in ("name", "contrast", "unit", "budget"):
            value = self.primary_endpoint[key]
            if not isinstance(value, str) or not value:
                raise ContractError(
                    f"primary_endpoint[{key!r}] must be a non-empty string"
                )
        if self.primary_endpoint["direction"] not in _ENDPOINT_DIRECTIONS:
            raise ContractError(
                "primary_endpoint direction must be one of "
                f"{sorted(_ENDPOINT_DIRECTIONS)}, "
                f"got {self.primary_endpoint['direction']!r}"
            )
        if isinstance(self.baseline_candidates, (list, tuple)):
            object.__setattr__(self, "baseline_candidates",
                               tuple(self.baseline_candidates))
        else:
            raise ContractError("baseline_candidates must be a list or tuple")
        if not self.baseline_candidates:
            raise ContractError("baseline_candidates must not be empty")
        for baseline_id in self.baseline_candidates:
            if not _is_baseline_id(baseline_id):
                raise ContractError(
                    f"baseline id {baseline_id!r} must match ^B[0-9]+$"
                )
        if not isinstance(self.sampling, dict):
            raise ContractError("sampling must be a dict")
        seed = self.sampling.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ContractError("sampling['seed'] must be an int")
        if not isinstance(self.split_allocation, dict) or not self.split_allocation:
            raise ContractError("split_allocation must be a non-empty dict")
        for key, value in self.split_allocation.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ContractError(
                    f"split_allocation[{key!r}] must be a positive int"
                )
        if not isinstance(self.uncertainty, dict):
            raise ContractError("uncertainty must be a dict")
        if not isinstance(self.calibration_screen, dict):
            raise ContractError("calibration_screen must be a dict")

    @classmethod
    def load(cls, path) -> "StudyConfig":
        target = Path(path)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError(f"cannot load study config {target}: {exc}") from exc
        if not isinstance(data, dict):
            raise ContractError("study config must be a JSON object")
        unknown = set(data) - _CONFIG_KEYS
        if unknown:
            raise ContractError(f"unknown study config keys: {sorted(unknown)}")
        missing = _CONFIG_KEYS - set(data)
        if missing:
            raise ContractError(f"study config missing keys: {sorted(missing)}")
        try:
            return cls(
                study_id=data["study_id"],
                schema_version=data["schema_version"],
                draft=data["draft"],
                primary_endpoint=dict(data["primary_endpoint"]),
                baseline_candidates=tuple(data["baseline_candidates"]),
                sampling=dict(data["sampling"]),
                split_allocation=dict(data["split_allocation"]),
                uncertainty=dict(data["uncertainty"]),
                calibration_screen=dict(data["calibration_screen"]),
            )
        except ContractError:
            raise
        except (TypeError, ValueError, AttributeError) as exc:
            raise ContractError(f"invalid study config: {exc}") from exc

    def to_dict(self) -> dict:
        return {
            "study_id": self.study_id,
            "schema_version": self.schema_version,
            "draft": self.draft,
            "primary_endpoint": dict(self.primary_endpoint),
            "baseline_candidates": list(self.baseline_candidates),
            "sampling": dict(self.sampling),
            "split_allocation": dict(self.split_allocation),
            "uncertainty": dict(self.uncertainty),
            "calibration_screen": dict(self.calibration_screen),
        }
