import json
from pathlib import Path

import pytest

from pdf_craft.catalogue import CatalogueDB, CatalogueFoundation, generate_candidates
from pdf_craft.catalogue.importers.google_books import stage_google_books_response
from pdf_craft.catalogue.isbn import normalize_isbn, normalize_isbn10
from pdf_craft.catalogue.matching import normalize_bengali, title_sort_key
from pdf_craft.catalogue.materialization import materialize_source_records
from pdf_craft.catalogue.resolution import (
    MATCHER_VERSION,
    _title_similarity,
    _volume_relation,
    accept_match,
    reject_match,
)


def test_isbn_validation_supports_x_and_rejects_bad_checksums() -> None:
    assert normalize_isbn10("0-8044-2957-X") == "080442957X"
    assert normalize_isbn("0-306-40615-2") == "9780306406157"
    with pytest.raises(ValueError):
        normalize_isbn("9780306406158")


def test_google_staging_is_restartable_and_candidate_review_is_pending(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    document_path = tmp_path / "author" / "book.pdf"
    document_path.parent.mkdir()
    document_path.write_bytes(b"document")
    document = CatalogueFoundation(db).upsert_local_document(
        document_path,
        metadata={"title": "শেষের কবিতা", "author": "রবীন্দ্রনাথ ঠাকুর", "isbn": "9780306406157"},
    )
    payload = {"items": [{"id": "g-1", "volumeInfo": {
        "title": "শেষের কবিতা", "authors": ["রবীন্দ্রনাথ ঠাকুর"],
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780306406157"}],
    }}]}
    assert stage_google_books_response(db, payload) == (1, 0)
    assert stage_google_books_response(db, payload) == (1, 0)
    assert generate_candidates(db, document.id) == []
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_editions").fetchone()[0] == 0
    assert materialize_source_records(db, source="google_books") == {
        "seen": 1, "materialized": 1, "skipped": 0,
    }
    match_ids = generate_candidates(db, document.id)
    assert len(match_ids) == 1
    assert db.conn.execute("SELECT status FROM catalogue_document_matches").fetchone()[0] == "candidate"
    assert db.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def _canonical_edition(db: CatalogueDB, title: str, author: str | None = None) -> int:
    work = db.conn.execute(
        "INSERT INTO catalogue_works (title, sort_title) VALUES (?, ?)",
        (title, title_sort_key(title)),
    )
    edition = db.conn.execute(
        "INSERT INTO catalogue_editions (work_id, title) VALUES (?, ?)",
        (work.lastrowid, title),
    )
    if author:
        normalized = normalize_bengali(author).lower()
        person = db.conn.execute(
            "INSERT INTO catalogue_people (name, sort_name, normalized_name) VALUES (?, ?, ?)",
            (author, author, normalized),
        )
        db.conn.execute(
            "INSERT INTO catalogue_edition_people (edition_id, person_id, role, position) VALUES (?, ?, 'author', 0)",
            (edition.lastrowid, person.lastrowid),
        )
    db.conn.commit()
    return int(edition.lastrowid)


def test_matcher_uses_path_fallback_for_indexed_exact_title_and_author(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    edition_id = _canonical_edition(db, "গোরা", "বঙ্কিমচন্দ্র")
    document_path = tmp_path / "বঙ্কিমচন্দ্র" / "গোরা.pdf"
    document_path.parent.mkdir()
    document_path.write_bytes(b"document")
    document = CatalogueFoundation(db).upsert_local_document(document_path, metadata={})

    match_ids = generate_candidates(db, document.id)

    assert len(match_ids) == 1
    match = db.conn.execute(
        "SELECT edition_id, method, score, evidence_json, status FROM catalogue_document_matches"
    ).fetchone()
    assert tuple(match[:3]) == (edition_id, "exact_title_author", 1.0)
    evidence = json.loads(match[3])
    assert evidence["matcher_version"] == MATCHER_VERSION
    assert evidence["signals"] == ["exact_title", "exact_author"]
    assert match[4] == "candidate"
    db.close()


def test_fuzzy_matching_is_bounded_idempotent_and_preserves_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    for index in range(150):
        _canonical_edition(db, f"Near Book {index:03d}")
    document_path = tmp_path / ".books" / "Near Book.pdf"
    document_path.parent.mkdir()
    document_path.write_bytes(b"document")
    document = CatalogueFoundation(db).upsert_local_document(document_path, metadata={})

    from pdf_craft.catalogue import resolution

    calls = 0
    original = resolution.fuzzy_match_score

    def counted_score(query: str, candidate: str) -> float:
        nonlocal calls
        calls += 1
        return original(query, candidate)

    monkeypatch.setattr(resolution, "fuzzy_match_score", counted_score)
    match_ids = generate_candidates(db, document.id)

    assert len(match_ids) == 10
    assert calls <= 100
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_document_matches").fetchone()[0] == 10
    first = match_ids[0]
    reject_match(db, first, reviewer="reviewer", reason="needs manual verification")
    assert generate_candidates(db, document.id) == match_ids
    assert db.conn.execute(
        "SELECT status FROM catalogue_document_matches WHERE id=?", (first,)
    ).fetchone()[0] == "rejected"
    evidence = json.loads(db.conn.execute(
        "SELECT evidence_json FROM catalogue_document_matches WHERE id=?", (first,)
    ).fetchone()[0])
    assert evidence["matcher_version"] == MATCHER_VERSION
    db.close()


def test_accepting_second_edition_for_document_is_rejected(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    document_path = tmp_path / "book.pdf"
    document_path.write_bytes(b"document")
    document = CatalogueFoundation(db).upsert_local_document(document_path, metadata={"title": "গোরা"})
    db.conn.execute("INSERT INTO catalogue_editions (title) VALUES ('গোরা')")
    db.conn.execute("INSERT INTO catalogue_editions (title) VALUES ('গোরা দ্বিতীয়')")
    db.conn.execute("INSERT INTO catalogue_document_matches (document_id, edition_id, score, method) VALUES (?, 1, 1, 'isbn')", (document.id,))
    db.conn.execute("INSERT INTO catalogue_document_matches (document_id, edition_id, score, method) VALUES (?, 2, .8, 'title')", (document.id,))
    db.conn.commit()
    accept_match(db, 1, reviewer="reviewer", reason="verified")
    with pytest.raises(ValueError):
        accept_match(db, 2, reviewer="reviewer", reason="conflicts")


def test_identical_snapshot_keeps_provenance_for_distinct_runs(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    foundation = CatalogueFoundation(db)
    payload = {"items": []}
    first = foundation.start_import_run("google_books", parser_version="p1", request={"q": "one"}, checksum="payload")
    second = foundation.start_import_run("google_books", parser_version="p1", request={"q": "two"}, checksum="payload")
    assert first.id != second.id
    snapshot = foundation.record_source_snapshot("google_books", "same", payload, import_run=first, request={"q": "one"}, parser_version="p1")
    foundation.record_source_snapshot("google_books", "same", payload, import_run=second, request={"q": "two"}, parser_version="p1")
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_source_snapshots").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_snapshot_provenance WHERE snapshot_id=?", (snapshot.id,)).fetchone()[0] == 2


def test_partial_staged_schema_recreates_snapshot_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_catalogue_import_checkpoints_run'"
    ).fetchone()
    db.conn.execute("DROP TABLE catalogue_snapshot_provenance")
    db.conn.commit()
    db.close()

    db = CatalogueDB(db_path)
    foundation = CatalogueFoundation(db)
    run = foundation.start_import_run(
        "google_books", parser_version="p1", request={"q": "migration"}, checksum="payload"
    )
    snapshot = foundation.record_source_snapshot(
        "google_books", "same", {"items": []}, import_run=run,
        request={"q": "migration"}, parser_version="p1",
    )

    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='catalogue_snapshot_provenance'"
    ).fetchone()
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_catalogue_import_checkpoints_run'"
    ).fetchone()
    provenance = db.conn.execute(
        "SELECT request_json, parser_version FROM catalogue_snapshot_provenance WHERE snapshot_id=?",
        (snapshot.id,),
    ).fetchone()
    assert json.loads(provenance[0]) == {"q": "migration"}
    assert provenance[1] == "p1"
    db.close()


