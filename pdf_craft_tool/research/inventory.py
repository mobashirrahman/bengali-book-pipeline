"""S1 corpus audit: pure-read inventory over cluster/catalogue artefacts.

Every SQLite access in this module is read-only (``mode=ro``). It never
writes, never touches the network, never hashes files under ``data/`` and
never reads ``.env``. Real-artifact access is read-only; unit tests use
synthetic fixtures instead.
"""

from __future__ import annotations

import csv
import datetime
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCAN_ROW_LIMIT = 2000


@dataclass(frozen=True)
class InventorySources:
    queue_db: Path | None = None
    catalogue_db: Path | None = None
    dedupe_report: Path | None = None
    metadata_csv: Path | None = None
    jobs_dir: Path | None = None
    data_root: Path | None = None

    def __post_init__(self):
        for name in ("queue_db", "catalogue_db", "dedupe_report",
                     "metadata_csv", "jobs_dir", "data_root"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Path):
                object.__setattr__(self, name, Path(value))


@dataclass(frozen=True)
class InventoryReport:
    as_of_utc: str
    sources: dict
    file_count: int | None
    distinct_content_sha256: int | None
    duplicate_cluster_count: int | None
    candidate_work_groups: int | None
    processing_profiles: dict
    job_state_counts: dict
    done_raw_artifacts_present: int | None
    done_proofread_artifacts_present: int | None
    ocr_page_total: int | None
    metadata_coverage: dict
    rights_coverage: dict
    unresolved_paths: list
    unknowns: list


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _provided(path: Path | None) -> bool:
    return path is not None and Path(path).exists()


