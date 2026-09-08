import base64
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from pdf_craft.catalogue.api.app import init_app
from pdf_craft.catalogue.database import CatalogueDB


def _database(path: Path) -> tuple[int, int, int, int]:
    db = CatalogueDB(path)
    conn = db.conn
    work = conn.execute("INSERT INTO catalogue_works (title) VALUES ('Alpha Work')").lastrowid
    edition = conn.execute(
        "INSERT INTO catalogue_editions (work_id, title) VALUES (?, 'Alpha Edition')", (work,)
    ).lastrowid
    person = conn.execute(
        "INSERT INTO catalogue_people (name, normalized_name) VALUES ('Author A', 'author a')"
    ).lastrowid
    conn.execute("INSERT INTO catalogue_edition_people VALUES (?, ?, 'author', 0)", (edition, person))
    conn.execute(
        "INSERT INTO catalogue_identifiers (entity_type, entity_id, namespace, value, normalized_value) VALUES ('edition', ?, 'isbn', '123', '123')",
        (edition,),
    )
    document = conn.execute(
        "INSERT INTO catalogue_local_documents (sha256, source_path, file_size) VALUES ('sha', 'alpha.pdf', 10)"
    ).lastrowid
    conn.execute(
        "INSERT INTO catalogue_document_matches (document_id, edition_id, score, method, status) VALUES (?, ?, 1, 'isbn', 'accepted')",
        (document, edition),
    )
    asset = conn.execute(
        "INSERT INTO catalogue_assets (edition_id, asset_type, storage_uri, is_selected) VALUES (?, 'cover', 'cover.jpg', 1)",
        (edition,),
    ).lastrowid
    conn.commit()
    db.close()
    return int(work), int(edition), int(document), int(asset)


def test_v2_normalized_reads_are_paginated_and_read_only(tmp_path: Path) -> None:
    work, edition, document, asset = _database(tmp_path / "catalogue.db")
    client = TestClient(init_app(tmp_path / "catalogue.db"))

    assert client.get("/v2/health").json() == {"status": "ok", "backend": "sqlite"}
    assert client.get("/v2/stats").json()["works"] == 1
    assert client.get(f"/v2/works/{work}").json()["editions"][0]["title"] == "Alpha Edition"
    edition_response = client.get(f"/v2/editions/{edition}").json()
    assert edition_response["assets"][0]["is_selected"] == 1
    assert client.get(f"/v2/documents/{document}").json()["matches"][0]["status"] == "accepted"
    locations = client.get(f"/v2/documents/{document}").json()["locations"]
    assert len(locations) == 1
    assert locations[0]["source_path"].endswith("/alpha.pdf")
    assert client.get(f"/v2/assets/{asset}").json()["storage_uri"] == "cover.jpg"

    first = client.get("/v2/search", params={"q": "Alpha", "limit": 1})
    assert first.status_code == 200
    cursor = first.json()["next"]
    assert cursor
    assert client.get("/v2/search", params={"q": "Alpha", "limit": 1, "after": cursor}).status_code == 200
    assert client.post("/v2/works", json={}).status_code == 405
    assert client.get("/v2/works/999").status_code == 404


def test_v2_works_supports_readable_only_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    conn = db.conn
    plain = conn.execute("INSERT INTO catalogue_works (title) VALUES ('Metadata Only')").lastrowid
    conn.execute("INSERT INTO catalogue_editions (work_id, title) VALUES (?, 'Plain Edition')", (plain,))
    readable = conn.execute("INSERT INTO catalogue_works (title) VALUES ('Readable Work')").lastrowid
    edition = conn.execute(
        "INSERT INTO catalogue_editions (work_id, title) VALUES (?, 'Readable Edition')", (readable,)
    ).lastrowid
    candidate_doc = conn.execute(
        "INSERT INTO catalogue_local_documents (sha256, source_path, file_size, media_type) VALUES ('c', 'candidate.epub', 1, 'application/epub+zip')"
    ).lastrowid
    conn.execute(
        "INSERT INTO catalogue_document_matches (document_id, edition_id, score, method, status) VALUES (?, ?, 1, 'manual', 'candidate')",
        (candidate_doc, edition),
    )
    accepted_doc = conn.execute(
        "INSERT INTO catalogue_local_documents (sha256, source_path, file_size, media_type) VALUES ('a', 'accepted.epub', 2, 'application/epub+zip')"
    ).lastrowid
    conn.execute(
        "INSERT INTO catalogue_document_matches (document_id, edition_id, score, method, status) VALUES (?, ?, 1, 'manual', 'accepted')",
        (accepted_doc, edition),
    )
    conn.commit()
    db.close()
    client = TestClient(init_app(db_path))

    assert {work["title"] for work in client.get("/v2/works").json()} == {"Metadata Only", "Readable Work"}
    filtered = client.get("/v2/works", params={"has_documents": "true"}).json()
    assert [work["title"] for work in filtered] == ["Readable Work"]
    assert client.get("/v2/works", params={"has_documents": "true", "after": readable}).json() == []
    assert client.get("/v2/stats").json()["readable_works"] == 1


