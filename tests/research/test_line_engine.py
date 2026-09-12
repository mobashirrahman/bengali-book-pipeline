"""Offline tests for line-level OCR runners and make_study_items.

Runs in .venv without GPU, without Tesseract/EasyOCR/Surya installed.
Tests pure helper functions and CLI plumbing.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Import from the workflow scripts directory
import sys

_RESEARCH = Path(__file__).resolve().parents[2] / "workflow" / "study" / "scripts"
sys.path.insert(0, str(_RESEARCH))

from make_study_items import main as _make_items_main  # noqa: E402
from run_line_engine import (  # noqa: E402
    _bbox_iou,
    easyocr_to_lines,
    load_done_ids,
    parse_tesseract_tsv,
    surya_page_lines,
    surya_to_lines,
    write_row,
)


# ---------------------------------------------------------------------------
# Tesseract TSV parsing
# ---------------------------------------------------------------------------

_TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"

def _tsv_row(level=5, page=1, block=1, par=1, line=1, word=1,
             left=10, top=20, width=100, height=30, conf=95.0, text="hello"):
    return (f"{level}\t{page}\t{block}\t{par}\t{line}\t{word}\t"
            f"{left}\t{top}\t{width}\t{height}\t{conf}\t{text}")


class TestParseTesseractTsv(unittest.TestCase):
    def test_basic_single_word(self):
        tsv = _TSV_HEADER + "\n" + _tsv_row(text="কখগ")
        lines = parse_tesseract_tsv(tsv)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "কখগ")
        self.assertEqual(lines[0]["bbox"], [10, 20, 110, 50])
        self.assertAlmostEqual(lines[0]["conf"], 95.0, places=1)

    def test_multiple_words_same_line(self):
        rows = [
            _tsv_row(block=1, par=1, line=1, word=1, left=10, top=20,
                     width=50, height=30, conf=90, text="hello"),
            _tsv_row(block=1, par=1, line=1, word=2, left=70, top=22,
                     width=40, height=28, conf=85, text="world"),
        ]
        tsv = _TSV_HEADER + "\n" + "\n".join(rows)
        lines = parse_tesseract_tsv(tsv)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "hello world")
        # Union bbox
        self.assertEqual(lines[0]["bbox"], [10, 20, 110, 50])
        # Mean conf = (90 + 85) / 2 = 87.5
        self.assertAlmostEqual(lines[0]["conf"], 87.5, places=1)

    def test_conf_negative_1_ignored(self):
        rows = [
            _tsv_row(block=1, par=1, line=1, word=1, left=10, top=20,
                     width=50, height=30, conf=90, text="good"),
            _tsv_row(block=1, par=1, line=1, word=2, left=70, top=22,
                     width=40, height=28, conf=-1, text="bad"),
        ]
        tsv = _TSV_HEADER + "\n" + "\n".join(rows)
        lines = parse_tesseract_tsv(tsv)
        self.assertEqual(len(lines), 1)
        # conf -1 ignored; mean of [90] = 90
        self.assertAlmostEqual(lines[0]["conf"], 90.0, places=1)

    def test_empty_line_skipped(self):
        rows = [
            _tsv_row(block=1, par=1, line=1, word=1, left=10, top=20,
                     width=50, height=30, conf=90, text="hello"),
            _tsv_row(block=1, par=1, line=2, word=1, left=10, top=60,
                     width=0, height=0, conf=-1, text=""),
        ]
        tsv = _TSV_HEADER + "\n" + "\n".join(rows)
        lines = parse_tesseract_tsv(tsv)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "hello")

    def test_different_lines_different_blocks(self):
        rows = [
            _tsv_row(block=1, par=1, line=1, word=1, left=10, top=20,
                     width=50, height=30, conf=90, text="line1"),
            _tsv_row(block=1, par=1, line=2, word=1, left=10, top=80,
                     width=50, height=30, conf=85, text="line2"),
        ]
        tsv = _TSV_HEADER + "\n" + "\n".join(rows)
        lines = parse_tesseract_tsv(tsv)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["text"], "line1")
        self.assertEqual(lines[1]["text"], "line2")

    def test_empty_tsv(self):
        self.assertEqual(parse_tesseract_tsv(""), [])
        self.assertEqual(parse_tesseract_tsv(_TSV_HEADER), [])

    def test_non_level5_rows_ignored(self):
        rows = [
            _tsv_row(level=1, text="page"),  # level 1
            _tsv_row(level=4, text="block"),  # level 4
            _tsv_row(level=5, text="word"),   # level 5
        ]
        tsv = _TSV_HEADER + "\n" + "\n".join(rows)
        lines = parse_tesseract_tsv(tsv)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "word")


# ---------------------------------------------------------------------------
# EasyOCR polygon -> bbox
# ---------------------------------------------------------------------------

class TestEasyocrToLines(unittest.TestCase):
    def test_basic_polygon(self):
        # EasyOCR returns (polygon, text, confidence)
        results = [
            ([[10, 20], [110, 20], [110, 50], [10, 50]], "কখগ", 0.95),
        ]
        lines = easyocr_to_lines(results)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "কখগ")
        self.assertEqual(lines[0]["bbox"], [10, 20, 110, 50])
        self.assertAlmostEqual(lines[0]["conf"], 0.95, places=2)

    def test_rotated_polygon(self):
        # Polygon not axis-aligned
        results = [
            ([[20, 10], [120, 15], [115, 55], [15, 50]], "test", 0.88),
        ]
        lines = easyocr_to_lines(results)
        self.assertEqual(len(lines), 1)
        # Axis-aligned bbox: xs=[20,120,115,15] min=15, ys=[10,15,55,50] min=10
        self.assertEqual(lines[0]["bbox"], [15, 10, 120, 55])

    def test_multiple_results(self):
        results = [
            ([[0, 0], [100, 0], [100, 30], [0, 30]], "line1", 0.9),
            ([[0, 40], [100, 40], [100, 70], [0, 70]], "line2", 0.85),
        ]
        lines = easyocr_to_lines(results)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["text"], "line1")
        self.assertEqual(lines[1]["text"], "line2")

    def test_empty_text_skipped(self):
        results = [
            ([[0, 0], [100, 0], [100, 30], [0, 30]], "", 0.5),
            ([[0, 40], [100, 40], [100, 70], [0, 70]], "keep", 0.9),
        ]
        lines = easyocr_to_lines(results)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "keep")

    def test_empty_results(self):
        self.assertEqual(easyocr_to_lines([]), [])


# ---------------------------------------------------------------------------
# IoU helper
# ---------------------------------------------------------------------------

class TestBboxIou(unittest.TestCase):
    def test_identical_boxes(self):
        self.assertAlmostEqual(_bbox_iou([0, 0, 100, 50], [0, 0, 100, 50]), 1.0)

    def test_no_overlap(self):
        self.assertAlmostEqual(_bbox_iou([0, 0, 10, 10], [20, 20, 30, 30]), 0.0)

    def test_partial_overlap(self):
        # a=[0,0,10,10] area=100, b=[5,5,15,15] area=100, inter=25
        iou = _bbox_iou([0, 0, 10, 10], [5, 5, 15, 15])
        self.assertAlmostEqual(iou, 25.0 / 175.0)

    def test_contained_box(self):
        # a=[0,0,100,100] area=10000, b=[10,10,50,50] area=1600, inter=1600
        iou = _bbox_iou([0, 0, 100, 100], [10, 10, 50, 50])
        self.assertAlmostEqual(iou, 1600.0 / 10000.0)


# ---------------------------------------------------------------------------
# Surya result -> lines (using fake objects)
# ---------------------------------------------------------------------------

class TestSuryaToLines(unittest.TestCase):
    def _make_block(self, html, polygon, conf=0.9, skipped=False,
                    error=False, reading_order=0):
        return SimpleNamespace(
            html=html,
            polygon=polygon,
            confidence=conf,
            skipped=skipped,
            error=error,
            reading_order=reading_order,
        )

    def test_basic_block(self):
        block = self._make_block(
            html="<p>কখগ</p>",
            polygon=[[10, 20], [110, 20], [110, 50], [10, 50]],
            conf=0.92,
        )
        result = SimpleNamespace(blocks=[block])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "কখগ")
        self.assertEqual(lines[0]["bbox"], [10, 20, 110, 50])
        self.assertAlmostEqual(lines[0]["conf"], 0.92, places=2)

    def test_html_tags_stripped(self):
        block = self._make_block(
            html="<p>hello</p> <span>world</span>",
            polygon=[[0, 0], [100, 0], [100, 30], [0, 30]],
        )
        result = SimpleNamespace(blocks=[block])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "hello world")

    def test_skipped_block_ignored(self):
        block = self._make_block(
            html="skipped",
            polygon=[[0, 0], [100, 0], [100, 30], [0, 30]],
            skipped=True,
        )
        result = SimpleNamespace(blocks=[block])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 0)

    def test_error_block_ignored(self):
        block = self._make_block(
            html="error",
            polygon=[[0, 0], [100, 0], [100, 30], [0, 30]],
            error=True,
        )
        result = SimpleNamespace(blocks=[block])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 0)

    def test_reading_order_sorted(self):
        block2 = self._make_block(
            html="second",
            polygon=[[0, 40], [100, 40], [100, 70], [0, 70]],
            reading_order=2,
        )
        block1 = self._make_block(
            html="first",
            polygon=[[0, 0], [100, 0], [100, 30], [0, 30]],
            reading_order=1,
        )
        result = SimpleNamespace(blocks=[block2, block1])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["text"], "first")
        self.assertEqual(lines[1]["text"], "second")

    def test_flat_polygon_format(self):
        # Some surya versions use flat [x0,y0,x1,y1,...] polygon
        block = self._make_block(
            html="flat",
            polygon=[10, 20, 110, 20, 110, 50, 10, 50],
        )
        result = SimpleNamespace(blocks=[block])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["bbox"], [10, 20, 110, 50])

    def test_empty_html_skipped(self):
        block = self._make_block(
            html="",
            polygon=[[0, 0], [100, 0], [100, 30], [0, 30]],
        )
        result = SimpleNamespace(blocks=[block])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 0)

    def test_no_blocks(self):
        result = SimpleNamespace(blocks=[])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 0)

    def test_bbox_fallback_to_bbox_attr(self):
        block = SimpleNamespace(
            html="fallback",
            polygon=None,
            bbox=[10, 20, 110, 50],
            confidence=0.8,
            skipped=False,
            error=False,
            reading_order=0,
        )
        result = SimpleNamespace(blocks=[block])
        lines = surya_to_lines(result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["bbox"], [10, 20, 110, 50])


# ---------------------------------------------------------------------------
# Surya page_lines: detection + recognition with fakes
# ---------------------------------------------------------------------------

def _make_layout_box(polygon, label="Text", raw_label="Text", position=0):
    """Fake surya.layout.schema.LayoutBox constructor."""
    return SimpleNamespace(polygon=polygon, label=label,
                           raw_label=raw_label, position=position)


def _make_layout_result(bboxes, image_bbox):
    """Fake surya.layout.schema.LayoutResult constructor."""
    return SimpleNamespace(bboxes=bboxes, image_bbox=image_bbox)


class TestSuryaPageLines(unittest.TestCase):
    """Tests for surya_page_lines() with fake detector/recognizer objects."""

    def _make_det_box(self, bbox):
        """Fake detection box with .bbox [x0,y0,x1,y1]."""
        return SimpleNamespace(bbox=bbox, polygon=bbox, confidence=0.9)

    def _make_rec_block(self, html, polygon, conf=0.9, reading_order=0):
        """Fake recognition block."""
        return SimpleNamespace(
            html=html, polygon=polygon, confidence=conf,
            skipped=False, error=False, reading_order=reading_order,
        )

    def test_basic_detection_and_recognition(self):
        """One detected box -> one LayoutBox -> one recognised block -> one line."""
        fake_img = SimpleNamespace(size=(100, 50))

        det_result = SimpleNamespace(
            bboxes=[self._make_det_box([10, 5, 90, 45])],
            image_bbox=[0, 0, 100, 50],
        )

        def fake_detector(images):
            return [det_result]

        rec_block = self._make_rec_block(
            html="<p>recognized text</p>",
            polygon=[10, 5, 90, 5, 90, 45, 10, 45],
        )
        rec_page = SimpleNamespace(blocks=[rec_block])

        def fake_recognizer(images, layouts, full_page=None):
            return [rec_page]

        lines = surya_page_lines(fake_img, fake_detector, fake_recognizer,
                                 _make_layout_box, _make_layout_result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "recognized text")
        self.assertEqual(lines[0]["bbox"], [10, 5, 90, 45])

    def test_one_layout_box_per_detected_line(self):
        """Two detected boxes -> two LayoutBoxes in the layout."""
        fake_img = SimpleNamespace(size=(200, 100))

        det_box1 = self._make_det_box([0, 0, 200, 40])
        det_box2 = self._make_det_box([0, 50, 200, 90])
        det_result = SimpleNamespace(bboxes=[det_box1, det_box2], image_bbox=[0, 0, 200, 100])

        def fake_detector(images):
            return [det_result]

        # Track what layout_cls was called with
        layout_calls = []

        def tracking_layout_cls(**kwargs):
            layout_calls.append(kwargs)
            return SimpleNamespace(**kwargs)

        rec_block1 = self._make_rec_block("line one", [0, 0, 200, 0, 200, 40, 0, 40], reading_order=0)
        rec_block2 = self._make_rec_block("line two", [0, 50, 200, 50, 200, 90, 0, 90], reading_order=1)
        rec_page = SimpleNamespace(blocks=[rec_block1, rec_block2])

        def fake_recognizer(images, layouts, full_page=None):
            # Verify two LayoutBoxes were created
            assert len(layouts) == 1, f"expected 1 layout, got {len(layouts)}"
            assert len(layouts[0].bboxes) == 2, f"expected 2 boxes, got {len(layouts[0].bboxes)}"
            return [rec_page]

        lines = surya_page_lines(fake_img, fake_detector, fake_recognizer,
                                 tracking_layout_cls, _make_layout_result)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["text"], "line one")
        self.assertEqual(lines[1]["text"], "line two")
        # Verify LayoutBox positions
        self.assertEqual(layout_calls[0]["position"], 0)
        self.assertEqual(layout_calls[1]["position"], 1)

    def test_blocks_shuffled_still_map_to_correct_boxes(self):
        """Recognition blocks in shuffled order still map to the right source boxes."""
        fake_img = SimpleNamespace(size=(200, 100))

        det_box_top = self._make_det_box([0, 0, 200, 40])
        det_box_bot = self._make_det_box([0, 50, 200, 90])
        det_result = SimpleNamespace(bboxes=[det_box_top, det_box_bot],
                                     image_bbox=[0, 0, 200, 100])

        def fake_detector(images):
            return [det_result]

        # Blocks returned in reverse order (shuffled)
        rec_block_bot = self._make_rec_block(
            "bottom text",
            [0, 50, 200, 50, 200, 90, 0, 90],
            reading_order=0,
        )
        rec_block_top = self._make_rec_block(
            "top text",
            [0, 0, 200, 0, 200, 40, 0, 40],
            reading_order=1,
        )
        rec_page = SimpleNamespace(blocks=[rec_block_bot, rec_block_top])

        def fake_recognizer(images, layouts, full_page=None):
            return [rec_page]

        lines = surya_page_lines(fake_img, fake_detector, fake_recognizer,
                                 _make_layout_box, _make_layout_result)
        self.assertEqual(len(lines), 2)
        # First line should be top (mapped by IoU), second should be bottom
        self.assertEqual(lines[0]["text"], "top text")
        self.assertEqual(lines[0]["bbox"], [0, 0, 200, 40])
        self.assertEqual(lines[1]["text"], "bottom text")
        self.assertEqual(lines[1]["bbox"], [0, 50, 200, 90])

    def test_unmatched_detected_box_not_emitted(self):
        """A detected box with no recognised block is not emitted as a line."""
        fake_img = SimpleNamespace(size=(200, 100))

        det_box1 = self._make_det_box([0, 0, 200, 40])
        det_box2 = self._make_det_box([0, 50, 200, 90])  # will have no match
        det_result = SimpleNamespace(bboxes=[det_box1, det_box2],
                                     image_bbox=[0, 0, 200, 100])

        def fake_detector(images):
            return [det_result]

        # Only one block matching box1
        rec_block = self._make_rec_block(
            "only one",
            [0, 0, 200, 0, 200, 40, 0, 40],
            reading_order=0,
        )
        rec_page = SimpleNamespace(blocks=[rec_block])

        def fake_recognizer(images, layouts, full_page=None):
            return [rec_page]

        lines = surya_page_lines(fake_img, fake_detector, fake_recognizer,
                                 _make_layout_box, _make_layout_result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "only one")

    def test_no_detected_boxes_falls_back_to_full_page(self):
        """When detection returns no boxes, fall back to full-page recognition."""
        fake_img = SimpleNamespace(size=(100, 50))

        det_result = SimpleNamespace(bboxes=[], image_bbox=[0, 0, 100, 50])

        def fake_detector(images):
            return [det_result]

        rec_block = self._make_rec_block("fallback text", None)
        rec_page = SimpleNamespace(blocks=[rec_block])

        call_count = [0]

        def fake_recognizer(images, layouts=None, full_page=None):
            call_count[0] += 1
            return [rec_page]

        lines = surya_page_lines(fake_img, fake_detector, fake_recognizer,
                                 _make_layout_box, _make_layout_result)
        # Should call recognizer with no layouts (full-page fallback)
        self.assertGreaterEqual(call_count[0], 1)

    def test_empty_html_blocks_not_emitted(self):
        """Recognised blocks with empty HTML are not emitted."""
        fake_img = SimpleNamespace(size=(200, 100))

        det_box = self._make_det_box([0, 0, 200, 40])
        det_result = SimpleNamespace(bboxes=[det_box], image_bbox=[0, 0, 200, 100])

        def fake_detector(images):
            return [det_result]

        rec_block = self._make_rec_block("", [0, 0, 200, 0, 200, 40, 0, 40])
        rec_page = SimpleNamespace(blocks=[rec_block])

        def fake_recognizer(images, layouts, full_page=None):
            return [rec_page]

        lines = surya_page_lines(fake_img, fake_detector, fake_recognizer,
                                 _make_layout_box, _make_layout_result)
        self.assertEqual(len(lines), 0)


# ---------------------------------------------------------------------------
# Resume: skip done ids
# ---------------------------------------------------------------------------

class TestResume(unittest.TestCase):
    def test_load_done_ids(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"id": "aaa", "lines": []}) + "\n")
            f.write(json.dumps({"id": "bbb", "lines": []}) + "\n")
            f.write(json.dumps({"id": "ccc", "lines": []}) + "\n")
            f.flush()
            path = Path(f.name)
        try:
            ids = load_done_ids(path)
            self.assertEqual(ids, {"aaa", "bbb", "ccc"})
        finally:
            path.unlink()

    def test_load_done_ids_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.flush()
            path = Path(f.name)
        try:
            ids = load_done_ids(path)
            self.assertEqual(ids, set())
        finally:
            path.unlink()

    def test_resume_skips_done_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output.jsonl"
            # Write two rows
            write_row(out, {"id": "aaa", "engine": "tesseract", "lines": []})
            write_row(out, {"id": "bbb", "engine": "tesseract", "lines": []})
            ids = load_done_ids(out)
            self.assertEqual(ids, {"aaa", "bbb"})
            # Simulate: items = [aaa, bbb, ccc]
            items = [{"id": "aaa"}, {"id": "bbb"}, {"id": "ccc"}]
            remaining = [i for i in items if i["id"] not in ids]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["id"], "ccc")


# ---------------------------------------------------------------------------
# Error row emitted when the engine raises
# ---------------------------------------------------------------------------

class TestErrorRow(unittest.TestCase):
    def test_run_tesseract_missing_file(self):
        from run_line_engine import run_tesseract
        lines, error, seconds = run_tesseract(
            "", "/usr/bin/tesseract", "/nonexistent", "ben", "1", "3", 30,
        )
        self.assertEqual(lines, [])
        self.assertIn("missing-file", error)
        self.assertGreaterEqual(seconds, 0)

    def test_run_easyocr_missing_file(self):
        from run_line_engine import run_easyocr
        # Pass a mock reader; missing file should be caught before calling it
        mock_reader = None
        lines, error, seconds = run_easyocr("", mock_reader, 30)
        self.assertEqual(lines, [])
        self.assertIn("missing-file", error)

    def test_run_surya_missing_file(self):
        from run_line_engine import run_surya
        lines, error, seconds = run_surya("", None, None, 30)
        self.assertEqual(lines, [])
        self.assertIn("missing-file", error)


# ---------------------------------------------------------------------------
# make_study_items on a tmp fixture
# ---------------------------------------------------------------------------

class TestMakeStudyItems(unittest.TestCase):
    def test_make_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            study_root = Path(tmp)
            pages_dir = study_root / "pages"
            pages_dir.mkdir()

            # Create synthetic PNGs (minimal valid PNG)
            import struct
            import zlib

            def make_png(path: Path, width: int = 10, height: int = 10):
                """Create a minimal valid PNG file."""
                def chunk(chunk_type: bytes, data: bytes) -> bytes:
                    c = chunk_type + data
                    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

                raw = b""
                for _ in range(height):
                    raw += b"\x00" + b"\xff\x00\x00" * width  # filter byte + RGB

                ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
                idat = zlib.compress(raw)

                png = b"\x89PNG\r\n\x1a\n"
                png += chunk(b"IHDR", ihdr)
                png += chunk(b"IDAT", idat)
                png += chunk(b"IEND", b"")
                path.write_bytes(png)

            # Create ocr_pages.json with known page_ids
            page_ids = ["aaa111", "bbb222", "ccc333"]
            ocr_pages = [
                {"page_id": pid, "image_ref": pid, "ocr_text": f"text {i}"}
                for i, pid in enumerate(page_ids)
            ]
            (study_root / "ocr_pages.json").write_text(
                json.dumps(ocr_pages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Create PNGs for each page
            for pid in page_ids:
                make_png(pages_dir / f"{pid}.png")

            # Run make_study_items
            output = study_root / "items.json"
            sys.argv = [
                "make_study_items.py",
                "--study-root", str(study_root),
                "--output", str(output),
            ]
            _make_items_main()

            # Verify output
            items = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(items), 3)
            for item in items:
                self.assertIn("id", item)
                self.assertIn("image", item)
                self.assertTrue(Path(item["image"]).is_file())

    def test_make_items_missing_png(self):
        """Missing PNGs are skipped with a warning."""
        with tempfile.TemporaryDirectory() as tmp:
            study_root = Path(tmp)
            pages_dir = study_root / "pages"
            pages_dir.mkdir()

            ocr_pages = [
                {"page_id": "exists", "image_ref": "exists", "ocr_text": "ok"},
                {"page_id": "missing", "image_ref": "missing", "ocr_text": "gone"},
            ]
            (study_root / "ocr_pages.json").write_text(
                json.dumps(ocr_pages, ensure_ascii=False), encoding="utf-8",
            )

            # Create only the "exists" PNG
            import struct, zlib
            def make_png(path):
                def chunk(ct, d):
                    c = ct + d
                    return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
                raw = b"".join(b"\x00" + b"\xff\x00\x00" * 10 for _ in range(10))
                ihdr = struct.pack(">IIBBBBB", 10, 10, 8, 2, 0, 0, 0)
                png = b"\x89PNG\r\n\x1a\n"
                png += chunk(b"IHDR", ihdr)
                png += chunk(b"IDAT", zlib.compress(raw))
                png += chunk(b"IEND", b"")
                path.write_bytes(png)
            make_png(pages_dir / "exists.png")

            output = study_root / "items.json"
            sys.argv = [
                "make_study_items.py",
                "--study-root", str(study_root),
                "--output", str(output),
            ]
            _make_items_main()

            items = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], "exists")


if __name__ == "__main__":
    unittest.main()
