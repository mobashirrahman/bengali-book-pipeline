import json
from unittest.mock import Mock
from zipfile import ZipFile, ZIP_STORED

import pytest
from PIL import Image

from pdf_craft import TesseractOCRLocalConfig
from pdf_craft.common import save_xml
from pdf_craft.extractor.chapter.chapter import Chapter, ParagraphLayout, BlockLayout, encode
from pdf_craft.extractor.chapter.jointer import Jointer
from pdf_craft.extractor.chapter.mergeable import check_mergeable
from pdf_craft.pdf.tesseract import TesseractPageExtractor, _Candidate
from pdf_craft.pdf.types import PageLayout
from pdf_craft.renderer.epub.renderer import EpubRenderer
from pdf_craft.renderer.markdown.bundle import render_markdown_bundle, split_chunks
from pdf_craft.transformer.proofreader import ConservativeProofreader, validate_edits
from pdf_craft_tool.book import parse_pages, fingerprint, _stage, verify_artifact
from tests.extraction_helpers import make_extraction


class _TestEncoding:
    def encode(self, text, **kwargs):
        return list(text)


def proposal(before="মেয়েঢিই", after="মেয়েটিই", **values):
    return json.dumps({"edits": [{"before": before, "after": after, "kind": "ocr_glyph", "confidence": .99, **values}]})


def chapter(text="মেয়েঢিই ঘরে গেল।"):
    return Chapter(1, 0, [ParagraphLayout("text", -1, [BlockLayout(1, 0, (5, 5, 90, 90), [text])])])


def test_exact_small_correction_and_protected_word():
    text = "মেয়েঢিই ঘরে গেল।"
    assert validate_edits(text, proposal())[0] == "মেয়েটিই ঘরে গেল।"
    assert validate_edits(text, proposal(), ("মেয়েঢিই",))[0] == text


@pytest.mark.parametrize("before,after", [("১২", "১৩"), ("ঘরে", "বাড়িতে"), ("ঢি", "টি"), ("মেয়েঢিই", "মেয়ে টি"), ("মেয়েঢিই", "")])
def test_rejects_unsafe_edits(before, after):
    text = "মেয়েঢিই ঘরে গেল। ১২"
    assert validate_edits(text, proposal(before, after))[0] == text


@pytest.mark.parametrize("response", ["```json\n{}\n```", '{"edits":{},"text":"invented"}', '{"edits":[null]}', '[]'])
def test_bad_schema_fails_closed(response):
    with pytest.raises(ValueError):
        validate_edits("বই", response)


def test_duplicate_and_overlapping_edits_fail():
    assert validate_edits("মেয়েঢিই মেয়েঢিই", proposal())[0] == "মেয়েঢিই মেয়েঢিই"
    edit = json.loads(proposal())["edits"][0]
    with pytest.raises(ValueError):
        validate_edits("মেয়েঢিই", json.dumps({"edits": [edit, edit]}))


def test_proofreader_keeps_original_and_resumes_cached_response(tmp_path):
    request = Mock(return_value=proposal())
    transformer = ConservativeProofreader(request, model_identity="test@digest", audit_path=tmp_path,
                                          verify_edit=lambda *_: {"supported": True})
    original = chapter()
    output = transformer.transform(original)
    assert original.layouts[0].blocks[0].content == ["মেয়েঢিই ঘরে গেল।"]
    assert output.layouts[0].blocks[0].content == ["মেয়েটিই ঘরে গেল।"]
    transformer.transform(original)
    assert request.call_count == 1
    assert json.loads((tmp_path / "p000001-b0000.json").read_text())["status"] == "changed"


def test_bad_response_or_review_only_never_changes_text(tmp_path):
    for response in ("not JSON", proposal()):
        transformer = ConservativeProofreader(lambda *_: response, model_identity=response,
                                               audit_path=tmp_path, apply_edits=False)
        original = chapter()
        assert transformer.transform(original) == original


def test_confident_blocks_not_sent_to_model(tmp_path):
    (tmp_path / "page_1.json").write_text('{"needs_review":false,"flagged_orders":[]}')
    request = Mock()
    transformer = ConservativeProofreader(request, model_identity="test", audit_path=tmp_path / "audit",
                                          diagnostics={1: {"needs_review": False, "flagged_orders": []}})
    assert transformer.transform(chapter()) == chapter()
    request.assert_not_called()


