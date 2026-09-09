"""Tests for python -m pdf_craft_tool.research (S7 CLI)."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research import __main__ as cli
from pdf_craft_tool.research import report, schema, splits

HEX_A = hashlib.sha256(b"s7-doc-a").hexdigest()
HEX_B = hashlib.sha256(b"s7-doc-b").hexdigest()


def _run_main(argv):
    """Run main(), normalising SystemExit to its exit code."""
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    return code


def _make_study(root: Path):
    """Write a tiny validated fixture study; return manifest path."""
    families = [
        splits.Family(family_id="family-a", members=("doc-a",),
                      content_hashes=(HEX_A,),
                      processed_members=("doc-a",),
                      needs_confirmation=()),
        splits.Family(family_id="family-b", members=("doc-b",),
                      content_hashes=(HEX_B,),
                      processed_members=("doc-b",),
                      needs_confirmation=()),
    ]
    result = splits.probability_sample(
        families=families,
        split_of={"family-a": "pilot", "family-b": "pilot"},
        pages_available={"doc-a": [1, 2], "doc-b": [1]},
        pages_per_family=1,
        seed=7,
        stratum_of={"family-a": "s1", "family-b": "s1"},
    )
    manifest_path = root / "sample_manifest.json"
    splits.write_sample_manifest(result, manifest_path)
    page_ids = [record["page_id"] for record in result.pages]
    predictions = []
    for page_id in page_ids:
        predictions.append(schema.Prediction(
            page_id=page_id, system_id="B0", config_hash="cfg",
            model_id="tesseract-production", prompt_hash="none",
            crop_hash="crop", raw_output="কখগ", parsed_text="কখগ",
            failure_state="ok", timing_ms=1, resource={}).to_dict())
    (root / "predictions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in predictions), encoding="utf-8")
    (root / "gold.json").write_text(
        json.dumps({page_id: "কখগ" for page_id in page_ids},
                   ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return manifest_path, len(page_ids)


class CliEndToEnd(unittest.TestCase):
    def test_offline_toy_study_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            root.mkdir()
            _, n_pages = _make_study(root)
            self.assertEqual(
                0, _run_main(["validate", "--study-root", str(root)]))
            self.assertEqual(
                0, _run_main(["evaluate", "--study-root", str(root)]))
            self.assertEqual(
                0, _run_main(["report", "--study-root", str(root)]))
            table = root / "report.md"
            self.assertTrue(table.is_file())
            text = table.read_text(encoding="utf-8")
            self.assertIn("B0", text)
            sidecar = root / "report.json"
            self.assertTrue(sidecar.is_file())
            rebuilt = json.loads(sidecar.read_text(encoding="utf-8"))
            report.reconcile(rebuilt, expected_records=n_pages)
            self.assertEqual(rebuilt["baseline"]["totals"]["accepted"],
                             n_pages)


class CliFailures(unittest.TestCase):
    def test_unknown_command_fails(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = _run_main(["frobnicate"])
        self.assertNotEqual(code, 0)
        self.assertIn("validate", stderr.getvalue())

    def test_invalid_config_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not valid json", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = _run_main(["inventory", "--config", str(bad),
                                  "--out", str(Path(tmp) / "out.json")])
            self.assertNotEqual(code, 0)
            self.assertIn("config", stderr.getvalue().lower())

    def test_fit_gate_refuses_test_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rows = [
                {"candidate_id": "c1", "family_id": "f1", "split": "train",
                 "features": {"a": 1.0, "b": 0.0}, "outcome": "beneficial"},
                {"candidate_id": "c2", "family_id": "f1",
                 "split": "calibration",
                 "features": {"a": 0.0, "b": 1.0}, "outcome": "harmful"},
                {"candidate_id": "c3", "family_id": "f2", "split": "test",
                 "features": {"a": 0.5, "b": 0.5}, "outcome": "beneficial"},
            ]
            labelled = tmp_path / "labelled.jsonl"
            labelled.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n"
                        for row in rows), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = _run_main(["fit-gate", "--study-root", str(tmp_path),
                                  "--input", str(labelled),
                                  "--out", str(tmp_path / "policy.json")])
            self.assertNotEqual(code, 0)
            self.assertIn("leak", stderr.getvalue().lower())

    def test_run_defaults_to_no_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            root.mkdir()
            manifest_path, _ = _make_study(root)
            baselines = {
                "adapters": [{
                    "adapter_id": "B3", "kind": "corrector",
                    "model_id": "text-only-corrector",
                    "prompt_id": "correct-textonly-v0",
                    "prompt_hash": "pending", "crop_policy": "none",
                    "config": {},
                }]
            }
            config_path = root / "baselines.json"
            config_path.write_text(json.dumps(baselines, sort_keys=True),
                                   encoding="utf-8")
            out = root / "dry_predictions.jsonl"
            code = _run_main(["run", "--study-root", str(root),
                              "--manifest", str(manifest_path),
                              "--config", str(config_path),
                              "--out", str(out)])
            self.assertEqual(0, code)
            rows = [json.loads(line) for line in
                    out.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual("unsupported", row["failure_state"])
                self.assertEqual("", row["parsed_text"])


if __name__ == "__main__":
    unittest.main()
