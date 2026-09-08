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
import uuid
from pathlib import Path

import pytest

import pdf_craft.catalogue.page_fingerprint as page_fp
from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.dedupe import (
    DEDUPE_TOOL_VERSION,
    MAX_AUTO_CLUSTER,
    RELATION_DUPLICATE,
    RELATION_FORMAT_VARIANT,
    RELATION_SAME_WORK,
    DocumentMetadata,
    DocumentQuality,
    DuplicateCluster,
    DuplicateEdge,
    apply_report,
    analyze_run,
    assign_keepers,
    build_clusters,
    build_report,
    choose_keeper,
    classify_relation,
    inventory_corpus,
    load_document_facts,
    load_report_file,
    metadata_edges,
    quality_score,
    removal_candidates,
    reconcile_report,
    rollback_manifest,
    safe_removal_candidates,
    sha256_edges,
    store_clusters,
    store_edges,
    summarize,
    write_report_files,
)
from pdf_craft.catalogue.schema import initialize_database
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


# --- Spec: relation classification, clustering, keepers, safety -----------------
#
# The tests below pin the three meanings of "same book" (duplicate /
# format_variant / same_work).  Only `duplicate` may ever remove a file.


PDF = "application/pdf"
EPUB = "application/epub+zip"


def _pdf_media(*ids: int) -> dict[int, str]:
    return {doc_id: PDF for doc_id in ids}


def test_page_image_match_between_pdfs_is_a_duplicate() -> None:
    edge = DuplicateEdge(1, 2, "page_image", 3.0, {})
    assert classify_relation(edge, _pdf_media(1, 2)) == RELATION_DUPLICATE


def test_epub_pdf_pair_is_a_format_variant_not_a_duplicate() -> None:
    # A PDF and an EPUB of the same book are both kept: different formats
    # are not competing copies.  Getting this wrong deletes a user's books.
    edge = DuplicateEdge(1, 2, "text_minhash", 0.9, {})
    media = {1: PDF, 2: EPUB}
    assert classify_relation(edge, media) == RELATION_FORMAT_VARIANT
    assert classify_relation(edge, media) != RELATION_DUPLICATE


def test_metadata_match_alone_is_same_work_never_a_duplicate() -> None:
    # Metadata cannot tell a duplicate file from a second printing, so it
    # must never justify removal.
    edge = DuplicateEdge(1, 2, "metadata_exact", 1.0, {})
    assert classify_relation(edge, _pdf_media(1, 2)) == RELATION_SAME_WORK


def test_unknown_media_types_never_justify_removal() -> None:
    # Without known media types the edge cannot prove same-format
    # redundancy, so it falls back to the non-removal same_work relation.
    # (Conservative on purpose: unknown evidence must keep both files.)
    for method in ("sha256", "page_image", "text_minhash"):
        edge = DuplicateEdge(1, 2, method, 1.0, {})
        assert classify_relation(edge, {}) == RELATION_SAME_WORK


def test_duplicate_edges_close_transitively() -> None:
    edges = [
        DuplicateEdge(1, 2, "page_image", 3.0, {}),
        DuplicateEdge(2, 3, "page_image", 3.0, {}),
    ]
    clusters = build_clusters(edges, _pdf_media(1, 2, 3),
                              relation=RELATION_DUPLICATE)
    assert [cluster.document_ids for cluster in clusters] == [[1, 2, 3]]


def test_duplicate_clustering_does_not_pull_in_the_epub() -> None:
    # The EPUB linked by a format_variant edge must not be dragged into the
    # redundant-PDF cluster, where it could be scored out and removed.
    edges = [
        DuplicateEdge(1, 2, "page_image", 3.0, {}),
        DuplicateEdge(2, 3, "sha256", 1.0, {}),
    ]
    media = {1: PDF, 2: PDF, 3: EPUB}
    clusters = build_clusters(edges, media, relation=RELATION_DUPLICATE)
    assert [cluster.document_ids for cluster in clusters] == [[1, 2]]


