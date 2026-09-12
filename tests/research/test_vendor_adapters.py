"""Offline checks for vendor OCR adapters, transports and quotas (P1).

Fake transports / fake ``urlopen`` only -- zero network. Every test runs
with ``allow_execution=False`` except the ones that explicitly exercise the
allowed path with a fake client/``urlopen``.
"""

import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from pdf_craft_tool.research import (
    adapters,
    runners,
    schema,
    vendor_transports,
)

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64

FAKE_GOOGLE_KEY = "FAKE-GOOGLE-KEY-9f8e7d6c5b4a"
FAKE_AZURE_KEY = "FAKE-AZURE-KEY-1a2b3c4d5e6f"
FAKE_AZURE_ENDPOINT = "https://fake-vendor-host-xyz.example/cogsvc"

GV_CONFIG = {
    "endpoint_env": "https://vision.googleapis.com/v1",
    "api_version": "v1",
    "model_version": "builtin/stable",
    "drift_epoch": "2026-09-11",
    "feature": "DOCUMENT_TEXT_DETECTION",
    "language_hints": ["bn"],
}
AZ_CONFIG = {
    "endpoint_env": "AZURE_VISION_ENDPOINT",
    "api_version": "2024-02-01",
    "model_version": "2023-10-01",
    "drift_epoch": "2026-09-11",
}


def _spec(adapter_id, model_id, config):
    return adapters.AdapterSpec(
        adapter_id=adapter_id,
        kind="recognizer",
        model_id=model_id,
        prompt_id="none",
        prompt_hash="none",
        crop_policy="page_image",
        config=dict(config),
    )


def _gv_spec(config=None):
    return _spec("GV", "google-vision-document-text-detection",
                 config or GV_CONFIG)


def _az_spec(config=None):
    return _spec("AZ", "azure-ai-vision-read", config or AZ_CONFIG)


class FakeClient:
    """Fake vendor client recording invocations."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or {
            "raw_output": '{"responses": []}',
            "parsed_text": "কখগ",
            "failure_state": "ok",
            "resource": {"http_status": 200, "attempts": 1},
        }

    def __call__(self, *, image_bytes, page_id, adapter_id, config):
        self.calls.append(
            {"page_id": page_id, "adapter_id": adapter_id,
             "n_bytes": len(image_bytes), "config": dict(config)}
        )
        return dict(self.reply)


class FakeResponse:
    """Minimal urlopen response double."""

    def __init__(self, payload: bytes, status: int = 200, headers=None):
        self._payload = payload
        self.status = status
        self.headers = dict(headers or {})

    def read(self):
        return self._payload


def _http_error(code, body: bytes, headers=None):
    return urllib.error.HTTPError(
        "https://unit.test/x", code, "error", dict(headers or {}),
        io.BytesIO(body),
    )


class RecordingUrlopen:
    """Fake urlopen with scripted replies; records requests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _header_items(request):
    found = {}
    for key, value in request.header_items():
        found[key.lower()] = value
    for key, value in getattr(request, "unredirected_hdrs", {}).items():
        found.setdefault(key.lower(), value)
    return found


