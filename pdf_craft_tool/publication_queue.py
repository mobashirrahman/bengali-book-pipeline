"""Local publication-only watcher. Reads the OCR queue; never mutates its jobs."""

import argparse
import fcntl
import json
from pathlib import Path
import signal
import sqlite3
import threading
import time

from .book import file_hash, fingerprint, write_json
from .book_metadata import load_metadata, publication_settings
from .publish import publish


def _exists(path) -> bool:
    """Existence check that treats overlong/unrepresentable names as absent.

    Adjacent ``<book>.metadata.json`` sidecars derived from very long Bengali
    PDF names can exceed filesystem limits; probing them raises OSError
    (Errno 36) instead of returning False. The central
    ``publications/metadata/<job-id>.json`` override remains usable there.
    """
    try:
        return path.exists()
    except OSError:
        return False


class PublicationQueue:
    def __init__(self, cluster_root: Path, output_root: Path, *, settle_seconds=120,
                 epubcheck=None, release="publication-3-reading-structure"):
        self.cluster_root, self.root = cluster_root, output_root
        self.settle_seconds, self.epubcheck, self.release = settle_seconds, epubcheck, release
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "records").mkdir(exist_ok=True)
        (self.root / "metadata").mkdir(exist_ok=True)
        self.hashes = {}

    def digest(self, path):
        stat = path.stat()
        signature = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if signature not in self.hashes:
            self.hashes[signature] = file_hash(path)
        return self.hashes[signature]

    def scan(self, stop=None):
        """Publish stable metadata revisions independently, retaining old outputs."""
        uri = (self.cluster_root / "queue.sqlite3").resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as database:
            database.execute("PRAGMA busy_timeout=30000")
            database.row_factory = sqlite3.Row
            jobs = [dict(row) for row in database.execute("SELECT id,source,sha256,result FROM jobs WHERE state='done' ORDER BY updated")]
        counts = {"published": 0, "unchanged": 0, "settling": 0, "superseded": 0, "errors": 0}
        for job in jobs:
            if stop and stop.is_set():
                break
            record_path = self.root / "records" / (job["id"] + ".json")
            old = json.loads(record_path.read_text()) if record_path.exists() else {}
            if old.get("retry_at", 0) > time.time():
                counts["errors"] += 1
                continue
            try:
                summary = json.loads(job["result"])
                package = Path(summary["proofreading"] if summary.get("proofreading", "not run") != "not run" else summary["raw"])
                source = Path(job["source"])
                central = self.root / "metadata" / (job["id"] + ".json")
                adjacent = source.with_suffix(".metadata.json")
                metadata_path = central if _exists(central) else adjacent
                metadata_arg = metadata_path if _exists(metadata_path) else None
                try:
                    current = self.digest(source)
                except (OSError, FileNotFoundError):
                    current = None
                if current != job["sha256"]:
                    write_json(record_path, {**old, "job_id": job["id"], "status": "superseded",
                                             "error": "Source PDF changed since OCR; refusing mismatched source/cover",
                                             "updated": time.time()})
                    counts["superseded"] += 1
                    continue
                values = load_metadata(source, metadata_path=metadata_arg)
                watched = [source, package] + ([metadata_path] if _exists(metadata_path) else [])
                if values.get("cover"):
                    watched.append(Path(values["cover"]))
                if any(time.time() - path.stat().st_mtime < self.settle_seconds for path in watched):
                    counts["settling"] += 1
                    continue
                key = fingerprint({"release": self.release, "extraction": self.digest(package),
                                   "source": job["sha256"], "metadata": publication_settings(values)})
                output = self.root / "books" / job["id"] / key[:20]
                if old.get("key") == key and old.get("status") == "done" and output.exists():
                    counts["unchanged"] += 1
                    continue
                if self.digest(source) != job["sha256"]:
                    write_json(record_path, {**old, "job_id": job["id"], "status": "superseded",
                                             "error": "Source PDF changed since OCR; refusing mismatched source/cover",
                                             "updated": time.time()})
                    counts["superseded"] += 1
                    continue
                if not output.exists():
                    publish(argparse.Namespace(extraction=package, source=source, output_dir=output,
                                               metadata=metadata_arg,
                                               chunk_tokens=800, epubcheck=self.epubcheck))
                # A crash after atomic publication but before recording is recoverable.
                for name in ("book.epub", "book.md", "chunks.jsonl", "source-map.json", "metadata.json", "reading.pcex", "structure-audit.json"):
                    artifact = output / name
                    if self.digest(artifact) != (output / (name + ".sha256")).read_text():
                        raise ValueError(f"Published artifact changed: {artifact}")
                write_json(record_path, {"job_id": job["id"], "key": key, "status": "done", "output": str(output),
                                         "metadata": str(metadata_path), "updated": time.time()})
                counts["published"] += 1
            except Exception as error:  # Isolate one malformed sidecar from other books.
                write_json(record_path, {**old, "job_id": job["id"], "status": "error", "error": str(error),
                                         "updated": time.time(), "retry_at": time.time() + 300})
                counts["errors"] += 1
        write_json(self.root / "status.json", {"updated": time.time(), "completed_ocr_books": len(jobs), **counts})
        return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--epubcheck", type=Path)
    parser.add_argument("--release", default="publication-3-reading-structure")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    cluster = Path(config["state_root"])
    root = args.output_root or cluster / "publications"
    queue = PublicationQueue(cluster, root, epubcheck=args.epubcheck, release=args.release)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    with (root / ".publisher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stop.is_set():
            print(json.dumps(queue.scan(stop)), flush=True)
            if args.once:
                break
            stop.wait(60)


if __name__ == "__main__":
    main()
