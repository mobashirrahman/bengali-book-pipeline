"""Tests for pdf_craft.catalogue.filename_parser (per-source templates)."""

from pdf_craft.catalogue.filename_parser import ParsedFilename, parse_filename


def test_granthagara_bilingual_title_no_author():
    result = parse_filename(
        "data/incoming-scraped/granthagara/books/"
        "গণদেবতা বাংলা বই পিডিএফ ডাউনলোড_ Ganadebata Bengali Book PDF Download.pdf"
    )
    assert result.titles == ("গণদেবতা", "Ganadebata")
    assert result.authors == ()
    assert result.source == "granthagara"
    assert result.confidence == 0.8
    assert not result.is_not_a_book


def test_granthagara_edition_bengali_digits():
    result = parse_filename(
        "data/incoming-scraped/granthagara/books/"
        "উপন্যাস [সংস্করণ-৫] বাংলা বই পিডিএফ ডাউনলোড_ Novel Bengali Book PDF Download.pdf"
    )
    assert result.edition == "5"
    assert result.titles == ("উপন্যাস", "Novel")


def test_granthagara_edition_english():
    result = parse_filename(
        "data/incoming-scraped/granthagara/books/"
        "উপন্যাস [Ed. 5] বাংলা বই পিডিএফ ডাউনলোড_ Novel Bengali Book PDF Download.pdf"
    )
    assert result.edition == "5"
    assert result.titles == ("উপন্যাস", "Novel")


def test_granthagara_volume_bengali():
    result = parse_filename(
        "data/incoming-scraped/granthagara/books/"
        "গল্প [ভাগ-২২] বাংলা বই পিডিএফ ডাউনলোড_ Story Bengali Book PDF Download.pdf"
    )
    assert result.volume == "22"
    assert result.titles == ("গল্প", "Story")


def test_granthagara_volume_english():
    result = parse_filename(
        "data/incoming-scraped/granthagara/books/"
        "গল্প [Pt. 23] বাংলা বই পিডিএফ ডাউনলোড_ Story Bengali Book PDF Download.pdf"
    )
    assert result.volume == "23"
    assert result.titles == ("গল্প", "Story")


def test_bengaliebook_both_scripts():
    result = parse_filename(
        "data/incoming-scraped/bengaliebook/books/"
        "অন্যদিন-হুমায়ূন আহমেদ (Anyadin by Humayun Ahmed).pdf"
    )
    assert result.titles == ("অন্যদিন", "Anyadin")
    assert result.authors == ("হুমায়ূন আহমেদ", "Humayun Ahmed")
    assert result.confidence == 1.0


def test_bengaliebook_tolerates_space_before_hyphen_and_capital_by():
    result = parse_filename(
        "data/incoming-scraped/bengaliebook/books/"
        "অন্যদিন - হুমায়ূন আহমেদ (Anyadin By Humayun Ahmed).pdf"
    )
    assert result.titles == ("অন্যদিন", "Anyadin")
    assert result.authors == ("হুমায়ূন আহমেদ", "Humayun Ahmed")


def test_amarbooks_author_first():
    result = parse_filename(
        "data/incoming-scraped/amarbooks/books/"
        "Tanjim Rahman is waiting to be download!!! - Dark Fall.pdf"
    )
    assert result.titles == ("Dark Fall",)
    assert result.authors == ("Tanjim Rahman",)
    assert result.confidence == 1.0


def test_amarbooks_unknown_author_is_title_only():
    result = parse_filename(
        "data/incoming-scraped/amarbooks/books/"
        "Unknown Author - The Alchemist is waiting to be download!!!.pdf"
    )
    assert result.titles == ("The Alchemist",)
    assert result.authors == ()
    assert result.confidence == 1.0


def test_rarebooksociety_author_first_split():
    result = parse_filename(
        "data/incoming-scraped/rarebooksociety/books/"
        "Henry Morris - The Governors-Generals of India.pdf"
    )
    # FIRST " - " split: the title keeps its own hyphen.
    assert result.authors == ("Henry Morris",)
    assert result.titles == ("The Governors-Generals of India",)
    assert result.confidence == 0.8


def test_amarboi_author_first_with_porbo_volume():
    result = parse_filename(
        "data/incoming-scraped/amarboi/books/মিশেল ফুকো - যৌনতার ইতিহাস পর্ব ০২.pdf"
    )
    assert result.authors == ("মিশেল ফুকো",)
    assert result.titles == ("যৌনতার ইতিহাস",)
    assert result.volume == "02"
    assert result.confidence == 0.8