def test_oversized_cluster_needs_review_and_is_not_removable() -> None:
    members = list(range(1, MAX_AUTO_CLUSTER + 2))  # one past the limit
    edges = [
        DuplicateEdge(left, left + 1, "page_image", 3.0, {})
        for left in members[:-1]
    ]
    clusters = build_clusters(edges, _pdf_media(*members),
                              relation=RELATION_DUPLICATE)
    assert len(clusters) == 1
    assert clusters[0].status == "review"
    assert clusters[0].removable is False  # a human must look first


def test_singleton_documents_form_no_clusters() -> None:
    edges = [DuplicateEdge(1, 2, "page_image", 3.0, {})]
    clusters = build_clusters(edges, _pdf_media(1, 2, 3),
                              relation=RELATION_DUPLICATE)
    assert all(cluster.size > 1 for cluster in clusters)
    assert 3 not in {doc for cluster in clusters for doc in cluster.document_ids}


def test_reversed_edge_endpoints_cluster_identically() -> None:
    edge = DuplicateEdge(5, 2, "sha256", 1.0, {})
    assert edge.normalized().left_id == 2
    assert edge.normalized().right_id == 5
    media = _pdf_media(2, 5)
    forward = build_clusters([DuplicateEdge(2, 5, "sha256", 1.0, {})],
                             media, relation=RELATION_DUPLICATE)
    reversed_ = build_clusters([edge], media, relation=RELATION_DUPLICATE)
    assert forward[0].document_ids == reversed_[0].document_ids == [2, 5]


def test_higher_ocr_confidence_wins_between_identical_scans() -> None:
    # Headline behaviour: scan quality decides between identical scans.
    sharp = DocumentQuality(1, ocr_confidence=90.0)
    blurry = DocumentQuality(2, ocr_confidence=40.0)
    assert choose_keeper([sharp, blurry])[0] == 1
    assert choose_keeper([blurry, sharp])[0] == 1


def test_ocr_confidence_outranks_source_trust() -> None:
    stained = DocumentQuality(1, source_template="granthagara",
                              ocr_confidence=90.0)
    clean = DocumentQuality(2, source_template="gutenberg_bengali",
                            ocr_confidence=40.0)
    assert choose_keeper([stained, clean])[0] == 1


def test_copy_with_a_text_layer_wins_when_all_else_equal() -> None:
    layered = DocumentQuality(1, has_text_layer=True)
    scan = DocumentQuality(2, has_text_layer=False)
    assert choose_keeper([layered, scan])[0] == 1


def test_longer_complete_copy_wins() -> None:
    excerpt = DocumentQuality(1, page_count=40, file_size=1000)
    full = DocumentQuality(2, page_count=500, file_size=1000)
    assert choose_keeper([excerpt, full])[0] == 2


def test_resolution_ignored_when_page_counts_differ_widely() -> None:
    short = DocumentQuality(1, page_count=40, file_size=100)
    long_ = DocumentQuality(2, page_count=500, file_size=200)
    _, scores = choose_keeper([short, long_])
    assert "resolution" not in scores[1][1]


def test_resolution_counts_when_page_counts_agree() -> None:
    small = DocumentQuality(1, page_count=100, file_size=100)
    large = DocumentQuality(2, page_count=100, file_size=200)
    _, scores = choose_keeper([small, large])
    assert "resolution" in scores[1][1]


def test_keeper_tie_breaks_on_lowest_document_id() -> None:
    first, _ = choose_keeper([DocumentQuality(7), DocumentQuality(3)])
    second, _ = choose_keeper([DocumentQuality(3), DocumentQuality(7)])
    assert first == second == 3


def test_choose_keeper_rejects_an_empty_cluster() -> None:
    with pytest.raises(ValueError):
        choose_keeper([])


def test_scorecard_terms_sum_to_the_total() -> None:
    document = DocumentQuality(1, file_size=500, page_count=100,
                               has_text_layer=True, has_title=True,
                               has_authors=True, catalogue_matched=True,
                               source_template="gutenberg_bengali",
                               ocr_confidence=80.0)
    total, terms = quality_score(document, max_page_count=100,
                                 max_file_size=500, page_counts_agree=True)
    assert total == pytest.approx(sum(terms.values()))


def _removable_cluster(**overrides) -> DuplicateCluster:
    fields: dict = {
        "document_ids": [1, 2, 3],
        "methods": {"sha256"},
        "relation": RELATION_DUPLICATE,
        "status": "auto",
        "keeper_id": 1,
    }
    fields.update(overrides)
    return DuplicateCluster(**fields)


