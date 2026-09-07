from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pdf_craft.catalogue.database import CatalogueDB


def test_version_two_partial_database_replays_catalogue_ddl(tmp_path: Path) -> None:
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (2)")
    conn.execute("CREATE TABLE catalogue_works (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    conn.commit()
    conn.close()

    db = CatalogueDB(path)
    for table in (
        "catalogue_editions",
        "catalogue_source_records",
        "catalogue_assets",
        "catalogue_asset_provenance",
        "catalogue_asset_selection_history",
    ):
        assert db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    columns = {
        row[1]
        for row in db.conn.execute("PRAGMA table_info(catalogue_works)")
    }
    assert {"subtitle", "sort_title", "language", "description", "created_at", "updated_at"} <= columns
    db.close()


def test_catalogue_works_sort_title_index_exists(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "catalogue.db")

    indexes = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?, ?, ?, ?)",
            (
                "idx_catalogue_works_sort_title",
                "idx_catalogue_source_records_source_id",
                "idx_catalogue_editions_title",
                "idx_catalogue_edition_people_person_role",
            ),
        )
    }

    assert indexes == {
        "idx_catalogue_works_sort_title",
        "idx_catalogue_source_records_source_id",
        "idx_catalogue_editions_title",
        "idx_catalogue_edition_people_person_role",
    }
    db.close()


def test_future_schema_database_is_rejected_without_mutating_sqlite_master(
    tmp_path: Path,
) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (999)")
    conn.commit()
    before = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    conn.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        CatalogueDB(path)

    conn = sqlite3.connect(path)
    after = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    conn.close()
    assert after == before
