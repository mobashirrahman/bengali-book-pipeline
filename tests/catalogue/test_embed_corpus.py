"""Tests for the corpus-wide metadata-embedding driver.

These never touch the real catalogue.db or any real book file: the catalogue
is a small temporary SQLite database, and ``embed_document`` is replaced by a
recording fake injected into the driver.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from pdf_craft.catalogue.embed_metadata import (
    FAILED,
    SKIPPED,
    WRITTEN,
    EmbedResult,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "pdf-craft-output" / "agents" / "embed_corpus.py"

_spec = importlib.util.spec_from_file_location("embed_corpus_driver", SCRIPT)
driver = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("embed_corpus_driver", driver)
_spec.loader.exec_module(driver)


class FakeEmbed:
    """Records every call and returns programmable results per path.

    Mirrors the real embed_document's contract by default: a dry run
    returns SKIPPED/"dry run" without touching anything; a live call
    returns WRITTEN unless a per-path override says otherwise.
    """

    def __init__(self, live_results: dict[str, EmbedResult] | None = None):
        self.calls: list[dict] = []
        self.live_results = live_results or {}

    def __call__(self, path, media_type, *, title="", authors=(), dry_run=False):
        self.calls.append(
            {
                "path": str(path),
                "media_type": media_type,
                "title": title,
                "authors": list(authors),
                "dry_run": dry_run,
            }
        )
        key = str(path)
        if dry_run:
            return EmbedResult(key, SKIPPED, "dry run", "abc123")
        if key in self.live_results:
            return self.live_results[key]
        return EmbedResult(key, WRITTEN, "", "abc123")


def make_catalogue(tmp_path: Path, rows: list[tuple[int, str, str, dict]]) -> Path:
    db = tmp_path / "catalogue.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE catalogue_local_documents ("
        " id INTEGER PRIMARY KEY, sha256 TEXT, source_path TEXT, file_size INTEGER,"
        " media_type TEXT, metadata_json TEXT)"
    )
    for document_id, source_path, media_type, meta in rows:
        conn.execute(
            "INSERT INTO catalogue_local_documents "
            "(id, sha256, source_path, file_size, media_type, metadata_json)"
            " VALUES (?, ?, ?, 0, ?, ?)",
            (document_id, f"sha{document_id}", source_path, media_type,
             json.dumps(meta, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()
    return db


def audit_rows(audit_db: Path) -> dict[int, dict]:
    conn = sqlite3.connect(audit_db)
    conn.row_factory = sqlite3.Row
    try:
        return {
            row["document_id"]: dict(row)
            for row in conn.execute("SELECT * FROM embed_corpus_audit")
        }
    finally:
        conn.close()


@pytest.fixture
def catalogue(tmp_path):
    return _make_catalogue(tmp_path)


def _make_catalogue(tmp_path: Path):
    rows = [
        # 1: normal document -- both title and authors.
        (1, "books/one.pdf", "application/pdf",
         {"title": "আনন্দমঠ", "authors": ["বঙ্কিমচন্দ্র চট্টোপাধ্যায়"]}),
        # 2: denylisted title, real authors -> embed authors only.
        (2, "books/two.pdf", "application/pdf",
         {"title": "Masud Rana Pdf", "authors": ["কাজী নজরুল ইসলাম"]}),
        # 3: no title, no authors -> never call embed_document.
        (3, "books/three.pdf", "application/pdf",
         {"title": "", "authors": []}),
        # 4: title only.
        (4, "books/four.epub", "application/epub+zip",
         {"title": "চন্দ্রশেকর", "authors": []}),
        # 5: denylisted title, no authors -> nothing to embed.
        (5, "books/five.pdf", "application/pdf",
         {"title": "Title Page", "authors": []}),
    ]
    return make_catalogue(tmp_path, rows)


def test_denylisted_title_authors_still_embedded(catalogue, tmp_path):
    fake = FakeEmbed()
    counts = driver.run(catalogue, tmp_path / "audit.db", live=True, embed=fake)

    call_two = next(c for c in fake.calls if c["path"].endswith("books/two.pdf"))
    assert call_two["title"] == ""
    assert call_two["authors"] == ["কাজী নজরুল ইসলাম"]
    # The denylisted string itself must not appear as the title anywhere.
    assert all(c["title"] != "Masud Rana Pdf" for c in fake.calls)
    # And it IS processed (unlike document 3).
    assert counts.processed == 3  # docs 1, 2, 4


def test_no_title_no_authors_is_never_embedded(catalogue, tmp_path):
    fake = FakeEmbed()
    driver.run(catalogue, tmp_path / "audit.db", live=True, embed=fake)

    paths = [c["path"] for c in fake.calls]
    assert not any(p.endswith("books/three.pdf") for p in paths)
    assert not any(p.endswith("books/five.pdf") for p in paths)
    row = audit_rows(tmp_path / "audit.db")[3]
    assert row["status"] == "SKIPPED"
    assert "no title and no authors" in row["reason"]


def test_default_is_dry_run_and_audits_would_be_outcome(catalogue, tmp_path):
    fake = FakeEmbed()
    driver.run(catalogue, tmp_path / "audit.db", live=False, embed=fake)

    assert fake.calls, "dry run must still call embed_document"
    assert all(c["dry_run"] is True for c in fake.calls)
    # The audit DB records the (skipped-because-dry-run) outcome per document.
    rows = audit_rows(tmp_path / "audit.db")
    assert rows[1]["status"] == "SKIPPED"
    assert rows[1]["reason"] == "dry run"


def test_written_documents_are_not_reprocessed(catalogue, tmp_path):
    audit_db = tmp_path / "audit.db"
    fake = FakeEmbed()
    driver.run(catalogue, audit_db, live=True, embed=fake)
    assert len(fake.calls) == 3

    rows = audit_rows(audit_db)
    assert rows[1]["status"] == "WRITTEN"
    assert rows[1]["title_written"] == "আনন্দমঠ"
    assert json.loads(rows[1]["authors_written"]) == ["বঙ্কিমচন্দ্র চট্টোপাধ্যায়"]

    fake_again = FakeEmbed()
    counts = driver.run(catalogue, audit_db, live=True, embed=fake_again)
    assert fake_again.calls == [], "second run must not re-call embed_document"
    assert counts.already_written == 3
    assert counts.processed == 0


def test_limit_processes_only_first_n_eligible(catalogue, tmp_path):
    fake = FakeEmbed()
    counts = driver.run(catalogue, tmp_path / "audit.db", live=True,
                        limit=1, embed=fake)
    assert counts.processed == 1
    assert len(fake.calls) == 1
    assert fake.calls[0]["path"].endswith("books/one.pdf")


def test_failed_result_is_recorded(catalogue, tmp_path):
    # Path that never matches: force failure via a wrapper instead.
    def failing(path, media_type, **kwargs):
        return EmbedResult(str(path), FAILED, "missing", "")

    counts = driver.run(catalogue, tmp_path / "audit.db", live=True, embed=failing)
    assert counts.failed == 3
    rows = audit_rows(tmp_path / "audit.db")
    assert rows[1]["status"] == "FAILED"
    assert rows[1]["reason"] == "missing"


def test_catastrophic_metadata_json_is_skipped(tmp_path):
    db = make_catalogue(tmp_path, [
        (1, "books/bad.pdf", "application/pdf", {}),
    ])
    # Corrupt the JSON beyond parsing, as the driver would see it.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE catalogue_local_documents SET metadata_json = 'not json'")
    conn.commit()
    conn.close()

    fake = FakeEmbed()
    driver.run(db, tmp_path / "audit.db", live=True, embed=fake)
    assert fake.calls == []
    rows = audit_rows(tmp_path / "audit.db")
    assert rows[1]["status"] == "SKIPPED"
    assert "invalid metadata_json" in rows[1]["reason"]


def test_a_hanging_document_times_out_instead_of_stalling_the_batch(tmp_path):
    # A real corrupted PDF made pypdf loop for 6+ CPU-minutes on a single
    # file during the first live run of this script, with nothing committed
    # (see the next test) -- this reproduces that class of failure without
    # actually waiting minutes: a fake embed that sleeps past the timeout.
    import time

    db = make_catalogue(tmp_path, [
        (1, "books/hangs.pdf", "application/pdf", {"title": "T"}),
        (2, "books/fine.pdf", "application/pdf", {"title": "U"}),
    ])

    def hangs_then_fine(path, media_type, *, title="", authors=(), dry_run=False):
        if "hangs" in str(path):
            time.sleep(5)
            return EmbedResult(str(path), WRITTEN, "", "abc")
        return EmbedResult(str(path), WRITTEN, "", "abc")

    counts = driver.run(
        db, tmp_path / "audit.db", live=True, embed=hangs_then_fine,
        per_document_timeout=1,
    )
    rows = audit_rows(tmp_path / "audit.db")
    assert rows[1]["status"] == "FAILED"
    assert "timed out" in rows[1]["reason"]
    assert rows[2]["status"] == "WRITTEN"  # the hang never blocked document 2
    assert counts.failed == 1
    assert counts.written == 1


def test_progress_is_committed_before_the_run_finishes(tmp_path, monkeypatch):
    # Regression: the first version of this driver only called
    # audit.commit() once, after the whole loop -- a kill (or a hang the
    # coordinator kills) partway through a long run lost every already-
    # verified WRITTEN row, defeating the resumability the audit table
    # exists for. Verified by reading the audit db from a second
    # connection mid-run, the way an external monitor actually would.
    rows_spec = [(i, f"books/{i}.pdf", "application/pdf", {"title": f"T{i}"}) for i in range(1, 6)]
    db = make_catalogue(tmp_path, rows_spec)
    audit_path = tmp_path / "audit.db"

    seen_mid_run: list[int] = []
    fake = FakeEmbed()
    real_call = fake.__call__

    def spying_call(path, media_type, *, title="", authors=(), dry_run=False):
        result = real_call(path, media_type, title=title, authors=authors, dry_run=dry_run)
        # A second, independent connection -- exactly what a monitor
        # tailing progress while the run is still going would use.
        monitor = sqlite3.connect(audit_path)
        seen_mid_run.append(
            monitor.execute(
                "SELECT COUNT(*) FROM embed_corpus_audit WHERE status='WRITTEN'"
            ).fetchone()[0]
        )
        monitor.close()
        return result

    driver.run(db, audit_path, live=True, embed=spying_call, commit_every=2)
    # By the time the 3rd document is processed, the first commit_every=2
    # batch must already be visible to an outside reader.
    assert seen_mid_run[2] >= 2


def test_main_reports_and_exits_zero(tmp_path, capsys, monkeypatch):
    fake = FakeEmbed()
    monkeypatch.setattr(driver, "embed_document", fake)
    code = driver.main([
        "--dry-run", "--limit", "2",
        "--catalogue", str(_make_catalogue(tmp_path)),
        "--audit", str(tmp_path / "a.db"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "mode=dry-run" in out
    assert "done:" in out
    assert "written=0" in out
    assert all(c["dry_run"] is True for c in fake.calls)