class VendorAdapterGating(unittest.TestCase):
    def test_gated_offline_no_file_read(self):
        for spec in (_gv_spec(), _az_spec()):
            adapter = adapters.make_adapter(spec)
            client = FakeClient()
            prediction = adapter.predict(
                page_id=HEX_A, image_ref="/nonexistent/image.png",
                ocr_text="", allow_execution=False, client=client)
            self.assertEqual(prediction.failure_state, "unsupported")
            self.assertEqual(prediction.parsed_text, "")
            self.assertEqual(client.calls, [])

    def test_no_client_is_unsupported(self):
        adapter = adapters.make_adapter(_gv_spec())
        prediction = adapter.predict(
            page_id=HEX_A, image_ref="whatever", ocr_text="",
            allow_execution=True, client=None)
        self.assertEqual(prediction.failure_state, "unsupported")

    def test_missing_image_is_invocation_error(self):
        adapter = adapters.make_adapter(_az_spec())
        client = FakeClient()
        prediction = adapter.predict(
            page_id=HEX_A, image_ref="/nonexistent/image.png",
            ocr_text="", allow_execution=True, client=client)
        self.assertEqual(prediction.failure_state, "invocation_error")
        self.assertEqual(client.calls, [])

    def test_registry(self):
        self.assertIs(adapters.ADAPTERS["GV"], adapters.GoogleVisionAdapter)
        self.assertIs(adapters.ADAPTERS["AZ"], adapters.AzureReadAdapter)

    def test_success_carries_timing_resource_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "page.png"
            image.write_bytes(b"fake-png-bytes")
            adapter = adapters.make_adapter(_gv_spec())
            prediction = adapter.predict(
                page_id=HEX_A, image_ref=str(image), ocr_text="",
                allow_execution=True, client=FakeClient())
            self.assertEqual(prediction.failure_state, "ok")
            self.assertEqual(prediction.parsed_text, "কখগ")
            self.assertIsInstance(prediction.timing_ms, int)
            self.assertGreaterEqual(prediction.timing_ms, 0)
            today = datetime.datetime.now(datetime.timezone.utc).date()
            self.assertEqual(prediction.resource["call_date"],
                             int(today.strftime("%Y%m%d")))
            self.assertEqual(prediction.resource["version_mismatch"], 0)
            self.assertEqual(prediction.resource["http_status"], 200)
            self.assertEqual(
                prediction.config_hash, adapter.identity()["config_hash"])

    def test_version_mismatch_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "page.png"
            image.write_bytes(b"fake-png-bytes")
            client = FakeClient(reply={
                "raw_output": "{}",
                "parsed_text": "কখগ",
                "failure_state": "ok",
                "resource": {"http_status": 200, "attempts": 1,
                             "model_version": "2099-01-01"},
            })
            adapter = adapters.make_adapter(_az_spec())
            prediction = adapter.predict(
                page_id=HEX_A, image_ref=str(image), ocr_text="",
                allow_execution=True, client=client)
            self.assertEqual(prediction.failure_state, "ok")
            self.assertEqual(prediction.resource["version_mismatch"], 1)

    def test_client_exception_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "page.png"
            image.write_bytes(b"fake-png-bytes")

            def exploding(*, image_bytes, page_id, adapter_id, config):
                raise RuntimeError(
                    f"boom {FAKE_GOOGLE_KEY} at {FAKE_AZURE_ENDPOINT}")

            with mock.patch.dict(os.environ, {
                "GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY,
                "AZURE_VISION_ENDPOINT": FAKE_AZURE_ENDPOINT,
            }):
                adapter = adapters.make_adapter(_gv_spec())
                prediction = adapter.predict(
                    page_id=HEX_A, image_ref=str(image), ocr_text="",
                    allow_execution=True, client=exploding)
            self.assertEqual(prediction.failure_state, "invocation_error")
            self.assertIn("RuntimeError", prediction.raw_output)
            for field in (prediction.raw_output, prediction.parsed_text,
                          json.dumps(prediction.resource, sort_keys=True)):
                self.assertNotIn(FAKE_GOOGLE_KEY, field)
                self.assertNotIn(FAKE_AZURE_ENDPOINT, field)
                self.assertNotIn("fake-vendor-host-xyz.example", field)

    def test_cache_key_moves_with_endpoint_and_drift(self):
        first = adapters.make_adapter(_az_spec())
        with mock.patch.dict(os.environ,
                             {"AZURE_VISION_ENDPOINT": "https://one.example"}):
            hash_one = first.identity()["config_hash"]
        with mock.patch.dict(os.environ,
                             {"AZURE_VISION_ENDPOINT": "https://two.example"}):
            hash_two = first.identity()["config_hash"]
        self.assertNotEqual(hash_one, hash_two)
        drifted = dict(AZ_CONFIG, drift_epoch="2026-09-12")
        third = adapters.make_adapter(_az_spec(drifted))
        with mock.patch.dict(os.environ,
                             {"AZURE_VISION_ENDPOINT": "https://one.example"}):
            hash_three = third.identity()["config_hash"]
        self.assertNotEqual(hash_one, hash_three)


