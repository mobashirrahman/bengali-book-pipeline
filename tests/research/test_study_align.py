"""Tests for line-to-census-region alignment (P3a, synthetic fixtures)."""

from __future__ import annotations

import json
import unittest
import unicodedata

from pdf_craft_tool.research import study_align
from pdf_craft_tool.research.study_align import (
    Alignment,
    EngineLine,
    align,
    align_page,
    lines_from_azure,
    lines_from_google,
    lines_from_records,
)


def _entries_two_regions():
    return [
        {"region_index": 0, "geometry": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000}},
        {"region_index": 1, "geometry": {"x0": 1100, "y0": 0, "x1": 2100, "y1": 1000}},
    ]


def _gword(text, box, brk=None):
    x0, y0, x1, y1 = box
    word = {
        "symbols": [{"text": ch} for ch in text],
        "boundingBox": {"vertices": [
            {"x": x0, "y": y0}, {"x": x1, "y": y0},
            {"x": x1, "y": y1}, {"x": x0, "y": y1},
        ]},
    }
    if brk is not None:
        word["property"] = {"detectedBreak": {"type": brk}}
    return word


def _google_fixture():
    words = [
        _gword("Alpha", (10, 10, 200, 60), "SPACE"),
        _gword("beta", (210, 10, 400, 60), "LINE_BREAK"),
        _gword("Gamma", (1150, 10, 1350, 60), "SPACE"),
        _gword("delta", (1360, 10, 1550, 60), "LINE_BREAK"),
        _gword("Zzz", (3000, 3000, 3200, 3050), "LINE_BREAK"),
    ]
    page = {"blocks": [{"paragraphs": [{"words": words}]}]}
    return json.dumps({"responses": [{"fullTextAnnotation": {"pages": [page]}}]})


def _azure_fixture():
    def line(text, box, confs):
        x0, y0, x1, y1 = box
        return {
            "text": text,
            "boundingPolygon": [
                {"x": x0, "y": y0}, {"x": x1, "y": y0},
                {"x": x1, "y": y1}, {"x": x0, "y": y1},
            ],
            "words": [{"text": w, "confidence": c} for w, c in zip(text.split(), confs)],
        }
    return json.dumps({
        "readResult": {"blocks": [{"lines": [
            line("Alpha beta", (10, 10, 400, 60), [0.9, 0.8]),
            line("Gamma delta", (1150, 10, 1550, 60), [0.7, 0.9]),
            line("Zzz far", (3000, 3000, 3200, 3050), [0.5, 0.5]),
        ]}]},
    })


class GoogleParsingTests(unittest.TestCase):
    def test_google_lines_and_alignment(self):
        lines = lines_from_google(_google_fixture())
        self.assertEqual([ln.text for ln in lines], ["Alpha beta", "Gamma delta", "Zzz"])
        result = align(lines, _entries_two_regions())
        self.assertIsInstance(result, Alignment)
        self.assertEqual(result.region_texts[0], "Alpha beta")
        self.assertEqual(result.region_texts[1], "Gamma delta")
        self.assertEqual([ln.text for ln in result.unassigned], ["Zzz"])
        # bboxes are unions of word boxes
        self.assertEqual(lines[0].bbox, (10, 10, 400, 60))

    def test_google_space_break_stays_on_line(self):
        words = [
            _gword("a", (0, 0, 10, 10), "SPACE"),
            _gword("b", (11, 0, 20, 10), "SURE_SPACE"),
            _gword("c", (21, 0, 30, 10), "LINE_BREAK"),
        ]
        page = {"blocks": [{"paragraphs": [{"words": words}]}]}
        raw = json.dumps({"responses": [{"fullTextAnnotation": {"pages": [page]}}]})
        lines = lines_from_google(raw)
        self.assertEqual([ln.text for ln in lines], ["a b c"])

    def test_google_error_response_gives_no_lines(self):
        raw = json.dumps({"responses": [{"error": {"code": 500, "message": "boom"}}]})
        self.assertEqual(lines_from_google(raw), [])

    def test_google_two_pages_parsed(self):
        def page(word_text, box):
            return {"blocks": [{"paragraphs": [{"words": [
                _gword(word_text, box, "LINE_BREAK"),
            ]}]}]}
        raw = json.dumps({"responses": [{"fullTextAnnotation": {"pages": [
            page("one", (10, 10, 100, 60)),
            page("two", (10, 10, 100, 60)),
        ]}}]})
        lines = lines_from_google(raw)
        self.assertEqual([ln.text for ln in lines], ["one", "two"])

    def test_google_malformed_and_missing_keys(self):
        self.assertEqual(lines_from_google("not json{{"), [])
        self.assertEqual(lines_from_google(json.dumps({})), [])
        self.assertEqual(lines_from_google(json.dumps({"responses": []})), [])
        self.assertEqual(lines_from_google(json.dumps({"responses": [{}]})), [])
        self.assertEqual(lines_from_google(""), [])
        # odd entries inside words are skipped, not raised
        bad_page = {"blocks": [{"paragraphs": [{"words": ["junk", 42, None]}]}, "junk-block"]}
        raw = json.dumps({"responses": [{"fullTextAnnotation": {"pages": [bad_page]}}]})
        self.assertEqual(lines_from_google(raw), [])


