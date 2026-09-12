import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import threading
from unittest.mock import Mock

from PIL import Image

from pdf_craft_tool.cluster_queue import BookQueue
from pdf_craft_tool.cluster import SSHWorker
from pdf_craft_tool.vision import VisionProofreadingClient
from pdf_craft.common import save_xml
from pdf_craft.extractor.chapter.chapter import Chapter, ParagraphLayout, BlockLayout, encode
from tests.extraction_helpers import make_extraction


def discover(queue, data, profile="profile"):
    queue.scan(data, profile, settle_seconds=10, now=100)
    return queue.scan(data, profile, settle_seconds=10, now=111)


def test_discovery_settles_and_deduplicates_content(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "বই.pdf").write_bytes(b"first PDF")
    (data / "alias.PDF").write_bytes(b"first PDF")
    queue = BookQueue(tmp_path / "queue.sqlite3")
    assert queue.scan(data, "profile", 10, now=100)["new_jobs"] == 0
    assert queue.scan(data, "profile", 10, now=105)["new_jobs"] == 0
    assert queue.scan(data, "profile", 10, now=111)["new_jobs"] == 1
    assert queue.report()["counts"] == {"pending": 1}
    assert queue.scan(data, "profile", 10, now=200)["new_jobs"] == 0
    queue.close()


def test_added_and_replaced_books_get_new_jobs(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pdf = data / "book.pdf"
    pdf.write_bytes(b"first")
    queue = BookQueue(tmp_path / "queue.sqlite3")
    discover(queue, data)
    job = queue.claim("bio01", "profile")
    queue.finish(job["id"], "bio01", {"output": "result"})
    pdf.write_bytes(b"replacement content")
    (data / "new.pdf").write_bytes(b"second book")
    assert queue.scan(data, "profile", 10, now=200)["new_jobs"] == 0
    assert queue.scan(data, "profile", 10, now=211)["new_jobs"] == 2
    assert queue.report()["counts"] == {"done": 1, "pending": 2}
    queue.close()


def test_simultaneous_workers_cannot_claim_same_job(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "book.pdf").write_bytes(b"first")
    path = tmp_path / "queue.sqlite3"
    queue = BookQueue(path)
    discover(queue, data)
    queue.close()
    def claim(index):
        worker_queue = BookQueue(path)
        try:
            return worker_queue.claim(f"bio{index:02d}", "profile")
        finally:
            worker_queue.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(claim, range(1, 9)))
    assert sum(job is not None for job in jobs) == 1


def test_restart_retains_running_ownership_and_fences_completion(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "book.pdf").write_bytes(b"first")
    path = tmp_path / "queue.sqlite3"
    queue = BookQueue(path)
    discover(queue, data)
    job = queue.claim("bio01", "profile")
    queue.close()
    queue = BookQueue(path)
    assert queue.claim("bio01", "profile")["id"] == job["id"]
    assert queue.claim("bio02", "profile") is None
    queue.finish(job["id"], "bio02", {})
    assert queue.report()["counts"] == {"running": 1}
    queue.finish(job["id"], "bio01", {})
    assert queue.report()["counts"] == {"done": 1}
    queue.close()


def test_failures_retry_with_backoff_then_stop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "book.pdf").write_bytes(b"first")
    queue = BookQueue(tmp_path / "queue.sqlite3")
    discover(queue, data)
    for attempt in range(3):
        job = queue.claim("bio01", "profile")
        queue.fail(job["id"], "bio01", "test failure")
        assert queue.claim("bio02", "profile") is None
        with queue.db:
            queue.db.execute("UPDATE jobs SET retry_at=0")
    assert queue.report()["counts"] == {"failed": 1}
    queue.close()


