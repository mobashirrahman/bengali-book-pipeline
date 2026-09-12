"""Frozen discovery of local cluster proofreading records for human review."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
DEFAULT_CAP = 1000


@dataclass(frozen=True)
class ReviewDataset:
    """A stable, read-only collection of review entries."""

    path: Path
    entries: tuple[dict[str, Any], ...]
    cap: int

    @classmethod
    def open(cls, path: Path) -> ReviewDataset:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != MANIFEST_VERSION or not isinstance(
            payload.get("entries"), list
        ):
            raise ValueError(f"Unsupported review manifest: {path}")
        entries = tuple(dict(entry) for entry in payload["entries"])
        if any(not isinstance(entry.get("id"), str) for entry in entries):
            raise ValueError(
                f"Review manifest contains an entry without an opaque id: {path}"
            )
        return cls(
            path=path, entries=entries, cap=int(payload.get("cap", len(entries)))
        )

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return next((entry for entry in self.entries if entry["id"] == entry_id), None)


def create_manifest(
    cluster_root: Path,
    manifest_path: Path,
    *,
    queue_path: Path | None = None,
    cap: int = DEFAULT_CAP,
) -> ReviewDataset:
    """Discover completed local jobs once and atomically freeze their entries."""
    if cap < 1:
        raise ValueError("Review cap must be positive")
    cluster_root = Path(cluster_root).resolve()
    queue_path = queue_path or cluster_root / "queue.sqlite3"
    entries = discover_entries(cluster_root, queue_path=queue_path, cap=cap)
    payload = {"version": MANIFEST_VERSION, "cap": cap, "entries": entries}
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(_json(payload), encoding="utf-8")
    temporary.replace(manifest_path)
    return ReviewDataset(path=manifest_path, entries=tuple(entries), cap=cap)


def discover_entries(
    cluster_root: Path, *, queue_path: Path | None = None, cap: int = DEFAULT_CAP
) -> list[dict[str, Any]]:
    """Return deterministic completed-job entries without writing a manifest."""
    if cap < 1:
        raise ValueError("Review cap must be positive")
    cluster_root = Path(cluster_root).resolve()
    sources = _queue_jobs(queue_path or cluster_root / "queue.sqlite3")
    return sorted(_discover(cluster_root, sources), key=_entry_sort_key)[:cap]


def load_or_create_manifest(
    cluster_root: Path,
    *,
    manifest_path: Path | None = None,
    queue_path: Path | None = None,
    cap: int = DEFAULT_CAP,
) -> ReviewDataset:
    """Reuse an existing manifest; discovery never changes a frozen session."""
    cluster_root = Path(cluster_root).resolve()
    path = Path(manifest_path or cluster_root / "review" / "manifest.json")
    if path.exists():
        return ReviewDataset.open(path)
    return create_manifest(cluster_root, path, queue_path=queue_path, cap=cap)


def _queue_jobs(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {}
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, source, title, sha256, state FROM jobs"
        ).fetchall()
        return {row["id"]: dict(row) for row in rows}
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def _discover(
    cluster_root: Path, jobs: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    jobs_root = cluster_root / "jobs"
    if not jobs_root.is_dir():
        return result
    for job_root in sorted(
        (path for path in jobs_root.iterdir() if path.is_dir()), key=lambda p: p.name
    ):
        summary_path = job_root / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        job_id = job_root.name
        queue_job = jobs.get(job_id)
        if queue_job is not None and queue_job.get("state") != "done":
            continue
        audit_value = summary.get("audit")
        if not isinstance(audit_value, str) or audit_value == "not run":
            continue
        audit_root = _local_path(audit_value, summary_path.parent)
        if not audit_root.is_dir():
            continue
        source = queue_job.get("source") if queue_job else summary.get("source")
        if not isinstance(source, str):
            source = ""
        extraction = _local_path(summary.get("raw"), summary_path.parent)
        for audit_path in sorted(audit_root.glob("p*.json"), key=lambda p: p.name):
            try:
                record = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                continue
            original = record.get("original")
            if not isinstance(original, str):
                continue
            page = _positive_int(record.get("page"))
            bbox = _bbox(record.get("bbox"))
            entry_id = hashlib.sha256(
                f"{job_id}\0{record['id']}\0{original}".encode()
            ).hexdigest()[:32]
            result.append(
                {
                    "id": entry_id,
                    "job_id": job_id,
                    "audit_id": record["id"],
                    "title": str(
                        queue_job.get("title") or summary.get("title") or job_id
                    ),
                    "source": source,
                    "source_exists": bool(source) and Path(source).is_file(),
                    "source_sha256": queue_job.get("sha256")
                    or summary.get("source_sha256"),
                    "extraction": str(extraction) if extraction else "",
                    "audit_path": str(audit_path),
                    "page": page,
                    "bbox": bbox,
                    "original": original,
                    "proposed": record.get("proposed")
                    if isinstance(record.get("proposed"), str)
                    else original,
                    "decisions": record.get("decisions")
                    if isinstance(record.get("decisions"), list)
                    else [],
                    "model": record.get("model", ""),
                    "status": record.get("status", "review"),
                    "reason": record.get("reason"),
                }
            )
    return result


def _local_path(value: object, base: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def _positive_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _bbox(value: object) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in value
    ):
        return None
    return [int(item) for item in value]


def _entry_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    bbox = entry.get("bbox") or [0, 0, 0, 0]
    return (
        str(entry.get("title", "")).casefold(),
        entry["job_id"],
        entry.get("page") or 0,
        tuple(bbox),
        entry.get("audit_id", ""),
        entry["id"],
    )


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
