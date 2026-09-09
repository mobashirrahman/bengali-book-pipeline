"""Generic non-title strings that must never be embedded as a book's title.

A handful of scraper-site labels and directory-level artifacts got resolved
into ``metadata_json.title`` alongside genuine titles: "Title Page", "Masud
Rana Pdf" (a series label, not a specific book), "১৫০টি কবিতার বই" ("150-poems
book", a folder-aggregator name), and similar. Each one is wrong on every
document it appears on, and each appears on five or more documents --
verified by cross-tabulating every title against how many documents share it
exactly, then reading the actual filenames behind the outliers.

Genuinely recurring titles -- classics reprinted many times
("আনন্দমঠ", "চন্দ্রশেখর"), multi-volume collected works ("রবীন্দ্র রচনাবলী"),
annual magazine specials ("Rahasya Patrika Pdf 2018") -- are deliberately
NOT here: repetition alone does not make a title wrong, and excluding those
would silently drop good metadata from hundreds of correctly-identified
documents.

Used only at embed time (embed_corpus.py): a denylisted title is treated the
same as no title at all for that one document. The catalogue's own
metadata_json is left untouched -- this is a filter on what gets written into
the user's files, not a correction to the resolved data.
"""

from __future__ import annotations

# Verified 2026-09-09 against catalogue_local_documents.metadata_json: every
# string below is the literal, exact `title` value on 5+ documents, and every
# one was read against its source filename and found to be a scraper/site
# label, a genre-only word, a person's name, or a folder-aggregator caption
# rather than that specific book's title.
DENYLISTED_TITLES: frozenset[str] = frozenset({
    # Scan/OCR bookkeeping artifacts, not titles.
    "Title Page",
    "Content Page",
    "Introduction",
    "Preliminary Page",
    "Other Pages",
    "RBSI",
    "RBSI - Digital Book",
    # Series/genre labels standing in for a specific, different book per file.
    "CID Series Detective Thriller",
    "Masud Rana Pdf",
    "Masud Rana Series",
    # Site/collection branding, not any one book's title.
    "Bangla Books Pdf",
    "বাংলা ম্যাগাজিন",
    "বাংলা ম্যাগাজিন Pdf",
    "Professors Current Affairs 2025 PDF Collection",
    "সংকলনঃ সিনেমা_নাটক_থিয়েটারের বই",  # "Compilation: cinema/drama/theatre books"
    "১৫০টি কবিতার বই",  # "150-poems book" -- a folder aggregator, not one book
    "সবগুলো শারদীয় ম্যাগাজিন ১৪২৪",  # "All Sharadiya magazines 1424"
    # A person's name, mis-extracted as the title.
    "রবীন্দ্রনাথ",
    "সুবোধ ঘোষ",
    "হরপ্রসাদ শাস্ত্রী",
    "সুধীর চক্রবর্তীর বই",  # "Sudhir Chakraborty's book(s)" -- possessive label
    # Bare genre words used across many different authors' unrelated books.
    "গল্পসমগ্র",
    "গল্প সমগ্র",
    "উপন্যাস সমগ্র",
    "কবিতাসমগ্র",
    "দশটি উপন্যাস",
    "শ্রেষ্ঠ কবিতা",
    "শ্রেষ্ঠ গল্প",
    "বিবিধ প্রবন্ধ",
    "বাংলা ছোটগল্প",
})


def is_denylisted_title(title: str) -> bool:
    return title.strip() in DENYLISTED_TITLES
