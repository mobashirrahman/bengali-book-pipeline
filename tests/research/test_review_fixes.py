"""Regression tests for coordinator fixes to reviewer findings on S0-S3.

Findings addressed:
  * annotation.set_status must not un-flag a terminal page
  * disagreement_stats char disagreement must be edit-distance based (NFC), not
    difflib block ratio
  * schema.Manifest.build must screen gold fields for inference-kind manifests
  * splits.probability_sample must skip 'unassigned' / unknown-split families
  * metrics: raw policy preserves what nfc_strict composes (NFD fixture)
"""

import json
import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research import export, metrics, report, schema, splits
from pdf_craft_tool.research.annotation import AnnotationStore
from pdf_craft_tool.research.census import build_census_from_regions

_HEX = "b" * 64
_HEX2 = "c" * 64
_GEO = {"x0": 0, "y0": 0, "x1": 10, "y1": 10}


def _page(store_pages, page_id, source, image):
    census = build_census_from_regions(
        page_id, source, image, "seed",
        [{"geometry": _GEO, "kind": "body", "unreadable": False}],
    ).to_dict()
    store_pages.append({"page_id": page_id, "source_sha256": source,
                        "image_sha256": image, "census": census})


class SetStatusTerminal(unittest.TestCase):
    def test_flagged_is_terminal(self):
        pages = []
        _page(pages, _HEX, _HEX2, "d" * 64)
        with tempfile.TemporaryDirectory() as d:
            store = AnnotationStore(Path(d) / "a.sqlite3", pages=pages)
            store.set_status(_HEX, "flagged")
            with self.assertRaises(ValueError):
                store.set_status(_HEX, "adjudicated")
            self.assertEqual(store.set_status(_HEX, "flagged"), "flagged")
            store.close()


class DisagreementIsEditDistance(unittest.TestCase):
    def test_edit_distance_ratio(self):
        pages = []
        _page(pages, _HEX, _HEX2, "e" * 64)
        with tempfile.TemporaryDirectory() as d:
            store = AnnotationStore(Path(d) / "a.sqlite3", pages=pages)
            store.assign(_HEX, "ann1")
            store.assign(_HEX, "ann2")
            store.submit(_HEX, "ann1", revision=0, lines={0: "abcdefghij"})
            store.submit(_HEX, "ann2", revision=0, lines={0: "abcdefghXY"})
            stats = store.disagreement_stats(_HEX)
            self.assertAlmostEqual(stats["pairwise_char_disagreement"], 0.2)
            self.assertEqual(stats["exact_line_agreement"], 0.0)
            store.close()


class ManifestGoldScreen(unittest.TestCase):
    def test_inference_manifest_rejects_gold(self):
        with self.assertRaises(schema.ContractError):
            schema.Manifest.build(
                "inference", [{"page_id": _HEX, "verified_text": "leak"}]
            )
        ok = schema.Manifest.build("sample", [{"page_id": _HEX, "x": 1}])
        self.assertEqual(ok.kind, "sample")


class ProbabilitySampleSkipsUnassigned(unittest.TestCase):
    def test_unassigned_family_skipped(self):
        fam = splits.Family(
            family_id="f1", members=("d1",), content_hashes=(_HEX,),
            processed_members=("d1",), needs_confirmation=(),
        )
        result = splits.probability_sample(
            families=[fam],
            split_of={"f1": "unassigned"},
            pages_available={"d1": [1, 2, 3, 4, 5, 6]},
            pages_per_family=3,
            seed=1,
            stratum_of={"f1": "s"},
        )
        self.assertEqual(result.pages, ())


class RawVsNfcStrict(unittest.TestCase):
    def test_nfd_reference_differs_by_policy(self):
        reference = "Á"      # A + combining acute (NFD, 2 codepoints)
        hypothesis = "Á"       # precomposed A-acute (NFC, 1 codepoint)
        self.assertEqual(
            metrics.character_error_rate(
                reference, hypothesis, policy="raw")["edits"],
            2,
        )
        self.assertEqual(
            metrics.character_error_rate(
                reference, hypothesis, policy="nfc_strict")["edits"],
            0,
        )
        self.assertEqual(
            metrics.character_error_rate(
                reference, hypothesis, policy="nfc_search")["edits"],
            0,
        )