def test_failed_import_run_reopens_and_preserves_checkpoint(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    foundation = CatalogueFoundation(db)
    run = foundation.start_import_run("google_books", parser_version="p1", checksum="payload")
    foundation.set_import_checkpoint(run, "records", cursor="4", state={"staged": 4})
    foundation.finish_import_run(run, status="failed")
    retry = foundation.start_import_run("google_books", parser_version="p1", checksum="payload")
    assert retry.id == run.id
    assert retry.status == "running"
    assert db.conn.execute("SELECT status, completed_at FROM catalogue_import_runs WHERE id=?", (run.id,)).fetchone()[0] == "running"
    assert db.conn.execute("SELECT cursor FROM catalogue_import_checkpoints WHERE import_run_id=?", (run.id,)).fetchone()[0] == "4"


def test_matcher_handles_volume_numbers_and_short_title_fragments() -> None:
    assert _volume_relation("রচনাবলী। ১", "রচনাবলী-১") == "match"
    assert _volume_relation("রচনাবলী। ১", "রচনাবলী-২") == "mismatch"
    assert _volume_relation("রচনাবলী। ১", "রচনাবলী") == "missing"
    assert _title_similarity("আহমদ শরীফ রচনাবলী। ১", "আহমদ শরীফ") < 0.8
