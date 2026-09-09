"""Gate calibration: a small L2-regularised logistic gate and the roadmap's
calibration-event screen (S5).

Leakage discipline is enforced structurally:
 * ``GateClassifier.fit`` uses ``train`` split items only.
 * ``choose_threshold`` uses ``calibration`` split items only.
 * ``split_guard`` refuses any ``test`` split item in either path.

The screen (roadmap section 5) is an operational minimum, not a statistical
guarantee. When it is not met, the study falls back to a rule-based policy.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from . import schema

TRAIN_SPLIT = "train"
CALIBRATION_SPLIT = "calibration"
TEST_SPLIT = "test"

_BENEFICIAL = "beneficial"
_HARMFUL = "harmful"
_NEUTRAL = "neutral"
_OUTCOMES = (_BENEFICIAL, _NEUTRAL, _HARMFUL)

POLICY_VERSION = "s5-1"


@dataclass(frozen=True)
class LabelledCandidate:
    candidate_id: str
    family_id: str
    split: str
    features: dict
    outcome: str

    def __post_init__(self):
        if not self.candidate_id:
            raise schema.ContractError("candidate_id required")
        if not self.family_id:
            raise schema.ContractError("family_id required")
        if self.split not in (TRAIN_SPLIT, CALIBRATION_SPLIT, TEST_SPLIT):
            raise schema.ContractError(f"unknown split {self.split!r}")
        if not isinstance(self.features, dict) or not self.features:
            raise schema.ContractError("features must be a non-empty dict")
        for key, value in self.features.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise schema.ContractError(
                    f"feature {key!r} must be numeric, got {value!r}"
                )
        if self.outcome not in _OUTCOMES:
            raise schema.ContractError(f"unknown outcome {self.outcome!r}")


def split_guard(items) -> None:
    """Raise if any held-out (test) item is present."""
    for item in items:
        if item.split == TEST_SPLIT:
            raise schema.ContractError(
                "held-out (test) candidates must never enter gate fitting or "
                "threshold selection"
            )


def _label(outcome: str) -> int:
    return 1 if outcome == _BENEFICIAL else 0


@dataclass
class GateClassifier:
    """Deterministic L2-regularised logistic regression (pure Python)."""

    feature_names: tuple = ()
    weights: tuple = ()
    bias: float = 0.0
    l2: float = 1.0
    mean: tuple = ()
    scale: tuple = ()
    fitted: bool = False

    def _standardise(self, features: dict) -> list[float]:
        return [
            (float(features[name]) - self.mean[i]) / self.scale[i]
            for i, name in enumerate(self.feature_names)
        ]

    def fit(self, items, *, l2: float = 1.0, seed: int = 0,
            iters: int = 500, lr: float = 0.1) -> "GateClassifier":
        split_guard(items)
        train = [it for it in items if it.split == TRAIN_SPLIT]
        if not train:
            raise schema.ContractError("no training-split items to fit on")
        names = tuple(sorted(train[0].features))
        for it in train:
            if tuple(sorted(it.features)) != names:
                raise schema.ContractError(
                    "every training item must carry the same feature set"
                )
        raw = [[float(it.features[n]) for n in names] for it in train]
        labels = [_label(it.outcome) for it in train]
        cols = list(zip(*raw))
        mean = [sum(c) / len(c) for c in cols]
        scale = [
            (max(1e-9, (sum((v - m) ** 2 for v in c) / len(c)) ** 0.5))
            for c, m in zip(cols, mean)
        ]
        x = [
            [(row[j] - mean[j]) / scale[j] for j in range(len(names))]
            for row in raw
        ]
        w = [0.0] * len(names)
        b = 0.0
        n = len(x)
        for _ in range(iters):
            grad_w = [0.0] * len(names)
            grad_b = 0.0
            for xi, yi in zip(x, labels):
                z = b + sum(wj * xij for wj, xij in zip(w, xi))
                pred = 1.0 / (1.0 + math.exp(-z))
                err = pred - yi
                for j in range(len(names)):
                    grad_w[j] += err * xi[j]
                grad_b += err
            for j in range(len(names)):
                grad_w[j] = grad_w[j] / n + l2 * w[j] / n
                w[j] -= lr * grad_w[j]
            b -= lr * grad_b / n
        self.feature_names = names
        self.weights = tuple(w)
        self.bias = b
        self.l2 = l2
        self.mean = tuple(mean)
        self.scale = tuple(scale)
        self.fitted = True
        return self

    def predict_proba(self, features: dict) -> float:
        if not self.fitted:
            raise schema.ContractError("classifier is not fitted")
        missing = [n for n in self.feature_names if n not in features]
        if missing:
            raise schema.ContractError(
                f"missing features for prediction: {missing}"
            )
        xi = self._standardise(features)
        z = self.bias + sum(w * v for w, v in zip(self.weights, xi))
        return 1.0 / (1.0 + math.exp(-z))

    def choose_threshold(self, calib_items, *, target: str = "min_harm",
                         grid: int = 101) -> float:
        split_guard(calib_items)
        calib = [it for it in calib_items if it.split == CALIBRATION_SPLIT]
        if not calib:
            raise schema.ContractError(
                "threshold selection needs calibration-split items only"
            )
        if any(it.split == TRAIN_SPLIT for it in calib_items):
            raise schema.ContractError(
                "train-split items must not reach choose_threshold"
            )
        best_t, best_score = 1.0, None
        for step in range(grid):
            t = step / (grid - 1)
            harmful = beneficial = 0
            for it in calib:
                if self.predict_proba(it.features) >= t:
                    if it.outcome == _HARMFUL:
                        harmful += 1
                    elif it.outcome == _BENEFICIAL:
                        beneficial += 1
            # minimise harm, then maximise beneficial accepts, then prefer the
            # MORE conservative (higher) threshold on a tie -- so a degenerate
            # calibration set with no beneficial/harmful items lands on
            # accept-nothing (t=1.0), not accept-everything.
            score = (harmful, -beneficial, -t)
            if best_score is None or score < best_score:
                best_score, best_t = score, t
        return best_t

    def to_dict(self) -> dict:
        return {
            "policy_version": POLICY_VERSION,
            "feature_names": list(self.feature_names),
            "weights": list(self.weights),
            "bias": self.bias,
            "l2": self.l2,
            "mean": list(self.mean),
            "scale": list(self.scale),
            "fitted": self.fitted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GateClassifier":
        return cls(
            feature_names=tuple(data["feature_names"]),
            weights=tuple(data["weights"]),
            bias=float(data["bias"]),
            l2=float(data.get("l2", 1.0)),
            mean=tuple(data["mean"]),
            scale=tuple(data["scale"]),
            fitted=bool(data.get("fitted", True)),
        )


@dataclass(frozen=True)
class CalibrationScreen:
    min_beneficial: int = 100
    min_harmful: int = 100
    min_families: int = 10

    def evaluate(self, calib_items) -> dict:
        calib = [it for it in calib_items if it.split == CALIBRATION_SPLIT]
        beneficial = sum(1 for it in calib if it.outcome == _BENEFICIAL)
        harmful = sum(1 for it in calib if it.outcome == _HARMFUL)
        families = {it.family_id for it in calib}
        stable = _leave_one_family_out_stable(calib)
        supported = (
            beneficial >= self.min_beneficial
            and harmful >= self.min_harmful
            and len(families) >= self.min_families
            and stable
        )
        return {
            "beneficial": beneficial,
            "harmful": harmful,
            "families": len(families),
            "leave_one_family_out_threshold_stable": stable,
            "supported": supported,
            "fallback": "rule_based_policy",
            "note": "operational minimum, not a statistical guarantee; enlarge "
                    "development/calibration data or keep a rule-based policy "
                    "if unmet",
        }


def _leave_one_family_out_stable(calib, *, tolerance: float = 0.15) -> bool:
    """A crude stability check: the beneficial fraction per held-out family
    must not swing by more than ``tolerance`` from the overall fraction."""
    if not calib:
        return False
    families = sorted({it.family_id for it in calib})
    if len(families) < 2:
        return False
    overall = sum(1 for it in calib if it.outcome == _BENEFICIAL) / len(calib)
    for family in families:
        rest = [it for it in calib if it.family_id != family]
        if not rest:
            return False
        frac = sum(1 for it in rest if it.outcome == _BENEFICIAL) / len(rest)
        if abs(frac - overall) > tolerance:
            return False
    return True


@dataclass(frozen=True)
class FrozenPolicy:
    classifier: dict
    threshold: float
    bank_hash: str
    config_hashes: dict
    policy_hash: str


@dataclass(frozen=True)
class Promotion:
    """The outcome of trying to promote a calibrated gate."""

    method: str              # "calibrated" | "rule_based_fallback"
    screen: dict
    classifier: GateClassifier | None
    threshold: float | None
    reason: str


def promote_calibrated_gate(classifier: GateClassifier, threshold: float,
                            calib_items, *,
                            screen: CalibrationScreen | None = None) -> Promotion:
    """Gate the calibrated method on the roadmap's calibration-event screen.

    Returns a ``calibrated`` promotion only when the screen is supported;
    otherwise a ``rule_based_fallback`` promotion (no classifier). This is the
    structural enforcement of S5's "insufficient class/family support blocks a
    calibrated-method promotion" -- callers must go through here, not construct
    a ``CalibratedGate`` directly on unscreened data.
    """
    result = (screen or CalibrationScreen()).evaluate(calib_items)
    if result["supported"]:
        return Promotion(
            method="calibrated", screen=result, classifier=classifier,
            threshold=float(threshold),
            reason="calibration-event screen supported",
        )
    return Promotion(
        method="rule_based_fallback", screen=result, classifier=None,
        threshold=None,
        reason="calibration-event screen not met; falling back to a "
               "rule-based policy (enlarge development/calibration data before "
               "presenting a learned gate)",
    )


def freeze_policy(classifier: GateClassifier, threshold: float, bank_hash: str,
                  path, *, config_hashes: dict | None = None,
                  screen: dict | None = None,
                  allow_unscreened: bool = False) -> Path:
    """Write the immutable frozen policy artifact BEFORE any final-test scoring.

    A calibrated policy may only be frozen when ``screen`` reports
    ``supported`` (or ``allow_unscreened=True`` is passed explicitly, e.g. to
    record a rule-based fallback policy). This prevents shipping a learned gate
    the calibration-event screen never cleared.
    """
    if not allow_unscreened:
        if not isinstance(screen, dict) or not screen.get("supported"):
            raise schema.ContractError(
                "refusing to freeze a calibrated policy without a supported "
                "calibration screen; pass screen=CalibrationScreen().evaluate("
                "calib_items) or allow_unscreened=True for a rule-based policy"
            )
    body = {
        "classifier": classifier.to_dict(),
        "threshold": float(threshold),
        "bank_hash": bank_hash,
        "config_hashes": dict(config_hashes or {}),
        "screen_supported": bool(screen.get("supported")) if isinstance(screen, dict) else False,
    }
    policy_hash = schema.record_hash(body)
    target = Path(path)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("policy_hash") != policy_hash:
            raise schema.ContractError(f"frozen policy is immutable: {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**body, "policy_hash": policy_hash}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(target)
    return target


def load_policy(path) -> FrozenPolicy:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    body = {k: data[k] for k in ("classifier", "threshold", "bank_hash",
                                 "config_hashes", "screen_supported")
            if k in data}
    if schema.record_hash(body) != data.get("policy_hash"):
        raise schema.ContractError("frozen policy hash mismatch")
    return FrozenPolicy(
        classifier=data["classifier"],
        threshold=data["threshold"],
        bank_hash=data["bank_hash"],
        config_hashes=data["config_hashes"],
        policy_hash=data["policy_hash"],
    )