def test_model_confidence_alone_does_not_authorize_an_edit(tmp_path):
    transformer = ConservativeProofreader(lambda *_: proposal(), model_identity="test", audit_path=tmp_path)
    assert transformer.transform(chapter()) == chapter()


def test_bengali_boundaries_and_nonadjacent_pages():
    assert not check_mergeable(["গেল।"], ["তারপর"])
    assert not check_mergeable(["গেল॥"], ["তারপর"])
    def layout(text):
        return [PageLayout("text", (0, 0, 100, 100), text, 0, None)]
    assert len(list(Jointer([(1, layout("একটি")), (3, layout("বই"))]).execute())) == 2
    result = list(Jointer([(1, layout("একটি")), (2, layout("বই"))]).execute())
    assert "".join(block.content[0] for block in result[0].blocks) == "একটি বই"


def test_tsv_preserves_words_and_normalizes_danda():
    tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    tsv += "5\t1\t1\t1\t1\t1\t5\t5\t40\t15\t97\tবই\n"
    tsv += "5\t1\t1\t1\t1\t2\t48\t5\t3\t15\t90\t।\n"
    words = TesseractPageExtractor._parse_tsv(tsv)
    layouts = TesseractPageExtractor._layouts_from_words(words, (100, 100))
    assert layouts[0].text == "বই।"
    assert layouts[0].det == (5, 5, 51, 20)


def test_blank_and_omitted_text_quality_gate():
    adapter = TesseractPageExtractor(TesseractOCRLocalConfig())
    assert adapter._quality_result([], 100, 1, 1, 0)[0]
    assert not adapter._quality_result([], 100, 1, 0, .1)[0]
    with pytest.raises(RuntimeError):
        adapter._parse_tsv("unexpected text output")


def test_retry_prefers_passing_candidate_not_higher_confidence(tmp_path):
    adapter = TesseractPageExtractor(TesseractOCRLocalConfig(retry_with_autocontrast=False))
    adapter.load_ocr_model = Mock()
    weak = _Candidate((), (), 99, 1, 0, .1, False, "omission")
    good = _Candidate((), (), 80, 1, 1, 0, True, "blank page")
    adapter._recognize_candidate = Mock(side_effect=[weak, good])
    page = adapter.recognize(Image.new("RGB", (50, 50), "white"), 1, False, None, lambda: False)
    assert page.diagnostics["needs_review"] is False
    assert len(page.diagnostics["attempts"]) == 2


def test_epub_bengali_and_markdown_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr("pdf_craft.renderer.markdown.bundle.get_encoding", lambda _: _TestEncoding())
    extraction = make_extraction(tmp_path / "source", with_toc=True)
    save_xml(encode(chapter()), tmp_path / "source/chapters/chapter_1.xml")
    epub_path = tmp_path / "book.epub"
    EpubRenderer().render(extraction, epub_path, lan="bn")
    with ZipFile(epub_path) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.infolist()[0].compress_type == ZIP_STORED
        opf = next(name for name in archive.namelist() if name.endswith(".opf"))
        assert b"<dc:language>bn</dc:language>" in archive.read(opf)
        xhtml = [archive.read(name).decode() for name in archive.namelist() if name.endswith(".xhtml")]
        assert any("মেয়েঢিই ঘরে গেল।" in text for text in xhtml)
        assert all('xml:lang="bn"' in text for text in xhtml)
    output = tmp_path / "bundle"
    render_markdown_bundle(extraction, output, source_id="abc", chunk_tokens=16)
    records = [json.loads(line) for line in (output / "chunks.jsonl").read_text().splitlines()]
    source_map = json.loads((output / "source-map.json").read_text())
    assert "".join(row["text"] for row in records) == source_map["segments"][0]["text"]
    assert all(row["pages"] == [1] for row in records)


def test_chunks_preserve_combining_marks_whitespace_and_special_tokens():
    text = "  বাংলা ভাষার বই।\n\n" * 20 + "<|endoftext|>"
    chunks = list(split_chunks(text, 16, _TestEncoding()))
    assert "".join(text[start:end] for start, end in chunks) == text
    assert all(start == 0 or text[start - 1].isspace() for start, _ in chunks)