def test_removal_candidates_exclude_the_keeper() -> None:
    # The keeper must never appear in its own removal list.
    candidates = removal_candidates([_removable_cluster()])
    assert 1 not in candidates
    assert candidates == [2, 3]


def test_format_variant_cluster_removes_nothing() -> None:
    # Both formats are kept: the EPUB is a reading copy, not a spare.
    cluster = _removable_cluster(relation=RELATION_FORMAT_VARIANT)
    assert removal_candidates([cluster]) == []


def test_same_work_cluster_removes_nothing() -> None:
    # Metadata alone may be two genuine printings; both stay.
    cluster = _removable_cluster(relation=RELATION_SAME_WORK)
    assert removal_candidates([cluster]) == []


def test_review_cluster_removes_nothing() -> None:
    # Oversized clusters wait for a human; nothing is actionable yet.
    cluster = _removable_cluster(status="review")
    assert removal_candidates([cluster]) == []


def test_summary_reports_zero_removable_for_links_only() -> None:
    clusters = [
        _removable_cluster(document_ids=[1, 2],
                           relation=RELATION_FORMAT_VARIANT),
        _removable_cluster(document_ids=[3, 4],
                           relation=RELATION_SAME_WORK),
    ]
    assert summarize(clusters)["removable_copies"] == 0


def test_identical_title_keys_link_exactly() -> None:
    documents = [
        DocumentMetadata(1, title_key="padma river boatman tale", author_key="a"),
        DocumentMetadata(2, title_key="padma river boatman tale", author_key="b"),
    ]
    edges = metadata_edges(documents)
    assert [(edge.left_id, edge.right_id, edge.method) for edge in edges] == [
        (1, 2, "metadata_exact")]


def test_short_title_keys_link_nothing() -> None:
    # A two-syllable title collides with hundreds of books.
    documents = [
        DocumentMetadata(1, title_key="golpo", author_key="a"),
        DocumentMetadata(2, title_key="golpo", author_key="a"),
    ]
    assert metadata_edges(documents) == []


def test_template_title_shared_by_many_links_nothing() -> None:
    # A stamp shared by dozens of files is boilerplate, not one book.
    documents = [
        DocumentMetadata(index, title_key="granthagara branded scan copy")
        for index in range(21)
    ]
    assert metadata_edges(documents) == []


def test_same_author_near_identical_titles_link_fuzzily() -> None:
    left = "a very long book title about the rivers of bengal volume one"
    right = "a very long book title about the rivers of bengal volume two"
    documents = [
        DocumentMetadata(1, title_key=left, author_key="manik bandopadhyay"),
        DocumentMetadata(2, title_key=right, author_key="manik bandopadhyay"),
    ]
    edges = metadata_edges(documents)
    assert [(edge.left_id, edge.right_id, edge.method) for edge in edges] == [
        (1, 2, "metadata_fuzzy")]


def test_same_author_unrelated_titles_do_not_link() -> None:
    documents = [
        DocumentMetadata(1, title_key="a very long book title about the rivers of bengal volume one",
                         author_key="manik bandopadhyay"),
        DocumentMetadata(2, title_key="completely different cookbook recipes for winter",
                         author_key="manik bandopadhyay"),
    ]
    assert metadata_edges(documents) == []


def test_exact_pair_gets_no_extra_fuzzy_edge() -> None:
    documents = [
        DocumentMetadata(1, title_key="padma river boatman tale", author_key="a"),
        DocumentMetadata(2, title_key="padma river boatman tale", author_key="a"),
    ]
    assert len(metadata_edges(documents)) == 1


def _seed_documents(conn, count: int, metadata: dict | None = None):
    ids = []
    for _ in range(count):
        cursor = conn.execute(
            "INSERT INTO catalogue_local_documents "
            "(sha256, source_path, file_size, media_type, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, f"/books/{uuid.uuid4().hex}.pdf", 100, PDF,
             json.dumps(metadata or {"title": "T", "authors": ["A"]})),
        )
        ids.append(int(cursor.lastrowid))
    conn.commit()
    return ids


