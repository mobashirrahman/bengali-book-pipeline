"""Blind independent double annotation with third-party adjudication (S2).

Two annotators transcribe each page from the frozen image only; the server
payload never carries OCR/model drafts, peer text, or agreement statistics.
A third party adjudicates every differing line before a page can export as
:class:`schema.GoldPage` with ``status="final"``.
"""

from __future__ import annotations

import enum
import json
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

from rapidfuzz.distance import Levenshtein

from pdf_craft_tool.research import schema


class AnnotationStatus(enum.Enum):
    ASSIGNED = "assigned"
    SUBMITTED = "submitted"
    IN_CONFLICT = "in_conflict"
    ADJUDICATED = "adjudicated"
    FINAL = "final"
    FLAGGED = "flagged"
    PROVISIONAL = "provisional"


_STATUS_VALUES = frozenset(member.value for member in AnnotationStatus)

#: Keys that must never appear (recursively) in an annotator-facing payload.
FORBIDDEN_IN_ANNOTATOR_PAYLOAD = frozenset({
    "tesseract",
    "qwen",
    "qwen_derived",
    "ocr",
    "ocr_text",
    "model",
    "candidate",
    "candidates",
    "other_annotator",
    "peer_text",
    "agreement",
    "verified_text",
    "context_proposed",
    "prediction",
})

TRIGGER_CHAR_DISAGREEMENT = 0.01
TRIGGER_EXACT_LINE_AGREEMENT = 0.95

#: Filenames this store refuses to open: production databases live elsewhere
#: and must never be touched by the annotation tool.
_RESERVED_DB_NAMES = frozenset({"gold.sqlite3", "queue.sqlite3"})


class RevisionConflict(RuntimeError):
    """An annotator/adjudicator saved against a stale revision."""


