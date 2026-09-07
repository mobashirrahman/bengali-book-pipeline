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
