"""Immutable candidate banks and disagreement proposals (S4).

A :class:`CandidateBank` freezes one proposer's candidates so every gate arm
is scored on byte-identical proposals: every gate arm must cite the bank's
``stamp()``. :func:`propose_from_disagreement` derives deterministic
candidates from pure string alignment between ``ocr_text`` and a pinned
alternate recognizer output -- no model, no network, stdlib only.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from . import schema


def _bank_hash(proposer_id: str, candidate_dicts: list[dict]) -> str:
    return schema.record_hash(
        {"proposer_id": proposer_id, "candidates": list(candidate_dicts)}
    )


def _sort_key(record: dict) -> tuple:
    return (
        record["page_id"],
        record["start"],
        record["end"],
        record["after"],
        record["candidate_id"],
    )


@dataclass(frozen=True)
class CandidateBank:
    """An immutable, hash-pinned set of candidate corrections."""

    bank_id: str
    proposer_id: str
    candidates: tuple
    bank_hash: str

    def __post_init__(self):
        if not isinstance(self.bank_id, str) or not self.bank_id:
            raise schema.ContractError("bank_id must be a non-empty string")
        if not isinstance(self.proposer_id, str) or not self.proposer_id:
            raise schema.ContractError("proposer_id must be a non-empty string")
        if isinstance(self.candidates, (dict, str, bytes)):
            raise schema.ContractError("candidates must be a list or tuple of dicts")
        try:
            records = tuple(self.candidates)
        except TypeError as exc:
            raise schema.ContractError(
                f"candidates must be a list or tuple of dicts: {exc}"
            ) from exc
        validated = []
        for record in records:
            if not isinstance(record, dict):
                raise schema.ContractError("candidates entries must be dicts")
            # Rejects gold fields and unknown keys; normalises defaults.
            validated.append(schema.Candidate.from_dict(record).to_dict())
        object.__setattr__(self, "candidates", tuple(validated))
        if not isinstance(self.bank_hash, str) or not self.bank_hash:
            raise schema.ContractError("bank_hash must be a non-empty string")
        expected = _bank_hash(self.proposer_id, list(self.candidates))
        if expected != self.bank_hash:
            raise schema.ContractError("bank_hash does not match bank contents")

    @classmethod
    def build(cls, proposer_id: str, candidate_records) -> "CandidateBank":
        """Validate, deterministically sort and freeze candidate records."""
        if not isinstance(proposer_id, str) or not proposer_id:
            raise schema.ContractError("proposer_id must be a non-empty string")
        if isinstance(candidate_records, (dict, str, bytes)):
            raise schema.ContractError(
                "candidate_records must be a list or tuple of dicts"
            )
        try:
            records = list(candidate_records)
        except TypeError as exc:
            raise schema.ContractError(
                f"candidate_records must be a list or tuple of dicts: {exc}"
            ) from exc
        validated = [schema.Candidate.from_dict(r).to_dict() for r in records]
        validated.sort(key=_sort_key)
        digest = _bank_hash(proposer_id, validated)
        return cls(
            bank_id=f"bank-{digest[:16]}",
            proposer_id=proposer_id,
            candidates=tuple(validated),
            bank_hash=digest,
        )

    def to_dict(self) -> dict:
        return {
            "bank_id": self.bank_id,
            "proposer_id": self.proposer_id,
            "candidates": [dict(record) for record in self.candidates],
            "bank_hash": self.bank_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateBank":
        if not isinstance(data, dict):
            raise schema.ContractError(
                f"CandidateBank must be built from a dict, got {type(data)}"
            )
        schema.assert_no_gold_fields(data, context="CandidateBank")
        known = {"bank_id", "proposer_id", "candidates", "bank_hash"}
        unknown = set(data) - known
        if unknown:
            raise schema.ContractError(
                f"unknown keys for CandidateBank: {sorted(unknown)}"
            )
        try:
            return cls(
                bank_id=data["bank_id"],
                proposer_id=data["proposer_id"],
                candidates=tuple(data["candidates"]),
                bank_hash=data["bank_hash"],
            )
        except KeyError as exc:
            raise schema.ContractError(
                f"CandidateBank missing required key {exc}"
            ) from exc

    def write(self, path) -> Path:
        """Write this bank immutably: refuse to overwrite differing content."""
        target = Path(path)
        if target.exists():
            existing = CandidateBank.load(target)
            if existing.bank_hash != self.bank_hash:
                raise schema.ContractError(
                    f"candidate bank is immutable: {target}"
                )
            if existing.to_dict() != self.to_dict():
                raise schema.ContractError(
                    f"candidate bank is immutable: {target}"
                )
            return target
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
    def load(cls, path) -> "CandidateBank":
        target = Path(path)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise schema.ContractError(
                f"cannot load candidate bank {target}: {exc}"
            ) from exc
        return cls.from_dict(data)

    def for_page(self, page_id: str) -> tuple[dict, ...]:
        """Return only this page's candidate dicts (bank order preserved)."""
        return tuple(r for r in self.candidates if r["page_id"] == page_id)

    def stamp(self) -> str:
        """Bank hash every gate arm must cite."""
        return self.bank_hash