class ReportPerPageFailures(unittest.TestCase):
    def test_partly_failed_arm_not_reported_as_ok(self):
        arm = report.ArmResult(
            arm_id="B3", status="failed", per_family_cer={"f1": 0.1},
            coverage=0.0, accepted=0, beneficial=0, neutral=0, harmful=0,
            exact_correction_precision=None,
            pages_attempted=60, pages_ok=40, pages_failed=20, pages_unsupported=0,
        )
        table = report.failure_denominator_table([arm], total_pages=60)
        row = table["rows"][0]
        self.assertEqual(row["pages_ok"], 40)
        self.assertEqual(row["pages_failed"], 20)
        self.assertEqual(row["granularity"], "per_page")

    def test_arm_status_fallback_when_no_per_page(self):
        arm = report.ArmResult(
            arm_id="B1", status="unsupported", per_family_cer={},
            coverage=None, accepted=0, beneficial=0, neutral=0, harmful=0,
            exact_correction_precision=None,
        )
        row = report.failure_denominator_table([arm], total_pages=60)["rows"][0]
        self.assertEqual(row["pages_unsupported"], 60)
        self.assertEqual(row["granularity"], "arm_status_fallback")

    def test_rebuild_counts_per_page_failure(self):
        with tempfile.TemporaryDirectory() as d:
            recs = Path(d) / "records.jsonl"
            lines = []
            for i in range(4):
                lines.append(schema.canonical_json({
                    "arm_id": "B3", "family": "fam1",
                    "reference": "abcdef", "hypothesis": "abcdef",
                    "status": "ok" if i < 3 else "truncated",
                    "accepted": False,
                }))
            recs.write_text("\n".join(lines) + "\n")
            cfg = Path(d) / "cfg.json"
            cfg.write_text(schema.canonical_json({
                "study_id": "t", "arms": ["B3"], "total_pages": 4,
                "expected_records": 4,
                "primary_contrast": {"a": "B0", "b": "B5"},
            }))
            rep = report.rebuild_from_records(recs, config=str(cfg))
            row = next(r for r in rep["failures"]["rows"] if r["arm_id"] == "B3")
            self.assertEqual(row["pages_ok"], 3)
            self.assertEqual(row["pages_failed"], 1)
            report.reconcile(rep, expected_records=4)


class ExportSecurity(unittest.TestCase):
    def _study(self, d, files: dict):
        root = Path(d) / "study"
        root.mkdir()
        for name, content in files.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return root

    def test_export_refused_is_a_value_error(self):
        # so the CLI's `except (ContractError, OSError, ValueError)` catches it
        self.assertTrue(issubclass(export.ExportRefused, schema.ContractError))
        self.assertTrue(issubclass(export.ExportRefused, ValueError))

    def test_build_bundle_refuses_nonempty_out_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._study(d, {"notes.txt": "hi"})
            out = Path(d) / "bundle"
            out.mkdir()
            (out / "stale.txt").write_text("old", encoding="utf-8")
            with self.assertRaises(export.ExportRefused):
                export.build_bundle(study_root=root, out_dir=out,
                                    kind="inference",
                                    release_decisions={"notes.txt": "approved"})

    def test_gold_map_named_reference_refused_in_inference(self):
        with tempfile.TemporaryDirectory() as d:
            gold_map = {("a" * 64): "রবীন্দ্রনাথ", ("b" * 64): "শরৎচন্দ্র"}
            root = self._study(d, {
                "reference_pages.json": schema.canonical_json(gold_map),
            })
            out = Path(d) / "bundle"
            with self.assertRaises(export.ExportRefused):
                export.build_bundle(
                    study_root=root, out_dir=out, kind="inference",
                    release_decisions={"reference_pages.json": "approved"})

    def test_decision_dict_without_status_is_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._study(d, {"data.json": '{"x": 1}'})
            out = Path(d) / "bundle"
            result = export.build_bundle(
                study_root=root, out_dir=out, kind="inference",
                release_decisions={"data.json": {"reviewer": "alice"}})
            self.assertEqual(result["items"], [])
            self.assertEqual(len(result["excluded"]), 1)

    def test_manifest_integrity_hash_checked(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._study(d, {"a.txt": "hello"})
            out = Path(d) / "bundle"
            export.build_bundle(study_root=root, out_dir=out, kind="inference",
                                release_decisions={"a.txt": "approved"})
            self.assertEqual(export.verify_bundle(out)["ok"], True)
            mpath = out / export.BUNDLE_MANIFEST_NAME
            m = json.loads(mpath.read_text())
            m["items"].append({"rel_path": "ghost", "sha256": "0" * 64,
                               "role": "asset", "release_basis": "approved"})
            mpath.write_text(json.dumps(m), encoding="utf-8")
            with self.assertRaises(export.ExportRefused):
                export.verify_bundle(out)


if __name__ == "__main__":
    unittest.main()
