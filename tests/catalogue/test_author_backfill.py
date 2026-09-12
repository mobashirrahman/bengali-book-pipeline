"""Tests for the high-confidence author backfill proposals.

Every test runs against a small constructed SQLite database that mirrors the
catalogue join shape; the real ``catalogue.db`` is never touched.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "pdf-craft-output" / "agents" / "author_backfill.py"
_spec = importlib.util.spec_from_file_location("author_backfill", _SCRIPT)
author_backfill = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("author_backfill", author_backfill)
_spec.loader.exec_module(author_backfill)

from pdf_craft.catalogue.text_normalize import normalize_name  # noqa: E402


DDL = """
CREATE TABLE catalogue_works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    subtitle TEXT,
    sort_title TEXT,
    language TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE catalogue_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT,
    normalized_name TEXT NOT NULL,
    UNIQUE(normalized_name)
);
CREATE TABLE catalogue_editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER REFERENCES catalogue_works(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    subtitle TEXT,
    publisher TEXT,
    publication_date TEXT,
    edition_statement TEXT,
    language TEXT,
    description TEXT,
    page_count INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE catalogue_edition_people (
    edition_id INTEGER NOT NULL REFERENCES catalogue_editions(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES catalogue_people(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (edition_id, person_id, role)
);
CREATE TABLE catalogue_local_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/pdf',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _person(conn, name):
    cur = conn.execute(
        "INSERT INTO catalogue_people (name, normalized_name) VALUES (?, ?)",
        (name, normalize_name(name)),
    )
    return cur.lastrowid


def _document(conn, *, path, title=None, authors=None, filename_source=None):
    metadata = {"_extraction": {"filename_source": filename_source or "granthagara"}}
    if title is not None:
        metadata["title"] = title
    if authors is not None:
        metadata["authors"] = authors
    cur = conn.execute(
        """INSERT INTO catalogue_local_documents
               (sha256, source_path, file_size, metadata_json)
           VALUES (?, ?, 100, ?)""",
        (f"sha-{path}", path, json.dumps(metadata, ensure_ascii=False)),
    )
    return cur.lastrowid


def _edition_with_author(conn, title, work_id, person_id):
    cur = conn.execute(
        "INSERT INTO catalogue_editions (work_id, title) VALUES (?, ?)",
        (work_id, title),
    )
    conn.execute(
        "INSERT INTO catalogue_edition_people (edition_id, person_id, role)"
        " VALUES (?, ?, 'author')",
        (cur.lastrowid, person_id),
    )


def _work(conn, title):
    return conn.execute(
        "INSERT INTO catalogue_works (title) VALUES (?)", (title,)
    ).lastrowid


@pytest.fixture()
def catalogue(tmp_path):
    conn = sqlite3.connect(tmp_path / "catalogue.db")
    conn.executescript(DDL)
    yield conn
    conn.close()


def _run(catalogue_path):
    """Collect proposals from a constructed db (read-only, like the script)."""
    conn = sqlite3.connect(f"file:{catalogue_path}?mode=ro", uri=True)
    try:
        return author_backfill.collect_proposals(conn)
    finally:
        conn.close()


def test_authordir_exact_match_is_proposed(catalogue, tmp_path):
    person = _person(catalogue, "সুনীল গঙ্গোপাধ্যায়")
    conn = catalogue
    doc = _document(
        conn,
        path="data/সুনীল গঙ্গোপাধ্যায়/book.pdf",
        title="নীল লোহিত",
        filename_source="authordir",
    )
    conn.commit()

    proposals = _run(tmp_path / "catalogue.db")
    match = [p for p in proposals if p.document_id == doc]
    assert len(match) == 1
    assert match[0].rule == "authordir_exact"
    assert match[0].proposed_author == "সুনীল গঙ্গোপাধ্যায়"
    evidence = json.loads(match[0].evidence)
    assert evidence["person_id"] == person
    assert evidence["catalogue_name"] == "সুনীল গঙ্গোপাধ্যায়"


def test_authordir_non_matching_directory_is_not_proposed(catalogue, tmp_path):
    _person(catalogue, "সুনীল গঙ্গোপাধ্যায়")
    doc = _document(
        catalogue,
        path="data/প্রকাশনী/book.pdf",
        title="নীল লোহিত",
        filename_source="authordir",
    )
    catalogue.commit()

    proposals = _run(tmp_path / "catalogue.db")
    assert all(p.document_id != doc for p in proposals)


def test_catalogue_single_match_is_proposed(catalogue, tmp_path):
    person = _person(catalogue, "বিভূতিভূষণ বন্দ্যোপাধ্যায়")
    work = _work(catalogue, "পথের পাঁচালী")
    _edition_with_author(catalogue, "পথের পাঁচালী", work, person)
    doc = _document(catalogue, path="data/x/পথের পাঁচালী.pdf", title="পথের পাঁচালী")
    catalogue.commit()

    proposals = _run(tmp_path / "catalogue.db")
    match = [p for p in proposals if p.document_id == doc]
    assert len(match) == 1
    assert match[0].rule == "catalogue_single_match"
    assert match[0].proposed_author == "বিভূতিভূষণ বন্দ্যোপাধ্যায়"


def test_homonymous_title_is_not_proposed(catalogue, tmp_path):
    # Same title, two DIFFERENT authors across catalogue matches: skip.
    author_a = _person(catalogue, "লেখক এক")
    author_b = _person(catalogue, "লেখক দুই")
    work = _work(catalogue, "রচনাসমগ্র")
    _edition_with_author(catalogue, "রচনাসমগ্র", work, author_a)
    _edition_with_author(catalogue, "রচনাসমগ্র", work, author_b)
    doc = _document(catalogue, path="data/x/রচনাসমগ্র.pdf", title="রচনাসমগ্র")
    catalogue.commit()

    proposals = _run(tmp_path / "catalogue.db")
    assert all(p.document_id != doc for p in proposals)


def test_document_with_author_is_never_touched(catalogue, tmp_path):
    person = _person(catalogue, "বিভূতিভূষণ বন্দ্যোপাধ্যায়")
    work = _work(catalogue, "পথের পাঁচালী")
    _edition_with_author(catalogue, "পথের পাঁচালী", work, person)
    doc = _document(
        catalogue,
        path="data/সুনীল গঙ্গোপাধ্যায়/পথের পাঁচালী.pdf",
        title="পথের পাঁচালী",
        authors=["প্রকাশক নাম"],
        filename_source="authordir",
    )
    catalogue.commit()

    proposals = _run(tmp_path / "catalogue.db")
    assert all(p.document_id != doc for p in proposals)


def test_audit_db_is_written(tmp_path):
    conn = sqlite3.connect(tmp_path / "catalogue.db")
    conn.executescript(DDL)
    person = _person(conn, "বিভূতিভূষণ বন্দ্যোপাধ্যায়")
    work = _work(conn, "পথের পাঁচালী")
    _edition_with_author(conn, "পথের পাঁচালী", work, person)
    doc = _document(conn, path="data/x/পথের পাঁচালী.pdf", title="পথের পাঁচালী")
    conn.commit()

    proposals = _run(tmp_path / "catalogue.db")
    author_backfill.write_audit_db(proposals, tmp_path / "audit.db")

    audit = sqlite3.connect(tmp_path / "audit.db")
    try:
        rows = audit.execute(
            "SELECT document_id, proposed_author, rule"
            " FROM author_backfill_proposals"
        ).fetchall()
    finally:
        audit.close()
    assert rows == [(doc, "বিভূতিভূষণ বন্দ্যোপাধ্যায়", "catalogue_single_match")]
