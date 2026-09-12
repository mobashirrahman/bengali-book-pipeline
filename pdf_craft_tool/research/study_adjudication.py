"""Blinded 5-voter adjudication for the OCR comparison study (P2).

Five independent voters (OCR engines / pipelines) transcribe each page
region; regions where all five agree are provisionally accepted, and every
other region goes to a human adjudicator who sees only anonymous ``A``..``E``
labels -- never voter ids, model ids, adapter ids, config hashes, raw
output, confidences, or resolver ids.

This module owns its own SQLite file (never the pilot annotation DB) and
reuses the blindness guardrails from
:mod:`pdf_craft_tool.research.annotation` (``_assert_blind``,
``RevisionConflict``).
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

from pdf_craft_tool.research.annotation import RevisionConflict, _assert_blind

#: Anonymous per-page candidate labels (one per expected voter).
LABELS = ("A", "B", "C", "D", "E")

#: Extra keys that must never appear (recursively) in an adjudicator payload,
#: on top of ``annotation.FORBIDDEN_IN_ANNOTATOR_PAYLOAD`` (checked by
#: calling ``_assert_blind`` first).
FORBIDDEN_IN_STUDY_PAYLOAD = frozenset({
    "voter_id",
    "voter",
    "system_id",
    "model_id",
    "adapter_id",
    "engine",
    "config_hash",
    "raw_output",
    "annotator_id",
    "annotator_a",
    "annotator_b",
    "resolver_id",
    "identity",
})

_OK = "ok"
_NO_OUTPUT = "no_output"


def _normalise_vote(text: str) -> str:
    """NFC-normalise, strip, and collapse inner whitespace (quorum rule)."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def _anonymous_candidate_lists_only(obj) -> bool:
    """True when ``obj`` is a list of anonymous ``{label,text,status}`` dicts."""
    return (
        isinstance(obj, list)
        and obj
        and all(
            isinstance(item, dict)
            and set(item) <= {"label", "text", "status"}
            for item in obj
        )
    )


def _without_anonymous_candidates(obj):
    """Deep-copy ``obj`` with anonymous A..E candidate lists removed.

    The adjudicator's anonymous ``candidates`` lists (``{label, text,
    status}`` dicts only) are the study's legitimate content and are
    distinct from the model-draft ``candidates`` banned from annotator
    payloads, so the whole key is dropped before delegating to
    :func:`annotation._assert_blind` (which flags the key name itself).
    Any other ``candidates`` value is left in place so ``_assert_blind``
    still flags it.
    """
    if isinstance(obj, dict):
        scrubbed = {}
        for key, value in obj.items():
            if (key == "candidates"
                    and _anonymous_candidate_lists_only(value)):
                continue
            scrubbed[key] = _without_anonymous_candidates(value)
        return scrubbed
    if isinstance(obj, (list, tuple)):
        return [_without_anonymous_candidates(value) for value in obj]
    return obj


#: Adjudicator free-text fields echoed in the blind adjudication response.
#: They carry the adjudicator's own words (never server-side identities),
#: so the identity-substring scan skips their values. Their keys are still
#: checked against the forbidden sets, and every other field -- including
#: voter-supplied candidate ``text`` -- is still substring-scanned.
BLIND_FREE_TEXT_KEYS = frozenset({"reason", "resolved_text"})


def _string_values_skipping_free_text(obj):
    """Yield string values, skipping ``BLIND_FREE_TEXT_KEYS`` entries."""
    stack = [(None, obj)]
    while stack:
        key, current = stack.pop()
        if isinstance(current, dict):
            for sub_key, value in current.items():
                stack.append((sub_key, value))
        elif isinstance(current, (list, tuple)):
            for value in current:
                stack.append((key, value))
        elif isinstance(current, str):
            if key not in BLIND_FREE_TEXT_KEYS:
                yield current


def _assert_study_blind(payload: dict, identity_strings) -> None:
    """Raise if ``payload`` leaks study identities.

    Extends :func:`annotation._assert_blind` (called on the payload minus
    its anonymous A..E candidate lists, which are the adjudicator's
    legitimate content) with the study's extra forbidden keys, and rejects
    any string value that contains any of the ``identity_strings`` (voter
    ids plus extra ids such as model ids), case-insensitively. Values under
    ``BLIND_FREE_TEXT_KEYS`` (the adjudicator's own ``reason`` /
    ``resolved_text`` echoes) are exempt from the substring scan; their
    keys are still checked.
    """
    _assert_blind(_without_anonymous_candidates(payload))
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in FORBIDDEN_IN_STUDY_PAYLOAD:
                    raise ValueError(
                        f"key {key!r} is forbidden in a study payload"
                    )
                stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    needles = [str(item).lower()
               for item in (identity_strings or []) if str(item)]
    if not needles:
        return
    for text in _string_values_skipping_free_text(payload):
        lowered = text.lower()
        for needle in needles:
            if needle and needle in lowered:
                raise ValueError(
                    "study payload leaks an identity string"
                )