def _tables(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> list:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return []
    return [row[1] for row in rows]


def _is_work_edition(relation) -> bool:
    if not isinstance(relation, str):
        return False
    norm = relation.strip().lower().replace("-", "_")
    return "work" in norm or "edition" in norm


def _read_queue(queue_db: Path, unknowns: list) -> dict:
    """Read file/job counters from the cluster queue DB (read-only)."""
    out = {"file_count": None, "distinct": None, "profiles": {},
           "states": {}, "done_ids": [], "ok_sources": False,
           "ok_jobs": False}
    try:
        conn = sqlite3.connect(f"file:{queue_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        unknowns.append(f"queue_db unreadable ({exc}): "
                        "file/job counts unknown")
        return out
    try:
        tables = _tables(conn)
        if "sources" in tables:
            cols = _columns(conn, "sources")
            try:
                out["file_count"] = conn.execute(
                    "SELECT COUNT(*) FROM sources").fetchone()[0]
                out["ok_sources"] = True
            except sqlite3.Error:
                out["file_count"] = None
            if "sha256" in cols:
                try:
                    row = conn.execute(
                        "SELECT COUNT(DISTINCT sha256) FROM sources "
                        "WHERE sha256 IS NOT NULL").fetchone()
                    out["distinct"] = row[0]
                except sqlite3.Error:
                    out["distinct"] = None
        else:
            unknowns.append("queue_db has no sources table: "
                            "file counts unknown")
        if "jobs" in tables:
            cols = _columns(conn, "jobs")
            if "state" in cols:
                try:
                    for state, count in conn.execute(
                            "SELECT state, COUNT(*) FROM jobs "
                            "GROUP BY state").fetchall():
                        out["states"][str(state) if state is not None
                                      else "unknown"] = count
                    out["ok_jobs"] = True
                except sqlite3.Error:
                    out["ok_jobs"] = False
            if out["ok_jobs"] and "profile" in cols:
                try:
                    for profile, count in conn.execute(
                            "SELECT profile, COUNT(*) FROM jobs "
                            "WHERE state='done' GROUP BY profile").fetchall():
                        out["profiles"][str(profile) if profile is not None
                                        else "unknown"] = count
                except sqlite3.Error:
                    out["profiles"] = {}
            id_col = ("id" if "id" in cols
                      else "job_id" if "job_id" in cols else None)
            if out["ok_jobs"] and id_col is not None:
                try:
                    out["done_ids"] = [
                        row[0] for row in conn.execute(
                            f'SELECT "{id_col}" FROM jobs '
                            "WHERE state='done'").fetchall()]
                except sqlite3.Error:
                    out["done_ids"] = []
            elif out["ok_jobs"]:
                unknowns.append("queue_db jobs table has no id column: "
                                "done-job artifact linkage unknown")
        else:
            unknowns.append("queue_db has no jobs table: "
                            "job state/profile counts unknown")
    finally:
        conn.close()
    return out


def _read_catalogue(catalogue_db: Path, unknowns: list) -> dict:
    """Read grouping counters from the catalogue DB (read-only)."""
    out = {"file_count": None, "distinct": None,
           "clusters": None, "work_groups": None}
    try:
        conn = sqlite3.connect(f"file:{catalogue_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        unknowns.append(f"catalogue_db unreadable ({exc}): "
                        "work/edition grouping unknown")
        return out
    try:
        tables = _tables(conn)
        if "catalogue_local_documents" in tables:
            cols = _columns(conn, "catalogue_local_documents")
            try:
                out["file_count"] = conn.execute(
                    "SELECT COUNT(*) FROM catalogue_local_documents"
                ).fetchone()[0]
            except sqlite3.Error:
                out["file_count"] = None
            if "sha256" in cols:
                try:
                    out["distinct"] = conn.execute(
                        "SELECT COUNT(DISTINCT sha256) "
                        "FROM catalogue_local_documents "
                        "WHERE sha256 IS NOT NULL").fetchone()[0]
                except sqlite3.Error:
                    out["distinct"] = None
        if "catalogue_duplicate_clusters" in tables:
            cols = _columns(conn, "catalogue_duplicate_clusters")
            try:
                out["clusters"] = conn.execute(
                    "SELECT COUNT(*) FROM catalogue_duplicate_clusters"
                ).fetchone()[0]
            except sqlite3.Error:
                out["clusters"] = None
            if "relation" in cols and out["clusters"] is not None:
                try:
                    relations = conn.execute(
                        "SELECT relation FROM catalogue_duplicate_clusters"
                    ).fetchall()
                    out["work_groups"] = sum(
                        1 for (rel,) in relations if _is_work_edition(rel))
                except sqlite3.Error:
                    out["work_groups"] = None
            else:
                unknowns.append("catalogue_db duplicate clusters lack a "
                                "relation column: work/edition grouping "
                                "unknown")
        else:
            unknowns.append("catalogue_db has no duplicate-cluster table: "
                            "work/edition grouping unknown")
    finally:
        conn.close()
    return out


def _read_dedupe_report(report_path: Path, unknowns: list) -> dict:
    out = {"clusters": None, "work_groups": None}
    try:
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        unknowns.append(f"dedupe report unreadable ({exc}): "
                        "duplicate grouping unknown")
        return out
    if not isinstance(data, dict) or not isinstance(
            data.get("clusters"), list):
        unknowns.append("dedupe report has no clusters list: "
                        "duplicate grouping unknown")
        return out
    clusters = data["clusters"]
    out["clusters"] = len(clusters)
    out["work_groups"] = sum(
        1 for entry in clusters
        if isinstance(entry, dict) and _is_work_edition(
            entry.get("relation", entry.get("type", ""))))
    return out


def _read_metadata_csv(csv_path: Path, unknowns: list) -> dict:
    out = {"rows": None, "title": None, "authors": None, "year": None}
    try:
        with open(csv_path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                unknowns.append("metadata csv has no header: "
                                "metadata coverage unknown")
                return out
            rows = title = authors = year = 0
            for record in reader:
                rows += 1
                if (record.get("extracted_title") or "").strip():
                    title += 1
                if (record.get("extracted_authors") or "").strip():
                    authors += 1
                if (record.get("year") or "").strip():
                    year += 1
    except (OSError, ValueError, csv.Error) as exc:
        unknowns.append(f"metadata csv unreadable ({exc}): "
                        "metadata coverage unknown")
        return out
    out.update({"rows": rows, "title": title,
                "authors": authors, "year": year})
    return out


def build_inventory(sources: InventorySources, *,
                    now_utc: str | None = None,
                    unresolved_cap: int = 200) -> InventoryReport:
    """Build a pure-read inventory report over the provided sources.

    Any source that is not provided (or whose file is missing) leaves the
    counters that depend on it as ``None`` and appends an explanatory entry
    to ``unknowns``. Unknown is never reported as 0.
    """
    unknowns: list = []
    provided = {
        "queue_db": _provided(sources.queue_db),
        "catalogue_db": _provided(sources.catalogue_db),
        "dedupe_report": _provided(sources.dedupe_report),
        "metadata_csv": _provided(sources.metadata_csv),
        "jobs_dir": _provided(sources.jobs_dir),
        "data_root": _provided(sources.data_root),
    }

    file_count = None
    distinct = None
    profiles: dict = {}
    states: dict = {}
    done_ids: list = []
    queue_ok_sources = False
    queue_ok_jobs = False

    if provided["queue_db"]:
        queue = _read_queue(Path(sources.queue_db), unknowns)
        file_count = queue["file_count"]
        distinct = queue["distinct"]
        profiles = queue["profiles"]
        states = queue["states"]
        done_ids = queue["done_ids"]
        queue_ok_sources = queue["ok_sources"]
        queue_ok_jobs = queue["ok_jobs"]
        if file_count is None and queue_ok_sources is False:
            unknowns.append("queue_db sources unreadable: "
                            "file counts unknown")
    else:
        unknowns.append("queue_db not provided: file counts and job "
                        "state/profile counts unknown")

    duplicate_clusters = None
    work_groups = None
    if provided["catalogue_db"]:
        catalogue = _read_catalogue(Path(sources.catalogue_db), unknowns)
        if file_count is None:
            file_count = catalogue["file_count"]
        if distinct is None:
            distinct = catalogue["distinct"]
        duplicate_clusters = catalogue["clusters"]
        work_groups = catalogue["work_groups"]
    else:
        unknowns.append("catalogue_db not provided: work/edition "
                        "grouping unknown")
    if (duplicate_clusters is None or work_groups is None) and provided[
            "dedupe_report"]:
        dedupe = _read_dedupe_report(Path(sources.dedupe_report), unknowns)
        if duplicate_clusters is None:
            duplicate_clusters = dedupe["clusters"]
        if work_groups is None:
            work_groups = dedupe["work_groups"]
    elif duplicate_clusters is None and not provided["dedupe_report"]:
        unknowns.append("dedupe report not provided: duplicate-cluster "
                        "fallback unknown")

    done_raw = None
    done_proof = None
    ocr_pages = None
    if queue_ok_jobs and provided["jobs_dir"]:
        jobs_dir = Path(sources.jobs_dir)
        raw_hits = proof_hits = page_total = 0
        for job_id in done_ids:
            result = None
            # Prefer <job>/summary.json: it records artifact paths rewritten to
            # this machine's local jobs tree. result.json embeds the same fields
            # under a "summary" key but with the worker-side paths.
            for name, unwrap in (("summary.json", False), ("result.json", True)):
                try:
                    payload = json.loads(
                        (jobs_dir / str(job_id) / name).read_text(
                            encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if unwrap and isinstance(payload.get("summary"), dict):
                    payload = payload["summary"]
                result = payload
                break
            if result is None:
                continue
            raw = result.get("raw")
            if isinstance(raw, str) and Path(raw).is_file():
                raw_hits += 1
            proof = result.get("proofreading")
            if (isinstance(proof, str) and proof != "not run"
                    and Path(proof).is_file()):
                proof_hits += 1
            pages = result.get("pages", [])
            if isinstance(pages, (list, tuple)):
                page_total += len(pages)
        done_raw, done_proof, ocr_pages = raw_hits, proof_hits, page_total
    elif not queue_ok_jobs:
        unknowns.append("done-job list unknown: raw/proofread artifact "
                        "presence and OCR page total unknown")
    else:
        unknowns.append("jobs_dir not provided: raw/proofread artifact "
                        "presence and OCR page total unknown")

    if provided["metadata_csv"]:
        meta = _read_metadata_csv(Path(sources.metadata_csv), unknowns)
    else:
        unknowns.append("metadata csv not provided: title/author/year "
                        "coverage unknown")
        meta = {"rows": None, "title": None,
                "authors": None, "year": None}
    metadata_coverage = {"rows": meta["rows"], "title": meta["title"],
                         "authors": meta["authors"], "year": meta["year"]}

    denom = (meta["rows"] if meta["rows"] is not None else file_count)
    if denom is None:
        rights_coverage = {"rows": None, "with_rights_basis": 0,
                           "unknown": None}
    else:
        rights_coverage = {"rows": denom, "with_rights_basis": 0,
                           "unknown": denom}
    unknowns.append("no redistribution-rights field in any inventory source")

    unresolved: list = []
    if queue_ok_sources and provided["queue_db"]:
        try:
            conn = sqlite3.connect(f"file:{sources.queue_db}?mode=ro", uri=True)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                cols = _columns(conn, "sources")
                if "path" in cols and "present" in cols:
                    rows = conn.execute(
                        "SELECT path, present FROM sources "
                        f"LIMIT {_SCAN_ROW_LIMIT}").fetchall()
                    data_root = (Path(sources.data_root)
                                 if provided["data_root"] else None)
                    cap = max(int(unresolved_cap), 0)
                    for raw_path, present in rows:
                        if not present or not raw_path:
                            continue
                        candidate = Path(raw_path)
                        if (data_root is not None
                                and not candidate.is_absolute()):
                            candidate = data_root / candidate
                        try:
                            exists = candidate.exists()
                        except OSError:
                            exists = False
                        if not exists:
                            unresolved.append(str(raw_path))
                            if len(unresolved) >= cap:
                                break
                else:
                    unknowns.append("queue_db sources lack path/present "
                                    "columns: path resolvability unknown")
            except sqlite3.Error as exc:
                unknowns.append(f"queue_db path sample unreadable ({exc}): "
                                "path resolvability unknown")
            finally:
                conn.close()
    else:
        unknowns.append("queue_db not provided: path resolvability unknown")

    return InventoryReport(
        as_of_utc=now_utc or _utc_now(),
        sources=provided,
        file_count=file_count,
        distinct_content_sha256=distinct,
        duplicate_cluster_count=duplicate_clusters,
        candidate_work_groups=work_groups,
        processing_profiles=profiles,
        job_state_counts=states,
        done_raw_artifacts_present=done_raw,
        done_proofread_artifacts_present=done_proof,
        ocr_page_total=ocr_pages,
        metadata_coverage=metadata_coverage,
        rights_coverage=rights_coverage,
        unresolved_paths=unresolved,
        unknowns=unknowns,
    )
