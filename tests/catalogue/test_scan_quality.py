"""Tests for scan-quality scoring.

Everything here runs without Tesseract or a PDF: the OCR subprocess and the
page renderer are both replaced with fakes, so these tests pass on machines
that have neither poppler nor trained data installed.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pdf_craft.catalogue import scan_quality
from pdf_craft.catalogue.scan_quality import (
    ScanQuality, measure_document, page_confidence,
)

_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _tsv_row(conf: int, text: str, word_num: int = 1) -> str:
    return f"5\t1\t1\t1\t1\t{word_num}\t0\t0\t10\t10\t{conf}\t{text}"


def _tsv(conf_texts: list[tuple[int, str]]) -> str:
    rows = [_tsv_row(conf, text, number) for number, (conf, text) in enumerate(conf_texts, start=1)]
    return "\n".join([_HEADER, *rows])


def _run_result(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=0)


def test_page_confidence_parses_tsv_mean():
    # Block-level rows (conf -1) and blank words are bookkeeping, not scored.
    tsv = _tsv([(90, "hello"), (60, "world"), (-1, ""), (50, "   "), (30, "again")])
    fake_binary = "/usr/bin/tesseract"
    with (
        patch.object(scan_quality, "TESSERACT_BINARY", fake_binary),
        patch.object(scan_quality, "MIN_WORDS", 3),
        patch.object(scan_quality.subprocess, "run", return_value=_run_result(tsv)) as run,
    ):
        mean, count = page_confidence("/tmp/page.png")
    assert count == 3
    assert mean == pytest.approx((90 + 60 + 30) / 3)
    argv = run.call_args.args[0]
    assert argv[:3] == [fake_binary, "/tmp/page.png", "stdout"]
    # TSV output is requested with the -c flag rather than the "tsv" config
    # file: a config file can silently fail to be found (e.g. a tessdata
    # cache with no configs/ directory) and tesseract falls back to plain
    # text at exit code 0, with no error to catch.
    assert "tessedit_create_tsv=1" in argv


def test_page_confidence_rejects_thin_evidence():
    # Two confident words say nothing about a whole page of Bengali type.
    tsv = _tsv([(95, "hello"), (96, "world")])
    with (
        patch.object(scan_quality, "TESSERACT_BINARY", "/usr/bin/tesseract"),
        patch.object(scan_quality.subprocess, "run", return_value=_run_result(tsv)),
    ):
        assert scan_quality.MIN_WORDS > 2
        assert page_confidence("/tmp/page.png") == (None, 0)


def test_page_confidence_timeout_is_unmeasurable():
    with (
        patch.object(scan_quality, "TESSERACT_BINARY", "/usr/bin/tesseract"),
        patch.object(
            scan_quality.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="tesseract", timeout=120),
        ),
    ):
        assert page_confidence("/tmp/page.png") == (None, 0)


def test_page_confidence_without_tesseract_is_unmeasurable():
    with patch.object(scan_quality, "TESSERACT_BINARY", None):
        assert page_confidence("/tmp/page.png") == (None, 0)


def test_measure_document_weights_pages_by_word_count():
    # A dense page is more evidence than a sparse one: the mean must be 50,
    # the word-weighted value, not the unweighted 60.
    images = {5: MagicMock(), 50: MagicMock()}
    with (
        patch.object(scan_quality, "render_pages", return_value=images),
        patch.object(
            scan_quality, "page_confidence", side_effect=[(80.0, 100), (40.0, 300)]
        ),
    ):
        result = measure_document(7, "/tmp/book.pdf", 100)
    assert isinstance(result, ScanQuality)
    assert result.document_id == 7
    assert result.mean_confidence == pytest.approx(50.0)
    assert result.word_count == 400
    assert result.pages_measured == 2


def test_measure_document_survives_render_failure():
    with patch.object(scan_quality, "render_pages", side_effect=RuntimeError("boom")):
        result = measure_document(9, "/tmp/book.pdf", 100)
    assert result.mean_confidence is None
    assert result.word_count == 0
    assert result.pages_measured == 0


def test_measure_document_never_samples_covers():
    # Page 0 is the cover and page 1 the scraper banner; neither is body text.
    seen: list[list[int]] = []

    def fake_render(pdf_path, page_indexes, dpi=300):
        seen.append(list(page_indexes))
        return {}

    with patch.object(scan_quality, "render_pages", side_effect=fake_render):
        measure_document(11, "/tmp/book.pdf", 200)
    assert seen, "render_pages was never called"
    for indexes in seen:
        assert indexes, "no interior pages were sampled"
        assert all(index >= 2 for index in indexes)
