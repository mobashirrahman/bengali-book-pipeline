"""Hand-calculated checks for pdf_craft_tool.research.metrics (S3)."""

import unittest

from pdf_craft_tool.research import metrics, schema

_HEX = "a" * 64


def _prediction(parsed_text, failure_state="ok"):
    return schema.Prediction(
        page_id=_HEX,
        system_id="B0",
        config_hash="cfg",
        model_id="tesseract",
        prompt_hash="p",
        crop_hash="c",
        raw_output=parsed_text,
        parsed_text=parsed_text,
        failure_state=failure_state,
        timing_ms=0,
        resource={},
    )


class CharacterMetrics(unittest.TestCase):
    def test_insert_delete_substitute(self):
        self.assertEqual(metrics.character_error_rate("abc", "abx")["operations"]["replace"], 1)
        self.assertAlmostEqual(metrics.character_error_rate("abc", "abx")["cer"], 1 / 3)
        self.assertEqual(metrics.character_error_rate("abc", "ab")["operations"]["delete"], 1)
        self.assertEqual(metrics.character_error_rate("abc", "abcd")["operations"]["insert"], 1)

    def test_raw_vs_nfc_quote(self):
        reference = "can’t"   # curly apostrophe
        hypothesis = "can't"        # straight apostrophe
        self.assertEqual(metrics.character_error_rate(reference, hypothesis, policy="raw")["edits"], 1)
        self.assertEqual(metrics.character_error_rate(reference, hypothesis, policy="nfc_strict")["edits"], 1)
        self.assertEqual(metrics.character_error_rate(reference, hypothesis, policy="nfc_search")["edits"], 0)

    def test_bengali_combining_and_numeral(self):
        result = metrics.character_error_rate("ক্ষ ৪", "কষ 4")
        self.assertEqual(result["reference_chars"], 5)
        self.assertAlmostEqual(result["cer"], 2 / 5)
        self.assertEqual(
            metrics.word_tokens("ক্ষ ৪"),
            ["ক্ষ", "৪"],
        )

    def test_grapheme_metric(self):
        result = metrics.grapheme_error_rate("ক্ষি", "কষি")
        if metrics.GRAPHEME_AVAILABLE:
            self.assertTrue(result["available"])
            self.assertEqual(result["reference_graphemes"], 1)
            self.assertEqual(result["regex_version"], metrics.REGEX_VERSION)
        else:  # pragma: no cover
            self.assertFalse(result["available"])

    def test_empty_and_truncated_prediction(self):
        self.assertAlmostEqual(metrics.character_error_rate("abcdef", "")["cer"], 1.0)
        scored = metrics.score_prediction(_prediction("abc", "truncated"), "abcdef")
        self.assertEqual(scored["failure_state"], "truncated")
        self.assertGreater(scored["character"]["cer"], 0)

    def test_missing_gold_rejected(self):
        with self.assertRaises(schema.ContractError):
            metrics.character_error_rate("", "anything")
        with self.assertRaises(schema.ContractError):
            metrics.score_prediction(_prediction("anything"), "")

    def test_identical_prediction_reproducible(self):
        a = metrics.character_error_rate("hello world", "helo world")
        b = metrics.character_error_rate("hello world", "helo world")
        self.assertEqual(a, b)


class LayoutMetrics(unittest.TestCase):
    def test_missing_line(self):
        report = metrics.omission_report(
            ["line one", "line two", "line three"], ["line one", "line three"]
        )
        self.assertEqual(report["omitted_lines"], 1)
        self.assertEqual(report["omitted_index"], [1])
        self.assertEqual(report["denominator"], 3)

    def test_repeated_line(self):
        report = metrics.omission_report(["a", "b"], ["a", "a", "b"])
        self.assertEqual(report["omitted_lines"], 0)
        self.assertEqual(report["spurious_lines"], 1)

    def test_swapped_region(self):
        self.assertEqual(
            metrics.reading_order_errors([0, 1, 2, 3], [0, 2, 1, 3])["inversions"], 1
        )


class EditAccounting(unittest.TestCase):
    def test_zero_edit_case(self):
        accepted = metrics.edit_accounting(
            [{"reference": "x", "before": "x", "after": "x", "accepted": True}]
        )
        self.assertEqual((accepted["beneficial"], accepted["neutral"], accepted["harmful"]), (0, 1, 0))
        self.assertEqual(accepted["beneficial_edit_fraction"], 0.0)
        self.assertEqual(accepted["exact_correction_precision"], 1.0)
        none_accepted = metrics.edit_accounting(
            [{"reference": "x", "before": "y", "after": "z", "accepted": False}]
        )
        self.assertIsNone(none_accepted["beneficial_edit_fraction"])
        self.assertIsNone(none_accepted["exact_correction_precision"])
        self.assertEqual(none_accepted["coverage"], 0.0)

    def test_damage_to_initially_correct(self):
        report = metrics.edit_accounting(
            [{"reference": "cat", "before": "cat", "after": "car", "accepted": True}]
        )
        self.assertEqual(report["damage_to_initially_correct"], 1)
        self.assertEqual(report["harmful"], 1)

    def test_classify_edit(self):
        self.assertEqual(metrics.classify_edit("cat", "bat", "cat"), "beneficial")
        self.assertEqual(metrics.classify_edit("cat", "cat", "car"), "harmful")
        self.assertEqual(metrics.classify_edit("cat", "cot", "cbt"), "neutral")


class SensitiveSpans(unittest.TestCase):
    def test_sensitive_span(self):
        report = metrics.sensitive_span_preservation(
            [
                {"kind": "name", "text": "রবীন্দ্রনাথ"},
                {"kind": "numeral", "text": "১৯৪৭"},
            ],
            "লিখেছেন রবীন্দ্রনাথ ঠাকুর",
        )
        self.assertEqual(report["preserved"], 1)
        self.assertEqual(report["checked"], 2)
        self.assertAlmostEqual(report["rate"], 0.5)
        self.assertEqual(report["per_kind"]["name"]["rate"], 1.0)
        self.assertEqual(report["per_kind"]["numeral"]["rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
