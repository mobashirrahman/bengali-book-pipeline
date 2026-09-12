import unicodedata
from pathlib import Path

import pytest

from pdf_craft_tool.external_eval import (
    load_mozhi_gt,
    mozhi_references,
    parse_page_xml,
    reid_references,
    score_hypotheses,
)

PAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
 <Page imageFilename="sample.tif">
  <TextRegion id="r1">
   <TextLine id="l1"><TextEquiv><Unicode>বাংলা ভাষা</Unicode></TextEquiv></TextLine>
   <TextLine id="l2"><TextEquiv><Unicode>সুন্দর</Unicode></TextEquiv></TextLine>
  </TextRegion>
 </Page>
</PcGts>
"""


def test_parse_page_xml_reading_order(tmp_path):
    path = tmp_path / "sample.xml"
    path.write_text(PAGE_XML, encoding="utf-8")
    assert parse_page_xml(path) == ["বাংলা ভাষা", "সুন্দর"]


def test_parse_page_xml_region_level_fallback(tmp_path):
    path = tmp_path / "region.xml"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
 <Page imageFilename="p.tif">
  <TextRegion id="r1"><TextEquiv><Unicode>পদ্মমুঞ্জরি ।
নবীন যৌবন</Unicode></TextEquiv></TextRegion>
 </Page>
</PcGts>""",
        encoding="utf-8",
    )
    assert parse_page_xml(path) == ["পদ্মমুঞ্জরি ।", "নবীন যৌবন"]


def test_reid_references_pairs_xml_with_image(tmp_path):
    (tmp_path / "p1.xml").write_text(PAGE_XML, encoding="utf-8")
    (tmp_path / "p1.tif").write_bytes(b"fake-tiff")
    (tmp_path / "empty.xml").write_text(PAGE_XML.replace("বাংলা ভাষা", "").replace("সুন্দর", ""), encoding="utf-8")
    items = reid_references(tmp_path)
    assert len(items) == 1
    assert items[0]["id"] == "reid:p1"
    assert items[0]["image"].endswith("p1.tif")
    assert items[0]["nlines"] == 2


def test_mozhi_gt_and_references(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "w1.jpeg").write_bytes(b"fake")
    (tmp_path / "test_gt.txt").write_text("w1.jpeg\tহৃদয়ে\nbad-line\n", encoding="utf-8")
    assert load_mozhi_gt(tmp_path / "test_gt.txt") == {
        "w1.jpeg": unicodedata.normalize("NFC", "হৃদয়ে")
    }
    items = mozhi_references(tmp_path, "test")
    assert len(items) == 1 and items[0]["reference"] == unicodedata.normalize("NFC", "হৃদয়ে")


def test_score_hypotheses_perfect_and_missing():
    items = [
        {"id": "a", "reference": "বাংলা"},
        {"id": "b", "reference": "ভাষা"},
    ]
    result = score_hypotheses(items, {"a": "বাংলা", "b": "ভাষা"})
    assert result["cer"] == 0 and result["wer"] == 0 and result["missing"] == 0
    result = score_hypotheses(items, {"a": "বাংলা"})
    assert result["missing"] == 1 and result["cer"] > 0
    with pytest.raises(ValueError):
        score_hypotheses([], {})
