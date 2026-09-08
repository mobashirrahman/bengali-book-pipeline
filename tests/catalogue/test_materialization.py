from __future__ import annotations

import json
from pathlib import Path

from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.importers.google_books import stage_google_books_response
from pdf_craft.catalogue.importers.rokomari import stage_rokomari_from_file
from pdf_craft.catalogue.materialization import (
    _assert,
    _get_person,
    materialize_source_records,
    parse_source_record,
    repair_edition_people,
)

SIDEBAR_AUTHORS = ["QNA publications", "খায়রুলস বেসিক ম্যাথ", "হুমায়ূন আহমেদ"]
SHARED_ISBN = "978-0-306-40615-7"


def _sidebar_record(title: str, external_id: str) -> dict[str, object]:
    return {
        "id": external_id,
        "productType": "book",
        "name": title,
        "url": f"https://www.rokomari.com/book/{external_id}",
        "authors": SIDEBAR_AUTHORS,
        "specification": {"Title": title, "Publisher": "Sidebar Publisher"},
    }


def _stage_rokomari(db: CatalogueDB, path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    assert stage_rokomari_from_file(db, path) == (len(records), 0)


def _edition_for_source(db: CatalogueDB, source_record_id: int) -> int:
    row = db.conn.execute(
        "SELECT edition_id FROM catalogue_source_record_editions WHERE source_record_id=?",
        (source_record_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _add_legacy_author_row(db: CatalogueDB, edition_id: int, name: str, source_record_id: int) -> None:
    """Mimic a sidebar-fallback import: person, edition link, and assertion."""
    person_id = _get_person(db.conn, name)
    position = db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_edition_people WHERE edition_id=?", (edition_id,)
    ).fetchone()[0]
    db.conn.execute(
        "INSERT OR IGNORE INTO catalogue_edition_people (edition_id, person_id, role, position)"
        " VALUES (?, ?, 'author', ?)",
        (edition_id, person_id, position),
    )
    _assert(db.conn, "person", person_id, "name", name, source_record_id)
    db.conn.commit()


def test_rokomari_sidebar_authors_are_ignored_without_spec_author() -> None:
    item = parse_source_record("rokomari", _sidebar_record("Sidebar Book", "rk-sidebar"))
    assert item.authors == ()


def test_rokomari_spec_author_wins_over_sidebar() -> None:
    record = _sidebar_record("Real Book", "rk-real")
    assert isinstance(record["specification"], dict)
    record["specification"]["Author"] = "Real Author"
    item = parse_source_record("rokomari", record)
    assert item.authors == ("Real Author",)


def test_google_books_top_level_authors_still_parse() -> None:
    item = parse_source_record(
        "google_books",
        {"volumeInfo": {"title": "Google Book", "authors": ["Author One", "Author Two"]}},
    )
    assert item.authors == ("Author One", "Author Two")


def test_repair_removes_sidebar_rows_but_keeps_spec_authors(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    real = _sidebar_record("Real Book", "rk-real")
    assert isinstance(real["specification"], dict)
    real["specification"]["Author"] = "Real Author"
    _stage_rokomari(db, tmp_path / "rokomari.jsonl", [_sidebar_record("Sidebar Book", "rk-sidebar"), real])
    assert materialize_source_records(db, source="rokomari")["materialized"] == 2

    sidebar_edition = _edition_for_source(db, 1)
    real_edition = _edition_for_source(db, 2)
    for name in SIDEBAR_AUTHORS[:2]:
        _add_legacy_author_row(db, sidebar_edition, name, 1)

    report = repair_edition_people(db)
    assert report == {
        "editions_examined": 1,
        "people_rows_deleted": 2,
        "people_deleted": 2,
        "assertions_deleted": 2,
    }
    assert db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_edition_people WHERE edition_id=?", (sidebar_edition,)
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_people WHERE normalized_name='qna publications'"
    ).fetchone()[0] == 0
    assert [
        tuple(row)
        for row in db.conn.execute(
            "SELECT p.name FROM catalogue_edition_people ep"
            " JOIN catalogue_people p ON p.id=ep.person_id"
            " WHERE ep.edition_id=? ORDER BY ep.position",
            (real_edition,),
        ).fetchall()
    ] == [("Real Author",)]
    db.close()


def test_repair_preserves_author_from_second_source_on_shared_edition(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")
    shared = _sidebar_record("Shared Book", "rk-shared")
    assert isinstance(shared["specification"], dict)
    shared["specification"]["ISBN"] = SHARED_ISBN
    _stage_rokomari(db, tmp_path / "rokomari.jsonl", [shared])
    staged, _ = stage_google_books_response(
        db,
        {"items": [{
            "id": "google-1",
            "volumeInfo": {
                "title": "Shared Book",
                "authors": ["Good Author"],
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": SHARED_ISBN}],
            },
        }]},
        request={"file": "google.json"},
    )
    assert staged == 1
    assert materialize_source_records(db, source="google_books")["materialized"] == 1
    assert materialize_source_records(db, source="rokomari")["materialized"] == 1

    google_edition = _edition_for_source(db, 2)
    assert _edition_for_source(db, 1) == google_edition
    _add_legacy_author_row(db, google_edition, SIDEBAR_AUTHORS[0], 1)
    _add_legacy_author_row(db, google_edition, SIDEBAR_AUTHORS[1], 1)

    report = repair_edition_people(db)
    assert report["editions_examined"] == 1
    assert report["people_rows_deleted"] == 2
    remaining = [
        tuple(row)
        for row in db.conn.execute(
            "SELECT p.name, ep.position FROM catalogue_edition_people ep"
            " JOIN catalogue_people p ON p.id=ep.person_id"
            " WHERE ep.edition_id=? ORDER BY ep.position",
            (google_edition,),
        ).fetchall()
    ]
    assert remaining == [("Good Author", 0)]
    db.close()


def test_repair_sweeps_orphans_once_regardless_of_batch_count(tmp_path, monkeypatch) -> None:
    """The orphan sweep scans whole tables, so it must run once, not per batch.

    Against the live catalogue (3.8M assertions, 76k people) sweeping inside
    the batch loop meant 424 full table scans for a result identical to one.
    """
    from pdf_craft.catalogue import materialization as module

    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    records = tmp_path / "r.jsonl"
    records.write_text("\n".join(
        json.dumps({"id": f"r{i}", "productType": "book", "name": f"B{i}",
                    "authors": ["Sidebar One", "Sidebar Two"],
                    "specification": {"Title": f"B{i}"}})
        for i in range(5)) + "\n")
    stage_rokomari_from_file(db, records)
    materialize_source_records(db, source="rokomari")

    calls = []
    original = module._delete_orphan_person_rows
    monkeypatch.setattr(module, "_delete_orphan_person_rows",
                        lambda *a, **k: (calls.append(1), original(*a, **k))[1])

    module.repair_edition_people(db, batch_size=1)

    assert len(calls) == 1, f"orphan sweep ran {len(calls)} times, expected once"
    db.close()


def test_repair_deletes_only_the_rejected_role(tmp_path) -> None:
    """A person holding two roles must lose only the role being rejected."""
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    conn = db.conn
    records = tmp_path / "r.jsonl"
    records.write_text(json.dumps({
        "id": "r1", "productType": "book", "name": "B",
        "authors": ["Sidebar Name"], "specification": {"Title": "B"}}) + "\n")
    stage_rokomari_from_file(db, records)
    materialize_source_records(db, source="rokomari")

    edition = int(conn.execute("SELECT id FROM catalogue_editions LIMIT 1").fetchone()[0])
    person = int(conn.execute(
        "INSERT INTO catalogue_people (name, sort_name, normalized_name)"
        " VALUES ('Sidebar Name', 'Sidebar Name', 'sidebar name')").lastrowid)
    conn.execute("INSERT INTO catalogue_edition_people VALUES (?, ?, 'author', 0)", (edition, person))
    conn.execute("INSERT INTO catalogue_edition_people VALUES (?, ?, 'illustrator', 1)", (edition, person))
    conn.commit()

    repair_edition_people(db)

    roles = {row[0] for row in conn.execute(
        "SELECT role FROM catalogue_edition_people WHERE edition_id=? AND person_id=?",
        (edition, person))}
    assert roles == {"illustrator"}, f"expected only the illustrator row to survive, got {roles}"
    db.close()


def test_repair_dry_run_matches_a_live_run_across_batches(tmp_path) -> None:
    """A multi-batch dry run reports live counts and writes nothing."""
    def _build(path):
        db = CatalogueDB(path)
        records = path.parent / f"{path.stem}.jsonl"
        records.write_text("\n".join(
            json.dumps({"id": f"r{i}", "productType": "book", "name": f"B{i}",
                        "authors": ["S1", "S2"], "specification": {"Title": f"B{i}"}})
            for i in range(4)) + "\n")
        stage_rokomari_from_file(db, records)
        materialize_source_records(db, source="rokomari")
        edition_ids = [int(r[0]) for r in db.conn.execute("SELECT id FROM catalogue_editions")]
        for eid in edition_ids:
            for pos, name in enumerate(("S1", "S2")):
                pid = _get_person(db.conn, name)
                db.conn.execute(
                    "INSERT OR IGNORE INTO catalogue_edition_people VALUES (?,?,'author',?)", (eid, pid, pos))
        db.conn.commit()
        return db

    def _counts(db):
        return (db.conn.execute("SELECT COUNT(*) FROM catalogue_edition_people").fetchone()[0],
                db.conn.execute("SELECT COUNT(*) FROM catalogue_people").fetchone()[0])

    dry_db = _build(tmp_path / "dry.db")
    before = _counts(dry_db)
    dry_report = repair_edition_people(dry_db, batch_size=1, dry_run=True)
    assert _counts(dry_db) == before, "dry run must not write"
    dry_db.close()

    live_db = _build(tmp_path / "live.db")
    live_report = repair_edition_people(live_db, batch_size=1)
    assert dry_report == live_report
    assert _counts(live_db) != before, "live run must actually delete"
    live_db.close()
