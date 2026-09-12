"""Resumable SQLite storage for line-level gold labels plus game stats.

Decisions: ``tesseract`` (candidate A correct), ``qwen`` (candidate B
correct), ``manual`` (edited verified text), ``flag`` (unreadable, needs a
second look), ``skip`` (no label, no points).

Scoring is deliberately simple and single-player: every verified line earns
base XP, consecutive non-skip labels build a streak bonus, and levels are
fixed thresholds so progress survives restarts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Self

DECISIONS = frozenset({"tesseract", "qwen", "manual", "flag", "skip"})
VERIFIED_DECISIONS = frozenset({"tesseract", "qwen", "manual"})

BASE_XP = {"tesseract": 10, "qwen": 10, "manual": 25, "flag": 5, "skip": 0}
STREAK_BONUS_PER_LINE = 2
STREAK_BONUS_CAP = 20

LEVELS = (
    (0, "নবীন"),
    (100, "শিক্ষানবিশ"),
    (300, "অক্ষর-শিকারি"),
    (600, "পুঁথি-পাঠক"),
    (1000, "স্বর্ণ-মান"),
    (1500, "পণ্ডিত"),
    (2500, "মহাপণ্ডিত"),
)


class RevisionConflict(RuntimeError):
    """The browser tried to save an entry changed since it was loaded."""


class UnknownEntry(KeyError):
    """The requested entry is not part of this frozen manifest."""


def level_for(xp: int) -> str:
    name = LEVELS[0][1]
    for threshold, label in LEVELS:
        if xp >= threshold:
            name = label
    return name


class GoldStore:
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
                    revision INTEGER NOT NULL DEFAULT 1,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    xp INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reviews_decision ON reviews(decision);
                CREATE INDEX IF NOT EXISTS reviews_updated ON reviews(updated);
            """)
            for ordinal, entry in enumerate(entries):
                self.db.execute(
                    "INSERT OR IGNORE INTO entries(entry_id, ordinal, entry_json) VALUES(?,?,?)",
                    (entry["id"], ordinal, _stable_json(entry)),
                )

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT e.entry_json, r.verified_text, r.decision, "
                "r.revision, r.elapsed_ms, r.xp, r.updated FROM entries e "
                "LEFT JOIN reviews r ON r.entry_id=e.entry_id WHERE e.entry_id=?",
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
                    "revision": row["revision"],
                    "elapsed_ms": row["elapsed_ms"],
                    "xp": row["xp"],
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
        elapsed_ms: int = 0,
    ) -> dict[str, Any]:
        if not isinstance(verified_text, str) or len(verified_text) > 10_000:
            raise ValueError("verified_text must be a string up to 10000 characters")
        if decision not in DECISIONS:
            raise ValueError(f"Unknown decision: {decision}")
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if type(elapsed_ms) is not int or elapsed_ms < 0 or elapsed_ms > 3_600_000:
            raise ValueError("elapsed_ms must be between 0 and 3600000")
        verified_text = unicodedata.normalize("NFC", verified_text)
        if decision in VERIFIED_DECISIONS and not verified_text.strip():
            raise ValueError("verified lines must contain non-blank text")
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
                        f"Expected revision {revision}, current is {current_revision}"
                    )
                streak = self._current_streak_locked()
                bonus = (
                    min(STREAK_BONUS_CAP, (streak + 1) * STREAK_BONUS_PER_LINE)
                    if decision != "skip"
                    else 0
                )
                awarded = BASE_XP[decision] + bonus
                new_revision = revision + 1
                self.db.execute(
                    "INSERT INTO reviews(entry_id, verified_text, decision, revision, elapsed_ms, xp, updated) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET verified_text=excluded.verified_text, "
                    "decision=excluded.decision, revision=excluded.revision, elapsed_ms=excluded.elapsed_ms, "
                    "xp=excluded.xp, updated=excluded.updated",
                    (
                        entry_id,
                        verified_text,
                        decision,
                        new_revision,
                        elapsed_ms,
                        awarded,
                        now,
                    ),
                )
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise
            return self.get(entry_id)  # type: ignore[return-value]

    upsert_review = save

    def _ordered_decisions_locked(self) -> list[str]:
        return [
            row["decision"]
            for row in self.db.execute("SELECT decision FROM reviews ORDER BY updated ASC")
        ]

    def _current_streak_locked(self) -> int:
        streak = 0
        for row in self.db.execute(
            "SELECT decision FROM reviews ORDER BY updated DESC"
        ):
            if row["decision"] == "skip":
                break
            streak += 1
        return streak

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.db.execute("SELECT count(*) FROM entries").fetchone()[0]
            completed = self.db.execute("SELECT count(*) FROM reviews").fetchone()[0]
            verified = self.db.execute(
                "SELECT count(*) FROM reviews WHERE decision IN ('tesseract','qwen','manual')"
            ).fetchone()[0]
            xp = (
                self.db.execute("SELECT coalesce(sum(xp),0) FROM reviews").fetchone()[0]
            )
            counts = {
                row["decision"]: row["count"]
                for row in self.db.execute(
                    "SELECT decision, count(*) AS count FROM reviews GROUP BY decision ORDER BY decision"
                )
            }
            decisions = list(self._ordered_decisions_locked())
            best = current = 0
            for item in decisions:
                if item == "skip":
                    current = 0
                else:
                    current += 1
                    best = max(best, current)
            first = self.db.execute(
                "SELECT min(updated) AS first, max(updated) AS last FROM reviews"
            ).fetchone()
            lines_per_hour = 0.0
            if first["first"] and first["last"] and first["last"] > first["first"]:
                hours = (first["last"] - first["first"]) / 3600
                if hours > 0:
                    lines_per_hour = round(completed / hours, 1)
            remaining = total - completed
            return {
                "total": total,
                "completed": completed,
                "verified": verified,
                "remaining": remaining,
                "percent": round(completed * 100 / total, 1) if total else 100.0,
                "decisions": counts,
                "xp": xp,
                "level": level_for(xp),
                "streak": self._current_streak_locked(),
                "best_streak": best,
                "lines_per_hour": lines_per_hour,
            }

    def progress(self) -> dict[str, Any]:
        return self.stats()

    def next_unreviewed(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT entry_id FROM entries WHERE entry_id NOT IN (SELECT entry_id FROM reviews) "
                "ORDER BY ordinal LIMIT 1"
            ).fetchone()
            return self.get(row["entry_id"]) if row else None

    def export_rows(self, *, verified_only: bool) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT e.entry_json, e.ordinal, r.* FROM entries e LEFT JOIN reviews r ON r.entry_id=e.entry_id "
            if verified_only:
                query += "WHERE r.decision IN ('tesseract','qwen','manual') "
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
                        "revision": row["revision"],
                        "elapsed_ms": row["elapsed_ms"],
                        "xp": row["xp"],
                        "updated": row["updated"],
                    }
                )
                # Provenance for the future model benchmark: reference text
                # never enters OCR or model prompts; it only scores them.
                entry["benchmark"] = {
                    "reference": row["verified_text"]
                    if row["decision"] in ("tesseract", "qwen", "manual", None)
                    and row["verified_text"]
                    else None,
                    "tesseract": entry.get("tesseract", ""),
                    "qwen": entry.get("qwen", ""),
                    "page": entry.get("page"),
                    "bbox": entry.get("bbox"),
                    "source_sha256": entry.get("source_sha256"),
                }
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

    def export_tesstrain(
        self, out_dir: Path, cache_root: Path
    ) -> dict[str, Any]:
        """Write ``<stem>.png`` + ``<stem>.gt.txt`` pairs for verified lines.

        Line crops must already exist in ``cache_root`` (written by the server
        when each card was viewed). Missing crops are reported, not guessed.
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_root = Path(cache_root)
        written, missing = 0, []
        for index, row in enumerate(self.export_rows(verified_only=True)):
            review = row.get("review") or {}
            text = unicodedata.normalize(
                "NFC", str(review.get("verified_text", ""))
            ).strip()
            if not text:
                continue
            stem = f"{index:05d}_{row['id'][:8]}"
            image = cache_root / f"{row['id']}.png"
            if not image.is_file():
                missing.append(row["id"])
                continue
            (out_dir / f"{stem}.png").write_bytes(image.read_bytes())
            (out_dir / f"{stem}.gt.txt").write_text(text + "\n", encoding="utf-8")
            written += 1
        return {"written": written, "missing_crops": missing}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
