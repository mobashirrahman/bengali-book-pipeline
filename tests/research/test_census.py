"""Tests for the S2 full-page census (unittest, stdlib only)."""

import unittest

from pdf_craft_tool.research import census
from pdf_craft_tool.research.schema import ContractError

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def make_region(index, x0, kind="body", unreadable=False):
    return {
        "region_index": index,
        "geometry": {"x0": x0, "y0": 0, "x1": x0 + 50, "y1": 20},
        "kind": kind,
        "unreadable": unreadable,
    }


def make_census():
    return census.build_census_from_regions(
        HASH_C, HASH_A, HASH_B, "ann1",
        [make_region(0, 0), make_region(1, 100), make_region(2, 200)],
    )


class CensusTests(unittest.TestCase):
    def test_census_retains_ocr_missed_line(self):
        page = make_census()
        ocr_lines = [
            {"text": "line one", "bbox": [0, 0, 50, 20]},
            {"text": "line two", "bbox": [100, 0, 150, 20]},
        ]
        report = census.census_omission_check(page, ocr_lines)
        self.assertEqual(report["census_text_regions"], 3)
        self.assertEqual(report["matched"], 2)
        self.assertEqual(report["omitted_lines"], 1)
        self.assertEqual(report["omitted_region_index"], [2])
        # The missed entry is still part of the census (and of gold).
        self.assertEqual(len(page.entries), 3)
        self.assertIn(2, [entry.region_index for entry in page.entries])

    def test_reading_order_must_be_permutation(self):
        with self.assertRaises(ContractError):
            census.build_census_from_regions(
                HASH_C, HASH_A, HASH_B, "ann1",
                [make_region(0, 0), make_region(1, 100)],
                reading_order=(0, 0),
            )
        data = make_census().to_dict()
        data["reading_order"] = [0, 1, 1]
        with self.assertRaises(ContractError):
            census.PageCensus.from_dict(data)

    def test_region_index_dense(self):
        with self.assertRaises(ContractError):
            census.build_census_from_regions(
                HASH_C, HASH_A, HASH_B, "ann1",
                [make_region(0, 0), make_region(2, 200)],
            )

    def test_unreadable_entry_has_empty_text_note_ok(self):
        page = census.build_census_from_regions(
            HASH_C, HASH_A, HASH_B, "ann1",
            [make_region(0, 0),
             {"region_index": 1,
              "geometry": {"x0": 100, "y0": 0, "x1": 150, "y1": 20},
              "kind": "body", "unreadable": True, "note": "torn corner"}],
        )
        self.assertTrue(page.entries[1].unreadable)
        report = census.census_omission_check(
            page, [{"text": "line one", "bbox": [0, 0, 50, 20]}])
        self.assertEqual(report["census_text_regions"], 1)
        self.assertEqual(report["omitted_lines"], 0)

    def test_unknown_keys_rejected(self):
        data = make_census().to_dict()
        data["entries"][0]["tesseract"] = "draft"
        with self.assertRaises(ContractError):
            census.PageCensus.from_dict(data)

    def test_roundtrip(self):
        page = make_census()
        self.assertEqual(census.PageCensus.from_dict(page.to_dict()), page)


if __name__ == "__main__":
    unittest.main()