def test_allbanglaboi_by_anchor_overemits_titles():
    result = parse_filename(
        "data/incoming-scraped/allbanglaboi/books/"
        "25 Ti sera rahasya by Sunil Gangopadhyay - ২৫টি সেরা রহস্য - সুনিল গঙ্গোপাধ্যায়.pdf"
    )
    assert result.authors == ("Sunil Gangopadhyay",)
    assert "25 Ti sera rahasya" in result.titles
    assert "২৫টি সেরা রহস্য" in result.titles
    assert "সুনিল গঙ্গোপাধ্যায়" in result.titles
    assert result.confidence == 1.0


def test_allbanglaboi_without_by_emits_all_segments_low_confidence():
    result = parse_filename(
        "data/incoming-scraped/allbanglaboi/books/পথের পাঁচালী - বিভূতিভূষণ - উপন্যাস.pdf"
    )
    assert result.titles == ("পথের পাঁচালী", "বিভূতিভূষণ", "উপন্যাস")
    assert result.authors == ()
    assert result.confidence == 0.2


def test_banglabooks_in_placeholder_is_empty():
    result = parse_filename(
        "data/incoming-scraped/banglabooks_in/books/Bangla eBooks pdf - a1b2c3d4e5.pdf"
    )
    assert result.titles == ()
    assert result.authors == ()
    assert result.confidence == 0.0
    assert not result.is_not_a_book


def test_banglabookshelf_placeholder_is_empty():
    result = parse_filename(
        "data/incoming-scraped/banglabookshelf/books/banglabookshelf - xyz789.pdf"
    )
    assert result.titles == ()
    assert result.authors == ()
    assert result.confidence == 0.0


def test_authordir_title_first_and_person_directory_last():
    # A directory that names a person is appended as a lower-ranked candidate.
    result = parse_filename("data/নীহাররঞ্জন গুপ্ত/মুখোশ - নীহাররঞ্জন গুপ্ত.pdf")
    assert result.source == "authordir"
    assert result.titles == ("মুখোশ",)
    assert result.authors == ("নীহাররঞ্জন গুপ্ত",)
    assert result.confidence == 0.5


def test_authordir_series_directory_is_not_an_author():
    # `গোয়েন্দা সিরিজ` is "detective series", not a person.  Emitting it wrote
    # series and genre names into the author field of thousands of documents.
    result = parse_filename("data/গোয়েন্দা সিরিজ/মুখোশ - নীহাররঞ্জন গুপ্ত.pdf")
    assert result.titles == ("মুখোশ",)
    assert result.authors == ("নীহাররঞ্জন গুপ্ত",)
    assert result.directory_author is None


def test_authordir_leading_bracket_volume():
    result = parse_filename("data/উপন্যাস/(০৩) মুখোশ - নীহাররঞ্জন গুপ্ত.pdf")
    assert result.volume == "03"
    assert result.titles == ("মুখোশ",)
    assert result.authors[0] == "নীহাররঞ্জন গুপ্ত"


def test_authordir_leading_dari_number_volume():
    result = parse_filename("data/কবিতা/৯। সঞ্চয়িতা - রবীন্দ্রনাথ ঠাকুর.pdf")
    assert result.volume == "9"
    assert result.titles == ("সঞ্চয়িতা",)


def test_authordir_leading_dotted_number_volume():
    result = parse_filename("data/poems/01. Some Title - Some Author.pdf")
    assert result.volume == "01"
    assert result.titles == ("Some Title",)
    assert result.authors[0] == "Some Author"


def test_authordir_trailing_year_bengali_digits():
    result = parse_filename("data/উপন্যাস/মুখোশ - নীহাররঞ্জন গুপ্ত [১৯৮৯].pdf")
    assert result.year == "1989"
    assert result.titles == ("মুখোশ",)


def test_authordir_trailing_paren_volume():
    result = parse_filename("data/ইতিহাস/মহাভারত (25).pdf")
    assert result.volume == "25"
    assert result.titles == ("মহাভারত",)


def test_banglabook_mojibake_round_trip():
    original = "আমার সোনার বাংলা বই"
    mojibake = original.encode("utf-8").decode("latin-1")
    assert mojibake != original  # sanity: the fixture really is mojibake
    result = parse_filename(
        f"data/incoming-scraped/banglabook/books/Unknown Author - {mojibake}.pdf"
    )
    assert result.source == "banglabook"
    assert result.titles == (original,)
    assert result.authors == ()


def test_banglabook_plain_ascii_untouched():
    result = parse_filename(
        "data/incoming-scraped/banglabook/books/Unknown Author - A Plain Title.pdf"
    )
    assert result.titles == ("A Plain Title",)


