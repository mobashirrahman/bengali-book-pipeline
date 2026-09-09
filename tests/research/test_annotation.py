"""Tests for S2 blind independent annotation (unittest, stdlib only)."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from pdf_craft_tool.research import annotation
from pdf_craft_tool.research.annotation import (
    FORBIDDEN_IN_ANNOTATOR_PAYLOAD,
    AnnotationStore,
    RevisionConflict,
    promotion_blocked,
)
from pdf_craft_tool.research.annotation_server import (
    TOKEN_HEADER,
    AnnotationApp,
    AnnotationHTTPServer,
)
from pdf_craft_tool.research import census as census_mod
from pdf_craft_tool.research.schema import ContractError, GoldPage

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def make_census_dict(n=3):
    regions = [
        {
            "region_index": index,
            "geometry": {"x0": index * 100, "y0": 0,
                         "x1": index * 100 + 50, "y1": 20},
            "kind": "body",
            "unreadable": False,
        }
        for index in range(n)
    ]
    return census_mod.build_census_from_regions(
        HASH_C, HASH_A, HASH_B, "seed", regions).to_dict()


def make_page(page_id=HASH_C, image=HASH_B, n=3):
    data = make_census_dict(n=n)
    data["page_id"] = page_id
    return {
        "page_id": page_id,
        "source_sha256": HASH_A,
        "image_sha256": image,
        "census": data,
    }


def make_lines(*texts):
    return {index: text for index, text in enumerate(texts)}


def assert_no_forbidden(test_case, obj):
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                test_case.assertNotIn(key, FORBIDDEN_IN_ANNOTATOR_PAYLOAD)
                stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


class AnnotationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = Path(self.tmpdir.name) / "annotation.sqlite3"

    def make_store(self, *pages):
        if not pages:
            pages = (make_page(),)
        return AnnotationStore(self.db, pages=list(pages))

    def test_blind_payload_has_no_drafts(self):
        page = make_page()
        page["census"]["tesseract"] = "stray draft that must never leak"
        store = self.make_store(page)
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        assert_no_forbidden(self, payload)
        self.assertNotIn("ann2", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(len(payload["line_slots"]), 3)
        self.assertEqual(payload["revision"], 0)

    def test_two_annotators_isolated(self):
        store = self.make_store()
        self.addCleanup(store.close)
        first = store.assign(HASH_C, "ann1")
        store.submit(HASH_C, "ann1", revision=first["revision"],
                     lines=make_lines("কখগ distinctive-one",
                                      "line two", "line three"))
        second = store.assign(HASH_C, "ann2")
        dump = json.dumps(second, ensure_ascii=False)
        self.assertNotIn("distinctive-one", dump)
        assert_no_forbidden(self, second)

    def test_conflict_requires_two_distinct(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        store.submit(HASH_C, "ann1", revision=payload["revision"],
                     lines=make_lines("a", "b", "c"))
        with self.assertRaises(ValueError):
            store.detect_conflicts(HASH_C)
        # Two submissions from the same annotator id still count as one.
        store.submit(HASH_C, "ann1", revision=payload["revision"] + 1,
                     lines=make_lines("a", "b", "c"))
        with self.assertRaises(ValueError):
            store.detect_conflicts(HASH_C)

    def _conflicting_store(self):
        store = self.make_store()
        first = store.assign(HASH_C, "ann1")
        second = store.assign(HASH_C, "ann2")
        store.submit(HASH_C, "ann1", revision=first["revision"],
                     lines=make_lines("same one", "ann1 two", "same three"))
        store.submit(HASH_C, "ann2", revision=second["revision"],
                     lines=make_lines("same one", "ann2 two", "same three"))
        return store

    def test_conflict_creates_adjudication_item(self):
        store = self._conflicting_store()
        self.addCleanup(store.close)
        rows = store.detect_conflicts(HASH_C)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["region_index"], 1)
        self.assertEqual(store.page_status(HASH_C), "in_conflict")
        with self.assertRaises(ValueError):
            store.finalize(HASH_C, adjudicator_id="judge1")

    def test_adjudicator_must_be_third_party(self):
        store = self._conflicting_store()
        self.addCleanup(store.close)
        store.detect_conflicts(HASH_C)
        with self.assertRaises(ValueError):
            store.adjudicate(HASH_C, 1, resolved_text="fixed",
                             resolver_id="ann1", reason="image check",
                             revision=0)
        with self.assertRaises(ValueError):
            store.adjudicate(HASH_C, 1, resolved_text="fixed",
                             resolver_id="ann2", reason="image check",
                             revision=0)

    def test_stale_revision_rejected(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        store.submit(HASH_C, "ann1", revision=payload["revision"],
                     lines=make_lines("a", "b", "c"))
        with self.assertRaises(RevisionConflict):
            store.submit(HASH_C, "ann1", revision=payload["revision"],
                         lines=make_lines("a", "b", "c"))

    def test_provisional_and_flagged_never_export(self):
        flagged = make_page(page_id=HASH_C)
        provisional = make_page(page_id=HASH_D)
        store = self.make_store(flagged, provisional)
        self.addCleanup(store.close)
        store.set_status(HASH_C, "flagged")
        store.set_status(HASH_D, "provisional")
        with self.assertRaises(ValueError):
            store.finalize(HASH_C, adjudicator_id="judge1")
        with self.assertRaises(ValueError):
            store.finalize(HASH_D, adjudicator_id="judge1")
        with self.assertRaises(ValueError):
            store.export_final([HASH_C])
        with self.assertRaises(ValueError):
            store.export_final([HASH_D])

    def test_source_image_mismatch_blocks(self):
        store = self.make_store()
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            store.assign(HASH_C, "ann1", image_sha256="f" * 64)
        payload = store.assign(HASH_C, "ann1")
        with self.assertRaises(ValueError):
            store.submit(HASH_C, "ann1", revision=payload["revision"],
                         lines=make_lines("a", "b", "c"),
                         image_sha256="f" * 64)

    def test_promotion_blocked_by_trigger(self):
        bad = [
            {"pairwise_char_disagreement": 0.02,
             "exact_line_agreement": 0.99, "lines": 10},
            {"pairwise_char_disagreement": 0.02,
             "exact_line_agreement": 0.99, "lines": 10},
        ]
        result = promotion_blocked(bad)
        self.assertTrue(result["blocked"])
        self.assertTrue(any("disagreement" in reason
                            for reason in result["reasons"]))
        clean = [
            {"pairwise_char_disagreement": 0.001,
             "exact_line_agreement": 0.99, "lines": 10},
        ]
        result = promotion_blocked(clean)
        self.assertFalse(result["blocked"])

    def test_finalize_builds_valid_goldpage(self):
        store = self._conflicting_store()
        self.addCleanup(store.close)
        store.detect_conflicts(HASH_C)
        stats = store.disagreement_stats(HASH_C)
        self.assertGreater(stats["pairwise_char_disagreement"], 0)
        store.adjudicate(HASH_C, 1, resolved_text="resolved two",
                         resolver_id="judge1", reason="image is clear",
                         revision=0)
        page = store.finalize(HASH_C, adjudicator_id="judge1")
        self.assertIsInstance(page, GoldPage)
        self.assertEqual(page.status, "final")
        self.assertEqual(len(set(page.annotator_ids)), 2)
        self.assertTrue(page.adjudicator_id)
        self.assertTrue(page.is_final_export_eligible())
        self.assertEqual(GoldPage.from_dict(page.to_dict()), page)
        exported = store.export_final([HASH_C])
        self.assertEqual(exported[0]["page_id"], HASH_C)

    def test_assign_payload_asserts_blindness(self):
        page = make_page()
        page["census"]["entries"][0]["peer_text"] = "should be stripped"
        store = self.make_store(page)
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        assert_no_forbidden(self, payload)

    def test_unknown_page_rejected(self):
        store = self.make_store()
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            store.assign("e" * 64, "ann1")

    def test_production_db_names_refused(self):
        with self.assertRaises(ValueError):
            AnnotationStore(Path(self.tmpdir.name) / "gold.sqlite3",
                            pages=[make_page()])


class AnnotationServerTests(unittest.TestCase):
    def test_page_response_body_has_no_forbidden_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            page = make_page()
            page["census"]["tesseract"] = "stray draft"
            store = AnnotationStore(Path(tmpdir) / "annotation.sqlite3",
                                    pages=[page])
            app = AnnotationApp(store)
            token = app.register_annotator("ann1")
            server = AnnotationHTTPServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10)
            try:
                connection.request("GET", f"/api/page/{HASH_C}",
                                   headers={TOKEN_HEADER: token})
                response = connection.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                assert_no_forbidden(self, body)
                self.assertNotIn("tesseract",
                                 json.dumps(body, ensure_ascii=False))
            finally:
                connection.close()
                server.shutdown()
                server.server_close()

    def _serve(self, app):
        server = AnnotationHTTPServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_image_route_serves_hash_checked_png(self):
        import hashlib
        png = (b"\x89PNG\r\n\x1a\n" + b"pilot-scan-bytes" * 4)
        image_sha = hashlib.sha256(png).hexdigest()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "img").mkdir()
            (tmp / "img" / f"{HASH_C}.png").write_bytes(png)
            store = AnnotationStore(tmp / "annotation.sqlite3",
                                    pages=[make_page(image=image_sha)])
            app = AnnotationApp(store, image_dir=tmp / "img")
            token = app.register_annotator("ann1")
            adj = app.register_adjudicator("judge1")
            server = self._serve(app)

            def get(path, tok):
                conn = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=10)
                try:
                    conn.request("GET", path, headers={TOKEN_HEADER: tok})
                    resp = conn.getresponse()
                    return resp.status, resp.read()
                finally:
                    conn.close()

            status, body = get(f"/api/page/{HASH_C}/image", token)
            self.assertEqual(status, 200)
            self.assertEqual(body, png)
            # adjudicator may also fetch the scan
            self.assertEqual(get(f"/api/page/{HASH_C}/image", adj)[0], 200)
            # no token -> refused
            self.assertEqual(get(f"/api/page/{HASH_C}/image", "")[0], 403)

    def test_image_route_rejects_swapped_file(self):
        import hashlib
        png = b"\x89PNG\r\n\x1a\nthe-real-scan"
        image_sha = hashlib.sha256(png).hexdigest()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "img").mkdir()
            (tmp / "img" / f"{HASH_C}.png").write_bytes(b"a different image")
            store = AnnotationStore(tmp / "annotation.sqlite3",
                                    pages=[make_page(image=image_sha)])
            app = AnnotationApp(store, image_dir=tmp / "img")
            token = app.register_annotator("ann1")
            server = self._serve(app)
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10)
            try:
                conn.request("GET", f"/api/page/{HASH_C}/image",
                             headers={TOKEN_HEADER: token})
                self.assertEqual(conn.getresponse().status, 409)
            finally:
                conn.close()

    def test_image_route_404_when_no_image_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AnnotationStore(Path(tmpdir) / "annotation.sqlite3",
                                    pages=[make_page()])
            app = AnnotationApp(store)
            token = app.register_annotator("ann1")
            server = self._serve(app)
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10)
            try:
                conn.request("GET", f"/api/page/{HASH_C}/image",
                             headers={TOKEN_HEADER: token})
                self.assertEqual(conn.getresponse().status, 404)
            finally:
                conn.close()

    def test_non_loopback_host_refused(self):
        with self.assertRaises(SystemExit):
            from pdf_craft_tool.research.annotation_server import main
            main(["--db", "/tmp/x.sqlite3", "--host", "0.0.0.0"])

    def test_contract_error_is_value_error(self):
        self.assertTrue(issubclass(ContractError, ValueError))


if __name__ == "__main__":
    unittest.main()
