"""Tests for the safe whole-corpus deduplication workflow.

Everything here runs against a throwaway side database and a tiny synthetic
corpus in ``tmp_path``: the real ``data/`` collection and the live catalogue
database are never touched.  Poppler is never invoked either -- the tests
inject stub page counters, fingerprinters and signers, so they exercise the
workflow logic (caching, matching, report reconciliation, apply, rollback)
rather than the renderers.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import pdf_craft.catalogue.page_fingerprint as page_fp
from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.dedupe import (
    DEDUPE_TOOL_VERSION,
    DuplicateCluster,
    DuplicateEdge,
    apply_report,
    analyze_run,
    build_clusters,
    build_report,
    classify_relation,
    inventory_corpus,
    load_report_file,
    reconcile_report,
    rollback_manifest,
    safe_removal_candidates,
    sha256_edges,
    write_report_files,
)
from pdf_craft.catalogue.page_fingerprint import (
    PageFingerprint,
    find_duplicate_documents,
)


# --- Fixtures and fakes -------------------------------------------------------


@pytest.fixture()
def side_db(tmp_path: Path) -> CatalogueDB:
    db = CatalogueDB(tmp_path / "side.db")
    yield db
    db.close()


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _content_pages(content: bytes) -> list[PageFingerprint]:
    """Two informative interior pages derived deterministically from bytes."""
    digest = hashlib.sha256(content).digest()
    first = int.from_bytes(digest[:8], "big")
    second = int.from_bytes(digest[8:16], "big")
    return [
        PageFingerprint(1, first, 50.0, True),
        PageFingerprint(2, second, 50.0, True),
    ]


class StubPipeline:
    """Poppler-free stand-ins that read file bytes instead of rendering."""

    def __init__(self, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()
        self.fingerprint_calls = 0

    def page_counter(self, path: str) -> int:
        if Path(path).name in self.fail_on:
            raise RuntimeError("boom: cannot count pages")
        return 100

    def fingerprinter(self, path: str, page_count: int):
        self.fingerprint_calls += 1
        if Path(path).name in self.fail_on:
            raise RuntimeError("boom: cannot render")
        return _content_pages(Path(path).read_bytes())

    def text_signer(self, document_id: int, path: str, media_type: str):
        return None


def _corpus(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "corpus"
    for name, payload in files.items():
        _write(root / name, payload)
    return root


def _run_all(side_db: CatalogueDB, root: Path, stub: StubPipeline,
             **kwargs) -> tuple[int, dict]:
    run_id = inventory_corpus(side_db.conn, root)
    summary = analyze_run(
        side_db.conn, run_id,
        page_counter=stub.page_counter,
        fingerprinter=stub.fingerprinter,
        text_signer=stub.text_signer,
        with_metadata=False,
        **kwargs,
    )
    return run_id, summary


# --- Inventory ----------------------------------------------------------------


def test_inventory_hashes_contents_and_keeps_aliases(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"same-bytes", "sub/b.pdf": b"same-bytes", "c.pdf": b"other",
    })
    run_id = inventory_corpus(side_db.conn, root)
    report = build_report(side_db.conn, run_id)
    assert report["inventory"]["discovered_paths"] == 3
    assert report["inventory"]["ok"] == 3
    assert report["inventory"]["unique_contents"] == 2
    documents = side_db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_local_documents").fetchone()[0]
    assert documents == 2  # identical bytes share one document row
    locations = side_db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_document_locations").fetchone()[0]
    assert locations == 3  # ... while every path keeps its alias


def test_inventory_records_malformed_paths_without_aborting(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {"good.pdf": b"readable"})
    dangling = root / "dangling.pdf"
    dangling.symlink_to(root / "does-not-exist.pdf")
    run_id = inventory_corpus(side_db.conn, root)
    report = build_report(side_db.conn, run_id)
    assert report["inventory"]["ok"] == 1
    assert report["inventory"]["unreadable"] == 1
    assert report["inventory"]["failed_total"] == 1
    assert report["failures"][0]["stage"] == "inventory"
    assert reconcile_report(report) == []


def test_fingerprint_failure_does_not_abort_the_run(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {"bad.pdf": b"unrenderable", "good.pdf": b"fine"})
    stub = StubPipeline(fail_on={"bad.pdf"})
    run_id, summary = _run_all(side_db, root, stub)
    assert summary["run_id"] == run_id
    failures = side_db.conn.execute(
        "SELECT stage FROM dedupe_run_failures WHERE run_id=?", (run_id,),
    ).fetchall()
    stages = [row[0] for row in failures]
    assert "fingerprint" in stages or "page_count" in stages
    report = build_report(side_db.conn, run_id)
    assert reconcile_report(report) == []


# --- Cache invalidation and restart -------------------------------------------


def test_fingerprints_reused_on_rerun_but_not_after_change(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {"a.pdf": b"v1-bytes", "b.pdf": b"steady"})
    stub = StubPipeline()
    run_id = inventory_corpus(side_db.conn, root)
    analyze_run(side_db.conn, run_id, page_counter=stub.page_counter,
                fingerprinter=stub.fingerprinter,
                text_signer=stub.text_signer, with_metadata=False)
    first_calls = stub.fingerprint_calls
    assert first_calls == 2
    # Restart: a fresh inventory plus analyze over unchanged files reuses the
    # cache keyed by SHA-256 plus algorithm version.
    run_id2 = inventory_corpus(side_db.conn, root)
    analyze_run(side_db.conn, run_id2, page_counter=stub.page_counter,
                fingerprinter=stub.fingerprinter,
                text_signer=stub.text_signer, with_metadata=False)
    assert stub.fingerprint_calls == first_calls
    # Change one file: its new content misses the cache and is recomputed,
    # while the untouched file still reuses its rows.
    _write(root / "a.pdf", b"v2-bytes!!")
    run_id3 = inventory_corpus(side_db.conn, root)
    analyze_run(side_db.conn, run_id3, page_counter=stub.page_counter,
                fingerprinter=stub.fingerprinter,
                text_signer=stub.text_signer, with_metadata=False)
    assert stub.fingerprint_calls == first_calls + 1


def test_algorithm_version_bump_invalidates_cache(
    side_db: CatalogueDB, tmp_path: Path, monkeypatch,
) -> None:
    root = _corpus(tmp_path, {"a.pdf": b"bytes"})
    stub = StubPipeline()
    run_id = inventory_corpus(side_db.conn, root)
    analyze_run(side_db.conn, run_id, page_counter=stub.page_counter,
                fingerprinter=stub.fingerprinter,
                text_signer=stub.text_signer, with_metadata=False)
    assert stub.fingerprint_calls == 1
    monkeypatch.setattr(page_fp, "FINGERPRINT_ALGO_VERSION", "test-bump-v2")
    run_id2 = inventory_corpus(side_db.conn, root)
    analyze_run(side_db.conn, run_id2, page_counter=stub.page_counter,
                fingerprinter=stub.fingerprinter,
                text_signer=stub.text_signer, with_metadata=False)
    assert stub.fingerprint_calls == 2


# --- Relations: duplicate vs format_variant vs same_work -----------------------


def test_byte_identical_same_format_is_duplicate() -> None:
    edges = sha256_edges([(1, "abc"), (2, "abc"), (3, "def")])
    media = {1: "application/pdf", 2: "application/pdf", 3: "application/pdf"}
    assert [(edge.left_id, edge.right_id) for edge in edges] == [(1, 2)]
    assert classify_relation(edges[0], media) == "duplicate"


def test_text_minhash_never_authorizes_removal() -> None:
    same_format = DuplicateEdge(1, 2, "text_minhash", 0.9, {})
    cross_format = DuplicateEdge(3, 4, "text_minhash", 0.9, {})
    same_media = {1: "application/pdf", 2: "application/pdf"}
    mixed_media = {3: "application/pdf", 4: "application/epub+zip"}
    assert classify_relation(same_format, same_media) == "same_work"
    assert classify_relation(cross_format, mixed_media) == "format_variant"
    clusters = build_clusters([same_format], same_media,
                              relation="same_work")
    assert len(clusters) == 1
    assert clusters[0].removable is False


def test_metadata_edges_are_same_work_only() -> None:
    edge = DuplicateEdge(1, 2, "metadata_exact", 1.0, {})
    media = {1: "application/pdf", 2: "application/pdf"}
    assert classify_relation(edge, media) == "same_work"
    assert build_clusters([edge], media, relation="duplicate") == []


def test_shared_cover_alone_is_not_a_duplicate() -> None:
    # Two books from one press share an identical cover (page 0) but their
    # interiors differ: with only the cover in common there is no pair.
    fingerprints = {
        1: [PageFingerprint(0, 777, 60.0, True),
            *_content_pages(b"first-book")],
        2: [PageFingerprint(0, 777, 60.0, True),
            *_content_pages(b"second-book")],
    }
    assert find_duplicate_documents(fingerprints, {1: 100, 2: 100}) == []


def test_transitive_member_without_direct_evidence_is_not_removable() -> None:
    # A-B and B-C agree visually, but C was never directly compared with the
    # keeper A: C must stay out of the removal list.
    edges = [
        DuplicateEdge(1, 2, "page_image", 3.0, {}),
        DuplicateEdge(2, 3, "page_image", 3.0, {}),
    ]
    media = {1: "application/pdf", 2: "application/pdf", 3: "application/pdf"}
    clusters = build_clusters(edges, media, relation="duplicate")
    assert len(clusters) == 1
    assert clusters[0].document_ids == [1, 2, 3]
    clusters[0].keeper_id = 1
    assert clusters[0].removable is True
    assert safe_removal_candidates(clusters, edges, media) == [2]


# --- Report --------------------------------------------------------------------


def test_report_reconciles_and_identifies_keeper(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes",
        "b.pdf": b"shared-scan-bytes",
        "c.pdf": b"entirely-other-book",
    })
    stub = StubPipeline()
    run_id, _ = _run_all(side_db, root, stub)
    report = build_report(side_db.conn, run_id)
    assert reconcile_report(report) == []
    assert report["dedupe_tool_version"] == DEDUPE_TOOL_VERSION
    assert report["inventory"]["unique_contents"] == 2
    assert report["summary"]["duplicate_clusters"] == 1
    assert len(report["removable"]) == 1
    candidate = report["removable"][0]
    assert candidate["keeper_path"].endswith("a.pdf")
    assert candidate["path"].endswith("b.pdf")
    assert report["estimated_bytes_saved"] == candidate["file_size"] > 0


def test_report_files_round_trip(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
    })
    stub = StubPipeline()
    run_id, _ = _run_all(side_db, root, stub)
    report = build_report(side_db.conn, run_id)
    json_out, csv_out = tmp_path / "report.json", tmp_path / "report.csv"
    write_report_files(report, json_out, csv_out)
    reloaded = load_report_file(json_out)
    assert reloaded["run_id"] == run_id
    assert reconcile_report(reloaded) == []
    rows = csv_out.read_text(encoding="utf-8").strip().splitlines()
    assert rows[0].startswith("path,sha256,")
    assert len(rows) == 2  # header plus the one removable candidate


# --- Apply and rollback ---------------------------------------------------------


def test_apply_dry_run_moves_nothing(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
    })
    stub = StubPipeline()
    run_id, _ = _run_all(side_db, root, stub)
    report = build_report(side_db.conn, run_id)
    summary = apply_report(side_db.conn, report,
                           quarantine_root=tmp_path / "quarantine",
                           dry_run=True)
    assert summary["dry_run"] is True
    assert summary["manifest"] is None
    assert (root / "a.pdf").is_file() and (root / "b.pdf").is_file()
    assert not (tmp_path / "quarantine").exists()


def test_apply_moves_loser_and_rollback_restores(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
        "c.pdf": b"entirely-other-book",
    })
    stub = StubPipeline()
    run_id, _ = _run_all(side_db, root, stub)
    report = build_report(side_db.conn, run_id)
    quarantine = tmp_path / "quarantine"
    manifest = tmp_path / "manifest.json"
    summary = apply_report(side_db.conn, report, quarantine_root=quarantine,
                           manifest_path=manifest)
    assert summary["moved"] == 1
    assert (root / "a.pdf").is_file()  # keeper stays
    assert not (root / "b.pdf").exists()  # loser moved, not deleted
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["run_id"] == run_id
    assert sum(1 for m in payload["moves"] if m["status"] == "moved") == 1

    rolled = rollback_manifest(manifest)
    assert rolled == {"restored": 1, "conflicts": [], "conflict_count": 0}
    assert (root / "b.pdf").is_file()

    # Rolling back twice cannot restore what is no longer quarantined.
    again = rollback_manifest(manifest)
    assert again["restored"] == 0
    assert again["conflict_count"] == 1
    assert again["conflicts"][0]["reason"] == "dst-missing"


def test_apply_skips_changed_files_and_never_overwrites(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
    })
    stub = StubPipeline()
    run_id, _ = _run_all(side_db, root, stub)
    report = build_report(side_db.conn, run_id)
    # The file changed after analysis: the pre-move SHA check must skip it.
    _write(root / "b.pdf", b"edited-after-analysis")
    quarantine = tmp_path / "quarantine"
    summary = apply_report(side_db.conn, report, quarantine_root=quarantine)
    assert summary["moved"] == 0
    assert summary["skipped"].get("changed") == 1
    assert (root / "b.pdf").is_file()

    # A colliding quarantine name with different bytes gets a suffixed name.
    _write(root / "b.pdf", b"shared-scan-bytes")
    report2 = build_report(side_db.conn, run_id)
    victim = report2["removable"][0]
    clash_dir = quarantine / victim["sha256"][:2]
    clash_dir.mkdir(parents=True, exist_ok=True)
    decoy = clash_dir / f"{victim['sha256']}_{Path(victim['path']).name}"
    _write(decoy, b"unrelated-preexisting-file")
    summary2 = apply_report(side_db.conn, report2, quarantine_root=quarantine)
    assert summary2["moved"] == 1
    assert decoy.read_bytes() == b"unrelated-preexisting-file"
    assert not Path(victim["path"]).exists()


def test_apply_requires_report_and_quarantine_root(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {"a.pdf": b"bytes"})
    stub = StubPipeline()
    run_id, _ = _run_all(side_db, root, stub)
    report = build_report(side_db.conn, run_id)
    with pytest.raises(ValueError, match="quarantine-root"):
        apply_report(side_db.conn, report, quarantine_root="  ")
    with pytest.raises(ValueError, match="report"):
        apply_report(side_db.conn, {"nope": True},
                     quarantine_root=tmp_path / "q")


def test_rollback_conflicts_when_original_reappeared(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
    })
    stub = StubPipeline()
    run_id, _ = _run_all(side_db, root, stub)
    report = build_report(side_db.conn, run_id)
    manifest = tmp_path / "manifest.json"
    apply_report(side_db.conn, report,
                 quarantine_root=tmp_path / "quarantine",
                 manifest_path=manifest)
    _write(root / "b.pdf", b"a-new-file-now-lives-here")
    rolled = rollback_manifest(manifest)
    assert rolled["restored"] == 0
    assert rolled["conflict_count"] == 1
    assert rolled["conflicts"][0]["reason"] == "src-exists"


def test_apply_rejects_nested_quarantine_and_outside_report_path(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
    })
    run_id, _ = _run_all(side_db, root, StubPipeline())
    report = build_report(side_db.conn, run_id)
    with pytest.raises(ValueError, match="disjoint from the corpus"):
        apply_report(side_db.conn, report, quarantine_root=root / "quarantine")
    outside = tmp_path / "outside.pdf"
    _write(outside, b"shared-scan-bytes")
    tampered = json.loads(json.dumps(report))
    tampered["removable"][0]["path"] = str(outside)
    summary = apply_report(side_db.conn, tampered,
                           quarantine_root=tmp_path / "quarantine")
    assert summary["moved"] == 0
    assert summary["skipped"].get("not-in-run") == 1
    assert outside.is_file()


def test_rollback_rejects_tampered_quarantine_copy(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
    })
    run_id, _ = _run_all(side_db, root, StubPipeline())
    report = build_report(side_db.conn, run_id)
    manifest = tmp_path / "manifest.json"
    apply_report(side_db.conn, report, quarantine_root=tmp_path / "quarantine",
                 manifest_path=manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    moved = next(item for item in payload["moves"] if item["status"] == "moved")
    Path(moved["dst"]).write_bytes(b"tampered")
    rolled = rollback_manifest(manifest)
    assert rolled["restored"] == 0
    assert rolled["conflicts"][0]["reason"] == "hash-mismatch"


def test_valid_symlink_is_not_followed(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {"real.pdf": b"bytes"})
    (root / "alias.pdf").symlink_to(root / "real.pdf")
    run_id = inventory_corpus(side_db.conn, root)
    statuses = dict(side_db.conn.execute(
        "SELECT source_path, status FROM dedupe_path_identities WHERE run_id=?",
        (run_id,),
    ).fetchall())
    assert statuses[str(root / "alias.pdf")] == "unreadable"


def test_invalid_report_timestamp_fails_closed(
    side_db: CatalogueDB, tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, {
        "a.pdf": b"shared-scan-bytes", "b.pdf": b"shared-scan-bytes",
    })
    run_id, _ = _run_all(side_db, root, StubPipeline())
    report = build_report(side_db.conn, run_id)
    report["created_at"] = "not-a-timestamp"
    with pytest.raises(ValueError, match="invalid created_at"):
        apply_report(side_db.conn, report, quarantine_root=tmp_path / "q")


def test_duplicate_cluster_helper_marks_only_removable() -> None:
    cluster = DuplicateCluster(
        document_ids=[1, 2], methods={"sha256"}, relation="duplicate",
        status="auto", keeper_id=1,
    )
    assert cluster.removable is True
    review = DuplicateCluster(
        document_ids=[1, 2], methods={"sha256"}, relation="duplicate",
        status="review", keeper_id=1,
    )
    assert review.removable is False
    variant = DuplicateCluster(
        document_ids=[1, 2], methods={"text_minhash"},
        relation="format_variant", status="auto", keeper_id=1,
    )
    assert variant.removable is False
