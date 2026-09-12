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
            self.assertEqual(status, 403)
            # adjudicator may still fetch the scan
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
            adj = app.register_adjudicator("judge1")
            server = self._serve(app)
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10)
            try:
                conn.request("GET", f"/api/page/{HASH_C}/image",
                             headers={TOKEN_HEADER: adj})
                self.assertEqual(conn.getresponse().status, 409)
            finally:
                conn.close()

    def test_image_route_404_when_no_image_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AnnotationStore(Path(tmpdir) / "annotation.sqlite3",
                                    pages=[make_page()])
            app = AnnotationApp(store)
            token = app.register_annotator("ann1")
            adj = app.register_adjudicator("judge1")
            server = self._serve(app)
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10)
            try:
                conn.request("GET", f"/api/page/{HASH_C}/image",
                             headers={TOKEN_HEADER: adj})
                self.assertEqual(conn.getresponse().status, 404)
            finally:
                conn.close()

    def test_non_loopback_host_refused(self):
        with self.assertRaises(SystemExit):
            from pdf_craft_tool.research.annotation_server import main
            main(["--db", "/tmp/x.sqlite3", "--host", "0.0.0.0"])

    def test_bind_failure_closes_cleanly(self):
        import socket
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AnnotationStore(Path(tmpdir) / "annotation.sqlite3",
                                    pages=[make_page()])
            self.addCleanup(store.close)
            app = AnnotationApp(store)
            first = AnnotationHTTPServer(("127.0.0.1", 0), app)
            try:
                port = first.server_address[1]
                with self.assertRaises(OSError):
                    AnnotationHTTPServer(("127.0.0.1", port), app)
            finally:
                first.server_close()

    def test_contract_error_is_value_error(self):
        self.assertTrue(issubclass(ContractError, ValueError))


def make_real_png(width=200, height=100):
    from PIL import Image
    image = Image.new("RGB", (width, height), color=(255, 255, 255))
    buffer = __import__("io").BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class PeekAndSignoffTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = Path(self.tmpdir.name) / "annotation.sqlite3"

    def test_log_peek_and_export(self):
        store = AnnotationStore(self.db, pages=[make_page()])
        self.addCleanup(store.close)
        logged = store.log_peek(HASH_C, "ann1", 1)
        self.assertEqual(logged["annotator_id"], "ann1")
        self.assertEqual(logged["region_index"], 1)
        peeks = store.export_peeks(HASH_C)
        self.assertEqual(len(peeks), 1)
        self.assertNotIn("text_json", json.dumps(peeks))
        with self.assertRaises(ValueError):
            store.log_peek(HASH_C, "ann1", 99)
        with self.assertRaises(ValueError):
            store.log_peek("e" * 64, "ann1", 0)

    def test_signoff_roundtrip_and_amendment_reset(self):
        store = AnnotationStore(self.db, pages=[make_page()])
        self.addCleanup(store.close)
        signed = store.set_census_signoff(HASH_C, "cover1", approved=True)
        self.assertTrue(signed["census_approved"])
        self.assertEqual(signed["census_reviewer"], "cover1")
        queue = store.coverage_queue()
        self.assertEqual(len(queue), 1)
        self.assertTrue(queue[0]["census_approved"])
        self.assertEqual(queue[0]["regions"], 3)
        self.assertEqual(queue[0]["pending_proposals"], 0)
        # An approved amendment resets the sign-off.
        proposal = store.propose_region(
            HASH_C, "ann1", geometry={"x0": 0, "y0": 100,
                                      "x1": 50, "y1": 140})
        store.review_proposal(proposal["id"], "cover1", decision="approve")
        queue = store.coverage_queue()
        self.assertFalse(queue[0]["census_approved"])
        self.assertEqual(queue[0]["regions"], 4)
        # Withdraw works too; terminal pages refuse.
        signed = store.set_census_signoff(HASH_C, "cover1", approved=False)
        self.assertFalse(signed["census_approved"])
        with self.assertRaises(ValueError):
            store.set_census_signoff(HASH_C, "cover1", approved="yes")
        store.set_status(HASH_C, "flagged")
        with self.assertRaises(ValueError):
            store.set_census_signoff(HASH_C, "cover1", approved=True)

    def test_migration_adds_columns_to_old_db(self):
        store = AnnotationStore(self.db, pages=[make_page()])
        self.addCleanup(store.close)
        columns = {row["name"] for row in store.db.execute(
            "PRAGMA table_info(pages)")}
        self.assertTrue({"census_approved", "census_reviewer",
                         "census_approved_at"} <= columns)
        # Reopening the same file migrates idempotently.
        reopened = AnnotationStore(self.db, pages=[])
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.coverage_queue()), 1)


class CropAndCoverageServerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def serve(self, app):
        server = AnnotationHTTPServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def call(self, server, method, path, token=None, body=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=10)
        try:
            headers = {}
            if token:
                headers[TOKEN_HEADER] = token
            data = json.dumps(body) if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, resp.headers, json.loads(raw)
            except ValueError:
                return resp.status, resp.headers, raw
        finally:
            conn.close()

    def make_crop_app(self):
        import hashlib
        tmp = Path(self.tmpdir.name)
        png = make_real_png(200, 100)
        image_sha = hashlib.sha256(png).hexdigest()
        (tmp / "img").mkdir(exist_ok=True)
        (tmp / "img" / f"{HASH_C}.png").write_bytes(png)
        census = make_census_dict(n=2)
        page = {"page_id": HASH_C, "source_sha256": HASH_A,
                "image_sha256": image_sha, "census": census}
        store = AnnotationStore(tmp / "annotation.sqlite3", pages=[page])
        app = AnnotationApp(store, image_dir=tmp / "img",
                            crop_cache=tmp / "crops")
        ann = app.register_annotator("ann1")
        adj = app.register_adjudicator("judge1")
        cov = app.register_coverage("cover1")
        return app, ann, adj, cov

    def test_crop_served_and_cached(self):
        from PIL import Image
        app, ann, _, _ = self.make_crop_app()
        self.addCleanup(app.close)
        server = self.serve(app)
        status, headers, body = self.call(
            server, "GET", f"/api/page/{HASH_C}/region/0/image", ann)
        self.assertEqual(status, 200)
        crop = Image.open(__import__("io").BytesIO(body))
        # Region 0 is x0=0..50 + 8% padding, clamped at edges.
        self.assertEqual(crop.size, (54, 21))
        status, _, body2 = self.call(
            server, "GET", "/api/page/pilot-01/region/1/image", ann)
        self.assertEqual(status, 200)
        self.assertEqual(
            Image.open(__import__("io").BytesIO(body2)).size, (58, 21))
        status, _, _ = self.call(server, "GET",
                                 f"/api/page/{HASH_C}/region/9/image", ann)
        self.assertEqual(status, 400)
        status, _, _ = self.call(server, "GET",
                                 f"/api/page/{HASH_C}/region/x/image", ann)
        self.assertEqual(status, 400)

    def test_peek_logs_and_serves_full_image(self):
        app, ann, _, _ = self.make_crop_app()
        self.addCleanup(app.close)
        server = self.serve(app)
        status, headers, body = self.call(
            server, "POST", f"/api/page/{HASH_C}/region/1/peek", ann, {})
        self.assertEqual(status, 200)
        self.assertIn("X-Peek-Id", dict(headers.items()))
        peeks = app.store.export_peeks(HASH_C)
        self.assertEqual(len(peeks), 1)
        self.assertEqual(peeks[0]["region_index"], 1)
        self.assertEqual(peeks[0]["annotator_id"], "ann1")

    def test_coverage_role_matrix(self):
        app, ann, adj, cov = self.make_crop_app()
        self.addCleanup(app.close)
        server = self.serve(app)
        # Coverage sees the queue with aliases, no transcripts.
        status, _, body = self.call(server, "GET", "/api/coverage/pages",
                                    cov)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["pages"]), 1)
        self.assertEqual(body["pages"][0]["alias"], "pilot-01")
        # Others are refused from the coverage queue.
        for tok in (ann, adj, None):
            status, _, _ = self.call(server, "GET", "/api/coverage/pages",
                                     tok)
            self.assertEqual(status, 403)
        # Coverage can triage proposals but cannot transcribe or adjudicate.
        status, _, proposal = self.call(
            server, "POST", f"/api/page/{HASH_C}/propose-region", ann,
            {"geometry": {"x0": 0, "y0": 50, "x1": 60, "y1": 90}})
        self.assertEqual(status, 200)
        status, _, _ = self.call(
            server, "POST",
            f"/api/proposals/{proposal['proposal']['id']}/review", cov,
            {"decision": "approve"})
        self.assertEqual(status, 200)
        status, _, _ = self.call(server, "GET", "/api/my-pages", cov)
        self.assertEqual(status, 403)
        status, _, _ = self.call(server, "POST",
                                 f"/api/page/{HASH_C}/submit", cov,
                                 {"revision": 0, "lines": {"0": "x"}})
        self.assertEqual(status, 403)
        # Coverage sign-off round-trips.
        status, _, body = self.call(
            server, "POST", "/api/coverage/pages/pilot-01/signoff", cov,
            {"approved": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["signoff"]["census_approved"])
        status, _, _ = self.call(
            server, "POST", "/api/coverage/pages/pilot-01/signoff", ann,
            {"approved": True})
        self.assertEqual(status, 403)


class AssignmentOverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = Path(self.tmpdir.name) / "annotation.sqlite3"

    def test_overview_tracks_own_work_only(self):
        store = AnnotationStore(
            self.db, pages=[make_page(page_id=HASH_C),
                            make_page(page_id=HASH_D)])
        self.addCleanup(store.close)
        first = store.assign(HASH_C, "ann1")
        store.submit(HASH_C, "ann1", revision=first["revision"],
                     lines={0: "distinctive-overview-text"})
        mine = store.assignment_overview("ann1")
        self.assertEqual(len(mine), 2)
        by_page = {entry["page_id"]: entry for entry in mine}
        self.assertEqual(by_page[HASH_C]["status"], "draft")
        self.assertEqual(by_page[HASH_C]["drafted_regions"], 1)
        self.assertEqual(by_page[HASH_C]["total_regions"], 3)
        self.assertEqual(by_page[HASH_D]["status"], "todo")
        # No transcribed text anywhere in the overview.
        self.assertNotIn("distinctive-overview-text",
                         json.dumps(mine, ensure_ascii=False))

    def test_overview_unknown_annotator_is_all_todo(self):
        store = AnnotationStore(self.db, pages=[make_page()])
        self.addCleanup(store.close)
        mine = store.assignment_overview("nobody")
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["status"], "todo")