def test_books_title_only():
    result = parse_filename("data/incoming-scraped/books/books/কিছু বইয়ের নাম.pdf")
    assert result.source == "books"
    assert result.titles == ("কিছু বইয়ের নাম",)
    assert result.authors == ()
    assert result.confidence == 0.2


def test_pdfporo_title_only():
    result = parse_filename("data/incoming-scraped/pdfporo/books/Random Title Here.pdf")
    assert result.source == "pdfporo"
    assert result.titles == ("Random Title Here",)
    assert result.confidence == 0.2


def test_bengali_role_marker_likhechen():
    result = parse_filename(
        "data/misc/রংপেন্সিল _ আইনস্টাইন ও ইন্দুবালা লিখেছেন হুমায়ূন আহমেদ.pdf"
    )
    assert result.authors[0] == "হুমায়ূন আহমেদ"
    assert all("হুমায়ূন আহমেদ" not in title for title in result.titles)
    assert result.confidence == 1.0


def test_unknown_author_prefix_never_returned_as_author():
    result = parse_filename(
        "data/incoming-scraped/books/books/Unknown Author - Dark Fall.pdf"
    )
    assert result.titles == ("Dark Fall",)
    assert "Unknown Author" not in result.authors
    assert all("Unknown Author" not in title for title in result.titles)


def test_hash_suffix_stripped():
    result = parse_filename(
        "data/incoming-scraped/books/books/Dark Fall - a1b2c3d4e5f6.pdf"
    )
    assert result.titles == ("Dark Fall",)


def test_bangla_pdf_boilerplate_stripped():
    result = parse_filename("data/incoming-scraped/books/books/My Title Bangla PDF.pdf")
    assert result.titles == ("My Title",)


def test_pdf_download_boilerplate_stripped():
    result = parse_filename(
        "data/incoming-scraped/books/books/My Title pdf download.pdf"
    )
    assert result.titles == ("My Title",)


def test_link_list_is_not_a_book():
    result = parse_filename("data/misc/৫০টি বইয়ের ডাউনলোড লিঙ্ক.pdf")
    assert isinstance(result, ParsedFilename)
    assert result.is_not_a_book
    assert result.titles == ()
    assert result.authors == ()


def test_hundred_plus_aggregate_is_not_a_book():
    result = parse_filename("data/misc/১০০+ বইয়ের তালিকা.pdf")
    assert result.is_not_a_book
    assert result.titles == ()


def test_preliminary_page_is_not_a_book():
    result = parse_filename("data/misc/Preliminary Page.pdf")
    assert result.is_not_a_book
    assert result.titles == ()
    assert result.authors == ()


def test_chapter_fragment_is_not_a_book():
    result = parse_filename("data/misc/Chapter 12.pdf")
    assert result.is_not_a_book


def test_amarboi_and_authordir_assign_opposite_roles():
    stem = "মিশেল ফুকো - যৌনতার ইতিহাস"
    from_amarboi = parse_filename(f"data/incoming-scraped/amarboi/books/{stem}.pdf")
    from_authordir = parse_filename(f"data/উপন্যাস/{stem}.pdf")
    # amarboi is author-first, authordir is title-first: proves per-source dispatch.
    assert from_amarboi.authors[0] == "মিশেল ফুকো"
    assert from_amarboi.titles == ("যৌনতার ইতিহাস",)
    assert from_authordir.titles == ("মিশেল ফুকো",)
    assert from_authordir.authors[0] == "যৌনতার ইতিহাস"


def test_source_key_extraction():
    assert (
        parse_filename("data/incoming-scraped/amarboi/books/x.pdf").source == "amarboi"
    )
    assert parse_filename("data/somedir/x.pdf").source == "authordir"
    assert parse_filename("x.pdf").source == "authordir"


# ---------------------------------------------------------------------------
# Directory authors: structural non-person rejection
# ---------------------------------------------------------------------------


def test_non_person_directories_are_rejected():
    """Series, genre, publisher and folder names must not become authors.

    Measured pollution from the first live backfill: `epub-staging` was written
    as the author of 1,626 documents, `মাসুদ রানা সিরিজ` (a series) of 309, and
    `ওয়েস্টার্ন বই - সেবা প্রকাশনী` (a publisher) of 120.
    """
    from pdf_craft.catalogue.filename_parser import is_probably_person_name

    for value in (
        "epub-staging", "Others", "বিবিধ", "উপন্যাস", "মাসুদ রানা সিরিজ",
        "ওয়েস্টার্ন বই - সেবা প্রকাশনী", "বাংলাদেশের ইতিহাস",
        "Chacha Chowdhury Comics", "হিন্দু ধর্ম", "মহাভারত",
    ):
        assert not is_probably_person_name(value), value


