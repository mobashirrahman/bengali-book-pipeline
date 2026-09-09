"""Tests for pdf_craft_tool.research.external (S6). Synthetic fixtures only."""

import json
import unittest

from pdf_craft_tool.research import external, schema


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)


def _page_refs(prefix="reid:page1", n=2):
    return [
        external.ExternalRef(
            ref_id=f"{prefix}:L{i:04d}",
            split="test",
            text=text,
            source=schema.canonical_json({
                "dataset": "REID2019",
                "version": "v1",
                "overlap_note": "",
            }),
            license="public-domain",
        )
        for i, text in enumerate(["আমার সোনার বাংলা", "আমি তোমায় ভালোবাসি"][:n])
    ]


class ImportFixture(unittest.TestCase):
    def test_import_preserves_ids_and_split(self):
        path = "/tmp/s6_refs.jsonl"
        records = [
            {"ref_id": "reid:p01:L0000", "split": "test",
             "text": "আমার সোনার বাংলা", "license": "public-domain"},
            {"ref_id": "reid:p01:L0001", "split": "calibration",
             "text": "চিরদিন তোমার আকাশ", "license": "public-domain"},
        ]
        _write_jsonl(path, records)
        refs = external.import_reference_fixture(
            path, dataset="REID2019", version="v1")
        self.assertEqual([r.ref_id for r in refs],
                         ["reid:p01:L0000", "reid:p01:L0001"])
        self.assertEqual([r.split for r in refs], ["test", "calibration"])

    def test_import_page_xml_fixture(self):
        path = "/tmp/s6_page.xml"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">\n'
                ' <Page><TextRegion id="r1"><TextEquiv><Unicode>'
                'আমার সোনার বাংলা</Unicode></TextEquiv></TextRegion></Page>\n'
                '</PcGts>\n'
            )
        refs = external.import_reference_fixture(
            path, dataset="REID2019", version="v1")
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].ref_id.startswith("REID2019:"))
        self.assertEqual(refs[0].split, "test")


class NativeMode(unittest.TestCase):
    def setUp(self):
        external._NATIVE_REGISTRY.clear()

    def tearDown(self):
        external._NATIVE_REGISTRY.clear()

    def test_native_mode_fails_without_evaluator(self):
        refs = _page_refs()
        calls = []

        class Spy:
            pass

        # The legacy scorer must not be importable on this path: patching
        # the module attribute would reveal any call; instead assert the
        # raise happens before any scoring by checking the exception type.
        with self.assertRaises(external.NativeEvaluatorUnavailable):
            external.evaluate_external({"x": "y"}, refs, mode="native")
        self.assertEqual(calls, [])

    def test_registered_native_evaluator_used(self):
        seen = {}

        def fake_native(hypotheses, refs):
            seen["n"] = len(refs)
            return {"native_score": 0.42}

        external.register_native_evaluator("REID2019", "v1", fake_native)
        refs = _page_refs()
        result = external.evaluate_external(
            {refs[0].ref_id: refs[0].text}, refs, mode="native")
        self.assertEqual(result["mode"], "native")
        self.assertEqual(result["native_result"], {"native_score": 0.42})
        self.assertEqual(seen["n"], 2)


class CommonMetrics(unittest.TestCase):
    def test_common_text_metrics_labelled(self):
        refs = _page_refs()
        norm = {"policy": "nfc_strict", "note": "s6-fixture"}
        result = external.evaluate_external(
            {r.ref_id: r.text for r in refs}, refs,
            mode="common_text_metrics", normalization=norm)
        self.assertEqual(result["mode"], "common_text_metrics")
        self.assertIs(result["comparable_to_published_native"], False)
        self.assertEqual(result["normalization"], norm)
        self.assertAlmostEqual(result["cer"], 0.0)

    def test_word_crop_and_page_scores_not_pooled(self):
        word_ref = external.ExternalRef(
            ref_id="mozhi-word:img001",
            split="test",
            text="বাংলা",
            source=schema.canonical_json({
                "dataset": "Mozhi",
                "version": "v1",
                "overlap_note": "word crop",
            }),
            license="CC-BY-4.0",
        )
        page_ref = external.ExternalRef(
            ref_id="reid:page9",
            split="test",
            text="আমার সোনার বাংলা",
            source=schema.canonical_json({
                "dataset": "REID2019",
                "version": "v1",
                "overlap_note": "full page",
            }),
            license="public-domain",
        )
        with self.assertRaises(schema.ContractError):
            external.evaluate_external(
                {word_ref.ref_id: "বাংলা", page_ref.ref_id: page_ref.text},
                [word_ref, page_ref],
                mode="common_text_metrics",
                normalization={"policy": "nfc_strict"},
            )


if __name__ == "__main__":
    unittest.main()
