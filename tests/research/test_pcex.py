"""Tests for pdf_craft_tool.research.pcex (OCR-region census seed)."""

import io
import unittest
import zipfile

from pdf_craft_tool.research import census, pcex, schema

_PAGES_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<pages index_base="1" coordinate_space="ocr_pixels" render_dpi="300">'
    '<page index="1" width="2550" height="3300" />'
    '<page index="2" width="2550" height="3300" />'
    '</pages>'
)

_CHAPTER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<chapter level="0"><body>'
    '<paragraph ref="sub_title">'
    '<block page_index="1" order="0" det="100,50,900,120">শিরোনাম</block>'
    '</paragraph>'
    '<paragraph ref="text">'
    '<block page_index="1" order="2" det="100,300,2400,600">দ্বিতীয়</block>'
    '<block page_index="1" order="1" det="100,150,2400,280">প্রথম</block>'
    '<block page_index="2" order="0" det="100,150,2400,280">পরের পাতা</block>'
    '</paragraph>'
    '</body></chapter>'
)


def _pcex_bytes(*, with_page2_blocks=True) -> bytes:
    chapter = _CHAPTER_XML
    if not with_page2_blocks:
        chapter = chapter.replace(
            '<block page_index="2" order="0" det="100,150,2400,280">'
            'পরের পাতা</block>', '')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pages.xml", _PAGES_XML)
        archive.writestr("chapters/chapter_1.xml", chapter)
    return buffer.getvalue()


class ReadPcexPages(unittest.TestCase):
    def _write(self, tmp, data):
        path = tmp / "raw.pcex"
        path.write_bytes(data)
        return path

    def test_orders_blocks_and_maps_heading_kind(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = self._write(Path(d), _pcex_bytes())
            pages = pcex.read_pcex_pages(path, [1])
            page = pages[1]
            self.assertEqual(page.width, 2550)
            self.assertEqual(page.render_dpi, 300)
            kinds = [region["kind"] for region in page.regions]
            self.assertEqual(kinds, ["heading", "body", "body"])
            # order attribute wins over document order
            ys = [region["y0"] for region in page.regions]
            self.assertEqual(ys, [50, 150, 300])

    def test_census_regions_are_int_and_dense(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = self._write(Path(d), _pcex_bytes())
            regions = pcex.read_pcex_pages(path, [1])[1].census_regions()
            self.assertEqual([r["region_index"] for r in regions], [0, 1, 2])
            geo = regions[0]["geometry"]
            self.assertIsInstance(geo["x0"], int)
            self.assertEqual(geo["unit"], "px")
            page_census = census.build_census_from_regions(
                page_id="a" * 64, source_sha256="b" * 64,
                image_sha256="c" * 64, annotator_id="census-seed",
                regions=regions)
            self.assertEqual(len(page_census.entries), 3)

    def test_page_without_blocks_gets_one_review_region(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = self._write(Path(d), _pcex_bytes(with_page2_blocks=False))
            regions = pcex.read_pcex_pages(path, [2])[2].census_regions()
            self.assertEqual(len(regions), 1)
            self.assertIn("census review required", regions[0]["note"])
            self.assertEqual(regions[0]["geometry"]["x1"], 2550)

    def test_missing_page_raises(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = self._write(Path(d), _pcex_bytes())
            with self.assertRaises(schema.ContractError):
                pcex.read_pcex_pages(path, [3])

    def test_not_a_zip_raises(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "raw.pcex"
            path.write_bytes(b"not a zip")
            with self.assertRaises(schema.ContractError):
                pcex.read_pcex_pages(path, [1])


class ReadPageText(unittest.TestCase):
    def test_text_is_reading_ordered_and_newline_joined(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "raw.pcex"
            path.write_bytes(_pcex_bytes())
            text = pcex.read_page_text(path, [1, 2])
            self.assertEqual(text[1], "শিরোনাম\nপ্রথম\nদ্বিতীয়")
            self.assertEqual(text[2], "পরের পাতা")

    def test_empty_page_is_empty_string(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "raw.pcex"
            path.write_bytes(_pcex_bytes(with_page2_blocks=False))
            self.assertEqual(pcex.read_page_text(path, [2]), {2: ""})


class OcrPagesCli(unittest.TestCase):
    def test_assembles_b0_input_from_provenance(self):
        import json
        import tempfile
        from pathlib import Path

        from pdf_craft_tool.research import __main__ as cli

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            job = root / "jobs" / "job-x" / "work" / "ocr" / "hash"
            job.mkdir(parents=True)
            (job / "raw.pcex").write_bytes(_pcex_bytes())
            study = root / "study"
            study.mkdir()
            pid1, pid2 = "a" * 64, "b" * 64
            (study / "pilot_provenance.json").write_text(json.dumps({
                "families": [{
                    "job_id": "job-x",
                    "pages": [
                        {"page_number": 1, "page_id": pid1,
                         "image_sha256": "c" * 64},
                        {"page_number": 2, "page_id": pid2,
                         "image_sha256": "d" * 64},
                    ],
                }],
            }), encoding="utf-8")
            code = cli.main(["ocr-pages", "--study-root", str(study),
                            "--jobs-dir", str(root / "jobs")])
            self.assertEqual(code, 0)
            pages = json.loads(
                (study / "ocr_pages.json").read_text(encoding="utf-8"))
            self.assertEqual([p["page_id"] for p in pages], [pid1, pid2])
            self.assertIn("প্রথম", pages[0]["ocr_text"])
            self.assertEqual(pages[0]["image_ref"], "c" * 64)


if __name__ == "__main__":
    unittest.main()
