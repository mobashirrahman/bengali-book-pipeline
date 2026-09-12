import hashlib
import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from pdf_craft_tool.gold_server import GoldApp, GoldHTTPServer
from pdf_craft_tool.gold_store import GoldStore, RevisionConflict
from pdf_craft_tool.gold_tasks import (
    GoldDataset,
    derive_qwen_line,
    discover_entries,
    group_words_into_lines,
)


def _words():
    return [
        {"block": 1, "paragraph": 1, "line": 1, "left": 10, "top": 20, "width": 40, "height": 20, "confidence": 90.0, "text": "বাংলা"},
        {"block": 1, "paragraph": 1, "line": 1, "left": 55, "top": 20, "width": 30, "height": 20, "confidence": 40.0, "text": "ভাষা"},
        {"block": 1, "paragraph": 1, "line": 2, "left": 10, "top": 50, "width": 40, "height": 20, "confidence": 95.0, "text": "সুন্দর"},
    ]


def test_group_words_into_lines_marks_low_confidence():
    lines = group_words_into_lines(_words())
    assert len(lines) == 2
    assert lines[0]["text"] == "বাংলা ভাষা"
    assert lines[0]["bbox"] == [10, 20, 85, 40]
    assert lines[0]["suspicious"] is True
    assert lines[1]["suspicious"] is False


def test_derive_qwen_line_applies_unique_word_edit():
    audits = [{"bbox": [0, 0, 200, 200],
               "decisions": [{"before": "ভাষা", "after": "ভাষাা", "kind": "ocr_glyph", "confidence": 0.99}]}]
    text, derived, _ = derive_qwen_line("বাংলা ভাষা", audits)
    assert (text, derived) == ("বাংলা ভাষাা", True)
    # Ambiguous repetition must not apply.
    text, derived, _ = derive_qwen_line("ভাষা ভাষা", audits)
    assert (text, derived) == ("ভাষা ভাষা", False)


def _write_ocr(ocr_dir: Path):
    ocr_dir.mkdir(parents=True)
    (ocr_dir / "page_4.json").write_text(json.dumps({
        "engine": "tesseract", "needs_review": False, "reason": "passed",
        "words": [
            {"block": 1, "paragraph": 2, "line": 1, "left": 150, "top": 480, "width": 60, "height": 30, "confidence": 30.0, "text": "অতুল"},
            {"block": 1, "paragraph": 2, "line": 1, "left": 215, "top": 480, "width": 60, "height": 30, "confidence": 96.0, "text": "আয়"},
            {"block": 1, "paragraph": 2, "line": 2, "left": 150, "top": 520, "width": 60, "height": 30, "confidence": 97.0, "text": "বাবা"},
        ],
    }), encoding="utf-8")


def _write_audit(audit_dir: Path):
    audit_dir.mkdir(parents=True)
    (audit_dir / "p000004-b0002.json").write_text(json.dumps({
        "id": "p000004-b0002", "model": "qwen@test", "status": "changed",
        "original": "অতুল আয়", "proposed": "অতুল আয়",
        "decisions": [{"before": "আয়", "after": "আয়়", "kind": "ocr_glyph", "confidence": 0.99}],
        "page": 4, "bbox": [146, 471, 1598, 626],
    }, ensure_ascii=False), encoding="utf-8")


def test_discover_entries_suspicious_first_and_stable_ids(tmp_path):
    ocr_dir, audit_dir = tmp_path / "ocr", tmp_path / "audit"
    _write_ocr(ocr_dir)
    _write_audit(audit_dir)
    first = discover_entries(ocr_dir=ocr_dir, audit_dir=audit_dir, source="/books/sarat.pdf",
                             source_sha256="sha", title="sarat", cap=10)
    assert len(first) == 2
    assert first[0]["suspicious"] is True  # mixed puts the weak line first
    assert first[0]["qwen"] == "অতুল আয়়" and first[0]["qwen_derived"] is True
    assert first[1]["qwen_derived"] is False
    second = discover_entries(ocr_dir=ocr_dir, audit_dir=audit_dir, source="/books/sarat.pdf",
                              source_sha256="sha", title="sarat", cap=10)
    assert [e["id"] for e in first] == [e["id"] for e in second]


