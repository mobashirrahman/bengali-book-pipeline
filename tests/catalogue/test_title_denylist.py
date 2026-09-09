from pdf_craft.catalogue.title_denylist import (
    DENYLISTED_AUTHORS,
    DENYLISTED_TITLES,
    clean_authors,
    is_denylisted_author,
    is_denylisted_title,
)


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


# --- authors -------------------------------------------------------------


def test_known_junk_author_is_denylisted():
    assert is_denylisted_author("সমগ্র ১")
    assert is_denylisted_author("রচনাসমগ্র-২")
    assert not is_denylisted_author("হুমায়ূন আহমেদ")


def test_clean_authors_drops_entry_identical_to_title():
    # id 5849 in the audited catalogue: title "শুভ্র" is a genuine book
    # title, but "শুভ্র" also leaked into the author list.
    assert clean_authors("শুভ্র", ["শুভ্র", "হুমায়ূন আহমেদ"]) == ["হুমায়ূন আহমেদ"]


def test_clean_authors_drops_volume_fragment():
    # id 4961: title "সুবোধ ঘোষ" (a person's name used as title), authors
    # ["সমগ্র ১", "সুবোধ ঘোষ"] -- "সমগ্র ১" ("Collection 1") is not a name.
    assert clean_authors("সুবোধ ঘোষ", ["সমগ্র ১", "সুবোধ ঘোষ"]) == []


def test_clean_authors_keeps_a_real_second_author():
    # id 4254: a genuine two-name editor credit must survive untouched.
    result = clean_authors("শক্তি চট্টোপাধ্যায়", ["জয় গোস্বামী ও অংশুমান কর", "শক্তি চট্টোপাধ্যায়"])
    assert result == ["জয় গোস্বামী ও অংশুমান কর"]


def test_clean_authors_keeps_ordinary_authors_untouched():
    assert clean_authors("কোনো বই", ["ক", "খ", "গ"]) == ["ক", "খ", "গ"]


def test_author_denylist_has_no_accidental_duplicates_or_empty_entries():
    assert "" not in DENYLISTED_AUTHORS
    assert len(DENYLISTED_AUTHORS) == len({a.strip() for a in DENYLISTED_AUTHORS})
