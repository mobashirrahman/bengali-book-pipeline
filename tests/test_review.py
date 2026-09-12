import http.client
import json
import threading
from pathlib import Path

from PIL import Image

from pdf_craft_tool.cluster_queue import BookQueue
from pdf_craft_tool.review_dataset import create_manifest, load_or_create_manifest
from pdf_craft_tool.review_server import ReviewApp, ReviewHTTPServer
from pdf_craft_tool.review_store import ReviewStore, RevisionConflict


def _job(
    root: Path,
    job_id: str,
    source: Path,
    *,
    title: str,
    audit_id: str = "p000001-b0000",
):
    audit = root / "jobs" / job_id / "work" / "audit"
    audit.mkdir(parents=True)
    (audit / f"{audit_id}.json").write_text(
        json.dumps(
            {
                "id": audit_id,
                "model": "model@digest",
                "status": "unchanged",
                "original": "বাংলা OCR",
                "proposed": "বাংলা OCR ✓",
                "decisions": [{"before": "OCR", "after": "OCR", "reason": "test"}],
                "page": 1,
                "bbox": [2, 3, 80, 40],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "jobs" / job_id / "summary.json").write_text(
        json.dumps(
            {
                "source": "/remote/worker/old.pdf",
                "source_sha256": f"sha-{job_id}",
                "raw": str(root / "jobs" / job_id / "work" / "raw.pcex"),
                "audit": str(audit),
            }
        ),
        encoding="utf-8",
    )
    return audit


def _done_job(queue_path: Path, job_id: str, source: Path, title: str):
    queue = BookQueue(queue_path)
    with queue.db:
        queue.db.execute(
            "INSERT INTO jobs(id,sha256,profile,source,title,state) VALUES(?,?,?,?,?,'done')",
            (job_id, f"sha-{job_id}", "profile", str(source), title),
        )
    queue.close()


def test_review_manifest_is_frozen_and_resolves_queue_source(tmp_path):
    cluster = tmp_path / "cluster"
    source = tmp_path / "বই (old).pdf"
    source.write_bytes(b"pdf")
    _job(cluster, "job-a", source, title="বাংলা বই")
    _done_job(cluster / "queue.sqlite3", "job-a", source, "বাংলা বই")
    missing = tmp_path / "missing.pdf"
    _job(cluster, "job-b", missing, title="Missing")
    _done_job(cluster / "queue.sqlite3", "job-b", missing, "Missing")
    manifest_path = cluster / "review" / "manifest.json"
    first = create_manifest(cluster, manifest_path, cap=1000)
    assert len(first.entries) == 2
    by_job = {entry["job_id"]: entry for entry in first.entries}
    assert by_job["job-a"]["source"] == str(source)
    assert by_job["job-a"]["source_exists"] is True
    assert by_job["job-b"]["source_exists"] is False
    assert all(len(entry["id"]) == 32 for entry in first.entries)
    _job(cluster, "job-c", source, title="new")
    _done_job(cluster / "queue.sqlite3", "job-c", source, "new")
    frozen = load_or_create_manifest(cluster, manifest_path=manifest_path, cap=1)
    assert [entry["job_id"] for entry in frozen.entries] == [
        entry["job_id"] for entry in first.entries
    ]


def test_review_store_resume_revision_and_exports(tmp_path):
    entries = [
        {"id": "a" * 32, "original": "পুরনো", "title": "বই"},
        {"id": "b" * 32, "original": "দ্বিতীয়", "title": "বই"},
    ]
    store = ReviewStore(tmp_path / "review.sqlite3", entries)
    assert store.progress()["remaining"] == 2
    saved = store.save(
        entries[0]["id"], verified_text="নতুন", decision="manual", revision=0
    )
    assert saved["review"]["revision"] == 1
    try:
        store.save(
            entries[0]["id"], verified_text="stale", decision="manual", revision=0
        )
    except RevisionConflict:
        pass
    else:
        raise AssertionError("stale revision was accepted")
    assert store.progress()["completed"] == 1
    verified = store.export_jsonl(tmp_path / "verified.jsonl")
    assert (
        json.loads(verified.read_text(encoding="utf-8"))["review"]["verified_text"]
        == "নতুন"
    )
    assert len(store.export_rows(verified_only=False)) == 2
    store.close()


def test_review_store_only_exports_accepted_model_edits_for_apply(tmp_path):
    entry = {"id": "d" * 32, "original": "পুরনো", "title": "বই"}
    store = ReviewStore(tmp_path / "review.sqlite3", [entry])
    decisions = [
        {"before": "পুরনো", "after": "নতুন", "status": "accepted"},
        {"before": "ভুল", "after": "ভাল", "status": "review"},
    ]
    store.save(entry["id"], verified_text="পুরনো", decision="keep", revision=0, accepted_edits=decisions)
    assert store.export_rows(verified_only=True)[0]["review"]["accepted_edits"] == []
    store.save(entry["id"], verified_text="নতুন", decision="apply", revision=1, accepted_edits=decisions)
    assert store.export_rows(verified_only=True)[0]["review"]["accepted_edits"] == [decisions[0]]
    store.close()


class _FakeDocument:
    def render_page(self, page, dpi):
        assert (page, dpi) == (1, 240)
        return Image.new("RGB", (100, 80), "white")

    def close(self):
        pass


class _FakeHandler:
    def open(self, source):
        assert source.exists()
        return _FakeDocument()


class _FakeExtraction:
    def render_dpi(self):
        return 240


def test_review_http_update_and_mocked_crop(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"not opened by fake handler")
    entry = {
        "id": "c" * 32,
        "job_id": "job",
        "audit_id": "p1",
        "title": "বই",
        "source": str(source),
        "source_exists": True,
        "extraction": "raw.pcex",
        "page": 1,
        "bbox": [2, 3, 50, 40],
        "original": "原文",
        "proposed": "建议",
        "decisions": [],
        "model": "m",
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "cap": 1, "entries": [entry]}, ensure_ascii=False),
        encoding="utf-8",
    )
    from pdf_craft_tool.review_dataset import ReviewDataset

    app = ReviewApp(
        ReviewDataset.open(manifest),
        tmp_path / "review.sqlite3",
        pdf_handler=_FakeHandler(),
        extraction_opener=lambda _: _FakeExtraction(),
    )
    server = ReviewHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    connection.request("GET", "/api/session")
    response = connection.getresponse()
    session = json.loads(response.read())
    assert response.status == 200 and session["next"]["id"] == entry["id"]
    connection.request("GET", f"/api/entry/{entry['id']}/crop")
    response = connection.getresponse()
    assert response.status == 200 and response.getheader("Content-Type").startswith(
        "image/png"
    )
    response.read()
    connection.request(
        "POST",
        f"/api/entry/{entry['id']}/review",
        body=json.dumps(
            {
                "verified_text": "确认",
                "decision": "manual",
                "revision": 0,
            }
        ),
        headers={
            "Content-Type": "application/json",
            "X-Review-Token": session["token"],
        },
    )
    response = connection.getresponse()
    assert response.status == 200
    response.read()
    connection.request("POST", f"/api/entry/{entry['id']}/review", body="{}")
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()
    server.shutdown()
    server.server_close()