def test_store_edges_is_idempotent(tmp_path: Path) -> None:
    conn = initialize_database(tmp_path / "c.db")
    first, second = _seed_documents(conn, 2)
    edge = DuplicateEdge(first, second, "sha256", 1.0, {})
    store_edges(conn, [edge])
    before = conn.execute(
        "SELECT COUNT(*) FROM catalogue_duplicate_edges").fetchone()[0]
    store_edges(conn, [edge])
    after = conn.execute(
        "SELECT COUNT(*) FROM catalogue_duplicate_edges").fetchone()[0]
    assert (before, after) == (1, 1)
    conn.close()


def test_store_clusters_marks_exactly_one_keeper(tmp_path: Path) -> None:
    conn = initialize_database(tmp_path / "c.db")
    ids = _seed_documents(conn, 3)
    cluster = DuplicateCluster(document_ids=sorted(ids), methods={"sha256"},
                               relation=RELATION_DUPLICATE, status="auto")
    scorecards = assign_keepers(
        [cluster], {doc_id: DocumentQuality(doc_id) for doc_id in ids})
    store_clusters(conn, [cluster], scorecards)
    keepers = conn.execute(
        "SELECT cluster_id, SUM(is_keeper) FROM catalogue_duplicate_members "
        "GROUP BY cluster_id").fetchall()
    assert [row[1] for row in keepers] == [1]
    conn.close()


def test_store_clusters_replace_preserves_human_decisions(tmp_path: Path) -> None:
    # A confirmed or rejected cluster is a human decision and must survive
    # a rerun; only automatic rows are replaced.
    conn = initialize_database(tmp_path / "c.db")
    ids = _seed_documents(conn, 4)
    cursor = conn.execute(
        "INSERT INTO catalogue_duplicate_clusters "
        "(relation, status, methods, size, keeper_document_id) "
        "VALUES ('duplicate', 'confirmed', 'sha256', 2, ?)", (ids[0],))
    confirmed_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO catalogue_duplicate_members "
        "(cluster_id, document_id, is_keeper, score, scorecard_json) "
        "VALUES (?, ?, 1, 1.0, '{}'), (?, ?, 0, 0.0, '{}')",
        (confirmed_id, ids[0], confirmed_id, ids[1]),
    )
    conn.execute(
        "INSERT INTO catalogue_duplicate_clusters "
        "(relation, status, methods, size, keeper_document_id) "
        "VALUES ('duplicate', 'auto', 'sha256', 1, NULL)")
    fresh = DuplicateCluster(document_ids=[ids[2], ids[3]],
                             methods={"sha256"},
                             relation=RELATION_DUPLICATE, status="auto",
                             keeper_id=ids[2])
    store_clusters(conn, [fresh],
                   {0: {ids[2]: (1.0, {}), ids[3]: (0.0, {})}}, replace=True)
    statuses = sorted(
        row[0] for row in
        conn.execute("SELECT status FROM catalogue_duplicate_clusters"))
    assert statuses == ["auto", "confirmed"]
    survivors = conn.execute(
        "SELECT COUNT(*) FROM catalogue_duplicate_members WHERE cluster_id=?",
        (confirmed_id,)).fetchone()[0]
    assert survivors == 2
    conn.close()


def test_load_document_facts_tolerates_malformed_json(tmp_path: Path) -> None:
    conn = initialize_database(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO catalogue_local_documents "
        "(sha256, source_path, file_size, media_type, metadata_json) "
        "VALUES ('good', '/good.pdf', 10, ?, ?)",
        (PDF, json.dumps({"title": "Padma", "authors": ["Manik"],
                          "page_count": 100})),
    )
    conn.execute(
        "INSERT INTO catalogue_local_documents "
        "(sha256, source_path, file_size, media_type, metadata_json) "
        "VALUES ('bad', '/bad.pdf', 10, ?, 'not json{')", (PDF,))
    conn.commit()
    qualities, _ = load_document_facts(conn)  # must not raise
    by_path = {
        row[0]: row[1] for row in conn.execute(
            "SELECT source_path, id FROM catalogue_local_documents")
    }
    good = qualities[by_path["/good.pdf"]]
    assert (good.has_title, good.has_authors) == (True, True)
    bad = qualities[by_path["/bad.pdf"]]
    assert (bad.has_title, bad.has_authors) == (False, False)
    conn.close()
