"""Tests for P2 blinded 5-voter adjudication (unittest, stdlib only)."""

import hashlib
import http.client
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from pdf_craft_tool.research.annotation import RevisionConflict
from pdf_craft_tool.research.annotation_server import (
    TOKEN_HEADER,
    AnnotationApp,
    AnnotationStore,
)
from pdf_craft_tool.research.study_adjudication import (
    StudyVoteStore,
    _assert_study_blind,
)
from pdf_craft_tool.research.study_server import (
    StudyCropSource,
    StudyHTTPServer,
)

HAS_PIL = importlib.util.find_spec("PIL") is not None

VOTERS = (
    "voter-tesseract-ben",
    "voter-qwen-chen",
    "voter-ocrmypdf-dana",
    "voter-easyocr-eli",
    "voter-paddle-fay",
)
MODELS = (
    "google-vision-document-text-detection",
    "azure-computervision-read-ava",
)
SEED = "study-seed-001"


def make_votes(text="same line", failures=None, missing=()):
    failures = failures or {}
    votes = {}
    for voter in VOTERS:
        if voter in missing:
            continue
        votes[voter] = {
            "text": text,
            "failure_state": failures.get(voter, "ok"),
        }
    return votes


def make_store(tmpdir, voters=VOTERS, seed=SEED, name="study.sqlite3"):
    return StudyVoteStore(Path(tmpdir) / name, voter_ids=voters,
                          label_seed=seed)


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_unanimous_is_provisional_accept(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        store.record_votes("page-1", 0, make_votes("hello world"))
        result = store.triage("page-1")
        self.assertEqual(result, {"provisional_accept": 1, "conflict": 0,
                                  "regions": 1})
        self.assertEqual(store.blind_conflicts("page-1")["items"], [])

    def test_split_is_conflict(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        votes = make_votes("alpha")
        votes[VOTERS[4]] = {"text": "beta", "failure_state": "ok"}
        store.record_votes("page-1", 0, votes)
        result = store.triage("page-1")
        self.assertEqual(result["conflict"], 1)
        self.assertEqual(result["provisional_accept"], 0)
        items = store.blind_conflicts("page-1")["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]["candidates"]), 5)

    def test_failure_state_is_conflict(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        store.record_votes("page-1", 0,
                           make_votes("x", failures={VOTERS[1]: "no_output"}))
        self.assertEqual(store.triage("page-1")["conflict"], 1)
        candidates = store.blind_conflicts("page-1")["items"][0]["candidates"]
        failed = [item for item in candidates if item["status"] == "no_output"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["text"], "")

    def test_missing_voter_is_conflict(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        store.record_votes("page-1", 0, make_votes("x", missing=(VOTERS[0],)))
        self.assertEqual(store.triage("page-1")["conflict"], 1)
        with self.assertRaises(ValueError):
            store.record_votes("page-1", 1, {"ghost-voter": {
                "text": "x", "failure_state": "ok"}})

    def test_whitespace_and_nfc_only_differences_still_unanimous(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        votes = make_votes("  hello   world  ")
        votes[VOTERS[1]] = {"text": "hello world", "failure_state": "ok"}
        votes[VOTERS[2]] = {"text": "hello\tworld\n",
                            "failure_state": "ok"}
        store.record_votes("page-1", 0, votes)
        # Region 0 mixes hello-world spacing variants -> still unanimous.
        self.assertEqual(store.triage("page-1")["provisional_accept"], 1)
        # NFC-only difference: e + combining acute vs precomposed e.
        votes_b = make_votes("cafe\u0301")
        votes_b[VOTERS[1]] = {"text": "caf\u00e9", "failure_state": "ok"}
        votes_b[VOTERS[2]] = {"text": "  cafe\u0301  ",
                              "failure_state": "ok"}
        store.record_votes("page-1", 1, votes_b)
        result = store.triage("page-1")
        self.assertEqual(result["provisional_accept"], 2)

    def test_record_votes_idempotent_upsert(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        store.record_votes("page-1", 0, make_votes("one"))
        store.record_votes("page-1", 0, make_votes("two"))
        self.assertEqual(store.triage("page-1")["provisional_accept"], 1)
        payload = store.blind_conflicts("page-1")
        self.assertEqual(payload["items"], [])


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_labels_stable_per_page_and_vary_across_pages(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        first = store.labels_for("page-1")
        self.assertEqual(sorted(first), ["A", "B", "C", "D", "E"])
        self.assertEqual(sorted(first.values()), sorted(VOTERS))
        self.assertEqual(store.labels_for("page-1"), first)
        others = [store.labels_for(f"page-{index}") for index in range(10)]
        self.assertTrue(any(item != first for item in others))

    def test_label_seed_generated_once_and_persisted(self):
        path = Path(self.tmpdir.name) / "seeded.sqlite3"
        first = StudyVoteStore(path, voter_ids=VOTERS)
        seed = first.label_seed
        self.assertTrue(isinstance(seed, str) and seed)
        first.close()
        second = StudyVoteStore(path, voter_ids=VOTERS)
        self.addCleanup(second.close)
        self.assertEqual(second.label_seed, seed)
        # An explicit seed still wins and labels stay deterministic.
        third = StudyVoteStore(Path(self.tmpdir.name) / "explicit.sqlite3",
                               voter_ids=VOTERS, label_seed=SEED)
        self.addCleanup(third.close)
        self.assertEqual(third.label_seed, SEED)


class BlindnessTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_assert_study_blind_raises_on_planted_id(self):
        # A clean payload (anonymous candidates included) passes.
        _assert_study_blind({"page_id": "p", "items": []}, [])
        _assert_study_blind(
            {"page_id": "p", "items": [{
                "region_index": 0, "revision": 0, "candidates": [
                    {"label": "A", "text": "hello", "status": "ok"}]}]},
            VOTERS + MODELS)
        with self.assertRaises(ValueError):
            _assert_study_blind({"label": "A", "note": "from "
                                 + VOTERS[0]}, VOTERS)
        with self.assertRaises(ValueError):
            _assert_study_blind({"note": MODELS[0].upper()}, VOTERS + MODELS)
        with self.assertRaises(ValueError):
            _assert_study_blind({"voter_id": "x"}, VOTERS)
        with self.assertRaises(ValueError):
            _assert_study_blind({"model_id": "x"}, VOTERS)
        with self.assertRaises(ValueError):
            _assert_study_blind({"nested": {"resolver_id": "x"}}, VOTERS)
        # A non-anonymous "candidates" value is still caught by _assert_blind.
        with self.assertRaises(ValueError):
            _assert_study_blind({"candidates": "model draft"}, VOTERS)

    def test_identity_strings_absent_from_store_payloads(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        votes = make_votes("alpha")
        votes[VOTERS[0]] = {"text": "different", "failure_state": "ok"}
        store.record_votes("study-page-001", 0, votes)
        payload = store.blind_conflicts("study-page-001")
        row = store.adjudicate("study-page-001", 0, resolved_text="alpha",
                               resolver_id="judge-1", reason="image is clear",
                               revision=0, choice_label=None)
        for obj in (payload, row):
            dump = json.dumps(obj, ensure_ascii=False).lower()
            for secret in VOTERS + MODELS:
                self.assertNotIn(secret.lower(), dump)


class AdjudicateRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _conflict_store(self, page="page-1"):
        store = make_store(self.tmpdir.name)
        votes = make_votes("alpha")
        votes[VOTERS[0]] = {"text": "beta", "failure_state": "ok"}
        store.record_votes(page, 0, votes)
        return store

    def test_resolver_must_differ_from_voters(self):
        store = self._conflict_store()
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            store.adjudicate("page-1", 0, resolved_text="alpha",
                             resolver_id=VOTERS[2], reason="clear",
                             revision=0)

    def test_empty_reason_rejected(self):
        store = self._conflict_store()
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            store.adjudicate("page-1", 0, resolved_text="alpha",
                             resolver_id="judge-1", reason="   ",
                             revision=0)

    def test_stale_revision_rejected(self):
        store = self._conflict_store()
        self.addCleanup(store.close)
        store.adjudicate("page-1", 0, resolved_text="alpha",
                         resolver_id="judge-1", reason="clear", revision=0)
        with self.assertRaises(RevisionConflict):
            store.adjudicate("page-1", 0, resolved_text="alpha",
                             resolver_id="judge-1", reason="clear",
                             revision=0)

    def test_only_conflicts_adjudicable(self):
        store = make_store(self.tmpdir.name)
        self.addCleanup(store.close)
        store.record_votes("page-1", 0, make_votes("same"))
        with self.assertRaises(ValueError):
            store.adjudicate("page-1", 0, resolved_text="same",
                             resolver_id="judge-1", reason="clear",
                             revision=0)
        with self.assertRaises(ValueError):
            store.adjudicate("page-1", 0, resolved_text="x",
                             resolver_id="judge-1", reason="clear",
                             revision=0, choice_label="Z")

    def test_unblinded_export_maps_choice_to_voter(self):
        store = self._conflict_store()
        self.addCleanup(store.close)
        labels = store.labels_for("page-1")
        store.adjudicate("page-1", 0, resolved_text="alpha",
                         resolver_id="judge-1", reason="image is clear",
                         revision=0, choice_label="B")
        exported = store.unblinded_export("page-1")
        self.assertEqual(exported["labels"], labels)
        item = exported["items"][0]
        self.assertEqual(item["resolution"]["chosen_voter_id"], labels["B"])
        self.assertEqual(item["resolution"]["choice_label"], "B")
        self.assertEqual(item["resolution"]["resolver_id"], "judge-1")

    def test_empty_resolved_text_closes_conflict(self):
        # Noise / stray ink: "no text" is a valid verdict, not "unresolved".
        store = self._conflict_store()
        self.addCleanup(store.close)
        self.assertEqual(store.open_conflicts("page-1"), 1)
        row = store.adjudicate("page-1", 0, resolved_text="",
                               resolver_id="judge-1",
                               reason="stray ink, no text", revision=0)
        self.assertEqual((row["resolved_text"], row["revision"]), ("", 1))
        self.assertEqual(store.open_conflicts("page-1"), 0)
        self.assertEqual(store._resolved("page-1"), {0})
        exported = store.unblinded_export("page-1")["items"][0]
        self.assertEqual(exported["resolution"]["resolved_text"], "")

    def test_reason_may_mention_voter_id(self):
        # The adjudicator's own reason/resolved_text echoes are exempt
        # from the identity-substring scan (their keys are still checked).
        store = self._conflict_store()
        self.addCleanup(store.close)
        row = store.adjudicate(
            "page-1", 0, resolved_text="alpha",
            resolver_id="judge-1",
            reason=f"clearer than the {VOTERS[0]} output", revision=0)
        self.assertEqual(row["revision"], 1)

    def test_failed_blind_check_rolls_back_write(self):
        store = self._conflict_store()
        self.addCleanup(store.close)
        original = store._check_blind

        def boom(payload):
            raise ValueError("planted blindness failure")

        store._check_blind = boom
        try:
            with self.assertRaises(ValueError):
                store.adjudicate("page-1", 0, resolved_text="alpha",
                                 resolver_id="judge-1", reason="clear",
                                 revision=0)
        finally:
            store._check_blind = original
        # The write was rolled back: revision never advanced.
        self.assertEqual(store._revision_of("page-1", 0), 0)
        self.assertEqual(store.open_conflicts("page-1"), 1)


class StudyServerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def _serve(self, votes, crops=None):
        store = AnnotationStore(self.tmp / "annotation.sqlite3", pages=[])
        app = AnnotationApp(store)
        adjudicator = app.register_adjudicator("judge-1")
        annotator = app.register_annotator("ann-1")
        server = StudyHTTPServer(("127.0.0.1", 0), app, votes, crops)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, adjudicator, annotator

    def _request(self, server, method, path, token, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=10)
        try:
            headers = {}
            if token:
                headers[TOKEN_HEADER] = token
            data = json.dumps(body) if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
        finally:
            connection.close()

    def _voted(self):
        votes = StudyVoteStore(self.tmp / "study.sqlite3",
                               voter_ids=VOTERS, label_seed=SEED)
        ballot = make_votes("alpha")
        ballot[VOTERS[0]] = {"text": "beta", "failure_state": "ok"}
        votes.record_votes("study-page-001", 0, ballot)
        return votes

    def test_blind_payloads_have_no_identities_over_http(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        status, payload = self._request(
            server, "GET", "/api/study/conflicts/study-page-001",
            adjudicator)
        self.assertEqual(status, 200)
        revision = payload["items"][0]["revision"]
        status, answered = self._request(
            server, "POST", "/api/study/adjudicate/study-page-001",
            adjudicator, {"region_index": 0, "resolved_text": "alpha",
                          "reason": "image is clear", "revision": revision,
                          "choice_label": None})
        self.assertEqual(status, 200)
        for obj in (payload, answered):
            dump = json.dumps(obj, ensure_ascii=False).lower()
            for secret in VOTERS + MODELS:
                self.assertNotIn(secret.lower(), dump)

    def test_pages_lists_open_conflicts(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        status, body = self._request(server, "GET", "/api/study/pages",
                                     adjudicator)
        self.assertEqual(status, 200)
        self.assertEqual(body["pages"],
                         [{"page_id": "study-page-001",
                           "open_conflicts": 1}])

    def test_empty_reason_is_400(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        status, _ = self._request(
            server, "POST", "/api/study/adjudicate/study-page-001",
            adjudicator, {"region_index": 0, "resolved_text": "alpha",
                          "reason": "   ", "revision": 0})
        self.assertEqual(status, 400)

    def test_stale_revision_is_409(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        status, _ = self._request(
            server, "POST", "/api/study/adjudicate/study-page-001",
            adjudicator, {"region_index": 0, "resolved_text": "alpha",
                          "reason": "clear", "revision": 0})
        self.assertEqual(status, 200)
        status, _ = self._request(
            server, "POST", "/api/study/adjudicate/study-page-001",
            adjudicator, {"region_index": 0, "resolved_text": "alpha",
                          "reason": "clear", "revision": 0})
        self.assertEqual(status, 409)

    def test_annotator_role_is_403(self):
        votes = self._voted()
        server, _, annotator = self._serve(votes)
        status, _ = self._request(
            server, "GET", "/api/study/conflicts/study-page-001", annotator)
        self.assertEqual(status, 403)
        status, _ = self._request(
            server, "POST", "/api/study/adjudicate/study-page-001",
            annotator, {"region_index": 0, "resolved_text": "alpha",
                        "reason": "clear", "revision": 0})
        self.assertEqual(status, 403)
        status, _ = self._request(server, "GET", "/api/study/pages",
                                   annotator)
        self.assertEqual(status, 403)

    def test_study_pages_served(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        status, _ = self._request(server, "GET", "/study", adjudicator)
        self.assertEqual(status, 200)
        status, _ = self._request(server, "GET",
                                  "/static/study_adjudicate.js", adjudicator)
        self.assertEqual(status, 200)

    def test_pages_payload_leak_fails_closed(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        votes.page_ids = lambda: [VOTERS[0]]
        status, body = self._request(server, "GET", "/api/study/pages",
                                     adjudicator)
        self.assertEqual(status, 500)
        self.assertNotIn(VOTERS[0].lower(),
                         json.dumps(body, ensure_ascii=False).lower())

    def test_seed_absent_from_every_http_payload(self):
        votes = StudyVoteStore(self.tmp / "unseeded.sqlite3",
                               voter_ids=VOTERS)
        seed = votes.label_seed
        self.assertTrue(seed)
        ballot = make_votes("alpha")
        ballot[VOTERS[0]] = {"text": "beta", "failure_state": "ok"}
        votes.record_votes("study-page-001", 0, ballot)
        server, adjudicator, _ = self._serve(votes)
        status, conflicts = self._request(
            server, "GET", "/api/study/conflicts/study-page-001",
            adjudicator)
        self.assertEqual(status, 200)
        status, pages = self._request(server, "GET", "/api/study/pages",
                                      adjudicator)
        self.assertEqual(status, 200)
        status, answered = self._request(
            server, "POST", "/api/study/adjudicate/study-page-001",
            adjudicator, {"region_index": 0, "resolved_text": "alpha",
                          "reason": "image is clear", "revision": 0})
        self.assertEqual(status, 200)
        for obj in (conflicts, pages, answered):
            self.assertNotIn(
                seed.lower(),
                json.dumps(obj, ensure_ascii=False).lower())

    def test_missing_voter_ids_is_argparse_error(self):
        from pdf_craft_tool.research.study_server import main
        with self.assertRaises(SystemExit):
            main(["--db", str(self.tmp / "a.sqlite3"),
                  "--votes-db", str(self.tmp / "v.sqlite3")])
        with self.assertRaises(SystemExit):
            main(["--db", str(self.tmp / "a.sqlite3"),
                  "--votes-db", str(self.tmp / "v.sqlite3"),
                  "--voter-ids", "only-one"])

    # -- region crops ----------------------------------------------------
    def _raw(self, server, path, token):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=10)
        try:
            headers = {TOKEN_HEADER: token} if token else {}
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return (response.status, response.getheader("Content-Type"),
                    response.read())
        finally:
            connection.close()

    def _crop_source(self, page_id="study-page-001"):
        from PIL import Image, ImageDraw, PngImagePlugin
        study = self.tmp / "study"
        (study / "pages").mkdir(parents=True)
        image = Image.new("RGB", (400, 300), "white")
        ImageDraw.Draw(image).rectangle((50, 40, 250, 80), fill="black")
        # Source metadata naming a voter must never reach the crop.
        info = PngImagePlugin.PngInfo()
        info.add_text("Software", VOTERS[0])
        png = study / "pages" / f"{page_id}.png"
        image.save(png, format="PNG", pnginfo=info)
        pages = [{"page_id": page_id,
                  "image_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
                  "census": {"entries": [{
                      "region_index": 0, "kind": "body",
                      "geometry": {"x0": 50, "y0": 40, "x1": 250, "y1": 80,
                                   "unit": "px"}}]}}]
        (study / "annotation_pages.json").write_text(json.dumps(pages))
        return StudyCropSource(study), study

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_crop_returns_padded_png_and_caches(self):
        from PIL import Image
        crops, study = self._crop_source()
        server, adjudicator, _ = self._serve(self._voted(), crops)
        status, ctype, body = self._raw(
            server, "/api/study/crop/study-page-001/0", adjudicator)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        # 200x40 region + 8% padding per side -> (34, 37, 266, 83).
        self.assertEqual(Image.open(io.BytesIO(body)).size, (232, 46))
        self.assertNotIn(VOTERS[0].encode(), body)
        self.assertEqual(len(list((study / "study-crops").glob("*.png"))), 1)
        status, _, again = self._raw(
            server, "/api/study/crop/study-page-001/0", adjudicator)
        self.assertEqual((status, again), (200, body))

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_crop_bad_region_is_4xx(self):
        crops, _ = self._crop_source()
        server, adjudicator, _ = self._serve(self._voted(), crops)
        for path, expected in (("/api/study/crop/study-page-001/9", 404),
                               ("/api/study/crop/study-page-001/x", 400),
                               ("/api/study/crop/study-page-001/-1", 400),
                               ("/api/study/crop/no-such-page/0", 404)):
            status, _, _ = self._raw(server, path, adjudicator)
            self.assertEqual(status, expected, path)

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_crop_requires_adjudicator_token(self):
        crops, _ = self._crop_source()
        server, _, annotator = self._serve(self._voted(), crops)
        for token in (annotator, None, "bogus"):
            status, _, _ = self._raw(
                server, "/api/study/crop/study-page-001/0", token)
            self.assertEqual(status, 403)

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_crop_tampered_image_fails_closed(self):
        from PIL import Image
        crops, study = self._crop_source()
        server, adjudicator, _ = self._serve(self._voted(), crops)
        Image.new("RGB", (400, 300), "gray").save(
            study / "pages" / "study-page-001.png", format="PNG")
        status, _, body = self._raw(
            server, "/api/study/crop/study-page-001/0", adjudicator)
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "internal error"})

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_crop_metadata_leak_fails_closed(self):
        from PIL import Image, PngImagePlugin
        crops, _ = self._crop_source()
        server, adjudicator, _ = self._serve(self._voted(), crops)
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", VOTERS[2])
        buffer = io.BytesIO()
        Image.new("L", (4, 4)).save(buffer, format="PNG", pnginfo=info)
        crops.crop = lambda page_id, region_index: buffer.getvalue()
        status, _, body = self._raw(
            server, "/api/study/crop/study-page-001/0", adjudicator)
        self.assertEqual(status, 500)
        self.assertNotIn(VOTERS[2].encode(), body)

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_crop_traversal_page_ids_rejected(self):
        crops, _ = self._crop_source()
        server, adjudicator, _ = self._serve(self._voted(), crops)
        for page in ("..", ".hidden", "..%2Fpages%2Fstudy-page-001",
                     "%2e%2e", "a%00b"):
            status, _, _ = self._raw(
                server, f"/api/study/crop/{page}/0", adjudicator)
            self.assertEqual(status, 404, page)
        with self.assertRaises(LookupError):
            crops.crop("../study/pages/study-page-001", 0)

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_crop_census_amendment_reloads_without_restart(self):
        from PIL import Image
        crops, study = self._crop_source()
        server, adjudicator, _ = self._serve(self._voted(), crops)
        _, _, first = self._raw(
            server, "/api/study/crop/study-page-001/0", adjudicator)
        pages_json = study / "annotation_pages.json"
        pages = json.loads(pages_json.read_text())
        pages[0]["census"]["entries"][0]["geometry"].update(x1=150, y1=140)
        pages_json.write_text(json.dumps(pages, indent=1))
        status, _, second = self._raw(
            server, "/api/study/crop/study-page-001/0", adjudicator)
        self.assertEqual(status, 200)
        # 100x100 region + 8% padding per side -> (42, 32, 158, 148).
        self.assertEqual(Image.open(io.BytesIO(second)).size, (116, 116))
        self.assertNotEqual(first, second)

    def test_unknown_conflicts_page_does_not_drop_connection(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)

        def boom(page_id):
            raise KeyError(VOTERS[0])

        votes.blind_conflicts = boom
        status, body = self._request(
            server, "GET", "/api/study/conflicts/nope", adjudicator)
        self.assertEqual(status, 500)
        self.assertNotIn(VOTERS[0], json.dumps(body))

    def test_crop_unconfigured_is_404(self):
        server, adjudicator, _ = self._serve(self._voted())
        status, _, _ = self._raw(
            server, "/api/study/crop/study-page-001/0", adjudicator)
        self.assertEqual(status, 404)

    def test_empty_resolved_text_resolves_over_http(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        status, body = self._request(
            server, "POST", "/api/study/adjudicate/study-page-001",
            adjudicator, {"region_index": 0, "resolved_text": "",
                          "reason": "stray ink, no text", "revision": 0})
        self.assertEqual(status, 200)
        self.assertEqual(body["adjudication"]["revision"], 1)
        status, conflicts = self._request(
            server, "GET", "/api/study/conflicts/study-page-001", adjudicator)
        self.assertEqual((status, conflicts["items"]), (200, []))
        _, pages = self._request(server, "GET", "/api/study/pages",
                                 adjudicator)
        self.assertEqual(pages["summary"]["resolved"], 1)
        self.assertEqual(pages["pages"][0]["open_conflicts"], 0)

    def test_pages_summary_counts(self):
        votes = self._voted()
        server, adjudicator, _ = self._serve(votes)
        status, body = self._request(server, "GET", "/api/study/pages",
                                     adjudicator)
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"], {"pages": 1, "open_conflicts": 1,
                                           "resolved": 0})
        self._request(server, "POST", "/api/study/adjudicate/study-page-001",
                      adjudicator, {"region_index": 0, "resolved_text": "alpha",
                                    "reason": "clear", "revision": 0})
        _, body = self._request(server, "GET", "/api/study/pages",
                                adjudicator)
        self.assertEqual(body["summary"], {"pages": 1, "open_conflicts": 0,
                                           "resolved": 1})

    def test_ui_shell_smoke(self):
        votes = self._voted()
        server, _, _ = self._serve(votes)
        status, html = self._request(server, "GET", "/study", None)
        self.assertEqual(status, 200)
        html = html.decode("utf-8")
        for needle in ('id="crop"', 'id="candidates"', 'id="resolved"',
                       'id="reason"', 'id="submit-btn"', 'id="p-page"',
                       'src="/static/study_adjudicate.js"'):
            self.assertIn(needle, html)
        self.assertNotIn("://", html)  # no CDN / remote assets
        status, script = self._request(
            server, "GET", "/static/study_adjudicate.js", None)
        self.assertEqual(status, 200)
        self.assertIn(b"/api/study/crop/", script)
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed; JS syntax check skipped")
        source = (Path(__file__).resolve().parents[2] / "pdf_craft_tool"
                  / "research" / "static" / "study_adjudicate.js")
        result = subprocess.run([node, "--check", str(source)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_loopback_host_refused(self):
        store = AnnotationStore(self.tmp / "annotation.sqlite3", pages=[])
        app = AnnotationApp(store)
        votes = StudyVoteStore(self.tmp / "study.sqlite3",
                               voter_ids=VOTERS, label_seed=SEED)
        try:
            with self.assertRaises(ValueError):
                StudyHTTPServer(("0.0.0.0", 8768), app, votes)
        finally:
            app.close()
            votes.close()


if __name__ == "__main__":
    unittest.main()
