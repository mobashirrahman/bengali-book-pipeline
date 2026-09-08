from __future__ import annotations

import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from pdf_craft.catalogue.api.app import init_app
from pdf_craft.catalogue.database import CatalogueDB
from pdf_craft.catalogue.ratings import materialize_external_ratings, upsert_user_rating


def _seed_rating_records(path: Path) -> None:
    db = CatalogueDB(path)
    conn = db.conn
    conn.execute("INSERT INTO catalogue_works(id, title) VALUES (1, 'Ratings fixture')")
    conn.execute("INSERT INTO catalogue_editions(id, work_id, title) VALUES (2, 1, 'Ratings edition')")
    conn.execute(
        "INSERT INTO catalogue_source_snapshots(id, source, snapshot_key, payload_sha256, payload_json) "
        "VALUES (1, 'fixture', 'ratings', 'ratings', '{}')"
    )
    records = (
        (2, "rokomari", {"ratingValue": "4.5", "ratingCount": "100", "reviewCount": "50", "url": "https://r.example/1"}),
        (3, "google_books", {"volumeInfo": {"averageRating": 4, "ratingsCount": 12}}),
        (4, "goodreads", {"average_rating": "not-a-rating", "ratings_count": 10}),
    )
    for record_id, source, payload in records:
        conn.execute(
            "INSERT INTO catalogue_source_records(id, snapshot_id, source, external_id, title, raw_json) "
            "VALUES (?, 1, ?, ?, 'Ratings fixture', ?)",
            (record_id, source, str(record_id), json.dumps(payload)),
        )
        conn.execute(
            "INSERT INTO catalogue_source_record_editions(source_record_id, edition_id) VALUES (?, 2)",
            (record_id,),
        )
    conn.commit()
    db.close()


def test_external_rating_materialization_is_bounded_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.db"
    _seed_rating_records(path)
    db = CatalogueDB.connect(path)
    assert materialize_external_ratings(db, batch_size=1) == {
        "seen": 3, "materialized": 2, "skipped": 1,
    }
    assert materialize_external_ratings(db, batch_size=1) == {
        "seen": 3, "materialized": 0, "skipped": 1,
    }
    rows = db.conn.execute(
        "SELECT provider, value, scale, rating_count, review_count FROM catalogue_external_ratings ORDER BY provider"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("google_books", 4.0, 5.0, 12, None),
        ("rokomari", 4.5, 5.0, 100, 50),
    ]
    db.close()


def test_ratings_api_contract_auth_boundary_and_asset_safety(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.db"
    _seed_rating_records(path)
    db = CatalogueDB.connect(path)
    materialize_external_ratings(db)
    db.close()
    public = TestClient(init_app(path))
    response = public.get("/v2/works/1/ratings")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"work_id", "community", "external"}
    assert {entry["provider"] for entry in payload["external"]} == {"rokomari", "google_books", "goodreads"}
    goodreads = next(entry for entry in payload["external"] if entry["provider"] == "goodreads")
    assert goodreads["status"] == "unavailable"
    assert goodreads["reason"] == "not_imported"
    assert public.put("/v2/works/1/rating", json={"rating": 5}).status_code == 401

    authenticated = TestClient(init_app(path, auth_resolver=lambda _request: "subject-1"))
    assert authenticated.put("/v2/works/1/rating", json={"rating": 5}).json()["community"] == {
        "average": 5.0, "count": 1, "user_rating": 5,
    }
    assert authenticated.put("/v2/works/1/rating", json={"rating": 3}).json()["community"] == {
        "average": 3.0, "count": 1, "user_rating": 3,
    }
    assert authenticated.put("/v2/works/1/rating", json={"rating": 6}).status_code == 422
    assert authenticated.delete("/v2/works/1/rating").json()["community"]["count"] == 0

    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    (asset_root / "cover.png").write_bytes(b"cover")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    db = CatalogueDB.connect(path)
    db.conn.execute(
        "INSERT INTO catalogue_assets(id, edition_id, asset_type, storage_uri, mime_type, is_selected) "
        "VALUES (10, 2, 'cover', 'cover.png', 'image/png', 1)"
    )
    db.conn.execute(
        "INSERT INTO catalogue_assets(id, edition_id, asset_type, storage_uri, mime_type, is_selected) "
        "VALUES (11, 2, 'cover', '../outside.png', 'image/png', 1)"
    )
    db.conn.commit()
    db.close()
    assets = TestClient(init_app(path, asset_root=asset_root))
    assert assets.get("/v2/assets/10/content").status_code == 200
    assert assets.get("/v2/assets/10/content").headers["x-content-type-options"] == "nosniff"
    assert assets.get("/v2/assets/11/content").status_code == 404
    assert TestClient(init_app(path)).get("/v2/assets/10/content").status_code == 503


def test_user_rating_upsert_uses_unique_work_subject_key(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.db"
    db = CatalogueDB(path)
    db.conn.execute("INSERT INTO catalogue_works(id, title) VALUES (1, 'Ratings fixture')")
    upsert_user_rating(db.conn, 1, "subject-1", 5)
    db.conn.commit()
    first = db.conn.execute(
        "SELECT id, rating, created_at, updated_at FROM catalogue_user_ratings"
    ).fetchone()
    upsert_user_rating(db.conn, 1, "subject-1", 2)
    db.conn.commit()
    second = db.conn.execute(
        "SELECT id, rating, created_at, updated_at FROM catalogue_user_ratings"
    ).fetchone()
    assert second[0] == first[0]
    assert second[1] == 2
    assert second[2] == first[2]
    assert db.conn.execute("SELECT COUNT(*) FROM catalogue_user_ratings").fetchone()[0] == 1
    db.close()


def test_default_and_custom_cors_allow_rating_writes(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.db"
    db = CatalogueDB(path)
    db.conn.execute("INSERT INTO catalogue_works(id, title) VALUES (1, 'Ratings fixture')")
    db.conn.commit()
    db.close()

    default = TestClient(init_app(path))
    default_preflight = default.options(
        "/v2/works/1/rating",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert default_preflight.status_code == 200
    assert {"GET", "POST", "PUT", "DELETE", "OPTIONS"} <= set(
        default_preflight.headers["access-control-allow-methods"].split(", ")
    )

    # The module-level FastAPI app is shared by the catalogue tests. Rebuild
    # its middleware stack before exercising a second init_app configuration.
    api_module = importlib.import_module("pdf_craft.catalogue.api.app")
    api_module.app.middleware_stack = None
    custom = TestClient(init_app(path, cors_origins=["https://frontend.example"]))
    custom_preflight = custom.options(
        "/v2/works/1/rating",
        headers={
            "Origin": "https://frontend.example",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert custom_preflight.status_code == 200
    assert custom_preflight.headers["access-control-allow-origin"] == "https://frontend.example"
    assert "DELETE" in custom_preflight.headers["access-control-allow-methods"]
