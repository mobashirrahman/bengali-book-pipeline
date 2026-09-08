from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pdf_craft.catalogue.assets import (
    AssetError,
    fetch_remote_cover,
    rank_cover_candidates,
    register_local_bytes,
    register_remote_cover,
    select_cover,
)
from pdf_craft.catalogue.database import CatalogueDB

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" +
       b"\x00\x00\x00\x10\x00\x00\x00\x20\x08\x02\x00\x00\x00" + b"x")
WEBP_VP8L = b"RIFF" + (13).to_bytes(4, "little") + b"WEBPVP8L" + (5).to_bytes(4, "little") + b"\x2f\x0f\xc0\x07\x00"
WEBP_VP8 = b"RIFF" + (18).to_bytes(4, "little") + b"WEBPVP8 " + (10).to_bytes(4, "little") + b"\x00\x00\x00\x9d\x01\x2a\x10\x00\x20\x00"
HIGH_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" +
            (600).to_bytes(4, "big") + (900).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"x")


def _edition(db: CatalogueDB) -> int:
    return int(db.conn.execute("INSERT INTO catalogue_editions (title) VALUES ('Book')").lastrowid)


def _source_record(db: CatalogueDB, source: str) -> int:
    snapshot = db.conn.execute(
        "INSERT INTO catalogue_source_snapshots (source, snapshot_key, payload_sha256, payload_json) VALUES (?, ?, 'x', '{}')",
        (source, source),
    ).lastrowid
    return int(db.conn.execute(
        "INSERT INTO catalogue_source_records (snapshot_id, source, raw_json) VALUES (?, ?, '{}')",
        (snapshot, source),
    ).lastrowid)


def test_local_asset_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    first = register_local_bytes(db, edition, PNG, tmp_path / "assets")
    second = register_local_bytes(db, edition, PNG, tmp_path / "assets")
    assert first.id == second.id
    assert first.sha256 in first.storage_uri
    assert Path(first.storage_uri).read_bytes() == PNG


def test_remote_candidate_does_not_download(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    candidate = register_remote_cover(db, _edition(db), "https://example.test/cover.jpg", rights="CC-BY")
    assert candidate.source_url == "https://example.test/cover.jpg"
    assert candidate.sha256 is None
    assert candidate.rights == "CC-BY"


def test_streaming_fetch_validates_and_limits_response(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    candidate = register_remote_cover(db, _edition(db), "https://example.test/cover.png")
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "image/png"}, content=PNG)))
    fetched = fetch_remote_cover(db, candidate.id, tmp_path / "assets", client=client)
    assert fetched.sha256 and fetched.mime_type == "image/png"
    client.close()
    too_large = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "image/png"}, content=PNG)))
    with pytest.raises(AssetError, match="maximum"):
        fetch_remote_cover(db, candidate.id, tmp_path / "other", client=too_large, max_bytes=4)
    too_large.close()


def test_fetch_rejects_html_and_wrong_content_type(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    candidate = register_remote_cover(db, _edition(db), "https://example.test/cover")
    html = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"<html>placeholder</html>")))
    with pytest.raises(AssetError, match="content type"):
        fetch_remote_cover(db, candidate.id, tmp_path / "assets", client=html)
    html.close()


