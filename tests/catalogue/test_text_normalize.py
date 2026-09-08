"""Tests for pdf_craft.catalogue.text_normalize."""

from pdf_craft.catalogue.text_normalize import (
    fold_bengali,
    normalize_name,
    normalize_text,
    repair_mojibake,
)


def test_yya_precomposed_and_decomposed_match():
    # Headline case: য় as precomposed U+09DF vs য (U+09AF) + nukta (U+09BC)
    # render identically but never compare equal without NFC.
    # (Built from escapes so the two forms cannot collapse in the editor.)
    precomposed = "হ\u09c1মা\u09df\u09c1ন আজাদ"
    decomposed = "হ\u09c1মা\u09af\u09bc\u09c1ন আজাদ"
    assert decomposed != precomposed  # guard: the test inputs really differ
    assert normalize_name(precomposed) == normalize_name(decomposed)


def test_nfc_composes_decomposed_form():
    assert normalize_text("\u09af\u09bc") == normalize_text("\u09df")


def test_zero_width_space_removed():
    assert normalize_text("রবী\u200bন্দ্রনাথ") == normalize_text("রবীন্দ্রনাথ")


def test_zero_width_joiners_removed():
    assert normalize_text("অ\u200cআ") == normalize_text("অআ")
    assert normalize_text("অ\u200dআ") == normalize_text("অআ")


def test_soft_hyphen_and_bom_removed():
    assert normalize_text("\ufeffরবী\u00adন্দ্র") == normalize_text("রবীন্দ্র")


def test_bidi_controls_removed():
    assert normalize_text("\u202aরবীন্দ্র\u202c") == normalize_text("রবীন্দ্র")
    assert normalize_text("\u202btest\u202e") == "test"


def test_casefold_not_lower():
    # casefold folds ß -> ss; lower() would not.
    assert normalize_text("STRASSE") == "strasse"
    assert normalize_text("Straße") == "strasse"


def test_punctuation_runs_become_single_space():
    assert normalize_text("রবীন্দ্র -- নাথ!!") == "রবীন্দ্র নাথ"


def test_bengali_vowel_signs_survive():
    # Regression guard: vowel signs are not alnum, but the Bengali block is
    # kept explicitly, so words must not be shredded to bare consonants.
    assert normalize_text("রবীন্দ্রনাথ") == "রবীন্দ্রনাথ"


def test_bengali_digits_convert():
    assert normalize_text("রচনাবলী ০২") == normalize_text("রচনাবলী 02")
    assert normalize_text("০১২৩৪৫৬৭৮৯") == "0123456789"


def test_mojibake_round_trip():
    original = "রবীন্দ্রনাথের গল্প"
    mojibake = original.encode("utf-8").decode("latin-1")
    assert repair_mojibake(mojibake) == original


def test_accented_latin_name_not_mojibake():
    # Non-regression: Émile Zola's bytes are invalid UTF-8, so the input
    # must come back untouched.
    assert repair_mojibake("Émile Zola") == "Émile Zola"


def test_clean_bengali_not_mojibake():
    assert repair_mojibake("রবীন্দ্রনাথ") == "রবীন্দ্রনাথ"


def test_last_first_comma_flipped():
    assert normalize_name("Chakraborty, Pranabesh") == "pranabesh chakraborty"


def test_comma_with_empty_side_kept():
    assert normalize_name("Chakraborty,") == normalize_text("Chakraborty,")
    assert normalize_name(", Pranabesh") == normalize_text(", Pranabesh")


def test_multiple_commas_not_flipped():
    assert normalize_name("a, b, c") == normalize_text("a, b, c")


def test_bengali_honorific_stripped():
    assert normalize_name("শ্রী স্বপনকুমার") == normalize_text("স্বপনকুমার")
    assert normalize_name("ড. ইসরাইল খান") == normalize_text("ইসরাইল খান")
    assert normalize_name("বিচারপতি মুহম্মদ হাবিবুর রহমান") == normalize_text(
        "মুহম্মদ হাবিবুর রহমান"
    )


def test_honorific_not_stripped_as_substring():
    # শ্রীকান্ত starts with শ্রী but is a different token; it must survive.
    assert normalize_name("শ্রীকান্ত") == normalize_text("শ্রীকান্ত")


def test_english_honorific_and_role_stripped():
    assert normalize_name("Professor Samiran Kumar Saha (Editor)") == (
        "samiran kumar saha"
    )
    assert normalize_name("Dr. John Smith (Translator)") == "john smith"


def test_bengali_disambiguation_marker_stripped():
    assert normalize_name("রবীন্দ্রনাথ (২)") == normalize_text("রবীন্দ্রনাথ")
    assert normalize_name("রবীন্দ্রনাথ (2)") == normalize_text("রবীন্দ্রনাথ")


def test_ocr_aa_ii_fold_same_key():
    # From the measured tesseract pair: দেবতা read as দেবতী.
    assert fold_bengali("দেবতী") == fold_bengali("দেবতা")


def test_ocr_ra_ba_fold_same_key():
    # From the measured tesseract pair: রবীন্দ্রনাথ read as ববীন্দ্রনাথ.
    assert fold_bengali("ববীন্দ্রনাথ") == fold_bengali("রবীন্দ্রনাথ")


def test_ocr_sibilant_na_bha_ja_folds():
    assert fold_bengali("শষস") == fold_bengali("সসস")
    assert fold_bengali("ণ") == fold_bengali("ন")
    assert fold_bengali("য") == fold_bengali("জ")
    assert fold_bengali("ভ") == fold_bengali("ব")


def test_fold_drops_vowel_signs():
    assert fold_bengali("কি") == fold_bengali("ক")
    assert fold_bengali("কু") == fold_bengali("ক")


def test_fold_is_lossy_and_differs_from_normalize():
    # Sanity: folding merges what normalize_text keeps apart.
    assert normalize_text("স") != normalize_text("শ")
    assert fold_bengali("স") == fold_bengali("শ")


def test_empty_and_whitespace_input():
    for func in (normalize_text, normalize_name, fold_bengali, repair_mojibake):
        assert func("") == ""
        assert func("   ") == ""
        assert func("\t\n ") == ""