def test_v2_requests_use_independent_connections(tmp_path: Path) -> None:
    _database(tmp_path / "catalogue.db")
    client = TestClient(init_app(tmp_path / "catalogue.db"))
    first = client.get("/v2/health")
    second = client.get("/v2/health")
    assert first.status_code == second.status_code == 200


def test_v2_startup_endpoints_serve_concurrent_requests(tmp_path: Path) -> None:
    """Mirror browser startup: concurrent health/works/stats must not 500.

    Smoke coverage for independent concurrent requests and response data
    only; the deterministic thread-handoff regression lives in
    TestRequestConnections in test_database.py.
    """
    _database(tmp_path / "catalogue.db")
    app = init_app(tmp_path / "catalogue.db")

    def fetch(path: str, params: dict[str, int] | None = None) -> tuple[int, object]:
        response = TestClient(app).get(path, params=params)
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=3) as pool:
        health_future = pool.submit(fetch, "/v2/health")
        works_future = pool.submit(fetch, "/v2/works", {"limit": 24})
        stats_future = pool.submit(fetch, "/v2/stats")
        health_status, health = health_future.result(timeout=60)
        works_status, works = works_future.result(timeout=60)
        stats_status, stats = stats_future.result(timeout=60)

    assert health_status == 200
    assert health == {"status": "ok", "backend": "sqlite"}
    assert works_status == 200
    assert isinstance(works, list)
    assert works[0]["title"] == "Alpha Work"
    assert stats_status == 200
    assert isinstance(stats, dict)
    assert stats["works"] == 1


def test_v2_document_content_supports_head_ranges_and_downloads(tmp_path: Path) -> None:
    payload = b"0123456789abcdef"
    (tmp_path / "alpha.pdf").write_bytes(payload)
    _, _, document, _ = _database(tmp_path / "catalogue.db")
    client = TestClient(init_app(tmp_path / "catalogue.db", content_root=tmp_path))

    full = client.get(f"/v2/documents/{document}/content")
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["content-type"] == "application/pdf"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(len(payload))

    head = client.head(f"/v2/documents/{document}/content")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(payload))

    ranged = client.get(f"/v2/documents/{document}/content", headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206
    assert ranged.content == payload[2:6]
    assert ranged.headers["content-range"] == f"bytes 2-5/{len(payload)}"

    suffix = client.get(f"/v2/documents/{document}/content", headers={"Range": "bytes=-4"})
    assert suffix.status_code == 206
    assert suffix.content == payload[-4:]

    invalid = client.get(f"/v2/documents/{document}/content", headers={"Range": "bytes=999-"})
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(payload)}"

    download = client.head(f"/v2/documents/{document}/download")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment; filename*=")


def test_v2_document_content_is_disabled_without_an_approved_root(tmp_path: Path) -> None:
    _, _, document, _ = _database(tmp_path / "catalogue.db")
    client = TestClient(init_app(tmp_path / "catalogue.db"))
    response = client.get(f"/v2/documents/{document}/content")
    assert response.status_code == 503


def test_v2_document_content_rejects_paths_outside_the_approved_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private")
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    document = db.conn.execute(
        "INSERT INTO catalogue_local_documents (sha256, source_path, file_size) VALUES ('outside', ?, 7)",
        (str(outside),),
    ).lastrowid
    db.conn.commit()
    db.close()

    client = TestClient(init_app(db_path, content_root=tmp_path / "approved"))
    response = client.get(f"/v2/documents/{document}/content")
    assert response.status_code == 404


