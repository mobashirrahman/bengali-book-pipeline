from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from pdf_craft.catalogue.cli import _non_negative_int, cmd_cover, cmd_resolve_local
from pdf_craft.catalogue.database import CatalogueDB


def test_cover_register_url_dry_run_does_not_create_database(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"

    cmd_cover(
        argparse.Namespace(
            action="register-url",
            db=db_path,
            dry_run=True,
            edition_id=1,
            url="https://example.test/cover.jpg",
            source_record_id=None,
            attribution=None,
            rights=None,
        )
    )

    assert not db_path.exists()


def test_resolve_local_limit_processes_documents(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    db.conn.execute(
        "INSERT INTO catalogue_local_documents (sha256, source_path, file_size, metadata_json) "
        "VALUES (?, ?, ?, ?)",
        ("fixture-sha", "data/Author/Fixture.pdf", 1, json.dumps({})),
    )
    db.conn.commit()
    db.close()

    cmd_resolve_local(
        argparse.Namespace(db=db_path, only_unmatched=True, limit=1)
    )

    assert "Resolved 1 local documents" in capsys.readouterr().out


def test_resolve_local_limit_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        cmd_resolve_local(argparse.Namespace(db="unused.db", only_unmatched=True, limit=-1))
    with pytest.raises(argparse.ArgumentTypeError, match="non-negative"):
        _non_negative_int("-1")
