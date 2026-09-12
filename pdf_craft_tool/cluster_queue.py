"""Coordinator-local durable book queue. Never put this SQLite database on NFS."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time


class BookQueue:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
                path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, stable_since REAL,
                sha256 TEXT, profile TEXT, present INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, profile TEXT NOT NULL,
                source TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
                host TEXT, attempts INTEGER DEFAULT 0, updated REAL, retry_at REAL DEFAULT 0,
                error TEXT, result TEXT, UNIQUE(sha256, profile));
            CREATE TABLE IF NOT EXISTS nodes (
                host TEXT PRIMARY KEY, state TEXT, detail TEXT, updated REAL);
            CREATE INDEX IF NOT EXISTS idx_sources_sha256_present ON sources(sha256, present);
            CREATE INDEX IF NOT EXISTS idx_jobs_profile_state ON jobs(profile, state);
        """)

    def close(self):
        self.db.close()

    def scan(self, source_root: Path, profile: str, settle_seconds: float = 120,
             now: float | None = None) -> dict:
        """Observe stable files twice; hash only additions or modifications.

        Path aliases of the same content share one job. A replacement at an old
        path gets a new identity. A changed file is never uploaded as an old job.
        """
        now = time.time() if now is None else now
        seen = set()
        added = 0
        batched = 0
        for path in sorted(source_root.rglob("*")):
            if path.suffix.lower() != ".pdf" or path.is_symlink() or not path.is_file():
                continue
            path = path.resolve()
            key = str(path)
            seen.add(key)
            stat = path.stat()
            row = self.db.execute("SELECT * FROM sources WHERE path=?", (key,)).fetchone()
            signature = (stat.st_size, stat.st_mtime_ns)
            if row is None or (row["size"], row["mtime"]) != signature:
                self.db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,1)",
                                (key, *signature, now, None, None))
                batched += 1
                if batched >= 500:
                    self.db.commit()
                    batched = 0
                continue
            self.db.execute("UPDATE sources SET present=1 WHERE path=?", (key,))
            batched += 1
            if batched >= 500:
                self.db.commit()
                batched = 0
            if now - row["stable_since"] < settle_seconds or stat.st_size == 0:
                continue
            if row["sha256"] and row["profile"] == profile:
                continue
            # Hashing a large upload must not hold the SQLite writer lock.
            self.db.commit()
            batched = 0
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != signature:
                continue
            job_id = hashlib.sha256(f"{digest}:{profile}".encode()).hexdigest()
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO jobs(id,sha256,profile,source,title,state,updated) VALUES(?,?,?,?,?,'pending',?)",
                (job_id, digest, profile, key, path.stem, now))
            added += cursor.rowcount
            self.db.execute("UPDATE sources SET sha256=?,profile=? WHERE path=?", (digest, profile, key))
            self.db.commit()
        for row in self.db.execute("SELECT path FROM sources").fetchall():
            if row["path"] not in seen:
                self.db.execute("UPDATE sources SET present=0 WHERE path=?", (row["path"],))
        self.db.commit()
        return {"pdf_files": len(seen), "new_jobs": added}

    def claim(self, host: str, profile: str) -> dict | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM jobs WHERE host=? AND state='running'", (host,)).fetchone()
            if row:
                return dict(row)
            # Prefer smaller books so fresh deployments produce reviewable results early.
            row = self.db.execute("""SELECT j.* FROM jobs j WHERE j.profile=?
                AND j.state IN ('pending','retry') AND j.retry_at<=?
                AND EXISTS(SELECT 1 FROM sources s WHERE s.sha256=j.sha256 AND s.present=1)
                ORDER BY (SELECT min(size) FROM sources s WHERE s.sha256=j.sha256), j.updated LIMIT 1""",
                (profile, time.time())).fetchone()
            if not row:
                return None
            alias = self.db.execute("SELECT path FROM sources WHERE sha256=? AND present=1 LIMIT 1", (row["sha256"],)).fetchone()
            self.db.execute("UPDATE jobs SET state='running',host=?,source=?,attempts=attempts+1,updated=?,error=NULL WHERE id=?",
                            (host, alias["path"], time.time(), row["id"]))
            return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def finish(self, job_id: str, host: str, result: dict):
        with self.db:
            self.db.execute("UPDATE jobs SET state='done',result=?,updated=?,error=NULL WHERE id=? AND host=? AND state='running'",
                            (json.dumps(result, ensure_ascii=False), time.time(), job_id, host))

    def fail(self, job_id: str, host: str, error: str, max_attempts: int = 3):
        with self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE id=? AND host=? AND state='running'", (job_id, host)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state=?,host=NULL,error=?,updated=?,retry_at=? WHERE id=?",
                                ("failed" if row["attempts"] >= max_attempts else "retry", error[-4000:],
                                 time.time(), time.time() + min(3600, 60 * 2 ** row["attempts"]), job_id))

    def node(self, host: str, state: str, detail: str = ""):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?)", (host, state, detail[-4000:], time.time()))

    def active(self) -> dict[str, dict]:
        return {row["host"]: dict(row) for row in self.db.execute("SELECT * FROM jobs WHERE state='running'")}

    def report(self) -> dict:
        return {"counts": {row[0]: row[1] for row in self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state")},
                "sources": self.db.execute("SELECT count(*) FROM sources WHERE present=1").fetchone()[0],
                "active": list(self.active().values()),
                "nodes": [dict(row) for row in self.db.execute("SELECT * FROM nodes ORDER BY host")]}