class AzureParsingTests(unittest.TestCase):
    def test_azure_lines_and_alignment(self):
        lines = lines_from_azure(_azure_fixture())
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].text, "Alpha beta")
        self.assertAlmostEqual(lines[0].conf, 0.85)
        self.assertEqual(lines[0].bbox, (10, 10, 400, 60))
        result = align(lines, _entries_two_regions())
        self.assertEqual(result.region_texts[0], "Alpha beta")
        self.assertEqual(result.region_texts[1], "Gamma delta")
        self.assertEqual([ln.text for ln in result.unassigned], ["Zzz far"])

    def test_azure_malformed_and_missing_keys(self):
        self.assertEqual(lines_from_azure("{{bad"), [])
        self.assertEqual(lines_from_azure(json.dumps({})), [])
        self.assertEqual(lines_from_azure(json.dumps({"readResult": {}})), [])
        self.assertEqual(lines_from_azure(json.dumps({"readResult": {"blocks": "nope"}})), [])
        # line missing polygon is skipped; line without word confs -> None
        kept = {"text": "kept", "boundingPolygon": [
            {"x": 1, "y": 2}, {"x": 5, "y": 2},
            {"x": 5, "y": 6}, {"x": 1, "y": 6}],
            "words": [{"text": "kept"}]}
        block = {"lines": [{"text": "skip me"}, kept, 42, None]}
        raw = json.dumps({"readResult": {"blocks": [block]}})
        lines = lines_from_azure(raw)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "kept")
        self.assertIsNone(lines[0].conf)


class RecordsParsingTests(unittest.TestCase):
    def test_records_fixture(self):
        rows = [
            {"text": "Alpha beta", "bbox": [10, 10, 400, 60], "conf": 0.9},
            {"text": "Gamma delta", "bbox": [1150, 10, 1550, 60], "conf": 0.5},
            {"text": "Zzz", "bbox": [3000, 3000, 3200, 3050]},
        ]
        lines = lines_from_records(rows)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].conf, 0.9)
        self.assertIsNone(lines[2].conf)
        result = align(lines, _entries_two_regions())
        self.assertEqual(result.region_texts[0], "Alpha beta")
        self.assertEqual(result.region_texts[1], "Gamma delta")
        self.assertEqual([ln.text for ln in result.unassigned], ["Zzz"])

    def test_records_skips_bad_entries(self):
        rows = [
            {"text": "ok", "bbox": [0, 0, 10, 10]},
            {"text": 42, "bbox": [0, 0, 10, 10]},
            {"text": "bad-box", "bbox": [0, 0]},
            {"text": "bad-coord", "bbox": ["a", 0, 1, 1]},
            "junk", None, 42,
        ]
        lines = lines_from_records(rows)
        self.assertEqual([ln.text for ln in lines], ["ok"])
        self.assertEqual(lines_from_records("not a list json"), [])
        self.assertEqual(lines_from_records("{{bad"), [])