def test_cover_verification_batch_is_bounded_resumable_and_idempotent(tmp_path: Path) -> None:
    from pdf_craft.catalogue.assets import verify_cover_batch

    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    valid = register_remote_cover(db, edition, "https://example.test/valid.png")
    low = register_remote_cover(db, edition, "https://example.test/low.png")
    oversized = register_remote_cover(db, edition, "https://example.test/large.png")
    invalid = register_remote_cover(db, edition, "https://example.test/invalid.png")
    unsafe = register_remote_cover(db, edition, "http://127.0.0.1/private.png")
    requests: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("invalid.png"):
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"not an image")
        if request.url.path.endswith("valid.png"):
            return httpx.Response(200, headers={"content-type": "image/png"}, content=HIGH_PNG)
        if request.url.path.endswith("low.png"):
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
        if request.url.path.endswith("large.png"):
            return httpx.Response(200, headers={"content-type": "image/png", "content-length": "99"}, content=HIGH_PNG)
        raise AssertionError(f"unexpected cover request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(transport))
    first = verify_cover_batch(db, tmp_path / "assets", limit=5, max_bytes=50, client=client)
    assert (first.examined, first.validated, first.rejected, first.selected) == (5, 1, 4, 1), first.outcomes
    assert first.complete is True
    assert requests == [
        "https://example.test/valid.png", "https://example.test/low.png",
        "https://example.test/large.png", "https://example.test/invalid.png",
    ]
    statuses = dict(db.conn.execute("SELECT id, status FROM catalogue_assets"))
    assert statuses == {valid.id: "validated", low.id: "rejected", oversized.id: "rejected", invalid.id: "rejected", unsafe.id: "rejected"}
    stored = db.conn.execute("SELECT storage_uri, width, height, metadata_json FROM catalogue_assets WHERE id=?", (valid.id,)).fetchone()
    assert stored[0].startswith(str(tmp_path / "assets"))
    assert (stored[1], stored[2]) == (600, 900)
    assert db.conn.execute("SELECT is_selected FROM catalogue_assets WHERE id=?", (valid.id,)).fetchone()[0] == 1
    assert '"status": "validated"' in stored[3]
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_asset_provenance WHERE asset_id=?", (valid.id,)).fetchone()[0] == 1

    second = verify_cover_batch(db, tmp_path / "assets", limit=5, after_id=first.next_after_id, client=client)
    assert second.examined == 0
    assert requests.count("https://example.test/valid.png") == 1
    client.close()


def test_ranking_and_manual_selection_retain_alternatives(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    low = register_local_bytes(db, edition, PNG, tmp_path / "assets")
    high = register_local_bytes(db, edition, WEBP_VP8L, tmp_path / "assets")
    assert {candidate.id for candidate in rank_cover_candidates(db, edition)} == {low.id, high.id}
    selected = select_cover(db, edition, candidate_id=low.id, manual=True, selected_by="editor")
    assert selected.id == low.id and selected.is_selected
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_assets WHERE edition_id=?", (edition,)).fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_assets WHERE edition_id=? AND is_selected=1", (edition,)).fetchone()[0] == 1


def test_automatic_selection_excludes_unvalidated_remote_and_manual_rejects_it(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    remote = register_remote_cover(db, edition, "https://example.test/unvalidated")
    with pytest.raises(AssetError, match="no cover candidates"):
        select_cover(db, edition)
    with pytest.raises(AssetError, match="not been validated"):
        select_cover(db, edition, candidate_id=remote.id, manual=True)


def test_selection_history_keeps_replaced_manual_choices(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    first = register_local_bytes(db, edition, PNG, tmp_path / "assets")
    second = register_local_bytes(db, edition, WEBP_VP8L, tmp_path / "assets")
    select_cover(db, edition, candidate_id=first.id, manual=True, selected_by="alice")
    select_cover(db, edition, candidate_id=second.id, manual=True, selected_by="bob")
    history = db.conn.execute(
        "SELECT asset_id, selected_by FROM catalogue_asset_selection_history WHERE edition_id=? ORDER BY id",
        (edition,),
    ).fetchall()
    assert [(row[0], row[1]) for row in history] == [(first.id, "alice"), (second.id, "bob")]


def test_duplicate_bytes_keep_provenance_observations(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    source_one = _source_record(db, "one")
    source_two = _source_record(db, "two")
    first = register_local_bytes(
        db, edition, PNG, tmp_path / "assets", source_record_id=source_one,
        source_url="https://one.example/cover", attribution="one",
    )
    second = register_local_bytes(
        db, edition, PNG, tmp_path / "assets", source_record_id=source_two,
        source_url="https://two.example/cover", rights="CC-BY",
    )
    assert first.id == second.id
    observations = db.conn.execute(
        "SELECT source_record_id, source_url, attribution, rights FROM catalogue_asset_provenance WHERE asset_id=? ORDER BY source_record_id",
        (first.id,),
    ).fetchall()
    assert [tuple(row) for row in observations] == [
        (source_one, "https://one.example/cover", "one", None),
        (source_two, "https://two.example/cover", None, "CC-BY"),
    ]


def test_webp_vp8_and_vp8l_dimensions_are_supported(tmp_path: Path) -> None:
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    vp8l = register_local_bytes(db, edition, WEBP_VP8L, tmp_path / "assets")
    vp8 = register_local_bytes(db, edition, WEBP_VP8, tmp_path / "assets")
    assert (vp8l.width, vp8l.height) == (16, 32)
    assert (vp8.width, vp8.height) == (16, 32)


def test_cover_trusts_sniffed_type_over_a_wrong_declared_type(tmp_path: Path) -> None:
    """A mislabelled Content-Type must not reject an otherwise valid cover.

    The origin CDN serves PNG bytes as image/jpeg on a large share of covers.
    The sniffed type is authoritative: it is what gets stored, what picks the
    file extension, and what is sent back to the browser (with nosniff), so the
    declared value never reaches a client.
    """
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    candidate = register_remote_cover(db, edition, "https://cdn.example.com/cover.jpg")
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "image/jpeg"}, content=HIGH_PNG))

    result = fetch_remote_cover(
        db, candidate.id, tmp_path / "assets", client=httpx.Client(transport=transport))

    assert result.mime_type == "image/png"
    assert result.status == "validated"
    assert Path(result.storage_uri).suffix == ".png"
    assert Path(result.storage_uri).read_bytes() == HIGH_PNG


def test_cover_still_rejects_a_declared_type_that_is_not_an_image(tmp_path: Path) -> None:
    """Only an image-shaped declared type is tolerated when it disagrees."""
    db = CatalogueDB(tmp_path / "db.sqlite")
    edition = _edition(db)
    candidate = register_remote_cover(db, edition, "https://cdn.example.com/cover.jpg")
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=HIGH_PNG))

    with pytest.raises(AssetError, match="unsupported image content type"):
        fetch_remote_cover(
            db, candidate.id, tmp_path / "assets", client=httpx.Client(transport=transport))
