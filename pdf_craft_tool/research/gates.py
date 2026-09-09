"""Acceptance policies (gates) over one frozen candidate bank (S5).

Every gate consumes the SAME ``candidates.CandidateBank`` and emits one
``schema.Decision`` per candidate, so a better proposer can never be mistaken
for a better gate. Gates never read gold: ``decide`` screens its evidence with
``schema.assert_no_gold_fields``. Missing a required signal is an explicit
abstention, not a guess. "Do nothing" leaves the original bytes exactly.

Overlapping accepts are resolved deterministically: candidates are ordered by
``(start, end, candidate_id)``; once a span is accepted, any later candidate
that overlaps it is forced to ``reject`` with a fixed reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import schema

CONFLICT_POLICY = "reject_overlapping_accepts"
GATE_VERSION = "s5-1"


def _screen_evidence(evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        raise schema.ContractError("evidence must be a dict")
    schema.assert_no_gold_fields(evidence, context="gate evidence")
    return evidence


def _decision(candidate: dict, gate_id: str, *, action: str, reason: str,
              threshold=None, evidence_refs=()) -> schema.Decision:
    return schema.Decision(
        candidate_id=candidate["candidate_id"],
        gate_id=gate_id,
        gate_version=GATE_VERSION,
        threshold=threshold,
        action=action,
        reason=reason,
        original_retained=action != schema.DecisionAction.ACCEPT.value,
        evidence=tuple(evidence_refs),
    )


@dataclass
class Gate:
    gate_id: str
    _bank_hash: str = ""
    gate_version: str = GATE_VERSION
    required_signals: tuple = ()

    def bind(self, bank) -> "Gate":
        self._bank_hash = bank.stamp()
        return self

    def bank_hash(self) -> str:
        return self._bank_hash

    # -- overridable ----------------------------------------------------------
    def _evaluate(self, candidate: dict, evidence: dict):
        raise NotImplementedError

    def decide(self, candidate: dict, *, evidence: dict) -> schema.Decision:
        evidence = _screen_evidence(evidence)
        missing = [s for s in self.required_signals if s not in evidence]
        if missing:
            return _decision(
                candidate, self.gate_id,
                action="abstain",
                reason=f"missing evidence signal(s): {', '.join(sorted(missing))}",
            )
        return self._evaluate(candidate, evidence)

    def decide_bank(self, bank, *, evidence_by_candidate: dict) -> list:
        decisions = []
        for candidate in bank.candidates:
            evidence = evidence_by_candidate.get(candidate["candidate_id"], {})
            decisions.append(self.decide(candidate, evidence=evidence))
        return _resolve_conflicts(bank.candidates, decisions)


class UnchangedGate(Gate):
    """Never accepts anything; every decision abstains, bytes untouched."""

    def __init__(self):
        super().__init__(gate_id="unchanged")

    def _evaluate(self, candidate, evidence):
        return _decision(candidate, self.gate_id, action="abstain",
                         reason="unchanged policy never edits")


class EditSizeGate(Gate):
    def __init__(self, max_edit_size: int = 2):
        super().__init__(gate_id="edit_size", required_signals=("edit_size",))
        self.max_edit_size = max_edit_size

    def _evaluate(self, candidate, evidence):
        size = evidence["edit_size"]
        if size <= self.max_edit_size:
            return _decision(candidate, self.gate_id, action="accept",
                             reason=f"edit_size {size} <= {self.max_edit_size}",
                             threshold=float(self.max_edit_size))
        return _decision(candidate, self.gate_id, action="reject",
                         reason=f"edit_size {size} > {self.max_edit_size}",
                         threshold=float(self.max_edit_size))


class OcrConfidenceGate(Gate):
    """Accept when OCR confidence is LOW (the OCR is likely wrong there)."""

    def __init__(self, max_confidence: float = 70.0):
        super().__init__(gate_id="ocr_confidence",
                         required_signals=("ocr_confidence",))
        self.max_confidence = max_confidence

    def _evaluate(self, candidate, evidence):
        conf = evidence["ocr_confidence"]
        if conf < self.max_confidence:
            return _decision(candidate, self.gate_id, action="accept",
                             reason=f"ocr_confidence {conf} < {self.max_confidence}",
                             threshold=self.max_confidence)
        return _decision(candidate, self.gate_id, action="reject",
                         reason=f"ocr_confidence {conf} >= {self.max_confidence}",
                         threshold=self.max_confidence)


class AgreementGate(Gate):
    def __init__(self):
        super().__init__(gate_id="agreement",
                         required_signals=("alt_recognizer_agrees",))

    def _evaluate(self, candidate, evidence):
        if bool(evidence["alt_recognizer_agrees"]):
            return _decision(candidate, self.gate_id, action="accept",
                             reason="alternate recogniser agrees with the edit")
        return _decision(candidate, self.gate_id, action="reject",
                         reason="alternate recogniser does not agree")


class ExistingPolicyGate(Gate):
    """Adapter for B4: accept iff the Tesseract-crop verifier passed AND the
    edit is small (mirrors ConservativeProofreader + TesseractCropVerifier)."""

    def __init__(self, max_edit_size: int = 2):
        super().__init__(gate_id="existing_policy",
                         required_signals=("verifier_pass", "edit_size"))
        self.max_edit_size = max_edit_size

    def _evaluate(self, candidate, evidence):
        if bool(evidence["verifier_pass"]) and evidence["edit_size"] <= self.max_edit_size:
            return _decision(candidate, self.gate_id, action="accept",
                             reason="verifier passed and edit is within budget",
                             threshold=float(self.max_edit_size))
        return _decision(candidate, self.gate_id, action="reject",
                         reason="verifier failed or edit exceeds budget",
                         threshold=float(self.max_edit_size))


class CalibratedGate(Gate):
    """Uses a fitted ``calibration.GateClassifier`` + a locked threshold.

    Abstains when any classifier feature is missing (unknown evidence -> no
    edit), rather than imputing.
    """

    def __init__(self, classifier, threshold: float, *, feature_names=None):
        super().__init__(gate_id="calibrated")
        self.classifier = classifier
        self.threshold = float(threshold)
        self.feature_names = tuple(
            feature_names if feature_names is not None
            else classifier.feature_names
        )
        self.required_signals = self.feature_names

    def _evaluate(self, candidate, evidence):
        features = {name: evidence[name] for name in self.feature_names}
        proba = self.classifier.predict_proba(features)
        if proba >= self.threshold:
            return _decision(candidate, self.gate_id, action="accept",
                             reason=f"P(beneficial)={proba:.3f} >= {self.threshold:.3f}",
                             threshold=self.threshold)
        return _decision(candidate, self.gate_id, action="abstain",
                         reason=f"P(beneficial)={proba:.3f} < {self.threshold:.3f}",
                         threshold=self.threshold)


GATES = {
    "unchanged": UnchangedGate,
    "edit_size": EditSizeGate,
    "ocr_confidence": OcrConfidenceGate,
    "agreement": AgreementGate,
    "existing_policy": ExistingPolicyGate,
}


# ---------------------------------------------------------------------------
# Conflict resolution + application.
# ---------------------------------------------------------------------------

def _spans_overlap(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def _resolve_conflicts(candidates, decisions):
    by_id = {c["candidate_id"]: c for c in candidates}
    order = sorted(
        decisions,
        key=lambda d: (
            by_id[d.candidate_id]["start"],
            by_id[d.candidate_id]["end"],
            d.candidate_id,
        ),
    )
    accepted_spans: list[dict] = []
    resolved = {}
    for decision in order:
        candidate = by_id[decision.candidate_id]
        if decision.action == schema.DecisionAction.ACCEPT.value:
            if any(_spans_overlap(candidate, span) for span in accepted_spans):
                resolved[decision.candidate_id] = _decision(
                    candidate, decision.gate_id, action="reject",
                    reason="overlaps an earlier accepted edit",
                    threshold=decision.threshold,
                )
                continue
            accepted_spans.append(candidate)
        resolved[decision.candidate_id] = decision
    # preserve the original decision list order
    return [resolved[d.candidate_id] for d in decisions]


def apply_decisions(base_text: str, candidates, decisions) -> dict:
    by_id = {c["candidate_id"]: c for c in candidates}
    accepts = [
        by_id[d.candidate_id]
        for d in decisions
        if d.action == schema.DecisionAction.ACCEPT.value
    ]
    accepts.sort(key=lambda c: (c["start"], c["end"], c["candidate_id"]))
    # apply right-to-left so earlier offsets stay valid
    text = base_text
    applied = 0
    last_start = len(text) + 1
    for candidate in reversed(accepts):
        start, end = candidate["start"], candidate["end"]
        if end > last_start:  # would overlap an already-applied edit
            continue
        if base_text[start:end] != candidate["before"]:
            # the recorded 'before' must match the base slice exactly
            continue
        text = text[:start] + candidate["after"] + text[end:]
        last_start = start
        applied += 1
    counts = {"accept": 0, "abstain": 0, "reject": 0}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    return {
        "text": text,
        "accepted": counts["accept"],
        "applied": applied,
        "abstained": counts["abstain"],
        "rejected": counts["reject"],
        "unchanged": text == base_text,
    }