class AlignBehaviourTests(unittest.TestCase):
    def test_overlap_below_threshold_unassigned(self):
        entries = _entries_two_regions()
        # line straddles the boundary: 40% inside region 0 -> below default 0.5
        line = EngineLine(text="split", bbox=(840, 100, 1240, 140), conf=None)
        result = align([line], entries)
        self.assertEqual(result.region_texts[0], "")
        self.assertEqual(result.region_texts[1], "")
        self.assertEqual(list(result.unassigned), [line])
        # with a lower threshold it assigns to the largest-share region
        result2 = align([line], entries, min_overlap=0.2)
        self.assertEqual(result2.region_texts[0], "split")
        self.assertEqual(result2.unassigned, [])

    def test_tie_ambiguous_lowest_index(self):
        entries = [
            {"region_index": 0, "geometry": {"x0": 0, "y0": 0, "x1": 100, "y1": 100}},
            {"region_index": 1, "geometry": {"x0": 100, "y0": 0, "x1": 200, "y1": 100}},
        ]
        line = EngineLine(text="tie", bbox=(50, 10, 150, 60), conf=None)
        result = align([line], entries)
        self.assertEqual(result.region_texts[0], "tie")
        self.assertEqual(result.region_texts[1], "")
        self.assertEqual(list(result.ambiguous), [line])

    def test_multi_column_row_ordering(self):
        entries = [{"region_index": 0,
                    "geometry": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000}}]
        # input order is right-column first; same visual row -> left-to-right
        right = EngineLine(text="RIGHT", bbox=(600, 10, 900, 60), conf=None)
        left = EngineLine(text="LEFT", bbox=(10, 12, 300, 62), conf=None)
        below = EngineLine(text="BELOW", bbox=(10, 500, 300, 550), conf=None)
        result = align([right, left, below], entries)
        self.assertEqual(result.region_texts[0], "LEFT RIGHT\nBELOW")

    def test_row_boundary_with_diverging_cx_order(self):
        # cy order (R1R, R1L, R2) differs from cx order (R1L, R1R, R2):
        # re-grouping the cx-reordered list would silently merge R2 into row 1.
        entries = [{"region_index": 0,
                    "geometry": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000}}]
        r1r = EngineLine(text="R1R", bbox=(500, 0, 700, 50), conf=None)    # cy 25
        r1l = EngineLine(text="R1L", bbox=(100, 15, 300, 65), conf=None)   # cy 40
        r2 = EngineLine(text="R2", bbox=(50, 25, 250, 75), conf=None)      # cy 50
        result = align([r1r, r1l, r2], entries)
        self.assertEqual(result.region_texts[0], "R1L R1R\nR2")

    def test_inverted_boxes_normalised(self):
        # Inverted census region (x1 < x0, y1 < y0) still receives its line.
        entries = [{"region_index": 0,
                    "geometry": {"x0": 500, "y0": 500, "x1": 100, "y1": 100}}]
        line = EngineLine(text="inside", bbox=(150, 150, 400, 400), conf=None)
        result = align([line], entries)
        self.assertEqual(result.region_texts[0], "inside")
        self.assertEqual(result.unassigned, [])
        # Inverted record bbox is normalised the same way.
        rows = [{"text": "inv", "bbox": [400, 400, 150, 150]}]
        parsed = lines_from_records(rows)
        self.assertEqual(parsed[0].bbox, (150, 150, 400, 400))
        result2 = align(parsed, [{"region_index": 0,
                                  "geometry": {"x0": 100, "y0": 100,
                                               "x1": 500, "y1": 500}}])
        self.assertEqual(result2.region_texts[0], "inv")

    def test_zero_area_line_unassigned(self):
        entries = [{"region_index": 0,
                    "geometry": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000}}]
        zero_width = EngineLine(text="flat", bbox=(10, 10, 10, 50), conf=None)
        zero_height = EngineLine(text="thin", bbox=(10, 10, 100, 10), conf=None)
        result = align([zero_width, zero_height], entries)
        self.assertEqual(result.region_texts[0], "")
        self.assertEqual(list(result.unassigned), [zero_width, zero_height])

    def test_empty_region_present(self):
        result = align([], _entries_two_regions())
        self.assertEqual(result.region_texts, {0: "", 1: ""})
        self.assertEqual(result.unassigned, [])
        self.assertEqual(result.ambiguous, [])

    def test_nfc_normalisation(self):
        entries = [{"region_index": 0,
                    "geometry": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000}}]
        decomposed = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(decomposed, unicodedata.normalize("NFC", decomposed))
        line = EngineLine(text=decomposed, bbox=(10, 10, 200, 60), conf=None)
        result = align([line], entries)
        self.assertEqual(result.region_texts[0], unicodedata.normalize("NFC", decomposed))

    def test_determinism(self):
        entries = _entries_two_regions()
        rows = [
            {"text": "Alpha beta", "bbox": [10, 10, 400, 60]},
            {"text": "Gamma delta", "bbox": [1150, 10, 1550, 60]},
            {"text": "Zzz", "bbox": [3000, 3000, 3200, 3050]},
        ]
        first = align(lines_from_records(rows), entries)
        second = align(lines_from_records(rows), entries)
        self.assertEqual(first.region_texts, second.region_texts)
        self.assertEqual(list(first.unassigned), list(second.unassigned))
        self.assertEqual(list(first.ambiguous), list(second.ambiguous))


class AlignPageTests(unittest.TestCase):
    def test_align_page_dispatch(self):
        entries = _entries_two_regions()
        gres = align_page("google", _google_fixture(), entries)
        self.assertEqual(gres.region_texts[0], "Alpha beta")
        ares = align_page("azure", _azure_fixture(), entries)
        self.assertEqual(ares.region_texts[1], "Gamma delta")
        rres = align_page("records", [
            {"text": "hi", "bbox": [10, 10, 100, 60]},
        ], entries)
        self.assertEqual(rres.region_texts[0], "hi")
        with self.assertRaises(ValueError):
            align_page("nope", "{}", entries)


if __name__ == "__main__":
    unittest.main()
