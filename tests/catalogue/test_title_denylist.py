from pdf_craft.catalogue.title_denylist import DENYLISTED_TITLES, is_denylisted_title


def test_known_junk_title_is_denylisted():
    assert is_denylisted_title("Masud Rana Pdf")
    assert is_denylisted_title("Title Page")


def test_genuine_title_is_not_denylisted():
    # A real, specific book title -- must never be filtered.
    assert not is_denylisted_title("চাঁদের অমাবস্যা")
    assert not is_denylisted_title("রবীন্দ্র রচনাবলী")  # legitimate recurring series title


def test_matching_is_exact_after_stripping_whitespace():
    assert is_denylisted_title("  Title Page  ")
    assert not is_denylisted_title("Title Page 2")


def test_denylist_has_no_accidental_duplicates_or_empty_entries():
    assert "" not in DENYLISTED_TITLES
    assert len(DENYLISTED_TITLES) == len({t.strip() for t in DENYLISTED_TITLES})