def test_mixed_sampling_does_not_overrepresent_suspicious_lines(tmp_path):
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    words = []
    for line in range(1, 9):
        words.append({
            "block": 1, "paragraph": 1, "line": line,
            "left": 10, "top": line * 40, "width": 40, "height": 20,
            "confidence": 20.0 if line <= 6 else 98.0,
            "text": f"লাইন{line}",
        })
    (ocr_dir / "page_1.json").write_text(
        json.dumps({"words": words}), encoding="utf-8"
    )
    entries = discover_entries(
        ocr_dir=ocr_dir, audit_dir=None, source="/books/sarat.pdf",
        source_sha256="sha", title="sarat", cap=4, sample="mixed", seed=3,
    )
    assert len(entries) == 4
    assert sum(entry["suspicious"] for entry in entries) == 2


def test_gold_store_xp_streak_and_tesstrain(tmp_path):
    entries = [
        {"id": "a" * 32, "tesseract": "ক", "title": "t"},
        {"id": "b" * 32, "tesseract": "খ", "title": "t"},
        {"id": "c" * 32, "tesseract": "গ", "title": "t"},
    ]
    store = GoldStore(tmp_path / "gold.sqlite3", entries)
    first = store.save(entries[0]["id"], verified_text="ক", decision="tesseract", revision=0, elapsed_ms=5000)
    assert first["review"]["xp"] == 10 + 2  # base + first streak bonus
    store.save(entries[1]["id"], verified_text="খ fixed", decision="manual", revision=0)
    stats = store.stats()
    assert stats["completed"] == 2 and stats["streak"] == 2 and stats["xp"] > 0
    assert stats["level"]
    try:
        store.save(entries[0]["id"], verified_text="stale", decision="manual", revision=0)
    except RevisionConflict:
        pass
    else:
        raise AssertionError("stale revision was accepted")
    try:
        store.save(entries[2]["id"], verified_text="   ", decision="manual", revision=0)
    except ValueError:
        pass
    else:
        raise AssertionError("blank verified text was accepted")
    cache = tmp_path / "cache"
    cache.mkdir()
    Image.new("RGB", (60, 20), "white").save(cache / ("a" * 32 + ".png"))
    Image.new("RGB", (60, 20), "white").save(cache / ("b" * 32 + ".png"))
    result = store.export_tesstrain(tmp_path / "train", cache)
    assert result["written"] == 2 and result["missing_crops"] == []
    gt_files = sorted((tmp_path / "train").glob("*.gt.txt"))
    assert len(gt_files) == 2
    assert gt_files[0].read_text(encoding="utf-8").strip() == "ক"
    assert "reference" in store.export_rows(verified_only=True)[0]["benchmark"]
    store.close()


class _FakeDocument:
    def render_page(self, page, dpi):
        assert (page, dpi) == (4, 300)
        return Image.new("RGB", (1700, 700), "white")

    def close(self):
        pass


class _FakeHandler:
    def open(self, source):
        assert Path(source).exists()
        return _FakeDocument()


def _manifest(path: Path, entries: list[dict]) -> GoldDataset:
    path.write_text(json.dumps({"version": 1, "cap": len(entries), "entries": entries}, ensure_ascii=False), encoding="utf-8")
    return GoldDataset.open(path)


def _entry(entry_id: str, source: Path, source_sha256: str) -> dict:
    return {
        "id": entry_id, "title": "sarat", "source": str(source),
        "source_sha256": source_sha256, "page": 1, "bbox": [10, 10, 60, 30], "dpi": 300,
        "tesseract": "বাংলা", "qwen": "বাংলা", "qwen_derived": False,
    }


