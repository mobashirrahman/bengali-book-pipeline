"""Tests for pdf_craft_tool.research.report (S6)."""

import json
import unittest

from pdf_craft_tool.research import report, schema


def _arm(arm_id, status="ok", families=None, coverage=0.5,
         accepted=10, beneficial=6, neutral=3, harmful=1,
         precision=0.6, notes=""):
    return report.ArmResult(
        arm_id=arm_id,
        status=status,
        per_family_cer=dict(families) if families else {},
        coverage=coverage,
        accepted=accepted,
        beneficial=beneficial,
        neutral=neutral,
        harmful=harmful,
        exact_correction_precision=precision,
        notes=notes,
    )


FAMS_A = {"f1": 0.10, "f2": 0.20, "f3": 0.15}
FAMS_B = {"f1": 0.08, "f2": 0.16, "f3": 0.15}


def _consistent_report():
    arms = [
        _arm("B0", families=FAMS_A),
        _arm("B5", families=FAMS_B),
        _arm("B1", status="failed", families={}, coverage=None,
             accepted=0, beneficial=0, neutral=0, harmful=0,
             precision=None, notes="crashed"),
    ]
    baseline = report.baseline_table(arms)
    by_id = {a.arm_id: a for a in arms}
    contrast = report.primary_contrast(by_id, a="B0", b="B5", iterations=200)
    return {
        "header": {"study_id": "t", "metric_version": "s3-1",
                   "config_hash": "abc", "expected_records": 20},
        "baseline": baseline,
        "primary_contrast": contrast,
        "harm_coverage": report.harm_coverage_points(arms),
        "failures": report.failure_denominator_table(arms, total_pages=20),
    }, arms


class BaselineTable(unittest.TestCase):
    def test_all_arms_appear_even_failed(self):
        table, _ = _consistent_report()
        ids = [row["arm_id"] for row in table["baseline"]["rows"]]
        self.assertIn("B1", ids)
        failed = [r for r in table["baseline"]["rows"] if r["arm_id"] == "B1"]
        self.assertEqual(failed[0]["status"], "failed")

    def test_zero_coverage_precision_undefined(self):
        arm = _arm("B9", families=FAMS_A, coverage=0.0, accepted=0,
                   beneficial=0, neutral=0, harmful=0, precision=None)
        table = report.baseline_table([arm])
        self.assertIsNone(table["rows"][0]["exact_correction_precision"])
        rendered = report.render_markdown({
            "header": {}, "baseline": table,
        })
        self.assertIn("undefined", rendered)
        self.assertNotIn("100%", rendered)
        self.assertNotIn("| 1.0 |", rendered)

    def test_table_totals_reconcile(self):
        rep, _ = _consistent_report()
        report.reconcile(rep, expected_records=20)
        doctored = json.loads(json.dumps(rep))
        doctored["baseline"]["totals"]["accepted"] += 1
        with self.assertRaises(schema.ContractError):
            report.reconcile(doctored, expected_records=20)


class Reproducibility(unittest.TestCase):
    def test_report_reproducible(self):
        rep, _ = _consistent_report()
        first = report.render_markdown(rep)
        second = report.render_markdown(rep)
        self.assertEqual(first, second)
        self.assertIsInstance(first, str)

    def test_rebuild_from_records_reproducible(self):
        records_path = "/tmp/s6_records.jsonl"
        cfg_path = "/tmp/s6_config.json"
        cfg = {
            "study_id": "s6-t",
            "arms": ["B0", "B5"],
            "primary_contrast": {"a": "B0", "b": "B5"},
            "expected_records": 4,
            "total_pages": 4,
        }
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, sort_keys=True)
        rows = [
            {"arm_id": "B0", "family": "f1", "reference": "কখগ",
             "hypothesis": "কখগ", "accepted": True, "edit_class": "beneficial",
             "exact": True, "status": "ok"},
            {"arm_id": "B0", "family": "f2", "reference": "কখগ",
             "hypothesis": "কখঘ", "accepted": False, "status": "ok"},
            {"arm_id": "B5", "family": "f1", "reference": "কখগ",
             "hypothesis": "কখগ", "accepted": True, "edit_class": "neutral",
             "exact": True, "status": "ok"},
            {"arm_id": "B5", "family": "f2", "reference": "কখগ",
             "hypothesis": "কখগ", "accepted": False, "status": "ok"},
        ]
        with open(records_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        first = report.rebuild_from_records(records_path, config=cfg_path)
        second = report.rebuild_from_records(records_path, config=cfg_path)
        self.assertEqual(first["baseline"], second["baseline"])
        self.assertIn("metric_version", first["header"])
        self.assertIn("config_hash", first["header"])


class PrimaryContrast(unittest.TestCase):
    def test_primary_contrast_pairs_families(self):
        by_id = {"B0": _arm("B0", families=FAMS_A),
                 "B5": _arm("B5", families=FAMS_B)}
        out = report.primary_contrast(by_id, a="B0", b="B5", iterations=200)
        self.assertEqual(out["n_families"], 3)
        self.assertIn("point_estimate", out)
        self.assertIn("B0", out["secondary_endpoints"])

    def test_primary_contrast_rejects_mismatched_families(self):
        by_id = {"B0": _arm("B0", families={"f1": 0.1}),
                 "B5": _arm("B5", families={"f2": 0.1})}
        with self.assertRaises(schema.ContractError):
            report.primary_contrast(by_id, a="B0", b="B5", iterations=50)

    def test_harm_coverage_points(self):
        _, arms = _consistent_report()
        points = report.harm_coverage_points(arms)
        self.assertEqual(len(points), 3)
        by_arm = {p["arm_id"]: p for p in points}
        self.assertAlmostEqual(by_arm["B0"]["harmful_rate"], 0.1)
        self.assertEqual(
            set(points[0]), {"arm_id", "coverage", "harmful_rate"})


if __name__ == "__main__":
    unittest.main()
