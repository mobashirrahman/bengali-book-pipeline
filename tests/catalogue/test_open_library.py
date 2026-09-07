from __future__ import annotations

import httpx

from pdf_craft.catalogue.importers.open_library import cover_url, search_open_library


def test_open_library_search_parses_work_and_edition(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "শেষের কবিতা"
        return httpx.Response(
            200,
            json={
                "docs": [{
                    "key": "/works/OL1W",
                    "cover_edition_key": "OL2M",
                    "title": "শেষের কবিতা",
                    "author_name": ["রবীন্দ্রনাথ ঠাকুর"],
                    "publisher": ["বিশ্বভারতী"],
                    "first_publish_year": 1929,
                    "isbn": ["9781234567890"],
                    "cover_i": 42,
                }],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = search_open_library("শেষের কবিতা", client=client)
    client.close()

    assert results[0].work_key == "/works/OL1W"
    assert results[0].edition_key == "OL2M"
    assert results[0].isbns == ["9781234567890"]


def test_cover_url_validates_size() -> None:
    assert cover_url("OL2M", size="L").endswith("OL2M-L.jpg")
