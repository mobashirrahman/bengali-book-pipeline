from __future__ import annotations

import argparse
from pathlib import Path

from pdf_craft.catalogue.cli import cmd_cover


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