def write_pin_file(tmpdir, lines=("ann1:1111", "ann2:2222",
                                  "judge1:3333")):
    path = Path(tmpdir) / "pins.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_login_app(tmpdir, pin_lines=("ann1:1111", "ann2:2222",
                                       "judge1:3333")):
    import types
    from pdf_craft_tool.research.annotation_server import build_app
    tmp = Path(tmpdir)
    pages_file = tmp / "pages.json"
    pages_file.write_text(json.dumps([make_page(page_id=HASH_C),
                                      make_page(page_id=HASH_D)]),
                          encoding="utf-8")
    args = types.SimpleNamespace(
        db=tmp / "annotation.sqlite3",
        pages=pages_file,
        images=None,
        annotators=["ann1", "ann2"],
        adjudicators=["judge1"],
        pin_file=write_pin_file(tmpdir, pin_lines),
    )
    return build_app(args)


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_pin_file_parsing(self):
        from pdf_craft_tool.research.annotation_server import read_pin_file
        tmp = Path(self.tmpdir.name)
        good = tmp / "good.txt"
        good.write_text("# comment\n\nann1:1111\nann2:2222\n",
                        encoding="utf-8")
        self.assertEqual(read_pin_file(good),
                         {"ann1": "1111", "ann2": "2222"})
        bad = tmp / "bad.txt"
        bad.write_text("ann1-1111\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            read_pin_file(bad)
        dup = tmp / "dup.txt"
        dup.write_text("ann1:1\nann1:2\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            read_pin_file(dup)
        empty = tmp / "empty.txt"
        empty.write_text("# nothing here\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            read_pin_file(empty)

    def test_missing_pin_for_registered_user_fails_fast(self):
        import types
        from pdf_craft_tool.research.annotation_server import build_app
        tmp = Path(self.tmpdir.name)
        pages_file = tmp / "pages.json"
        pages_file.write_text(json.dumps([make_page()]), encoding="utf-8")
        args = types.SimpleNamespace(
            db=tmp / "annotation.sqlite3",
            pages=pages_file,
            images=None,
            annotators=["ann1", "ann2"],
            adjudicators=[],
            pin_file=write_pin_file(self.tmpdir.name, ("ann1:1111",)),
        )
        with self.assertRaises(SystemExit):
            build_app(args)

    def test_login_mints_working_token(self):
        app = build_login_app(self.tmpdir.name)
        self.addCleanup(app.close)
        server = AnnotationHTTPServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, body, token=None):
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10)
            try:
                headers = {"Content-Type": "application/json"}
                if token:
                    headers[TOKEN_HEADER] = token
                conn.request("POST", path, body=json.dumps(body),
                             headers=headers)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())
            finally:
                conn.close()

        status, body = post("/api/login",
                            {"user_id": "ann1", "pin": "1111"})
        self.assertEqual(status, 200)
        self.assertEqual(body["user_id"], "ann1")
        self.assertEqual(body["role"], "annotator")
        # The minted token authorises real work.
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=10)
        try:
            conn.request("GET", f"/api/page/{HASH_C}",
                         headers={TOKEN_HEADER: body["token"]})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()
        # Wrong PIN and unknown user are rejected without revealing which.
        status, _ = post("/api/login", {"user_id": "ann1", "pin": "0000"})
        self.assertEqual(status, 401)
        status, _ = post("/api/login", {"user_id": "ghost", "pin": "1111"})
        self.assertEqual(status, 401)
        status, _ = post("/api/login", {"user_id": "ann1"})
        self.assertEqual(status, 400)

    def test_lockout_after_repeated_failures(self):
        from pdf_craft_tool.research import annotation_server as srv
        app = build_login_app(self.tmpdir.name)
        self.addCleanup(app.close)
        old_max, old_secs = srv.MAX_LOGIN_FAILURES, srv.LOGIN_LOCKOUT_SECONDS
        srv.MAX_LOGIN_FAILURES, srv.LOGIN_LOCKOUT_SECONDS = 3, 60
        self.addCleanup(setattr, srv, "MAX_LOGIN_FAILURES", old_max)
        self.addCleanup(setattr, srv, "LOGIN_LOCKOUT_SECONDS", old_secs)
        server = AnnotationHTTPServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def login(pin):
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10)
            try:
                conn.request("POST", "/api/login",
                             body=json.dumps({"user_id": "ann1",
                                              "pin": pin}),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status
            finally:
                conn.close()

        self.assertEqual(login("0000"), 401)
        self.assertEqual(login("0000"), 401)
        self.assertEqual(login("0000"), 401)
        # Correct PIN is now locked out too.
        self.assertEqual(login("1111"), 429)


class AliasTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def serve(self, app):
        server = AnnotationHTTPServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def call(self, server, method, path, token=None, body=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=10)
        try:
            headers = {}
            if token:
                headers[TOKEN_HEADER] = token
            data = json.dumps(body) if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
        finally:
            conn.close()

    def test_alias_matches_hash_payload(self):
        app = build_login_app(self.tmpdir.name)
        self.addCleanup(app.close)
        token = app.register_annotator("ann1")
        server = self.serve(app)
        # Sorted ids: HASH_C ("c…") -> pilot-01, HASH_D ("d…") -> pilot-02.
        status, by_hash = self.call(server, "GET", f"/api/page/{HASH_C}",
                                    token)
        self.assertEqual(status, 200)
        status, by_alias = self.call(server, "GET", "/api/page/pilot-01",
                                     token)
        self.assertEqual(status, 200)
        self.assertEqual(by_alias, by_hash)
        self.assertEqual(by_alias["alias"], "pilot-01")
        status, _ = self.call(server, "GET", "/api/page/pilot-99", token)
        self.assertEqual(status, 404)
        status, _ = self.call(server, "GET", "/api/page/bogus", token)
        self.assertEqual(status, 404)

    def test_submit_and_propose_by_alias(self):
        app = build_login_app(self.tmpdir.name)
        self.addCleanup(app.close)
        token = app.register_annotator("ann1")
        server = self.serve(app)
        status, body = self.call(server, "GET", "/api/page/pilot-01", token)
        revision = body["revision"]
        status, body = self.call(server, "POST", "/api/page/pilot-01/submit",
                                 token, {"revision": revision,
                                         "lines": {"0": "ক"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["assignment"]["status"], "draft")
        status, body = self.call(
            server, "POST", "/api/page/pilot-01/propose-region", token,
            {"geometry": {"x0": 0, "y0": 100, "x1": 50, "y1": 140}})
        self.assertEqual(status, 200)
        self.assertEqual(body["proposal"]["status"], "pending")

    def test_my_pages_lists_own_work_only(self):
        app = build_login_app(self.tmpdir.name)
        self.addCleanup(app.close)
        ann1 = app.register_annotator("ann1")
        ann2 = app.register_annotator("ann2")
        judge = app.register_adjudicator("judge1")
        server = self.serve(app)
        _, body = self.call(server, "GET", "/api/page/pilot-01", ann1)
        self.call(server, "POST", "/api/page/pilot-01/submit", ann1,
                  {"revision": body["revision"],
                   "lines": {"0": "distinctive-alias-text"}})
        status, mine = self.call(server, "GET", "/api/my-pages", ann1)
        self.assertEqual(status, 200)
        self.assertEqual(len(mine["pages"]), 2)
        first = [p for p in mine["pages"] if p["alias"] == "pilot-01"][0]
        self.assertEqual(first["status"], "draft")
        self.assertEqual(first["drafted_regions"], 1)
        self.assertEqual(first["total_regions"], 3)
        dump = json.dumps(mine, ensure_ascii=False)
        self.assertNotIn("distinctive-alias-text", dump)
        self.assertNotIn("ann2", dump)
        # Peer sees their own clean slate, nothing of ann1.
        status, peer = self.call(server, "GET", "/api/my-pages", ann2)
        self.assertEqual(status, 200)
        self.assertTrue(all(p["status"] == "todo" for p in peer["pages"]))
        # Adjudicators and strangers are refused.
        status, _ = self.call(server, "GET", "/api/my-pages", judge)
        self.assertEqual(status, 403)
        status, _ = self.call(server, "GET", "/api/my-pages", None)
        self.assertEqual(status, 403)


class DraftLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = Path(self.tmpdir.name) / "annotation.sqlite3"

    def make_store(self, *pages):
        if not pages:
            pages = (make_page(),)
        return AnnotationStore(self.db, pages=list(pages))

    def test_partial_submit_stays_draft(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        result = store.submit(HASH_C, "ann1", revision=payload["revision"],
                              lines={0: "প্রথম"})
        self.assertEqual(result["status"], "draft")
        self.assertFalse(store.peer_ready(HASH_C))
        progress = store.progress()
        self.assertEqual(progress["submitted_assignments"], 0)

    def test_partial_saves_merge_then_promote(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        first = store.submit(HASH_C, "ann1", revision=payload["revision"],
                             lines={0: "এক"})
        self.assertEqual(first["status"], "draft")
        second = store.submit(HASH_C, "ann1", revision=first["revision"],
                              lines={1: "দুই"})
        self.assertEqual(second["status"], "draft")
        # Reloading returns the author's own drafts and nothing else's.
        resumed = store.assign(HASH_C, "ann1")
        self.assertEqual(resumed["drafts"], {"0": "এক", "1": "দুই"})
        self.assertEqual(resumed["revision"], second["revision"])
        third = store.submit(HASH_C, "ann1", revision=second["revision"],
                             lines={2: "তিন"})
        self.assertEqual(third["status"], "submitted")

    def test_drafts_hidden_from_peer(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        store.submit(HASH_C, "ann1", revision=payload["revision"],
                     lines={0: "distinctive-draft-one"})
        peer = store.assign(HASH_C, "ann2")
        self.assertNotIn("distinctive-draft-one",
                         json.dumps(peer, ensure_ascii=False))
        self.assertEqual(peer["drafts"], {})
        assert_no_forbidden(self, peer)

    def test_unknown_region_rejected(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        with self.assertRaises(ValueError):
            store.submit(HASH_C, "ann1", revision=payload["revision"],
                         lines={99: "nope"})

    def test_empty_lines_rejected(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        with self.assertRaises(ValueError):
            store.submit(HASH_C, "ann1", revision=payload["revision"],
                         lines={})

    def test_stale_revision_rejected_on_partial(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        store.submit(HASH_C, "ann1", revision=payload["revision"],
                     lines={0: "x"})
        with self.assertRaises(RevisionConflict):
            store.submit(HASH_C, "ann1", revision=payload["revision"],
                         lines={1: "y"})

    def test_elapsed_ms_accumulates_with_total_cap(self):
        store = self.make_store()
        self.addCleanup(store.close)
        payload = store.assign(HASH_C, "ann1")
        first = store.submit(HASH_C, "ann1", revision=payload["revision"],
                             lines={0: "x"}, elapsed_ms=600_000)
        second = store.submit(HASH_C, "ann1", revision=first["revision"],
                              lines={1: "y"}, elapsed_ms=600_000)
        rows = store.db.execute(
            "SELECT elapsed_ms FROM assignments WHERE page_id=? AND "
            "annotator_id=?", (HASH_C, "ann1")).fetchone()
        self.assertEqual(rows["elapsed_ms"], 1_200_000)
        third = store.submit(HASH_C, "ann1", revision=second["revision"],
                             lines={2: "z"},
                             elapsed_ms=annotation.MAX_SAVE_ELAPSED_MS)
        rows = store.db.execute(
            "SELECT elapsed_ms FROM assignments WHERE page_id=? AND "
            "annotator_id=?", (HASH_C, "ann1")).fetchone()
        self.assertLessEqual(rows["elapsed_ms"],
                             annotation.MAX_TOTAL_ELAPSED_MS)
        self.assertEqual(third["status"], "submitted")
        with self.assertRaises(ValueError):
            store.submit(HASH_C, "ann1", revision=third["revision"],
                         lines={0: "x2"},
                         elapsed_ms=annotation.MAX_SAVE_ELAPSED_MS + 1)


def make_geometry(x0=0, y0=0, x1=50, y1=20):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "unit": "px"}


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = Path(self.tmpdir.name) / "annotation.sqlite3"

    def make_store(self, *pages):
        if not pages:
            pages = (make_page(),)
        return AnnotationStore(self.db, pages=list(pages))

    def submit_full(self, store, page_id, annotator, texts):
        payload = store.assign(page_id, annotator)
        return store.submit(page_id, annotator,
                            revision=payload["revision"], lines=texts)

    def test_propose_and_list(self):
        store = self.make_store()
        self.addCleanup(store.close)
        proposal = store.propose_region(
            HASH_C, "ann1", geometry=make_geometry(), note="missed folio")
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["reporter_id"], "ann1")
        pending = store.list_proposals(HASH_C, status="pending")
        self.assertEqual(len(pending), 1)
        # Proposals never leak into annotator payloads.
        payload = store.assign(HASH_C, "ann2")
        self.assertNotIn("missed folio",
                         json.dumps(payload, ensure_ascii=False))
        assert_no_forbidden(self, payload)

    def test_propose_validation(self):
        store = self.make_store()
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            store.propose_region(HASH_C, "ann1",
                                 geometry={"x0": 10, "y0": 0,
                                           "x1": 5, "y1": 20})
        with self.assertRaises(ValueError):
            store.propose_region(HASH_C, "ann1",
                                 geometry={"x0": -1, "y0": 0,
                                           "x1": 5, "y1": 20})
        with self.assertRaises(ValueError):
            store.propose_region(HASH_C, "ann1",
                                 geometry=make_geometry(),
                                 note="x" * 501)
        with self.assertRaises(ValueError):
            store.propose_region("e" * 64, "ann1",
                                 geometry=make_geometry())
        with self.assertRaises(ValueError):
            store.list_proposals(status="bogus")

    def test_propose_rejected_on_terminal_page(self):
        flagged = make_page(page_id=HASH_C)
        store = self.make_store(flagged)
        self.addCleanup(store.close)
        store.set_status(HASH_C, "flagged")
        with self.assertRaises(ValueError):
            store.propose_region(HASH_C, "ann1",
                                 geometry=make_geometry())

    def test_approve_appends_region_and_reopens(self):
        store = self.make_store()
        self.addCleanup(store.close)
        self.submit_full(store, HASH_C, "ann1",
                         make_lines("a", "b", "c"))
        self.submit_full(store, HASH_C, "ann2",
                         make_lines("a", "b", "c"))
        proposal = store.propose_region(
            HASH_C, "ann1", geometry=make_geometry(y0=100, y1=140),
            note="missed line")
        before = store.assign(HASH_C, "ann1")["revision"]
        result = store.review_proposal(proposal["id"], "judge1",
                                       decision="approve")
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["region_index"], 3)
        # Census grew; reading order is still a valid permutation.
        page = store.assign(HASH_C, "ann1")
        indices = sorted(slot["region_index"]
                         for slot in page["line_slots"])
        self.assertEqual(indices, [0, 1, 2, 3])
        self.assertEqual(sorted(page["reading_order"]), [0, 1, 2, 3])
        # Both assignments reopened as drafts with bumped revisions.
        self.assertEqual(page["assignment_status"], "draft")
        self.assertEqual(page["revision"], before + 1)
        self.assertEqual(page["drafts"],
                         {"0": "a", "1": "b", "2": "c"})
        # Stale-revision saves conflict; export waits for resubmission.
        with self.assertRaises(RevisionConflict):
            store.submit(HASH_C, "ann1", revision=before,
                         lines={3: "d"})
        with self.assertRaises(ValueError):
            store.finalize(HASH_C, adjudicator_id="judge1")
        # Resubmit including the new region promotes back to submitted.
        resub = store.submit(HASH_C, "ann1", revision=page["revision"],
                             lines={3: "d"})
        self.assertEqual(resub["status"], "submitted")

    def test_review_rules(self):
        store = self.make_store()
        self.addCleanup(store.close)
        proposal = store.propose_region(
            HASH_C, "ann1", geometry=make_geometry())
        with self.assertRaises(ValueError):
            store.review_proposal(proposal["id"], "ann1",
                                  decision="approve")
        with self.assertRaises(ValueError):
            store.review_proposal(proposal["id"], "judge1",
                                  decision="reject", reason="   ")
        with self.assertRaises(ValueError):
            store.review_proposal(proposal["id"], "judge1",
                                  decision="maybe")
        rejected = store.review_proposal(proposal["id"], "judge1",
                                         decision="reject",
                                         reason="smudge, not text")
        self.assertEqual(rejected["status"], "rejected")
        page = store.assign(HASH_C, "ann1")
        self.assertEqual(len(page["line_slots"]), 3)
        with self.assertRaises(ValueError):
            store.review_proposal(proposal["id"], "judge1",
                                  decision="approve")

    def test_new_region_flows_to_gold(self):
        store = self.make_store()
        self.addCleanup(store.close)
        self.submit_full(store, HASH_C, "ann1",
                         make_lines("a", "b", "c"))
        self.submit_full(store, HASH_C, "ann2",
                         make_lines("a", "b", "c"))
        proposal = store.propose_region(
            HASH_C, "ann1", geometry=make_geometry(y0=100, y1=140))
        store.review_proposal(proposal["id"], "judge1", decision="approve")
        first = store.assign(HASH_C, "ann1")
        store.submit(HASH_C, "ann1", revision=first["revision"],
                     lines={3: "d"})
        second = store.assign(HASH_C, "ann2")
        store.submit(HASH_C, "ann2", revision=second["revision"],
                     lines={3: "d"})
        store.detect_conflicts(HASH_C)
        page = store.finalize(HASH_C, adjudicator_id="judge1")
        self.assertEqual(len(page.lines), 4)
        self.assertEqual(page.lines[3].text, "d")


def make_png(width, height):
    import struct
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
            + b"\x00\x00\x00\x00")


class PngSizeTests(unittest.TestCase):
    def test_valid_png(self):
        from pdf_craft_tool.research.annotation_server import png_size
        self.assertEqual(png_size(make_png(2550, 3300)), (2550, 3300))

    def test_rejects_non_png(self):
        from pdf_craft_tool.research.annotation_server import png_size
        with self.assertRaises(ValueError):
            png_size(b"not a png at all, just text...........")
        with self.assertRaises(ValueError):
            png_size(b"\x89PNG\r\n\x1a\nshort")


class ProposalServerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _serve(self, app):
        server = AnnotationHTTPServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _request(self, server, method, path, token, body=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=10)
        try:
            data = json.dumps(body) if body is not None else None
            conn.request(method, path, body=data,
                         headers={TOKEN_HEADER: token,
                                  "Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
        finally:
            conn.close()

    def make_app(self, with_image=True):
        tmp = Path(self.tmpdir.name)
        image_sha = HASH_B
        image_dir = None
        if with_image:
            png = make_png(800, 600)
            image_sha = __import__("hashlib").sha256(png).hexdigest()
            (tmp / "img").mkdir(exist_ok=True)
            (tmp / "img" / f"{HASH_C}.png").write_bytes(png)
            image_dir = tmp / "img"
        store = AnnotationStore(tmp / "annotation.sqlite3",
                                pages=[make_page(image=image_sha)])
        app = AnnotationApp(store, image_dir=image_dir)
        ann = app.register_annotator("ann1")
        adj = app.register_adjudicator("judge1")
        return app, ann, adj

    def test_assign_carries_image_size_and_own_proposals(self):
        app, ann, _ = self.make_app(with_image=True)
        server = self._serve(app)
        status, body = self._request(server, "GET", f"/api/page/{HASH_C}",
                                     ann)
        self.assertEqual(status, 200)
        self.assertEqual(body["image_size"], {"width": 800, "height": 600})
        self.assertEqual(body["drafts"], {})
        self.assertEqual(body["own_proposals"], [])

    def test_assign_without_image_dir_omits_size(self):
        app, ann, _ = self.make_app(with_image=False)
        server = self._serve(app)
        status, body = self._request(server, "GET", f"/api/page/{HASH_C}",
                                     ann)
        self.assertEqual(status, 200)
        self.assertIsNone(body["image_size"])

    def test_role_gates(self):
        app, ann, adj = self.make_app()
        server = self._serve(app)
        geo = make_geometry()
        status, _ = self._request(
            server, "POST", f"/api/page/{HASH_C}/propose-region", adj,
            {"geometry": geo})
        self.assertEqual(status, 403)
        status, _ = self._request(server, "GET", "/api/proposals", ann)
        self.assertEqual(status, 403)
        status, _ = self._request(server, "POST", "/api/proposals/1/review",
                                  ann, {"decision": "approve"})
        self.assertEqual(status, 403)

    def test_propose_review_flow_over_http(self):
        app, ann, adj = self.make_app()
        server = self._serve(app)
        geo = make_geometry(y0=100, y1=140)
        status, body = self._request(
            server, "POST", f"/api/page/{HASH_C}/propose-region", ann,
            {"geometry": geo, "note": "folio"})
        self.assertEqual(status, 200)
        proposal_id = body["proposal"]["id"]
        status, body = self._request(server, "GET",
                                     "/api/proposals?status=pending", adj)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["proposals"]), 1)
        status, body = self._request(
            server, "POST", f"/api/proposals/{proposal_id}/review", adj,
            {"decision": "approve"})
        self.assertEqual(status, 200)
        self.assertEqual(body["proposal"]["region_index"], 3)
        # The annotator's next load shows the new slot.
        status, body = self._request(server, "GET", f"/api/page/{HASH_C}",
                                     ann)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["line_slots"]), 4)
        # Bad decision and unknown id are 400s, not 500s.
        status, _ = self._request(server, "POST",
                                  "/api/proposals/9999/review", adj,
                                  {"decision": "approve"})
        self.assertEqual(status, 400)
        status, body = self._request(
            server, "POST", f"/api/page/{HASH_C}/propose-region", ann,
            {"geometry": {"x0": 5, "y0": 0, "x1": 1, "y1": 2}})
        self.assertEqual(status, 400)

    def test_partial_save_over_http(self):
        app, ann, _ = self.make_app()
        server = self._serve(app)
        status, body = self._request(server, "GET", f"/api/page/{HASH_C}",
                                     ann)
        revision = body["revision"]
        status, body = self._request(
            server, "POST", f"/api/page/{HASH_C}/submit", ann,
            {"revision": revision, "lines": {"0": "ক"}, "elapsed_ms": 5000})
        self.assertEqual(status, 200)
        self.assertEqual(body["assignment"]["status"], "draft")
        revision = body["assignment"]["revision"]
        status, body = self._request(
            server, "POST", f"/api/page/{HASH_C}/submit", ann,
            {"revision": revision,
             "lines": {"1": "খ", "2": "গ"}, "elapsed_ms": 7000})
        self.assertEqual(status, 200)
        self.assertEqual(body["assignment"]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