class StudyVoteStore:
    """SQLite-backed blind 5-voter state for the OCR comparison study."""

    def __init__(self, path, *, voter_ids: tuple, label_seed=None):
        voters = tuple(voter_ids)
        if len(voters) != 5 or len(set(voters)) != 5:
            raise ValueError("voter_ids must be exactly 5 distinct ids")
        if not all(isinstance(item, str) and item for item in voters):
            raise ValueError("voter_ids must be non-empty strings")
        if label_seed is not None and (
                not isinstance(label_seed, str) or not label_seed):
            raise ValueError("label_seed must be a non-empty string")
        resolved = Path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.path = resolved
        self.voter_ids = voters
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(resolved), timeout=30,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS votes (
                    page_id TEXT NOT NULL,
                    region_index INTEGER NOT NULL,
                    voter_id TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    failure_state TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL,
                    PRIMARY KEY (page_id, region_index, voter_id)
                );
                CREATE TABLE IF NOT EXISTS page_labels (
                    page_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    voter_id TEXT NOT NULL,
                    PRIMARY KEY (page_id, label)
                );
                CREATE TABLE IF NOT EXISTS resolutions (
                    page_id TEXT NOT NULL,
                    region_index INTEGER NOT NULL,
                    resolved_text TEXT NOT NULL DEFAULT '',
                    resolver_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    choice_label TEXT NOT NULL DEFAULT '',
                    chosen_voter TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL,
                    PRIMARY KEY (page_id, region_index)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
            """)
        #: Seed for the anonymous A..E labels. Explicit when passed in;
        #: otherwise generated once (``secrets.token_hex(32)``) and stored
        #: in the ``meta`` table so later starts reuse it. Never exposed
        #: via any route, log line, or error.
        if label_seed is not None:
            self.label_seed = label_seed
        else:
            self.label_seed = self._load_or_create_seed()

    def _load_or_create_seed(self) -> str:
        """Return the persisted label seed, generating it on first start."""
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='label_seed'").fetchone()
            if row is not None and row["value"]:
                return row["value"]
            seed = secrets.token_hex(32)
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO meta(key, value) "
                    "VALUES('label_seed', ?)", (seed,))
                row = self.db.execute(
                    "SELECT value FROM meta WHERE key='label_seed'"
                ).fetchone()
            return row["value"]

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "StudyVoteStore":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    # -- votes -----------------------------------------------------------
    def record_votes(self, page_id: str, region_index: int,
                     votes: dict) -> None:
        """Upsert one region's votes (idempotent).

        ``votes`` maps ``voter_id`` to ``{"text": str, "failure_state": str}``.
        Unknown voter ids raise ``ValueError``. Text is stored
        NFC-normalised.
        """
        if not isinstance(page_id, str) or not page_id:
            raise ValueError("page_id must be a non-empty string")
        if (type(region_index) is bool
                or not isinstance(region_index, int)):
            raise ValueError("region_index must be an integer")
        if not isinstance(votes, dict) or not votes:
            raise ValueError("votes must be a non-empty dict")
        rows = []
        for voter_id, vote in votes.items():
            if voter_id not in self.voter_ids:
                raise ValueError(f"unknown voter {voter_id!r}")
            if not isinstance(vote, dict):
                raise ValueError(
                    f"vote for {voter_id!r} must be a dict")
            text = vote.get("text", "")
            failure = vote.get("failure_state", "")
            if not isinstance(text, str):
                raise ValueError(
                    f"text for {voter_id!r} must be a string")
            if not isinstance(failure, str) or not failure:
                raise ValueError(
                    f"failure_state for {voter_id!r} must be a "
                    "non-empty string")
            rows.append((voter_id, unicodedata.normalize("NFC", text),
                         failure))
        now = time.time()
        with self._lock, self.db:
            for voter_id, text, failure in rows:
                self.db.execute(
                    "INSERT INTO votes(page_id, region_index, voter_id, "
                    "text, failure_state, updated) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(page_id, region_index, voter_id) "
                    "DO UPDATE SET text=excluded.text, "
                    "failure_state=excluded.failure_state, "
                    "updated=excluded.updated",
                    (page_id, region_index, voter_id, text, failure, now),
                )

    def _votes_for(self, page_id: str) -> dict:
        """``{region_index: {voter_id: {"text", "failure_state"}}}``."""
        rows = self.db.execute(
            "SELECT region_index, voter_id, text, failure_state FROM votes "
            "WHERE page_id=?", (page_id,))
        grouped: dict[int, dict] = {}
        for row in rows:
            grouped.setdefault(row["region_index"], {})[row["voter_id"]] = {
                "text": row["text"],
                "failure_state": row["failure_state"],
            }
        return grouped

    # -- triage ----------------------------------------------------------
    def _region_status(self, region_votes: dict) -> tuple[str, str]:
        """``(status, agreed_text)`` for one region's votes."""
        if any(voter not in region_votes for voter in self.voter_ids):
            return "conflict", ""
        if any(info["failure_state"] != _OK
               for info in region_votes.values()):
            return "conflict", ""
        normalised = {_normalise_vote(region_votes[voter]["text"])
                      for voter in self.voter_ids}
        if len(normalised) == 1:
            return "provisional_accept", next(iter(normalised))
        return "conflict", ""

    def triage(self, page_id: str) -> dict:
        """Split a page's regions into unanimous vs conflict.

        Quorum needs every expected voter present with
        ``failure_state == "ok"`` and equal normalised texts (NFC + strip +
        collapse inner whitespace). Anything else -- 4-1, 3-2, 2-2-1, any
        failure, any missing voter -- is a conflict.
        """
        with self._lock:
            grouped = self._votes_for(page_id)
            accept = 0
            conflict = 0
            for region_votes in grouped.values():
                status, _ = self._region_status(region_votes)
                if status == "provisional_accept":
                    accept += 1
                else:
                    conflict += 1
            return {
                "provisional_accept": accept,
                "conflict": conflict,
                "regions": accept + conflict,
            }

    # -- anonymous labels ------------------------------------------------
    def labels_for(self, page_id: str) -> dict[str, str]:
        """Stable per-page ``{label: voter_id}`` permutation of ``A``..``E``.

        Derived from ``sha256(label_seed + page_id)`` and stored server-side,
        so it is stable per page and differs across pages.
        """
        with self._lock:
            stored = list(self.db.execute(
                "SELECT label, voter_id FROM page_labels WHERE page_id=?",
                (page_id,)))
            if stored:
                return {row["label"]: row["voter_id"] for row in stored}
            digest = hashlib.sha256(
                (self.label_seed + page_id).encode("utf-8")).digest()
            order = list(self.voter_ids)
            random.Random(int.from_bytes(digest[:8], "big")).shuffle(order)
            mapping = {label: voter
                       for label, voter in zip(LABELS, order)}
            with self.db:
                for label, voter in mapping.items():
                    self.db.execute(
                        "INSERT OR IGNORE INTO page_labels(page_id, label, "
                        "voter_id) VALUES(?,?,?)",
                        (page_id, label, voter),
                    )
            return dict(mapping)

    def _revision_of(self, page_id: str, region_index: int) -> int:
        row = self.db.execute(
            "SELECT revision FROM resolutions WHERE page_id=? AND "
            "region_index=?", (page_id, region_index)).fetchone()
        return row["revision"] if row is not None else 0

    def _resolved(self, page_id: str) -> set[int]:
        # Any adjudicated row counts (revision >= 1): an empty resolved
        # text is a legitimate verdict ("region holds no text", e.g. noise).
        return {row["region_index"] for row in self.db.execute(
            "SELECT region_index FROM resolutions WHERE page_id=? AND "
            "revision>0", (page_id,))}

    # -- blind adjudicator payload ---------------------------------------
    def blind_conflicts(self, page_id: str) -> dict:
        """Conflict items with anonymous labels (no voter/model identities)."""
        with self._lock:
            grouped = self._votes_for(page_id)
            labels = self.labels_for(page_id)
            items = []
            for region_index in sorted(grouped):
                status, _ = self._region_status(grouped[region_index])
                if status != "conflict":
                    continue
                region_votes = grouped[region_index]
                candidates = []
                for label in sorted(labels):
                    voter = labels[label]
                    info = region_votes.get(voter)
                    if info is not None and info["failure_state"] == _OK:
                        candidates.append({
                            "label": label,
                            "text": info["text"],
                            "status": _OK,
                        })
                    else:
                        candidates.append({
                            "label": label,
                            "text": "",
                            "status": _NO_OUTPUT,
                        })
                items.append({
                    "region_index": region_index,
                    "revision": self._revision_of(page_id, region_index),
                    "candidates": candidates,
                })
            payload = {"page_id": page_id, "items": items}
            self._check_blind(payload)
            return payload

    def _check_blind(self, payload: dict) -> None:
        _assert_study_blind(payload, self._identity_strings())

    def _identity_strings(self) -> list[str]:
        return list(self.voter_ids)

    def page_ids(self) -> list[str]:
        """Sorted page ids present in the votes store."""
        with self._lock:
            return [row["page_id"] for row in self.db.execute(
                "SELECT DISTINCT page_id FROM votes ORDER BY page_id")]

    def open_conflicts(self, page_id: str) -> int:
        """Count of conflict regions on ``page_id`` not yet adjudicated."""
        with self._lock:
            grouped = self._votes_for(page_id)
            done = self._resolved(page_id)
            count = 0
            for region_index, region_votes in grouped.items():
                status, _ = self._region_status(region_votes)
                if status == "conflict" and region_index not in done:
                    count += 1
            return count

    # -- adjudication -----------------------------------------------------
    def adjudicate(self, page_id: str, region_index: int, *,
                   resolved_text: str, resolver_id: str, reason: str,
                   revision: int, choice_label=None) -> dict:
        """Resolve one conflict region as a non-voter third party."""
        if not isinstance(resolved_text, str):
            raise ValueError("resolved_text must be a string")
        if not isinstance(resolver_id, str) or not resolver_id:
            raise ValueError("resolver_id must be a non-empty string")
        if resolver_id in self.voter_ids:
            raise ValueError(
                "the adjudicator must be distinct from every voter")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("adjudication needs a non-empty reason")
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if choice_label is not None and choice_label not in LABELS:
            raise ValueError(
                f"choice_label must be one of {list(LABELS)} or None")
        if (type(region_index) is bool
                or not isinstance(region_index, int)):
            raise ValueError("region_index must be an integer")
        with self._lock:
            grouped = self._votes_for(page_id)
            if region_index not in grouped:
                raise ValueError(
                    f"no votes for region {region_index} on "
                    f"page {page_id!r}")
            status, _ = self._region_status(grouped[region_index])
            if status != "conflict":
                raise ValueError(
                    f"region {region_index} on page {page_id!r} is not "
                    "a conflict and cannot be adjudicated")
            labels = self.labels_for(page_id)
            chosen_voter = ""
            if choice_label is not None:
                chosen_voter = labels[choice_label]
            current = self.db.execute(
                "SELECT revision FROM resolutions WHERE page_id=? AND "
                "region_index=?", (page_id, region_index)).fetchone()
            current_revision = current["revision"] if current else 0
            if current_revision != revision:
                raise RevisionConflict(
                    f"expected revision {revision}, current is "
                    f"{current_revision}")
            now = time.time()
            clean_text = unicodedata.normalize("NFC", resolved_text)
            row = {
                "page_id": page_id,
                "region_index": region_index,
                "resolved_text": clean_text,
                "reason": reason,
                "choice_label": choice_label,
                "revision": revision + 1,
                "status": "adjudicated",
            }
            # Write and blindness-check in one transaction: a failing check
            # rolls the write back, so the revision never advances on a
            # blindness failure.
            with self.db:
                self.db.execute(
                    "INSERT INTO resolutions(page_id, region_index, "
                    "resolved_text, resolver_id, reason, choice_label, "
                    "chosen_voter, revision, updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(page_id, region_index) DO UPDATE SET "
                    "resolved_text=excluded.resolved_text, "
                    "resolver_id=excluded.resolver_id, "
                    "reason=excluded.reason, "
                    "choice_label=excluded.choice_label, "
                    "chosen_voter=excluded.chosen_voter, "
                    "revision=excluded.revision, "
                    "updated=excluded.updated",
                    (page_id, region_index, clean_text, resolver_id,
                     reason, choice_label or "", chosen_voter,
                     revision + 1, now),
                )
                self._check_blind(row)
            return row

    # -- offline analysis (never routed) -----------------------------------
    def unblinded_export(self, page_id: str) -> dict:
        """Full per-region votes, labels and resolutions for offline analysis.

        Never served over HTTP; the adjudicator payloads stay blind.
        """
        with self._lock:
            grouped = self._votes_for(page_id)
            labels = self.labels_for(page_id)
            inverted = {voter: label for label, voter in labels.items()}
            resolutions = {
                row["region_index"]: {
                    "resolved_text": row["resolved_text"],
                    "resolver_id": row["resolver_id"],
                    "reason": row["reason"],
                    "choice_label": row["choice_label"] or None,
                    "chosen_voter_id": row["chosen_voter"] or None,
                    "revision": row["revision"],
                }
                for row in self.db.execute(
                    "SELECT * FROM resolutions WHERE page_id=?", (page_id,))
            }
            items = []
            for region_index in sorted(grouped):
                status, agreed = self._region_status(grouped[region_index])
                items.append({
                    "region_index": region_index,
                    "status": status,
                    "agreed_text": agreed,
                    "labels": dict(inverted),
                    "votes": {
                        voter: dict(grouped[region_index].get(
                            voter, {"text": "", "failure_state": "missing"}))
                        for voter in self.voter_ids
                    },
                    "resolution": resolutions.get(region_index),
                })
            return {
                "page_id": page_id,
                "labels": dict(labels),
                "triage": self.triage(page_id),
                "items": items,
            }


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