class GoogleTransport(unittest.TestCase):
    def _client(self, urlopen, env=None):
        return vendor_transports.make_google_vision_transport(
            rights_gate=lambda page_id: True,
            env=dict(env or {}),
            urlopen=urlopen,
        )

    def test_success(self):
        body = json.dumps(
            {"responses": [{"fullTextAnnotation": {"text": "কখগ"}}]}
        ).encode("utf-8")
        urlopen = RecordingUrlopen([FakeResponse(body)])
        client = self._client(
            urlopen, {"GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY})
        result = client(image_bytes=b"img", page_id=HEX_A, adapter_id="GV",
                        config=dict(GV_CONFIG))
        self.assertEqual(result["failure_state"], "ok")
        self.assertEqual(result["parsed_text"], "কখগ")
        request = urlopen.requests[0]
        headers = _header_items(request)
        self.assertEqual(headers.get("x-goog-api-key"), FAKE_GOOGLE_KEY)
        self.assertNotIn(FAKE_GOOGLE_KEY, request.full_url)
        self.assertNotIn("key=", request.full_url.lower())

    def test_error_field_is_invocation_error(self):
        body = json.dumps(
            {"responses": [{"error": {"message": "bad"}}]}
        ).encode("utf-8")
        urlopen = RecordingUrlopen([FakeResponse(body)])
        result = self._client(
            urlopen, {"GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY})(
                image_bytes=b"img", page_id=HEX_A, adapter_id="GV",
                config=dict(GV_CONFIG))
        self.assertEqual(result["failure_state"], "invocation_error")

    def test_empty_text_is_empty(self):
        body = json.dumps({"responses": [{}]}).encode("utf-8")
        urlopen = RecordingUrlopen([FakeResponse(body)])
        result = self._client(
            urlopen, {"GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY})(
                image_bytes=b"img", page_id=HEX_A, adapter_id="GV",
                config=dict(GV_CONFIG))
        self.assertEqual(result["failure_state"], "empty")

    def test_missing_key_unsupported(self):
        urlopen = RecordingUrlopen([FakeResponse(b"{}")])
        result = self._client(urlopen, {})(
            image_bytes=b"img", page_id=HEX_A, adapter_id="GV",
            config=dict(GV_CONFIG))
        self.assertEqual(result["failure_state"], "unsupported")
        self.assertIn("GOOGLE_VISION_API_KEY", result["raw_output"])
        self.assertEqual(urlopen.requests, [])

    def test_rights_gate_blocks_network(self):
        urlopen = RecordingUrlopen([FakeResponse(b"{}")])
        client = vendor_transports.make_google_vision_transport(
            rights_gate=lambda page_id: False,
            env={"GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY},
            urlopen=urlopen,
        )
        result = client(image_bytes=b"img", page_id=HEX_A, adapter_id="GV",
                        config=dict(GV_CONFIG))
        self.assertEqual(result["failure_state"], "unsupported")
        self.assertEqual(
            result["raw_output"], "rights review not recorded for page")
        self.assertEqual(urlopen.requests, [])

    def test_key_never_leaks_into_errors(self):
        def leaking(request, timeout=None):
            raise RuntimeError(
                f"auth broke {FAKE_GOOGLE_KEY} suddenly")

        client = self._client(
            leaking, {"GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY})
        result = client(image_bytes=b"img", page_id=HEX_A, adapter_id="GV",
                        config=dict(GV_CONFIG))
        self.assertEqual(result["failure_state"], "invocation_error")
        self.assertNotIn(FAKE_GOOGLE_KEY, result["raw_output"])


class AzureTransport(unittest.TestCase):
    ENV = {"AZURE_VISION_KEY": FAKE_AZURE_KEY,
           "AZURE_VISION_ENDPOINT": FAKE_AZURE_ENDPOINT}

    def _client(self, urlopen, env=None, **kwargs):
        return vendor_transports.make_azure_read_transport(
            rights_gate=lambda page_id: True,
            env=dict(env if env is not None else self.ENV),
            urlopen=urlopen,
            **kwargs,
        )

    def _ok_body(self):
        return json.dumps({
            "modelVersion": "2023-10-01",
            "readResult": {"blocks": [
                {"lines": [{"text": "line1"}, {"text": "line2"}]},
                {"lines": [{"text": "line3"}]},
            ]},
        }).encode("utf-8")

    def test_success_joins_lines(self):
        urlopen = RecordingUrlopen([FakeResponse(self._ok_body())])
        result = self._client(urlopen)(
            image_bytes=b"img", page_id=HEX_A, adapter_id="AZ",
            config=dict(AZ_CONFIG))
        self.assertEqual(result["failure_state"], "ok")
        self.assertEqual(result["parsed_text"], "line1\nline2\nline3")
        self.assertEqual(result["resource"]["model_version"], "2023-10-01")
        request = urlopen.requests[0]
        headers = _header_items(request)
        self.assertEqual(
            headers.get("ocp-apim-subscription-key"), FAKE_AZURE_KEY)
        self.assertIn("api-version=2024-02-01", request.full_url)
        self.assertIn("features=read", request.full_url)

    def test_retry_then_success_uses_sleeper(self):
        sleeps = []
        urlopen = RecordingUrlopen([
            _http_error(429, b'{"error": "busy"}',
                        {"Retry-After": "2"}),
            FakeResponse(self._ok_body()),
        ])
        result = self._client(urlopen, sleeper=sleeps.append)(
            image_bytes=b"img", page_id=HEX_A, adapter_id="AZ",
            config=dict(AZ_CONFIG))
        self.assertEqual(result["failure_state"], "ok")
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(result["resource"]["attempts"], 2)

    def test_exhausted_retries_is_invocation_error(self):
        sleeps = []
        urlopen = RecordingUrlopen([
            _http_error(429, b"busy", {"Retry-After": "1"}),
        ])
        result = self._client(urlopen, sleeper=sleeps.append,
                              max_retries=2)(
            image_bytes=b"img", page_id=HEX_A, adapter_id="AZ",
            config=dict(AZ_CONFIG))
        self.assertEqual(result["failure_state"], "invocation_error")
        self.assertEqual(len(sleeps), 2)

    def test_missing_key_unsupported(self):
        urlopen = RecordingUrlopen([FakeResponse(b"{}")])
        result = self._client(
            urlopen, {"AZURE_VISION_ENDPOINT": FAKE_AZURE_ENDPOINT})(
                image_bytes=b"img", page_id=HEX_A, adapter_id="AZ",
                config=dict(AZ_CONFIG))
        self.assertEqual(result["failure_state"], "unsupported")
        self.assertIn("AZURE_VISION_KEY", result["raw_output"])
        self.assertEqual(urlopen.requests, [])

    def test_rights_gate_blocks_network(self):
        urlopen = RecordingUrlopen([FakeResponse(b"{}")])
        client = vendor_transports.make_azure_read_transport(
            rights_gate=lambda page_id: False,
            env=dict(self.ENV),
            urlopen=urlopen,
        )
        result = client(image_bytes=b"img", page_id=HEX_A, adapter_id="AZ",
                        config=dict(AZ_CONFIG))
        self.assertEqual(result["failure_state"], "unsupported")
        self.assertEqual(
            result["raw_output"], "rights review not recorded for page")
        self.assertEqual(urlopen.requests, [])


class VendorEndToEnd(unittest.TestCase):
    def test_adapter_over_transport_zero_network(self):
        body = json.dumps({
            "modelVersion": "2023-10-01",
            "readResult": {"blocks": [
                {"lines": [{"text": "কখগ"}]}]},
        }).encode("utf-8")
        urlopen = RecordingUrlopen([FakeResponse(body)])
        transport = vendor_transports.make_azure_read_transport(
            rights_gate=lambda page_id: True,
            env={"AZURE_VISION_KEY": FAKE_AZURE_KEY,
                 "AZURE_VISION_ENDPOINT": FAKE_AZURE_ENDPOINT},
            urlopen=urlopen,
        )
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "page.png"
            image.write_bytes(b"fake-png-bytes")
            adapter = adapters.make_adapter(_az_spec())
            prediction = adapter.predict(
                page_id=HEX_A, image_ref=str(image), ocr_text="",
                allow_execution=True, client=transport)
        self.assertEqual(prediction.failure_state, "ok")
        self.assertEqual(prediction.parsed_text, "কখগ")
        self.assertIsInstance(prediction, schema.Prediction)
        clone = schema.Prediction.from_dict(prediction.to_dict())
        self.assertEqual(clone.to_dict(), prediction.to_dict())

    def test_redaction_across_prediction_fields(self):
        def leaking(request, timeout=None):
            raise RuntimeError(
                f"denied {FAKE_AZURE_KEY} via {FAKE_AZURE_ENDPOINT}")

        transport = vendor_transports.make_azure_read_transport(
            rights_gate=lambda page_id: True,
            env={"AZURE_VISION_KEY": FAKE_AZURE_KEY,
                 "AZURE_VISION_ENDPOINT": FAKE_AZURE_ENDPOINT},
            urlopen=leaking,
        )
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "page.png"
            image.write_bytes(b"fake-png-bytes")
            with mock.patch.dict(os.environ, {
                "AZURE_VISION_KEY": FAKE_AZURE_KEY,
                "AZURE_VISION_ENDPOINT": FAKE_AZURE_ENDPOINT,
            }):
                adapter = adapters.make_adapter(_az_spec())
                prediction = adapter.predict(
                    page_id=HEX_A, image_ref=str(image), ocr_text="",
                    allow_execution=True, client=transport)
        self.assertEqual(prediction.failure_state, "invocation_error")
        for field in (prediction.raw_output, prediction.parsed_text,
                      json.dumps(prediction.resource, sort_keys=True)):
            self.assertNotIn(FAKE_AZURE_KEY, field)
            self.assertNotIn("fake-vendor-host-xyz.example", field)


def _b0(adapter_id="T1"):
    return adapters.UnchangedTesseractAdapter(
        adapters.AdapterSpec(
            adapter_id=adapter_id, kind="recognizer",
            model_id="tesseract-production", prompt_id="none",
            prompt_hash="none", crop_policy="none", config={}))


class FakeClock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


class QuotaBehavior(unittest.TestCase):
    def _pages(self):
        return [
            {"page_id": HEX_A, "image_ref": "", "ocr_text": "ক"},
            {"page_id": HEX_B, "image_ref": "", "ocr_text": "খ"},
            {"page_id": HEX_C, "image_ref": "", "ocr_text": "গ"},
        ]

    def test_monthly_cap_stops_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = runners.QuotaLedger(Path(tmp) / "quota.jsonl")
            clock = FakeClock(1780000000.0)
            plan = runners.RunPlan(
                study_id="quota", adapter_ids=("T1",),
                sample_manifest="m",
                budget={"max_model_calls": 100, "max_wall_seconds": 600,
                        "per_adapter": {"T1": {"max_calls_per_month": 1,
                                               "min_interval_seconds": 0}}},
            )
            result = runners.run_baselines(
                plan, pages=self._pages(), adapters={"T1": _b0()},
                allow_execution=True, clients={"T1": object()},
                cache=runners.PredictionCache(Path(tmp) / "cache.jsonl"),
                ledger=ledger, clock=clock, sleeper=lambda s: None)
            self.assertEqual(len(result["predictions"]), 1)
            self.assertEqual(result["quota_exhausted"]["T1"], 1)
            month = runners.QuotaLedger.month_of(1780000000.0)
            self.assertEqual(ledger.count("T1", month), 1)

    def test_min_interval_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []
            clock = FakeClock(1780000000.0)
            plan = runners.RunPlan(
                study_id="quota", adapter_ids=("T1",),
                sample_manifest="m",
                budget={"max_model_calls": 100, "max_wall_seconds": 600,
                        "per_adapter": {"T1": {"max_calls_per_month": 100,
                                               "min_interval_seconds": 5.0}}},
            )
            result = runners.run_baselines(
                plan, pages=self._pages()[:2], adapters={"T1": _b0()},
                allow_execution=True, clients={"T1": object()},
                ledger=runners.QuotaLedger(Path(tmp) / "quota.jsonl"),
                clock=clock, sleeper=sleeps.append)
            self.assertEqual(len(result["predictions"]), 2)
            self.assertEqual(sleeps, [5.0])
            self.assertEqual(result["quota_exhausted"], {})

    def test_absent_limits_unchanged(self):
        sleeps = []
        plan = runners.RunPlan(
            study_id="quota", adapter_ids=("T1",),
            sample_manifest="m",
            budget={"max_model_calls": 100, "max_wall_seconds": 600},
        )
        result = runners.run_baselines(
            plan, pages=self._pages()[:2], adapters={"T1": _b0()},
            clock=FakeClock(1780000000.0), sleeper=sleeps.append)
        self.assertEqual(len(result["predictions"]), 2)
        self.assertEqual(sleeps, [])
        self.assertEqual(result["quota_exhausted"], {})

    def test_bad_per_adapter_rejected(self):
        with self.assertRaises(schema.ContractError):
            runners.RunPlan(
                study_id="quota", adapter_ids=("T1",),
                sample_manifest="m",
                budget={"max_model_calls": 1, "max_wall_seconds": 1,
                        "per_adapter": {"ZZ": {"max_calls_per_month": 1,
                                               "min_interval_seconds": 0}}},
            )


class StudyBaselinesConfig(unittest.TestCase):
    def test_loads_via_specs_loader(self):
        specs = adapters.load_adapter_specs(
            "research/configs/study-baselines.json")
        self.assertEqual([spec.adapter_id for spec in specs], ["GV", "AZ"])
        by_id = {spec.adapter_id: spec for spec in specs}
        self.assertEqual(
            by_id["GV"].model_id, "google-vision-document-text-detection")
        self.assertEqual(by_id["AZ"].model_id, "azure-ai-vision-read")
        for spec in specs:
            self.assertEqual(spec.crop_policy, "page_image")
            for key in ("endpoint_env", "api_version", "model_version",
                        "drift_epoch"):
                self.assertIn(key, spec.config)
            adapter = adapters.make_adapter(spec)
            prediction = adapter.predict(
                page_id=HEX_A, image_ref="img", ocr_text="কখগ",
                allow_execution=False)
            self.assertEqual(prediction.failure_state, "unsupported")


class DryExecuteCacheLedger(unittest.TestCase):
    """Gated/refused rows must not poison the cache or ledger (P1 repair)."""

    def _setup(self, tmp):
        image = Path(tmp) / "page.png"
        image.write_bytes(b"fake-png-bytes")
        adapter = adapters.make_adapter(_gv_spec())
        plan = runners.RunPlan(
            study_id="vendor-gating", adapter_ids=("GV",),
            sample_manifest="m",
            budget={"max_model_calls": 10, "max_wall_seconds": 600},
        )
        pages = [{"page_id": HEX_A, "image_ref": str(image),
                  "ocr_text": ""}]
        return adapter, plan, pages

    @staticmethod
    def _cache_rows(path):
        path = Path(path)
        if not path.exists():
            return []
        return [line for line in
                path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_dry_then_execute_same_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, plan, pages = self._setup(tmp)
            cache_path = Path(tmp) / "cache.jsonl"
            dry = runners.run_baselines(
                plan, pages=pages, adapters={"GV": adapter},
                cache=runners.PredictionCache(cache_path))
            self.assertEqual(
                dry["predictions"][0]["failure_state"], "unsupported")
            self.assertEqual(self._cache_rows(cache_path), [])
            fake = FakeClient()
            live = runners.run_baselines(
                plan, pages=pages, adapters={"GV": adapter},
                allow_execution=True, clients={"GV": fake},
                cache=runners.PredictionCache(cache_path))
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(
                live["predictions"][0]["failure_state"], "ok")
            self.assertEqual(len(self._cache_rows(cache_path)), 1)

    def test_gated_run_leaves_ledger_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, plan, pages = self._setup(tmp)
            ledger_path = Path(tmp) / "quota.jsonl"
            ledger = runners.QuotaLedger(ledger_path)
            runners.run_baselines(
                plan, pages=pages, adapters={"GV": adapter},
                ledger=ledger)
            self.assertEqual(ledger._rows, [])
            self.assertFalse(ledger_path.exists())

    def test_rights_refusal_reports_zero_attempts(self):
        google = vendor_transports.make_google_vision_transport(
            rights_gate=lambda page_id: False,
            env={"GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY},
            urlopen=RecordingUrlopen([FakeResponse(b"{}")]),
        )
        azure = vendor_transports.make_azure_read_transport(
            rights_gate=lambda page_id: False,
            env={"AZURE_VISION_KEY": FAKE_AZURE_KEY,
                 "AZURE_VISION_ENDPOINT": FAKE_AZURE_ENDPOINT},
            urlopen=RecordingUrlopen([FakeResponse(b"{}")]),
        )
        for client, config, adapter_id in (
            (google, GV_CONFIG, "GV"), (azure, AZ_CONFIG, "AZ"),
        ):
            result = client(image_bytes=b"img", page_id=HEX_A,
                            adapter_id=adapter_id, config=dict(config))
            self.assertEqual(result["failure_state"], "unsupported")
            self.assertEqual(result["resource"].get("attempts"), 0)

    def test_refusal_not_cached_not_ledgered(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, plan, pages = self._setup(tmp)
            transport = vendor_transports.make_google_vision_transport(
                rights_gate=lambda page_id: False,
                env={"GOOGLE_VISION_API_KEY": FAKE_GOOGLE_KEY},
                urlopen=RecordingUrlopen([FakeResponse(b"{}")]),
            )
            cache_path = Path(tmp) / "cache.jsonl"
            ledger = runners.QuotaLedger(Path(tmp) / "quota.jsonl")
            result = runners.run_baselines(
                plan, pages=pages, adapters={"GV": adapter},
                allow_execution=True, clients={"GV": transport},
                cache=runners.PredictionCache(cache_path), ledger=ledger)
            record = result["predictions"][0]
            self.assertEqual(record["failure_state"], "unsupported")
            self.assertEqual(record["resource"].get("attempts"), 0)
            self.assertEqual(self._cache_rows(cache_path), [])
            self.assertEqual(ledger._rows, [])

    def test_executed_ok_cached_rerun_is_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, plan, pages = self._setup(tmp)
            cache_path = Path(tmp) / "cache.jsonl"
            first_fake = FakeClient()
            first = runners.run_baselines(
                plan, pages=pages, adapters={"GV": adapter},
                allow_execution=True, clients={"GV": first_fake},
                cache=runners.PredictionCache(cache_path))
            self.assertEqual(len(first_fake.calls), 1)
            self.assertEqual(len(self._cache_rows(cache_path)), 1)
            second_fake = FakeClient()
            second = runners.run_baselines(
                plan, pages=pages, adapters={"GV": adapter},
                allow_execution=True, clients={"GV": second_fake},
                cache=runners.PredictionCache(cache_path))
            self.assertEqual(second_fake.calls, [])
            self.assertEqual(second["predictions"], first["predictions"])
            self.assertEqual(len(self._cache_rows(cache_path)), 1)


if __name__ == "__main__":
    unittest.main()
