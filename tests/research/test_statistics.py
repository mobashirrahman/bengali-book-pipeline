"""Independently-derived checks for pdf_craft_tool.research.statistics (S3)."""

import random
import unittest

from pdf_craft_tool.research import schema, statistics


class PairedBootstrap(unittest.TestCase):
    def test_paired_bootstrap_deterministic(self):
        a = {"f1": 0.10, "f2": 0.20, "f3": 0.15}
        b = {"f1": 0.08, "f2": 0.16, "f3": 0.15}
        first = statistics.paired_bootstrap(a, b, iterations=500, seed=1)
        second = statistics.paired_bootstrap(a, b, iterations=500, seed=1)
        self.assertEqual(first, second)

    def test_paired_bootstrap_point_estimate(self):
        a = {"f1": 0.10, "f2": 0.20}
        b = {"f1": 0.08, "f2": 0.16}
        result = statistics.paired_bootstrap(a, b, iterations=200, seed=7)
        self.assertEqual(result["n_families"], 2)
        # diffs are -0.02 and -0.04; their mean is -0.03
        self.assertAlmostEqual(result["point_estimate"], -0.03, places=12)
        self.assertEqual(result["resample_unit"], "work_edition_family")

    def test_paired_bootstrap_mismatched_families(self):
        with self.assertRaises(schema.ContractError):
            statistics.paired_bootstrap({"f1": 0.1}, {"f2": 0.1})

    def test_bootstrap_draws_whole_families(self):
        rng = random.Random(0)
        picks = statistics._resample_indices(5, rng)
        self.assertEqual(len(picks), 5)
        self.assertTrue(all(0 <= i < 5 for i in picks))


class HarmReport(unittest.TestCase):
    def test_harm_report_zero_harms(self):
        report = statistics.harm_rate_report({"f1": 0, "f2": 0, "f3": 0}, iterations=200)
        self.assertTrue(report["zero_observed_harms"])
        self.assertFalse(report["safety_claim_supported"])
        self.assertEqual(report["total_harm_events"], 0)
        self.assertEqual(report["families_with_any_harm"], 0)

    def test_harm_report_some_harms(self):
        report = statistics.harm_rate_report({"f1": 2, "f2": 0, "f3": 1}, iterations=200)
        self.assertEqual(report["families_with_any_harm"], 2)
        self.assertAlmostEqual(report["fraction_families_with_harm"], 2 / 3)
        self.assertEqual(report["total_harm_events"], 3)
        self.assertFalse(report["safety_claim_supported"])

    def test_harm_report_rejects_bad_counts(self):
        with self.assertRaises(schema.ContractError):
            statistics.harm_rate_report({"f1": -1})


class Reconciliation(unittest.TestCase):
    def test_macro_cer(self):
        self.assertAlmostEqual(statistics.macro_cer({"f1": 0.1, "f2": 0.3}), 0.2)

    def test_paired_effect_table_reconciles(self):
        rows = [
            {"family": "f1", "a": 0.10, "b": 0.08},
            {"family": "f2", "a": 0.20, "b": 0.22},
            {"family": "f3", "a": 0.30, "b": 0.24},
        ]
        table = statistics.paired_effect_table(rows)
        self.assertAlmostEqual(table["macro_diff"], table["macro_b"] - table["macro_a"])
        self.assertEqual(table["n_families"], 3)

    def test_paired_effect_table_rejects_duplicate_family(self):
        with self.assertRaises(schema.ContractError):
            statistics.paired_effect_table(
                [{"family": "f1", "a": 0.1, "b": 0.2}, {"family": "f1", "a": 0.1, "b": 0.2}]
            )


if __name__ == "__main__":
    unittest.main()