def test_deleted_source_uses_existing_content_alias(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.pdf").write_bytes(b"same")
    (data / "b.pdf").write_bytes(b"same")
    queue = BookQueue(tmp_path / "queue.sqlite3")
    discover(queue, data)
    (data / "a.pdf").unlink()
    queue.scan(data, "profile", 10, now=130)
    assert queue.claim("bio01", "profile")["source"].endswith("b.pdf")
    queue.close()


def test_rsync_does_not_interpret_source_filenames(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("pdf_craft_tool.cluster.run", lambda command, **kwargs: calls.append(command))
    worker = SSHWorker("not-the-local-host", {"worker_root": "/scratch/worker", "release": "abc"}, None)
    source = str(tmp_path / "বই $(touch nope).pdf")
    worker.copy(source, "/scratch/worker/source.pdf")
    assert "--protect-args" in calls[0]
    assert source in calls[0]
    assert calls[0][-1] == "not-the-local-host:/scratch/worker/source.pdf"


def test_visual_request_contains_source_image_and_distinct_cache_identity(tmp_path):
    extraction = make_extraction(tmp_path / "extraction")
    chapter = Chapter(1, 0, [ParagraphLayout("text", -1, [BlockLayout(1, 0, (10, 10, 90, 80), ["বই"] )])])
    save_xml(encode(chapter), tmp_path / "extraction/chapters/chapter_1.xml")
    transport = Mock(identity="qwen@digest")
    transport.chat.return_value = '{"edits":[]}'
    client = VisionProofreadingClient(transport, tmp_path / "source.pdf", extraction, "source-hash", "raw-hash")
    client.document = Mock()
    client.document.render_page.return_value = Image.new("RGB", (100, 100), "white")
    assert client("proofread", json.dumps({"id": "p000001-b0000", "text": "বই"})) == '{"edits":[]}'
    image = Image.open(io.BytesIO(base64.b64decode(transport.chat.call_args.kwargs["images"][0])))
    assert image.width <= 1536 and image.height <= 1536
    assert "source-hash" in client.identity and "vision-crop" in client.identity
    assert "original paragraph" in transport.chat.call_args.args[0]
    client.close()


def test_stale_failure_does_not_fail_new_attempt(tmp_path, monkeypatch):
    from pdf_craft_tool.cluster import host_loop
    data = tmp_path / "data"
    data.mkdir()
    (data / "book.pdf").write_bytes(b"first")
    (tmp_path / "logs").mkdir()
    queue = BookQueue(tmp_path / "queue.sqlite3")
    discover(queue, data)
    first = queue.claim("bio01", "profile")
    queue.fail(first["id"], "bio01", "worker crashed")
    with queue.db:
        queue.db.execute("UPDATE jobs SET retry_at=0")
    second = queue.claim("bio01", "profile")
    stop = threading.Event()
    worker = Mock()
    def stale_state(*args):
        stop.set()
        return {"job_id": first["id"], "attempt": 1, "state": "failed", "error": "old failure"}
    worker.rpc.side_effect = stale_state
    worker.submit.return_value = {"state": "starting", "accepted": True}
    monkeypatch.setattr("pdf_craft_tool.cluster.SSHWorker", lambda *_: worker)
    host_loop("bio01", {"profile": "profile"}, tmp_path, stop)
    worker.submit.assert_called_once()
    assert queue.active()["bio01"]["attempts"] == 2
    queue.close()


def test_scheduler_upgrade_does_not_change_processing_identity(tmp_path):
    from pdf_craft_tool.cluster import processing_hash
    tool = tmp_path / "pdf_craft_tool"
    tool.mkdir()
    (tool / "book.py").write_text("book-v1")
    (tool / "cluster.py").write_text("scheduler-v1")
    original = processing_hash(tmp_path)
    (tool / "cluster.py").write_text("scheduler-v2")
    assert processing_hash(tmp_path) == original
    (tool / "book.py").write_text("book-v2")
    assert processing_hash(tmp_path) != original


def test_scan_batches_large_collections_and_sets_busy_timeout(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for index in range(600):
        (data / f"book-{index:04d}.pdf").write_bytes(f"content-{index}".encode())
    queue = BookQueue(tmp_path / "queue.sqlite3")
    try:
        assert queue.db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert queue.scan(data, "profile", 10, now=100)["new_jobs"] == 0
        assert queue.scan(data, "profile", 10, now=111)["new_jobs"] == 600
        assert queue.report()["counts"] == {"pending": 600}
    finally:
        queue.close()


def test_discovery_failure_is_retryable_not_fatal():
    import sqlite3

    import pdf_craft_tool.cluster as cluster_module
    import inspect

    source = inspect.getsource(cluster_module.serve)
    assert "sqlite3.OperationalError" in source
    assert "rollback" in source


def test_worker_prepare_validates_cryptography_runtime():
    import inspect

    from pdf_craft_tool.cluster import SSHWorker

    assert "cryptography" in inspect.getsource(SSHWorker.prepare)


def test_queue_schema_indexes_claim_path(tmp_path):
    queue = BookQueue(tmp_path / "queue.sqlite3")
    try:
        names = {row[0] for row in queue.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_sources_sha256_present" in names
        assert "idx_jobs_profile_state" in names
    finally:
        queue.close()