def test_page_ranges_and_stage_identity(tmp_path):
    assert parse_pages("4-6,2,6", 10) == [2, 4, 5, 6]
    with pytest.raises(ValueError):
        parse_pages("0-3", 10)
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert _stage(tmp_path, "ocr", {"source": "a"}) != _stage(tmp_path, "ocr", {"source": "b"})


def test_artifact_corruption_is_not_silently_reused(tmp_path):
    path = tmp_path / "book.pcex"
    path.write_bytes(b"original")
    verify_artifact(path)
    path.write_bytes(b"truncated")
    with pytest.raises(ValueError, match="Cached artifact changed"):
        verify_artifact(path)


@pytest.mark.parametrize("values", [{"minimum_confidence": float("nan")}, {"minimum_ink_coverage": float("inf")},
                                  {"page_segmentation_modes": (0,)}, {"language": ""}])
def test_invalid_ocr_settings_fail_early(values):
    with pytest.raises(ValueError):
        TesseractOCRLocalConfig(**values)


def test_weak_page_retains_original_image(tmp_path):
    from pdf_craft.pdf.page_extractor import PageExtractorNode
    from pdf_craft.pdf.types import Page
    node = PageExtractorNode(TesseractOCRLocalConfig())
    node._page_extractor = Mock()
    node._page_extractor.recognize.return_value = Page(
        index=1, image=None, body_layouts=[], footnotes_layouts=[], input_tokens=0, output_tokens=0,
        diagnostics={"needs_review": True, "reason": "no text detected"})
    hub = Mock()
    hub.clip.return_value = "source-image-hash"
    page = node._tesseract_image2page(Image.new("RGB", (100, 100)), 1, hub, "tiny", False,
                                     False, None, None, None, None, lambda: False)
    assert page.body_layouts[0].ref == "image"
    assert page.body_layouts[0].hash == "source-image-hash"


def test_crop_verifier_requires_two_matching_readings(tmp_path):
    from pdf_craft.pdf.crop_verifier import TesseractCropVerifier
    from pdf_craft.pdf.tesseract import _Word
    block = chapter().layouts[0].blocks[0]
    diagnostics = {1: {"words": [{"text": "মেয়েঢিই", "left": 5, "top": 5, "width": 40, "height": 20}]}}
    verifier = TesseractCropVerifier(tmp_path / "source.pdf", TesseractOCRLocalConfig(), diagnostics, tmp_path)
    verifier.document = Mock()
    verifier.document.render_page.return_value = Image.new("RGB", (100, 100), "white")
    word = _Word(1, 1, 1, 5, 5, 40, 20, 95, "মেয়েটিই")
    good = _Candidate((word,), (), 95, 1, 1, .1, True, "passed")
    bad = _Candidate((), (), 95, 1, 1, .1, False, "omission")
    verifier.adapter._recognize_candidate = Mock(side_effect=[good, bad])
    assert not verifier(block, json.loads(proposal())["edits"][0])["supported"]
    verifier.adapter._recognize_candidate = Mock(side_effect=[good, good])
    assert verifier(block, json.loads(proposal())["edits"][0])["supported"]
    verifier.close()


def test_benchmark_reports_true_levenshtein_and_bengali_word_tokens():
    pytest.importorskip("rapidfuzz")
    from pdf_craft_tool.benchmark import score, words
    assert words("বাংলা ১২। বই") == ["বাংলা", "১২", "বই"]
    result = score("একটি বই", "একটি নতুন বই")
    assert result["character_edits"] == 5
    assert result["word_edits"] == 1


def test_epub_keeps_linked_footnote_markers(tmp_path):
    from epub_generator import Mark
    from pdf_craft.extractor.chapter.chapter import Reference
    from pdf_craft.renderer.epub.render import _convert_chapter_to_epub
    value = chapter()
    reference = Reference(1, 5, "1", chapter("টীকা").layouts)
    value.layouts[0].blocks[0].content.append(reference)
    converted = _convert_chapter_to_epub(value, tmp_path, True, {(1, 5): 1})
    assert any(isinstance(item, Mark) for item in converted.elements[0].content)
    assert converted.footnotes[0].id == 1