def _normalise_line(text: str) -> str:
    """NFC-normalise and collapse inner whitespace (guideline section 6)."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def _strip_forbidden(obj):
    """Deep-copy ``obj`` with every forbidden key removed."""
    if isinstance(obj, dict):
        return {
            key: _strip_forbidden(value)
            for key, value in obj.items()
            if key not in FORBIDDEN_IN_ANNOTATOR_PAYLOAD
        }
    if isinstance(obj, (list, tuple)):
        return [_strip_forbidden(value) for value in obj]
    return obj


def _assert_blind(payload: dict) -> None:
    """Raise if any forbidden key survives anywhere in ``payload``."""
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in FORBIDDEN_IN_ANNOTATOR_PAYLOAD:
                    raise schema.ContractError(
                        f"key {key!r} is forbidden in an annotator payload"
                    )
                stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _validate_page_seed(page: dict, position: int) -> dict:
    if not isinstance(page, dict):
        raise schema.ContractError(
            f"pages[{position}] must be a dict, got {type(page)}"
        )
    for key in ("page_id", "source_sha256", "image_sha256", "census"):
        if key not in page:
            raise schema.ContractError(
                f"pages[{position}] missing required key {key!r}"
            )
    if not schema._is_sha256(page["page_id"]):
        raise schema.ContractError(f"pages[{position}]['page_id'] must be sha256")
    if not schema._is_sha256(page["source_sha256"]):
        raise schema.ContractError(
            f"pages[{position}]['source_sha256'] must be sha256"
        )
    if not schema._is_sha256(page["image_sha256"]):
        raise schema.ContractError(
            f"pages[{position}]['image_sha256'] must be sha256"
        )
    census = page["census"]
    if not isinstance(census, dict) or not isinstance(
        census.get("entries"), list
    ):
        raise schema.ContractError(
            f"pages[{position}]['census'] must be a dict with an 'entries' list"
        )
    for entry in census["entries"]:
        if not isinstance(entry, dict):
            raise schema.ContractError(
                f"pages[{position}] census entries must be dicts"
            )
        index = entry.get("region_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise schema.ContractError(
                f"pages[{position}] census region_index must be a "
                "non-negative int"
            )
        schema.Geometry.from_dict(entry.get("geometry", {}))
    status = page.get("status", AnnotationStatus.ASSIGNED.value)
    if status not in _STATUS_VALUES:
        raise schema.ContractError(
            f"pages[{position}]['status'] must be one of "
            f"{sorted(_STATUS_VALUES)}, got {status!r}"
        )
    return {
        "page_id": page["page_id"],
        "source_sha256": page["source_sha256"],
        "image_sha256": page["image_sha256"],
        "census": dict(census),
        "status": status,
        "adjudicator_id": page.get("adjudicator_id", ""),
    }


class AnnotationStore:
    """SQLite-backed blind annotation state for frozen census pages."""

    def __init__(self, path: Path, *, pages: list[dict]):
        resolved = Path(path)
        if resolved.name in _RESERVED_DB_NAMES:
            raise ValueError(
                f"refusing to open production database {resolved.name}; "
                "the annotation store needs its own private SQLite file"
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.path = resolved
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(resolved), timeout=30,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema(pages)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "AnnotationStore":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    # -- schema ---------------------------------------------------------
    def _init_schema(self, pages) -> None:
        if not isinstance(pages, (list, tuple)):
            raise schema.ContractError("pages must be a list of dicts")
        seeds = [_validate_page_seed(page, position)
                 for position, page in enumerate(pages)]
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS pages (
                    page_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    census_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'assigned',
                    adjudicator_id TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    page_id TEXT NOT NULL REFERENCES pages(page_id)
                        ON DELETE CASCADE,
                    annotator_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    text_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'assigned',
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL,
                    PRIMARY KEY (page_id, annotator_id)
                );
                CREATE TABLE IF NOT EXISTS adjudications (
                    page_id TEXT NOT NULL REFERENCES pages(page_id)
                        ON DELETE CASCADE,
                    region_index INTEGER NOT NULL,
                    annotator_a TEXT NOT NULL,
                    annotator_b TEXT NOT NULL,
                    annotator_a_text TEXT NOT NULL,
                    annotator_b_text TEXT NOT NULL,
                    resolved_text TEXT NOT NULL DEFAULT '',
                    resolver_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL,
                    PRIMARY KEY (page_id, region_index)
                );
            """)
            now = time.time()
            for seed in seeds:
                self.db.execute(
                    "INSERT OR IGNORE INTO pages(page_id, source_sha256, "
                    "image_sha256, census_json, status, adjudicator_id, "
                    "updated) VALUES(?,?,?,?,?,?,?)",
                    (
                        seed["page_id"],
                        seed["source_sha256"],
                        seed["image_sha256"],
                        _stable_json(seed["census"]),
                        seed["status"],
                        seed["adjudicator_id"],
                        now,
                    ),
                )

    # -- internal helpers ------------------------------------------------
    def _page_row(self, page_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM pages WHERE page_id=?", (page_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown page {page_id!r}")
        return row

    @staticmethod
    def _census_of(row: sqlite3.Row) -> dict:
        return json.loads(row["census_json"])

    @staticmethod
    def _region_indices(census: dict) -> list[int]:
        return sorted(entry["region_index"] for entry in census["entries"])

    @staticmethod
    def _census_order(census: dict) -> list[int]:
        order = census.get("reading_order")
        indices = AnnotationStore._region_indices(census)
        if order is None:
            return list(indices)
        if sorted(order) != indices:
            raise schema.ContractError(
                "stored census reading_order is not a permutation of "
                "the region indices"
            )
        return list(order)

    @staticmethod
    def _check_image(row: sqlite3.Row, image_sha256) -> None:
        if image_sha256 is None:
            return
        if image_sha256 != row["image_sha256"]:
            raise ValueError("source image does not match the frozen page")

    @staticmethod
    def _require_annotator(annotator_id: str) -> str:
        if not isinstance(annotator_id, str) or not annotator_id:
            raise ValueError("annotator_id must be a non-empty string")
        return annotator_id

    def _submitted_rows(self, page_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM assignments WHERE page_id=? AND status IN "
            "('submitted','adjudicated') ORDER BY annotator_id",
            (page_id,),
        ))

    def _conflict_rows(self, page_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM adjudications WHERE page_id=? ORDER BY region_index",
            (page_id,),
        ))

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict:
        return {
            "page_id": row["page_id"],
            "region_index": row["region_index"],
            "annotator_a": row["annotator_a"],
            "annotator_b": row["annotator_b"],
            "annotator_a_text": row["annotator_a_text"],
            "annotator_b_text": row["annotator_b_text"],
            "resolved_text": row["resolved_text"],
            "resolver_id": row["resolver_id"],
            "reason": row["reason"],
            "revision": row["revision"],
            "updated": row["updated"],
        }

    # -- annotator flow ---------------------------------------------------
    def assign(self, page_id: str, annotator_id: str, *,
               image_sha256=None) -> dict:
        """Create an ASSIGNED assignment and return its blind payload."""
        self._require_annotator(annotator_id)
        with self._lock:
            row = self._page_row(page_id)
            self._check_image(row, image_sha256)
            census = self._census_of(row)
            now = time.time()
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO assignments(page_id, annotator_id, "
                    "revision, text_json, status, elapsed_ms, updated) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (page_id, annotator_id, 0, "{}", "assigned", 0, now),
                )
                current = self.db.execute(
                    "SELECT revision, status FROM assignments "
                    "WHERE page_id=? AND annotator_id=?",
                    (page_id, annotator_id),
                ).fetchone()
            payload = {
                "page_id": page_id,
                "image_sha256": row["image_sha256"],
                "census": _strip_forbidden(census),
                "line_slots": [
                    {
                        "region_index": entry["region_index"],
                        "geometry": entry["geometry"],
                    }
                    for entry in sorted(census["entries"],
                                        key=lambda item: item["region_index"])
                ],
                "revision": current["revision"],
            }
            _assert_blind(payload)
            return payload

    def submit(self, page_id: str, annotator_id: str, *, revision: int,
               lines: dict, elapsed_ms: int = 0, image_sha256=None) -> dict:
        """Save one annotator's transcription with optimistic concurrency."""
        self._require_annotator(annotator_id)
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if (type(elapsed_ms) is not int or elapsed_ms < 0
                or elapsed_ms > 3_600_000):
            raise ValueError("elapsed_ms must be between 0 and 3600000")
        if not isinstance(lines, dict) or not lines:
            raise ValueError("lines must be a non-empty dict")
        with self._lock:
            row = self._page_row(page_id)
            self._check_image(row, image_sha256)
            census = self._census_of(row)
            expected = set(self._region_indices(census))
            normalised: dict[int, str] = {}
            for key, value in lines.items():
                try:
                    index = int(key) if not isinstance(key, bool) else key
                except (TypeError, ValueError):
                    raise ValueError(
                        f"lines keys must be region indices, got {key!r}"
                    ) from None
                if not isinstance(index, int) or isinstance(index, bool):
                    raise ValueError(
                        f"lines keys must be region indices, got {key!r}"
                    )
                if not isinstance(value, str):
                    raise ValueError(
                        f"lines[{index}] must be a string, got {type(value)}"
                    )
                normalised[index] = unicodedata.normalize("NFC", value)
            if set(normalised) != expected:
                raise ValueError(
                    "lines must cover exactly the census region indices "
                    f"{sorted(expected)}, got {sorted(normalised)}"
                )
            with self.db:
                current = self.db.execute(
                    "SELECT revision FROM assignments "
                    "WHERE page_id=? AND annotator_id=?",
                    (page_id, annotator_id),
                ).fetchone()
                if current is None:
                    raise ValueError(
                        f"no assignment for annotator {annotator_id!r} on "
                        f"page {page_id!r}; call assign() first"
                    )
                if current["revision"] != revision:
                    raise RevisionConflict(
                        f"expected revision {revision}, current is "
                        f"{current['revision']}"
                    )
                now = time.time()
                self.db.execute(
                    "UPDATE assignments SET revision=?, text_json=?, "
                    "status='submitted', elapsed_ms=?, updated=? "
                    "WHERE page_id=? AND annotator_id=?",
                    (
                        revision + 1,
                        _stable_json({str(k): normalised[k]
                                      for k in sorted(normalised)}),
                        elapsed_ms,
                        now,
                        page_id,
                        annotator_id,
                    ),
                )
            return {
                "page_id": page_id,
                "annotator_id": annotator_id,
                "revision": revision + 1,
                "status": AnnotationStatus.SUBMITTED.value,
            }

    def peer_ready(self, page_id: str) -> bool:
        """True once at least two distinct annotators have submitted."""
        with self._lock:
            self._page_row(page_id)
            return len(self._submitted_rows(page_id)) >= 2

    # -- conflict handling --------------------------------------------------
    def detect_conflicts(self, page_id: str) -> list[dict]:
        """Diff two submissions line-by-line; one row per differing line.

        Comparison is NFC plus inner-whitespace collapse. Nothing is
        auto-resolved here; adjudication rows start unresolved.
        """
        with self._lock:
            self._page_row(page_id)
            submitted = self._submitted_rows(page_id)
            distinct = sorted({row["annotator_id"] for row in submitted})
            if len(distinct) < 2:
                raise ValueError(
                    "conflict detection needs two SUBMITTED assignments from "
                    "distinct annotators"
                )
            first, second = submitted[0], submitted[1]
            first_text = json.loads(first["text_json"])
            second_text = json.loads(second["text_json"])
            census = self._census_of(self._page_row(page_id))
            now = time.time()
            with self.db:
                for index in self._region_indices(census):
                    left = _normalise_line(first_text.get(str(index), ""))
                    right = _normalise_line(second_text.get(str(index), ""))
                    if left == right:
                        continue
                    self.db.execute(
                        "INSERT OR IGNORE INTO adjudications(page_id, "
                        "region_index, annotator_a, annotator_b, "
                        "annotator_a_text, annotator_b_text, resolved_text, "
                        "resolver_id, reason, revision, updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            page_id,
                            index,
                            first["annotator_id"],
                            second["annotator_id"],
                            unicodedata.normalize(
                                "NFC", first_text.get(str(index), "")),
                            unicodedata.normalize(
                                "NFC", second_text.get(str(index), "")),
                            "",
                            "",
                            "",
                            0,
                            now,
                        ),
                    )
                rows = self._conflict_rows(page_id)
                unresolved = [row for row in rows if not row["resolved_text"]]
                if unresolved:
                    self.db.execute(
                        "UPDATE pages SET status='in_conflict', updated=? "
                        "WHERE page_id=?",
                        (now, page_id),
                    )
                elif rows:
                    self.db.execute(
                        "UPDATE pages SET status='adjudicated', updated=? "
                        "WHERE page_id=?",
                        (now, page_id),
                    )
                else:
                    self.db.execute(
                        "UPDATE assignments SET status='adjudicated' "
                        "WHERE page_id=? AND status='submitted'",
                        (page_id,),
                    )
                    self.db.execute(
                        "UPDATE pages SET status='adjudicated', updated=? "
                        "WHERE page_id=?",
                        (now, page_id),
                    )
                return [self._row_dict(row) for row in rows]

    def list_conflicts(self, page_id: str) -> list[dict]:
        """Read the current adjudication rows without mutating anything."""
        with self._lock:
            self._page_row(page_id)
            return [self._row_dict(row) for row in self._conflict_rows(page_id)]

    def adjudicate(self, page_id: str, region_index: int, *,
                   resolved_text: str, resolver_id: str, reason: str,
                   revision: int) -> dict:
        """Resolve one conflict line as a third party (not an annotator)."""
        if not isinstance(resolver_id, str) or not resolver_id:
            raise ValueError("resolver_id must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("adjudication needs a non-empty reason")
        if not isinstance(resolved_text, str):
            raise ValueError("resolved_text must be a string")
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        with self._lock:
            self._page_row(page_id)
            row = self.db.execute(
                "SELECT * FROM adjudications WHERE page_id=? AND region_index=?",
                (page_id, region_index),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"no conflict item for region {region_index} on "
                    f"page {page_id!r}"
                )
            if resolver_id in (row["annotator_a"], row["annotator_b"]):
                raise ValueError(
                    "the adjudicator must be a third party, distinct from "
                    "both annotators"
                )
            if row["revision"] != revision:
                raise RevisionConflict(
                    f"expected revision {revision}, current is "
                    f"{row['revision']}"
                )
            now = time.time()
            with self.db:
                self.db.execute(
                    "UPDATE adjudications SET resolved_text=?, resolver_id=?, "
                    "reason=?, revision=?, updated=? "
                    "WHERE page_id=? AND region_index=?",
                    (
                        unicodedata.normalize("NFC", resolved_text),
                        resolver_id,
                        reason,
                        revision + 1,
                        now,
                        page_id,
                        region_index,
                    ),
                )
                self.db.execute(
                    "UPDATE pages SET adjudicator_id=?, updated=? "
                    "WHERE page_id=?",
                    (resolver_id, now, page_id),
                )
                remaining = self.db.execute(
                    "SELECT count(*) FROM adjudications "
                    "WHERE page_id=? AND resolved_text=''",
                    (page_id,),
                ).fetchone()[0]
                if remaining == 0:
                    self.db.execute(
                        "UPDATE pages SET status='adjudicated', updated=? "
                        "WHERE page_id=?",
                        (now, page_id),
                    )
            return self._row_dict(self.db.execute(
                "SELECT * FROM adjudications WHERE page_id=? AND region_index=?",
                (page_id, region_index),
            ).fetchone())

    # -- statistics ---------------------------------------------------------
    def disagreement_stats(self, page_id: str) -> dict:
        """Pairwise disagreement between the two submissions of a page."""
        with self._lock:
            self._page_row(page_id)
            submitted = self._submitted_rows(page_id)
            if len({row["annotator_id"] for row in submitted}) < 2:
                raise ValueError(
                    "disagreement statistics need two submissions from "
                    "distinct annotators"
                )
            first_text = json.loads(submitted[0]["text_json"])
            second_text = json.loads(submitted[1]["text_json"])
            census = self._census_of(self._page_row(page_id))
            indices = self._region_indices(census)
            total_edits = 0
            total_chars = 0
            equal = 0
            for index in indices:
                left = _normalise_line(first_text.get(str(index), ""))
                right = _normalise_line(second_text.get(str(index), ""))
                if left == right:
                    equal += 1
                # NFC character disagreement = Levenshtein edit distance over the
                # longer side (a proper CER-style ratio, per RESEARCH_PROTOCOL
                # section 6 -- not difflib's block-match ratio).
                total_edits += Levenshtein.distance(left, right)
                total_chars += max(len(left), len(right))
            total = len(indices)
            return {
                "pairwise_char_disagreement":
                    (total_edits / total_chars) if total_chars else 0.0,
                "exact_line_agreement": (equal / total) if total else 1.0,
                "lines": total,
            }

    # -- page status ----------------------------------------------------------
    def page_status(self, page_id: str) -> str:
        with self._lock:
            return self._page_row(page_id)["status"]

    def page_image_sha256(self, page_id: str) -> str:
        """Frozen image hash for a page (read-only; no transcribed text)."""
        with self._lock:
            return self._page_row(page_id)["image_sha256"]

    def set_status(self, page_id: str, status: str) -> str:
        """Record a page-level status (e.g. ``flagged`` / ``provisional``).

        ``flagged`` and ``provisional`` are terminal: once a page carries one it
        can never be moved back toward final export (the "can never enter final
        export" guarantee must not be silently reversible).
        """
        if status not in _STATUS_VALUES:
            raise ValueError(
                f"status must be one of {sorted(_STATUS_VALUES)}, "
                f"got {status!r}"
            )
        _terminal = (AnnotationStatus.FLAGGED.value,
                     AnnotationStatus.PROVISIONAL.value)
        with self._lock:
            current = self._page_row(page_id)["status"]
            if current in _terminal and status != current:
                raise ValueError(
                    f"page {page_id!r} is {current} and cannot be moved to "
                    f"{status!r}; that status is terminal"
                )
            with self.db:
                self.db.execute(
                    "UPDATE pages SET status=?, updated=? WHERE page_id=?",
                    (status, time.time(), page_id),
                )
            return status

    def progress(self) -> dict:
        """Counts only: safe to expose, never carries transcribed text."""
        with self._lock:
            total = self.db.execute("SELECT count(*) FROM pages").fetchone()[0]
            submitted = self.db.execute(
                "SELECT count(*) FROM assignments "
                "WHERE status IN ('submitted','adjudicated')"
            ).fetchone()[0]
            open_conflicts = self.db.execute(
                "SELECT count(*) FROM adjudications WHERE resolved_text=''"
            ).fetchone()[0]
            counts = {
                row["status"]: row["count"]
                for row in self.db.execute(
                    "SELECT status, count(*) AS count FROM pages "
                    "GROUP BY status ORDER BY status"
                )
            }
            return {
                "pages": total,
                "submitted_assignments": submitted,
                "open_conflicts": open_conflicts,
                "page_status": counts,
            }

    # -- export -------------------------------------------------------------
    def _merged_texts(self, page_id: str) -> tuple:
        """Merged per-region text, raising on any unresolved conflict."""
        page = self._page_row(page_id)
        submitted = self._submitted_rows(page_id)
        distinct = sorted({row["annotator_id"] for row in submitted})
        if len(distinct) < 2:
            raise ValueError(
                "final export needs two submissions from distinct annotators"
            )
        first_text = json.loads(submitted[0]["text_json"])
        second_text = json.loads(submitted[1]["text_json"])
        resolved = {
            row["region_index"]: row["resolved_text"]
            for row in self._conflict_rows(page_id)
            if row["resolved_text"]
        }
        census = self._census_of(page)
        merged = {}
        for index in self._region_indices(census):
            if index in resolved:
                merged[index] = resolved[index]
                continue
            left = _normalise_line(first_text.get(str(index), ""))
            right = _normalise_line(second_text.get(str(index), ""))
            if left != right:
                raise ValueError(
                    f"region {index} on page {page_id!r} differs between "
                    "annotators and has no adjudication"
                )
            merged[index] = unicodedata.normalize(
                "NFC", first_text.get(str(index), ""))
        return page, census, distinct, merged

    def finalize(self, page_id: str, *, adjudicator_id: str) -> schema.GoldPage:
        """Build the final :class:`schema.GoldPage` for a resolved page."""
        if not isinstance(adjudicator_id, str) or not adjudicator_id:
            raise ValueError("finalize needs a non-empty adjudicator_id")
        with self._lock:
            page = self._page_row(page_id)
            if page["status"] in (AnnotationStatus.FLAGGED.value,
                                  AnnotationStatus.PROVISIONAL.value):
                raise ValueError(
                    f"page {page_id!r} is {page['status']} and can never "
                    "enter final export"
                )
            page, census, annotators, merged = self._merged_texts(page_id)
            order = self._census_order(census)
            by_index = {entry["region_index"]: entry
                        for entry in census["entries"]}
            lines = []
            unreadable_regions = []
            for index in sorted(by_index):
                entry = by_index[index]
                text = merged[index]
                unreadable = bool(entry.get("unreadable")) or not text
                if unreadable:
                    text = ""
                    unreadable_regions.append({
                        "region_index": index,
                        "note": entry.get("note", ""),
                    })
                lines.append(schema.GoldLine(
                    line_index=index,
                    geometry=schema.Geometry.from_dict(entry["geometry"]),
                    text=text,
                    unreadable=unreadable,
                ))
            stats = self.disagreement_stats(page_id)
            elapsed = {
                row["annotator_id"]: row["elapsed_ms"]
                for row in self._submitted_rows(page_id)
            }
            return schema.GoldPage(
                page_id=page["page_id"],
                source_sha256=page["source_sha256"],
                image_sha256=page["image_sha256"],
                lines=tuple(lines),
                reading_order=tuple(order),
                annotator_ids=tuple(annotators),
                adjudicator_id=adjudicator_id,
                status="final",
                guideline_version="annot-1",
                disagreement_rate=stats["pairwise_char_disagreement"],
                unreadable_regions=tuple(unreadable_regions),
                revisions=tuple(
                    {"annotator_id": name, "elapsed_ms": elapsed.get(name, 0)}
                    for name in annotators
                ),
            )

    def export_final(self, page_ids: list[str], *,
                     adjudicator_id=None) -> list[dict]:
        """Return ``GoldPage.to_dict()`` for each fully resolvable page."""
        if not isinstance(page_ids, (list, tuple)) or not page_ids:
            raise ValueError("page_ids must be a non-empty list of strings")
        exported = []
        with self._lock:
            for page_id in page_ids:
                page = self._page_row(page_id)
                if page["status"] in (AnnotationStatus.FLAGGED.value,
                                      AnnotationStatus.PROVISIONAL.value):
                    raise ValueError(
                        f"page {page_id!r} is {page['status']} and can never "
                        "enter final export"
                    )
                resolver = adjudicator_id
                if resolver is None:
                    rows = [row for row in self._conflict_rows(page_id)
                            if row["resolved_text"]]
                    if rows:
                        resolver = rows[0]["resolver_id"]
                    elif page["adjudicator_id"]:
                        resolver = page["adjudicator_id"]
                if not resolver:
                    raise ValueError(
                        f"page {page_id!r} has no recorded adjudicator; "
                        "pass adjudicator_id explicitly"
                    )
                exported.append(
                    self.finalize(page_id, adjudicator_id=resolver).to_dict()
                )
        return exported


def promotion_blocked(pilot_stats: list[dict]) -> dict:
    """Gate main-study annotation on the pilot disagreement triggers.

    Blocked when mean ``pairwise_char_disagreement`` exceeds 1% or mean
    ``exact_line_agreement`` drops below 95%.
    """
    if not isinstance(pilot_stats, (list, tuple)):
        raise ValueError("pilot_stats must be a list of disagreement dicts")
    if not pilot_stats:
        return {"blocked": False, "reasons": []}
    try:
        char = [float(item["pairwise_char_disagreement"])
                for item in pilot_stats]
        exact = [float(item["exact_line_agreement"]) for item in pilot_stats]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid pilot_stats entries: {exc}") from exc
    mean_char = sum(char) / len(char)
    mean_exact = sum(exact) / len(exact)
    reasons = []
    if mean_char > TRIGGER_CHAR_DISAGREEMENT:
        reasons.append(
            f"mean pairwise character disagreement {mean_char:.4f} exceeds "
            f"trigger {TRIGGER_CHAR_DISAGREEMENT:.2f}"
        )
    if mean_exact < TRIGGER_EXACT_LINE_AGREEMENT:
        reasons.append(
            f"mean exact-line agreement {mean_exact:.4f} below trigger "
            f"{TRIGGER_EXACT_LINE_AGREEMENT:.2f}"
        )
    return {"blocked": bool(reasons), "reasons": reasons}
