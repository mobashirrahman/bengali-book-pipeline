"""Offline checks for research baseline adapters and runners (S4)."""

import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research import adapters, runners, schema

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


def _spec(adapter_id, kind="recognizer", model_id="model",
          prompt_hash="ph", crop_policy="none", config=None):
    return adapters.AdapterSpec(
        adapter_id=adapter_id,
        kind=kind,
        model_id=model_id,
        prompt_id="prompt",
        prompt_hash=prompt_hash,
        crop_policy=crop_policy,
        config=dict(config or {}),
    )


def _b0():
    return adapters.UnchangedTesseractAdapter(
        _spec("B0", model_id="tesseract-production", prompt_hash="none"))


class CountingClient:
    """Fake transport recording invocations; never touches models/network."""

    def __init__(self, reply="corrected text"):
        self.calls = 0
        self.seen = []
        self.reply = reply

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.seen.append((args, kwargs))
        return self.reply


class SpyClient:
    """Records the crop identity the adapter claims it attached."""

    def __init__(self):
        self.crops = []

    def __call__(self, *, text, crop, page_id, adapter_id):
        self.crops.append(dict(crop))
        return {
            "raw_output": f"corrected:{text}",
            "parsed_text": f"corrected:{text}",
            "failure_state": "ok",
        }


def _plan(adapter_ids, max_calls=1000, max_wall=600):
    return runners.RunPlan(
        study_id="s4-test",
        adapter_ids=tuple(adapter_ids),
        sample_manifest="manifest.json",
        budget={"max_model_calls": max_calls, "max_wall_seconds": max_wall},
    )


def _page(page_id, ocr_text="কখগ", image_ref="img-1"):
    return {"page_id": page_id, "image_ref": image_ref, "ocr_text": ocr_text}


class AdapterBehavior(unittest.TestCase):
    def test_b0_returns_ocr_verbatim(self):
        prediction = _b0().predict(
            page_id=HEX_A, ocr_text="কখগ", allow_execution=False)
        self.assertIsInstance(prediction, schema.Prediction)
        self.assertEqual(prediction.parsed_text, "কখগ")
        self.assertEqual(prediction.failure_state, "ok")

    def test_unavailable_adapter_is_explicit(self):
        adapter = adapters.UnavailableRecognizerAdapter(
            _spec("B1", model_id="bbocr-unpinned", crop_policy="page_image",
                  config={"status": "unavailable_pending_reproducible_build"}))
        prediction = adapter.predict(
            page_id=HEX_A, ocr_text="কখগ", allow_execution=False)
        self.assertEqual(prediction.failure_state, "unsupported")
        self.assertTrue(prediction.raw_output)
        self.assertEqual(prediction.parsed_text, "")
        self.assertNotIn("কখগ", prediction.parsed_text)

    def test_gold_field_rejected_before_invocation(self):
        client = CountingClient()
        adapter = adapters.TextOnlyCorrectionAdapter(
            _spec("B3", kind="corrector", model_id="text-only-corrector"))
        with self.assertRaises(schema.ContractError):
            adapter.predict(
                page_id=HEX_A, ocr_text="কখগ", allow_execution=True,
                client=client, verified_text="gold must not enter")
        self.assertEqual(client.calls, 0)

        plan = _plan(["B0"])
        bad_pages = [dict(_page(HEX_A), gold_text="gold must not enter")]
        with self.assertRaises(schema.ContractError):
            runners.run_baselines(
                plan, pages=bad_pages, adapters={"B0": _b0()},
                allow_execution=False, clients={"B0": client})
        self.assertEqual(client.calls, 0)

    def test_spy_client_receives_expected_crop(self):
        adapter = adapters.TextOnlyCorrectionAdapter(
            _spec("B4", kind="corrector", model_id="conservative-proofreader",
                  prompt_hash="repo", crop_policy="line_crop"))
        spy = SpyClient()
        prediction = adapter.predict(
            page_id=HEX_A, image_ref="page-scan-1", ocr_text="কখগ",
            allow_execution=True, client=spy)
        self.assertEqual(len(spy.crops), 1)
        self.assertEqual(spy.crops[0]["policy"], "line_crop")
        self.assertEqual(spy.crops[0]["crop_hash"], prediction.crop_hash)

    def test_all_adapters_same_schema(self):
        specs = adapters.load_adapter_specs()
        self.assertEqual(len(specs), 6)
        for spec in specs:
            adapter = adapters.make_adapter(spec)
            result = adapter.predict(
                page_id=HEX_A, image_ref="img", ocr_text="কখগ",
                allow_execution=False)
            self.assertIsNotNone(result)
            self.assertIsInstance(result, schema.Prediction)
            # Round-trips through the frozen record contract.
            clone = schema.Prediction.from_dict(result.to_dict())
            self.assertEqual(clone.to_dict(), result.to_dict())


class CacheAndRunner(unittest.TestCase):
    def test_cache_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = runners.PredictionCache(Path(tmp) / "cache.jsonl")
            adapter = _b0()
            page = _page(HEX_A)
            identity = dict(adapter.identity())
            identity["crop_hash"] = adapter.crop_hash_for(page["image_ref"])
            prediction = adapter.predict(
                page_id=page["page_id"], image_ref=page["image_ref"],
                ocr_text=page["ocr_text"])
            cache.put(prediction, identity)
            self.assertIsNotNone(cache.get(page["page_id"], identity))
            altered = dict(identity, prompt_hash="changed")
            self.assertIsNone(cache.get(page["page_id"], altered))
            other_crop = dict(
                identity,
                crop_hash=adapters.UnavailableRecognizerAdapter(
                    _spec("B1", crop_policy="page_image")).crop_hash_for(
                        page["image_ref"]))
            self.assertIsNone(cache.get(page["page_id"], other_crop))

    def test_resume_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = runners.PredictionCache(Path(tmp) / "cache.jsonl")
            plan = _plan(["B0"])
            pages = [_page(HEX_A, "কখগ"), _page(HEX_B, "গঘঙ")]
            first = runners.run_baselines(
                plan, pages=pages, adapters={"B0": _b0()}, cache=cache)
            lines_after_first = len(
                (Path(tmp) / "cache.jsonl").read_text(
                    encoding="utf-8").splitlines())
            second = runners.run_baselines(
                plan, pages=pages, adapters={"B0": _b0()}, cache=cache)
            lines_after_second = len(
                (Path(tmp) / "cache.jsonl").read_text(
                    encoding="utf-8").splitlines())
            self.assertEqual(len(first["predictions"]), 2)
            self.assertEqual(len(second["predictions"]), 2)
            self.assertEqual(first["predictions"], second["predictions"])
            self.assertEqual(lines_after_first, lines_after_second)
            self.assertFalse(first["budget_exhausted"])
            self.assertFalse(second["budget_exhausted"])

    def test_budget_exhaustion_reported(self):
        plan = _plan(["B0"], max_calls=1)
        pages = [_page(HEX_A, "কখগ"), _page(HEX_B, "গঘঙ"), _page(HEX_C, "চছজ")]
        result = runners.run_baselines(
            plan, pages=pages, adapters={"B0": _b0()})
        self.assertTrue(result["budget_exhausted"])
        self.assertLess(len(result["predictions"]), len(pages))
        self.assertEqual(len(result["predictions"]), 1)
        self.assertIn("B0", result["by_adapter"])
        self.assertEqual(
            sum(result["by_adapter"]["B0"].values()),
            len(result["predictions"]))


if __name__ == "__main__":
    unittest.main()