def test_v2_api_allows_the_documented_vite_origin(tmp_path: Path) -> None:
    _database(tmp_path / "catalogue.db")
    client = TestClient(init_app(tmp_path / "catalogue.db"))
    response = client.get("/v2/health", headers={"Origin": "http://127.0.0.1:4173"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"


def test_v2_search_cursor_preserves_case_insensitive_order(tmp_path: Path) -> None:
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    for title in ("zeta", "Alpha", "alpha", "BETA", "beta"):
        db.conn.execute("INSERT INTO catalogue_works (title) VALUES (?)", (title,))
    db.conn.commit()
    db.close()
    client = TestClient(init_app(db_path))

    items = []
    after = None
    while True:
        params = {"q": "a", "limit": 1}
        if after is not None:
            params["after"] = after
        response = client.get("/v2/search", params=params)
        assert response.status_code == 200
        payload = response.json()
        items.extend(payload["items"])
        after = payload["next"]
        if after is None:
            break

    assert [(item["title"].lower(), item["id"]) for item in items] == [
        ("alpha", 2),
        ("alpha", 3),
        ("beta", 4),
        ("beta", 5),
        ("zeta", 1),
    ]


def test_v2_search_cursor_preserves_non_ascii_title_order(tmp_path: Path) -> None:
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    for title in ("Äther", "Ångstrom", "äther"):
        db.conn.execute("INSERT INTO catalogue_works (title) VALUES (?)", (title,))
    db.conn.commit()
    db.close()
    client = TestClient(init_app(db_path))

    items = []
    after = None
    while True:
        params = {"q": "t", "limit": 1}
        if after is not None:
            params["after"] = after
        response = client.get("/v2/search", params=params)
        assert response.status_code == 200
        payload = response.json()
        items.extend(payload["items"])
        after = payload["next"]
        if after is None:
            break

    assert [(item["title"], item["id"], item["kind"]) for item in items] == [
        ("Äther", 1, "work"),
        ("Ångstrom", 2, "work"),
        ("äther", 3, "work"),
    ]


def test_v2_search_rejects_malformed_cursors(tmp_path: Path) -> None:
    _database(tmp_path / "catalogue.db")
    client = TestClient(init_app(tmp_path / "catalogue.db"))

    malformed = (
        {"title": "Alpha", "id": 1, "kind": "work"},
        ["Alpha", "1", "work"],
        ["Alpha", 1, "unknown"],
        ["Alpha", 1],
        ["Alpha", 1, "work", "extra"],
    )
    for decoded in malformed:
        cursor = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
        response = client.get("/v2/search", params={"q": "Alpha", "after": cursor})
        assert response.status_code == 400
        assert response.json()["detail"] == "invalid cursor"


def test_v2_search_matches_identifiers_and_people_per_entity_kind(tmp_path: Path) -> None:
    """Identifier and author matches must not leak across entity kinds.

    The union that backs /v2/search carries a bare ``id`` per row, so matching
    identifier ``entity_id`` or ``catalogue_edition_people.edition_id`` against
    it without checking the row's kind made a work match whenever its id
    happened to equal an unrelated edition's id.  Ids are allocated
    independently per table, so those collisions are routine.
    """
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    conn = db.conn
    # Decoy work whose id collides with the edition that really carries the
    # identifier and the author; its own title shares no term with the query.
    decoy = conn.execute("INSERT INTO catalogue_works (title) VALUES ('Unrelated Manual')").lastrowid
    match = conn.execute("INSERT INTO catalogue_works (title) VALUES ('Tagore Collected')").lastrowid
    edition = conn.execute(
        "INSERT INTO catalogue_editions (work_id, title) VALUES (?, 'Tagore Collected, First')", (match,)
    ).lastrowid
    assert int(edition) == int(decoy), "fixture needs a work id equal to the edition id"
    person = conn.execute(
        "INSERT INTO catalogue_people (name, normalized_name) VALUES ('Rabindranath Tagore', 'rabindranath tagore')"
    ).lastrowid
    conn.execute("INSERT INTO catalogue_edition_people VALUES (?, ?, 'author', 0)", (edition, person))
    conn.execute(
        "INSERT INTO catalogue_identifiers (entity_type, entity_id, namespace, value, normalized_value)"
        " VALUES ('edition', ?, 'url', 'rabindranath-tagore', 'rabindranath-tagore')",
        (edition,),
    )
    conn.commit()
    db.close()

    client = TestClient(init_app(db_path))
    items = client.get("/v2/search", params={"q": "Rabindranath", "limit": 50}).json()["items"]

    assert ("work", int(decoy), "Unrelated Manual") not in [(i["kind"], i["id"], i["title"]) for i in items]
    assert {"work", "edition", "person"} >= {i["kind"] for i in items}
    # The author's own work and edition still match through the edition people.
    assert ("work", int(match)) in [(i["kind"], i["id"]) for i in items]
    assert ("edition", int(edition)) in [(i["kind"], i["id"]) for i in items]
    assert ("person", int(person)) in [(i["kind"], i["id"]) for i in items]


def test_v2_search_supports_readable_only_filter(tmp_path: Path) -> None:
    """has_documents narrows search to works/editions a reader can open."""
    db_path = tmp_path / "catalogue.db"
    db = CatalogueDB(db_path)
    conn = db.conn
    readable = conn.execute("INSERT INTO catalogue_works (title) VALUES ('Alpha Readable')").lastrowid
    readable_edition = conn.execute(
        "INSERT INTO catalogue_editions (work_id, title) VALUES (?, 'Alpha Readable Edition')", (readable,)
    ).lastrowid
    conn.execute("INSERT INTO catalogue_works (title) VALUES ('Alpha Metadata Only')")
    conn.execute("INSERT INTO catalogue_people (name, normalized_name) VALUES ('Alpha Person', 'alpha person')")
    document = conn.execute(
        "INSERT INTO catalogue_local_documents (sha256, source_path, file_size) VALUES ('sha', 'alpha.pdf', 10)"
    ).lastrowid
    conn.execute(
        "INSERT INTO catalogue_document_matches (document_id, edition_id, score, method, status)"
        " VALUES (?, ?, 1, 'isbn', 'accepted')",
        (document, readable_edition),
    )
    conn.commit()
    db.close()

    client = TestClient(init_app(db_path))
    unfiltered = client.get("/v2/search", params={"q": "Alpha", "limit": 50}).json()["items"]
    filtered = client.get("/v2/search", params={"q": "Alpha", "limit": 50, "has_documents": "true"}).json()["items"]

    assert len(unfiltered) == 4  # two works, one edition, one person
    assert [(item["kind"], item["id"]) for item in filtered] == [
        ("work", int(readable)),
        ("edition", int(readable_edition)),
    ]