def test_gold_http_session_crop_review_and_token(tmp_path):
    source = tmp_path / "sarat.pdf"
    source.write_bytes(b"fake pdf")
    entry = {
        "id": "d" * 32, "title": "sarat", "source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "page": 4, "bbox": [150, 480, 275, 510], "dpi": 300,
        "tesseract": "অতুল আয়", "qwen": "অতুল আয়়", "qwen_derived": True,
        "context_original": "অতুল আয়", "context_proposed": "অতুল আয়়",
        "model": "qwen@test", "line_confidence": 63.0, "suspicious": True,
        "line_key": [1, 2, 1], "dataset_version": "gold-1",
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "cap": 1, "entries": [entry]}, ensure_ascii=False), encoding="utf-8")
    app = GoldApp(GoldDataset.open(manifest), tmp_path / "gold.sqlite3", pdf_handler=_FakeHandler())
    server = GoldHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        session = json.loads(response.read())
        assert response.status == 200 and session["next"]["id"] == entry["id"]
        assert session["stats"]["streak"] == 0
        connection.request("GET", f"/api/task/{entry['id']}/crop")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("image/png")
        response.read()
        connection.request(
            "POST", f"/api/task/{entry['id']}/review",
            body=json.dumps({"verified_text": "অতুল আয়়", "decision": "qwen",
                             "revision": 0, "elapsed_ms": 12000}),
            headers={"Content-Type": "application/json", "X-Gold-Token": session["token"]},
        )
        response = connection.getresponse()
        saved = json.loads(response.read())
        assert response.status == 200 and saved["entry"]["review"]["decision"] == "qwen"
        assert saved["progress"]["xp"] > 0
        connection.request("POST", f"/api/task/{entry['id']}/review", body="{}")
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_verified_save_requires_available_matching_render_but_flag_is_allowed(tmp_path):
    missing = tmp_path / "missing.pdf"
    mismatched = tmp_path / "mismatched.pdf"
    mismatched.write_bytes(b"changed source")
    entries = [
        _entry("e" * 32, missing, "missing-hash"),
        _entry("f" * 32, mismatched, "frozen-hash"),
    ]
    app = GoldApp(_manifest(tmp_path / "manifest.json", entries), tmp_path / "gold.sqlite3",
                  pdf_handler=_FakeHandler())
    server = GoldHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        token = json.loads(response.read())["token"]
        for entry in entries:
            connection.request("POST", f"/api/task/{entry['id']}/review",
                               body=json.dumps({"verified_text": "বাংলা", "decision": "qwen", "revision": 0}),
                               headers={"Content-Type": "application/json", "X-Gold-Token": token})
            response = connection.getresponse()
            assert response.status == 409
            assert "Cannot save verified label" in json.loads(response.read())["error"]
        connection.request("POST", f"/api/task/{entries[0]['id']}/review",
                           body=json.dumps({"verified_text": "", "decision": "flag", "revision": 0}),
                           headers={"Content-Type": "application/json", "X-Gold-Token": token})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


class _ConcurrentDocument:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def render_page(self, page, dpi):
        assert dpi == 300
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        image = Image.new("RGB", (100, 80), "red" if page == 1 else "blue")
        with self._lock:
            self.active -= 1
        return image

    def close(self):
        pass


class _ConcurrentHandler:
    def __init__(self):
        self.document = _ConcurrentDocument()

    def open(self, source):
        return self.document


def test_concurrent_crops_serialize_shared_document_state(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"stable source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    entries = [_entry("a" * 32, source, digest) | {"page": 1},
               _entry("b" * 32, source, digest) | {"page": 2},
               _entry("c" * 32, source, digest) | {"page": 1},
               _entry("d" * 32, source, digest) | {"page": 2}]
    handler = _ConcurrentHandler()
    app = GoldApp(_manifest(tmp_path / "manifest.json", entries), tmp_path / "gold.sqlite3",
                  pdf_handler=handler)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(app.crop, [entry["id"] for entry in entries]))
        assert all(outputs)
        assert handler.document.max_active == 1
    finally:
        app.close()