def _token_spans(text: str) -> list[tuple[str, int, int]]:
    """Whitespace-tokenise ``text`` keeping character offsets."""
    spans = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        while index < length and not text[index].isspace():
            index += 1
        spans.append((text[start:index], start, index))
    return spans


def propose_from_disagreement(
    *,
    page_id: str,
    base_field: str,
    ocr_text: str,
    alt_text: str,
    proposer_id: str,
    bank_seed: str,
) -> list[dict]:
    """Propose candidates where OCR and a pinned alternate disagree.

    Pure ``difflib`` token alignment on the two strings; no model, no
    network, no gold access. ``base_field`` selects which input carries the
    candidate spans (``"ocr"`` or ``"alt"``). Returns validated
    ``schema.Candidate`` dicts in deterministic order.
    """
    if base_field not in ("ocr", "alt"):
        raise schema.ContractError(
            f"base_field must be 'ocr' or 'alt', got {base_field!r}"
        )
    for name, value in (
        ("ocr_text", ocr_text),
        ("alt_text", alt_text),
        ("proposer_id", proposer_id),
        ("bank_seed", bank_seed),
    ):
        if not isinstance(value, str) or not value:
            raise schema.ContractError(f"{name} must be a non-empty string")
    schema.assert_no_gold_fields(
        {"page_id": page_id, "ocr_text": ocr_text, "alt_text": alt_text},
        context="candidate proposal input",
    )
    proposal_bank_hash = schema.record_hash(
        {"proposer_id": proposer_id, "bank_seed": bank_seed}
    )
    base_text = ocr_text if base_field == "ocr" else alt_text
    ocr_tokens = [token for token, _, _ in _token_spans(ocr_text)]
    alt_tokens = [token for token, _, _ in _token_spans(alt_text)]
    base_spans = _token_spans(base_text)
    matcher = difflib.SequenceMatcher(None, ocr_tokens, alt_tokens, autojunk=False)

    proposals = []
    index = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if base_field == "ocr":
            base_lo, base_hi = i1, i2
            other_tokens = alt_tokens[j1:j2]
        else:
            base_lo, base_hi = j1, j2
            other_tokens = ocr_tokens[i1:i2]
        if base_lo < base_hi:
            start = base_spans[base_lo][1]
            end = base_spans[base_hi - 1][2]
        elif base_lo < len(base_spans):
            start = end = base_spans[base_lo][1]
        elif base_spans:
            start = end = base_spans[-1][2]
        else:
            start = end = 0
        before = base_text[start:end]
        after = " ".join(other_tokens)
        record = {
            "candidate_id": f"{proposer_id}-{bank_seed}-{index:04d}",
            "page_id": page_id,
            "base": base_field,
            "start": start,
            "end": end,
            "before": before,
            "after": after,
            "proposer_id": proposer_id,
            "evidence_refs": [f"{proposer_id}/{bank_seed}/span-{index:04d}"],
            "proposal_bank_hash": proposal_bank_hash,
        }
        # Validates offsets, insertion rules and the absence of gold fields.
        proposals.append(schema.Candidate.from_dict(record).to_dict())
        index += 1
    return proposals
