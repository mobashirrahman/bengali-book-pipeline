"""Local, resumable SQLite storage for human OCR review labels."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Self

DECISIONS = frozenset({"keep", "apply", "manual", "flag", "skip"})


class RevisionConflict(RuntimeError):
    """The browser tried to save an entry changed by another reviewer."""


class UnknownEntry(KeyError):
    """The requested entry is not part of this frozen manifest."""


class ReviewStore:
    def __init__(
        self, path: Path, entries: list[dict[str, Any]] | tuple[dict[str, Any], ...]
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema(entries)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _init_schema(self, entries) -> None:
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    entry_id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, entry_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    entry_id TEXT PRIMARY KEY REFERENCES entries(entry_id) ON DELETE CASCADE,
                    verified_text TEXT NOT NULL, decision TEXT NOT NULL,
                    accepted_edits_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reviews_decision ON reviews(decision);
            """)
            for ordinal, entry in enumerate(entries):
                self.db.execute(
                    "INSERT OR IGNORE INTO entries(entry_id, ordinal, entry_json) VALUES(?,?,?)",
                    (entry["id"], ordinal, _stable_json(entry)),
                )

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT e.entry_json, r.verified_text, r.decision, r.accepted_edits_json, "
                "r.revision, r.updated FROM entries e LEFT JOIN reviews r ON r.entry_id=e.entry_id "
                "WHERE e.entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                return None
            entry = json.loads(row["entry_json"])
            entry["review"] = (
                None
                if row["decision"] is None
                else {
                    "verified_text": row["verified_text"],
                    "decision": row["decision"],
                    "accepted_edits": json.loads(row["accepted_edits_json"]),
                    "revision": row["revision"],
                    "updated": row["updated"],
                }
            )
            return entry

    def save(
        self,
        entry_id: str,
        *,
        verified_text: str,
        decision: str,
        revision: int,
        accepted_edits: list[Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(verified_text, str) or len(verified_text) > 100_000:
            raise ValueError(
                "verified_text must be a string no longer than 100000 characters"
            )
        if decision not in DECISIONS:
            raise ValueError(f"Unknown decision: {decision}")
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if accepted_edits is None:
            accepted_edits = []
        if not isinstance(accepted_edits, list):
            raise TypeError("accepted_edits must be a list")
        # Only a deliberate model-accept action can carry accepted model edits.
        # Keep/flag/skip/manual are human outcomes; copying the model's full
        # decision list into those rows would make rejected suggestions look
        # like gold corrections during later benchmarks.
        if decision != "apply":
            accepted_edits = []
        else:
            accepted_edits = [
                item
                for item in accepted_edits
                if isinstance(item, dict) and item.get("status") == "accepted"
            ]
        encoded_edits = _stable_json(accepted_edits)
        now = time.time()
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                if (
                    self.db.execute(
                        "SELECT 1 FROM entries WHERE entry_id=?", (entry_id,)
                    ).fetchone()
                    is None
                ):
                    self.db.rollback()
                    raise UnknownEntry(entry_id)
                current = self.db.execute(
                    "SELECT revision FROM reviews WHERE entry_id=?", (entry_id,)
                ).fetchone()
                current_revision = current["revision"] if current else 0
                if current_revision != revision:
                    self.db.rollback()
                    raise RevisionConflict(
                        f"Expected revision {revision}, current revision is {current_revision}"
                    )
                new_revision = revision + 1
                self.db.execute(
                    "INSERT INTO reviews(entry_id, verified_text, decision, accepted_edits_json, revision, updated) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET verified_text=excluded.verified_text, "
                    "decision=excluded.decision, accepted_edits_json=excluded.accepted_edits_json, "
                    "revision=excluded.revision, updated=excluded.updated",
                    (
                        entry_id,
                        verified_text,
                        decision,
                        encoded_edits,
                        new_revision,
                        now,
                    ),
                )
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise
            return self.get(entry_id)  # type: ignore[return-value]

    upsert_review = save

    def progress(self) -> dict[str, Any]:
        with self._lock:
            total = self.db.execute("SELECT count(*) FROM entries").fetchone()[0]
            completed = self.db.execute("SELECT count(*) FROM reviews").fetchone()[0]
            counts = {
                row["decision"]: row["count"]
                for row in self.db.execute(
                    "SELECT decision, count(*) AS count FROM reviews GROUP BY decision ORDER BY decision"
                )
            }
            return {
                "total": total,
                "completed": completed,
                "remaining": total - completed,
                "percent": round(completed * 100 / total, 1) if total else 100.0,
                "decisions": counts,
            }

    def next_unreviewed(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT entry_id FROM entries WHERE entry_id NOT IN (SELECT entry_id FROM reviews) "
                "ORDER BY ordinal LIMIT 1"
            ).fetchone()
            return self.get(row["entry_id"]) if row else None

    def export_rows(self, *, verified_only: bool) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT e.entry_json, r.* FROM entries e LEFT JOIN reviews r ON r.entry_id=e.entry_id "
            if verified_only:
                query += "WHERE r.decision IN ('keep','apply','manual') "
            query += "ORDER BY e.ordinal"
            rows = []
            for row in self.db.execute(query):
                entry = json.loads(row["entry_json"])
                entry["review"] = (
                    None
                    if row["decision"] is None
                    else {
                        "verified_text": row["verified_text"],
                        "decision": row["decision"],
                        "accepted_edits": json.loads(row["accepted_edits_json"]),
                        "revision": row["revision"],
                        "updated": row["updated"],
                    }
                )
                rows.append(entry)
            return rows

    def export_jsonl(self, path: Path, *, verified_only: bool = True) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in self.export_rows(verified_only=verified_only):
                stream.write(_stable_json(row) + "\n")
        temporary.replace(path)
        return path

    def export_verified(self, path: Path) -> Path:
        return self.export_jsonl(path, verified_only=True)

    def export_all(self, path: Path) -> Path:
        return self.export_jsonl(path, verified_only=False)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
