"""Tests for pdf_craft_tool.research.export (S7 reproducible export)."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research import export, report, schema, splits

HEX_A = hashlib.sha256(b"s7-export-a").hexdigest()


def _sample_manifest(root: Path) -> str:
    families = [splits.Family(
        family_id="family-a", members=("doc-a",),
        content_hashes=(HEX_A,), processed_members=("doc-a",),
        needs_confirmation=())]
    result = splits.probability_sample(
        families=families, split_of={"family-a": "pilot"},
        pages_available={"doc-a": [1]}, pages_per_family=1, seed=3,
        stratum_of={"family-a": "s1"})
    path = root / "sample_manifest.json"
    splits.write_sample_manifest(result, path)
    return result.pages[0]["page_id"]


def _prediction(page_id: str, **overrides) -> dict:
    base = {
        "page_id": page_id, "system_id": "B0", "config_hash": "cfg",
        "model_id": "tesseract-production", "prompt_hash": "none",
        "crop_hash": "crop", "raw_output": "কখগ", "parsed_text": "কখগ",
        "failure_state": "ok", "timing_ms": 1, "resource": {},
    }
    base.update(overrides)
    return base


def _write_predictions(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False,
                                       sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8")


def _records_rows():
    return [
        {"arm_id": "B0", "family": "f1", "reference": "কখগ",
         "hypothesis": "কখগ", "accepted": True, "edit_class": "neutral",
         "exact": True, "status": "ok"},
        {"arm_id": "B0", "family": "f2", "reference": "কখগখ",
         "hypothesis": "কখগ", "accepted": False, "status": "ok"},
        {"arm_id": "B5", "family": "f1", "reference": "কখগ",
         "hypothesis": "কখগ", "accepted": True, "edit_class": "neutral",
         "exact": True, "status": "ok"},
        {"arm_id": "B5", "family": "f2", "reference": "কখগখ",
         "hypothesis": "কখগখ", "accepted": False, "status": "ok"},
    ]


class InferenceBundle(unittest.TestCase):
    def test_inference_bundle_has_no_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            root.mkdir()
            page_id = _sample_manifest(root)
            preds = root / "predictions.jsonl"
            _write_predictions(preds, [_prediction(page_id)])
            decisions = {"sample_manifest.json": "approved",
                         "predictions.jsonl": "approved"}
            bundle = Path(tmp) / "bundle"
            result = export.build_bundle(
                study_root=root, out_dir=bundle, kind="inference",
                release_decisions=decisions)
            self.assertTrue(result["items"])
            self.assertEqual([], result["excluded"])
            check = export.verify_bundle(bundle)
            self.assertTrue(check["ok"])
            # Injecting a gold field must refuse the inference bundle.
            _write_predictions(
                preds, [_prediction(page_id, gold_text="leaked gold")])
            with self.assertRaises(export.ExportRefused):
                export.build_bundle(
                    study_root=root, out_dir=Path(tmp) / "bundle2",
                    kind="inference", release_decisions=decisions)

    def test_unapproved_content_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            root.mkdir()
            page_id = _sample_manifest(root)
            preds = root / "predictions.jsonl"
            _write_predictions(preds, [_prediction(page_id)])
            (root / "notes.txt").write_text("internal only\n",
                                            encoding="utf-8")
            decisions = {"sample_manifest.json": "approved",
                         "predictions.jsonl": "approved"}
            result = export.build_bundle(
                study_root=root, out_dir=Path(tmp) / "bundle",
                kind="inference", release_decisions=decisions)
            excluded = {item["rel_path"]: item["reason"]
                        for item in result["excluded"]}
            self.assertIn("notes.txt", excluded)
            self.assertTrue(excluded["notes.txt"])
            manifest = json.loads(
                (Path(tmp) / "bundle" / "bundle_manifest.json").read_text(
                    encoding="utf-8"))
            self.assertIn("notes.txt", {item["rel_path"] for item in
                                        manifest["excluded"]})

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            root.mkdir()
            page_id = _sample_manifest(root)
            preds = root / "predictions.jsonl"
            decisions = {"sample_manifest.json": "approved",
                         "predictions.jsonl": "approved"}
            for bad_ref in ("../../etc/passwd", "/etc/passwd"):
                _write_predictions(
                    preds, [_prediction(page_id, asset_path=bad_ref)])
                with self.assertRaises(export.ExportRefused,
                                       msg=f"ref={bad_ref}"):
                    export.build_bundle(
                        study_root=root,
                        out_dir=Path(tmp) / f"bundle-{len(bad_ref)}",
                        kind="inference", release_decisions=decisions)


class BundleIntegrity(unittest.TestCase):
    def test_every_asset_checksum_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            root.mkdir()
            page_id = _sample_manifest(root)
            preds = root / "predictions.jsonl"
            _write_predictions(preds, [_prediction(page_id)])
            decisions = {"sample_manifest.json": "approved",
                         "predictions.jsonl": "approved"}
            bundle = Path(tmp) / "bundle"
            export.build_bundle(
                study_root=root, out_dir=bundle, kind="inference",
                release_decisions=decisions)
            self.assertTrue(export.verify_bundle(bundle)["ok"])
            target = bundle / "predictions.jsonl"
            data = target.read_bytes()
            target.write_bytes(data + b" ")
            with self.assertRaises(export.ExportRefused):
                export.verify_bundle(bundle)

    def test_rebuild_tables_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            root.mkdir()
            records = root / "records.jsonl"
            rows = _records_rows()
            records.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n"
                        for row in rows), encoding="utf-8")
            config = {"study_id": "s7-t", "arms": ["B0", "B5"],
                      "expected_records": len(rows),
                      "total_pages": len(rows)}
            config_path = Path(tmp) / "final.json"
            config_path.write_text(json.dumps(config, sort_keys=True),
                                   encoding="utf-8")
            expected = report.rebuild_from_records(records, config=config_path)
            decisions = {"records.jsonl": {"status": "approved",
                                           "release_basis": "licensed"}}
            bundle = Path(tmp) / "bundle"
            export.build_bundle(
                study_root=root, out_dir=bundle, kind="gold",
                release_decisions=decisions)
            rebuilt = export.rebuild_tables_offline(bundle)
            self.assertEqual(expected["baseline"]["totals"],
                             rebuilt["baseline"]["totals"])
            self.assertEqual(len(rows), rebuilt["n_records"])


if __name__ == "__main__":
    unittest.main()