def test_real_authors_survive_even_when_absent_from_the_catalogue():
    """Catalogue membership cannot be the test.

    The catalogue is a current-market retailer while the collection is largely
    classic and out-of-print, so genuinely famous authors are missing from it --
    `মুহম্মদ জাফর ইকবাল` labels 234 documents and is a real person. Rejecting
    everything the catalogue lacks would discard them.
    """
    from pdf_craft.catalogue.filename_parser import is_probably_person_name

    for value in (
        "মুহম্মদ জাফর ইকবাল", "তসলিমা নাসরিন", "রবীন্দ্রনাথ ঠাকুর",
        "বুদ্ধদেব গুহ", "J.M. Wikeley", "Henry Morris",
    ):
        assert is_probably_person_name(value), value


def test_directory_author_is_labelled_separately():
    # The caller needs to know which author came from the directory so it can
    # weight it lower than one parsed from a rigid template.
    parsed = parse_filename("data/বুদ্ধদেব গুহ/মাধুকরী - বুদ্ধদেব গুহ.pdf")
    assert parsed.directory_author == "বুদ্ধদেব গুহ"


def test_rejected_directory_is_not_emitted_as_an_author():
    parsed = parse_filename("data/epub-staging/Bishoron.pdf")
    assert parsed.directory_author is None
    assert "epub-staging" not in parsed.authors


# ---------------------------------------------------------------------------
# Embedded titles that are really filenames
# ---------------------------------------------------------------------------


def test_embedded_title_splits_title_from_author():
    """1,630 EPUBs carry a numeric filename and the original PDF name as
    dc:title, so the author dc:creator lacks is sitting in the title."""
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("শ্রেষ্ঠ কবিতা - শফিকুল ইসলাম")
    assert result.titles == ("শ্রেষ্ঠ কবিতা",)
    assert result.authors == ("শফিকুল ইসলাম",)


def test_embedded_title_bengali_danda_separator():
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("অনুর পাঠশালা ।। মাহমুদুল হক")
    assert result.titles == ("অনুর পাঠশালা",)
    assert result.authors == ("মাহমুদুল হক",)


def test_embedded_title_strips_scraper_boilerplate():
    # `Unknown Author -` leaked into 332 OPF titles and the download banner
    # into 138, because these EPUBs were converted from the scraped PDFs.
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("Unknown Author - Maa is waiting to be download!!!")
    assert result.titles == ("Maa",)
    assert result.authors == ()


def test_embedded_title_extracts_bengali_volume_digits():
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("৬৩। একজন মায়াবতী - হুমায়ূন আহমেদ")
    assert result.volume == "63"
    assert result.titles == ("একজন মায়াবতী",)
    assert result.authors == ("হুমায়ূন আহমেদ",)


def test_embedded_title_trailing_parenthetical_author():
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("বাংলা গল্প-বিচিত্রা (বিভূতিভূষণ বন্দ্যোপাধ্যায়)")
    assert result.authors == ("বিভূতিভূষণ বন্দ্যোপাধ্যায়",)
    # The hyphen inside the title must not be treated as a separator.
    assert result.titles == ("বাংলা গল্প-বিচিত্রা",)


def test_embedded_title_without_a_separator_yields_no_author():
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("CH3")
    assert result.titles == ("CH3",)
    assert result.authors == ()


def test_embedded_title_rejects_a_non_person_tail():
    # A dash in a title does not make the tail an author.
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("রচনাবলী - ২য় খণ্ড সমগ্র")
    assert result.authors == ()


def test_embedded_title_strips_file_extension_and_site_stamp():
    """These titles are filenames, so they arrive with the extension attached.

    Real values: `শেক্সপীয়র রচনাবলী ।। পৃথ্বীরাজ সেন.pdf` and
    `ca$hvertising ( PDFDrive.com ).epub`. The trailing `.pdf` also defeated the
    danda split until it was stripped first.
    """
    from pdf_craft.catalogue.filename_parser import parse_embedded_title

    result = parse_embedded_title("শেক্সপীয়র রচনাবলী ।। পৃথ্বীরাজ সেন.pdf")
    assert result.titles == ("শেক্সপীয়র রচনাবলী",)
    assert result.authors == ("পৃথ্বীরাজ সেন",)

    stamped = parse_embedded_title("ca$hvertising ( PDFDrive.com ).epub")
    assert stamped.titles == ("ca$hvertising",)
