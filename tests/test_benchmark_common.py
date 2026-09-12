"""Regression tests for workflow/scripts/common.assign_word_lines.

The helper is the NaN-tolerant line assignment behind the bbOCR page-path
fix (upstream ApsisOCR.process_boxes 0.0.7 crashed with "cannot convert
float NaN to integer" when a word box overlapped no line box).
"""

import importlib.util
from pathlib import Path

COMMON_PATH = (
    Path(__file__).resolve().parent.parent / "workflow" / "scripts" / "common.py"
)


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "benchmark_common_under_test", COMMON_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _localize_like_apsis(word, lines):
    """Minimal localize_box stand-in: index of first containing line else None."""
    x1, y1, x2, y2 = word
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    for idx, (lx1, ly1, lx2, ly2) in enumerate(lines):
        if lx1 <= cx <= lx2 and ly1 <= cy <= ly2:
            return idx
    return None


def test_hits_pass_through_unchanged():
    common = _load_common()
    lines = [[0, 0, 100, 10], [0, 20, 100, 30]]
    words = [[5, 1, 20, 9], [10, 21, 30, 29]]
    assert common.assign_word_lines(words, lines, _localize_like_apsis) == [0, 1]


def test_orphan_word_falls_back_to_nearest_line():
    common = _load_common()
    lines = [[0, 0, 100, 10], [0, 100, 100, 110]]
    # centered between the lines but closer to the second one
    assert common.assign_word_lines(
        [[0, 60, 100, 70]], lines, _localize_like_apsis
    ) == [1]
    assert common.assign_word_lines(
        [[0, 11, 100, 19]], lines, _localize_like_apsis
    ) == [0]


def test_no_lines_orders_words_top_to_bottom():
    common = _load_common()
    words = [[0, 50, 10, 60], [0, 5, 10, 15], [0, 90, 10, 100]]
    assert common.assign_word_lines(words, [], _localize_like_apsis) == [1, 0, 2]


def test_empty_words():
    common = _load_common()
    assert common.assign_word_lines([], [[0, 0, 10, 10]], _localize_like_apsis) == []
    assert common.assign_word_lines([], [], _localize_like_apsis) == []
