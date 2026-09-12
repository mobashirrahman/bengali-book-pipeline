#!/usr/bin/env python3
"""Provisional main-study sampling frame: 60 families x 10 pages = 600 pages.

Read-only, offline. This is a PROPOSAL, not a frozen manifest: the real
`schema.Manifest` is only built after pilot variance is known and the
allocation is rescaled (`RESEARCH_PROTOCOL.md` §5). This script rebuilds the
candidate frame from live metadata, applies the same reproducible selection
rule as the 60-page pilot proposal, and previews the draw.

Selection rule (mirrors `research/proposals/development-sampling-60.md` §2,
with the length band binarised on the frame median instead of the pilot's
ad-hoc <=30 / >120 relaxation):

  * Frame = `done` jobs with >= 6 OCR pages and a raw `.pcex` on disk, minus
    the comics directory, minus catalogue duplicate non-keepers, minus every
    book (and real-author directory) used by the pilot.
  * Strata = era(pre1947|modern) x quality(clean|mixed|noisy)
    x length(short|long) = 12 cells; 5 families per cell = 60 families.
  * Family pick within a cell: rank by sha256(f"{seed}\\0{cell}\\0{book_sha}")
    ascending, take the first 5 with distinct author directories that have not
    been used by an earlier cell. If a cell has < 5, relax length, then era,
    recording each relaxation.
  * Split: `splits.assign_splits(families, {train:24,calibration:12,test:24},
    seed)`.
  * Page pick within a family: drop the first 2 and last 2 OCR pages (when
    >= 12), then `random.Random(sha256(f"{seed}\\0{book_sha}"))` sample 10;
    `selection_probability = 10 / core_page_count`.

Usage::

    .venv/bin/python research/build_main_study_frame.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pdf_craft_tool.research import splits

REPO = Path(__file__).resolve().parent.parent
QUEUE_DB = REPO / "pdf-craft-output" / "cluster" / "queue.sqlite3"
CATALOGUE_DB = REPO / "pdf-craft-output" / "catalogue" / "catalogue.db"
JOBS = REPO / "pdf-craft-output" / "cluster" / "jobs"
PILOT_JSON = REPO / "research" / "proposals" / "development-sampling-60.json"
DEFAULT_SEED = 20260909
DEFAULT_OUT = REPO / "pdf-craft-output" / "main-study" / "frame-600.json"
PROPOSAL_JSON = REPO / "research" / "proposals" / "main-study-frame-600.json"

COMICS_DIR = "Chacha Chowdhury Comics"

#: Author directories that are shared collection buckets, not one author --
#: excluded from the pilot-author exclusion (they hold hundreds of works).
COLLECTION_BUCKETS = frozenset({
    "incoming-scraped", "অনুবাদ সাহিত্য", "ছোটদের বই", "সঙ্গীত",
    "ওয়েস্টার্ন বই - সেবা প্রকাশনী",
})

#: Crude author-name proxy for "published a substantial body of work before
#: 1947". A Bengali-literature specialist must confirm/replace this (§5 limits).
PRE1947_AUTHORS = frozenset({
    "রবীন্দ্রনাথ ঠাকুর", "বঙ্কিমচন্দ্র চট্টোপাধ্যায়", "শরৎচন্দ্র চট্টোপাধ্যায়",
    "কাজী নজরুল ইসলাম", "মাইকেল মধুসূদন দত্ত", "ঈশ্বরচন্দ্র বিদ্যাসাগর",
    "প্রমথ চৌধুরী", "জীবনানন্দ দাশ", "সুকুমার রায়", "উপেন্দ্রকিশোর রায়",
    "অবনীন্দ্রনাথ ঠাকুর", "দ্বিজেন্দ্রলাল রায়", "অতুলপ্রসাদ সেন",
    "রজনীকান্ত সেন", "দীনবন্ধু মিত্র", "গিরিশচন্দ্র ঘোষ", "স্বামী বিবেকানন্দ",
    "রামমোহন রায়", "হরপ্রসাদ শাস্ত্রী", "অক্ষয়কুমার দত্ত",
    "প্যারীচাঁদ মিত্র", "কালীপ্রসন্ন সিংহ", "ত্রৈলোক্যনাথ মুখোপাধ্যায়",
    "স্বর্ণকুমারী দেবী", "বিভূতিভূষণ বন্দ্যোপাধ্যায়",
    "তারাশঙ্কর বন্দ্যোপাধ্যায়", "মানিক বন্দ্যোপাধ্যায়",
    "অচিন্ত্যকুমার সেনগুপ্ত", "প্রেমেন্দ্র মিত্র", "বুদ্ধদেব বসু",
})


@dataclass(frozen=True)
class FrameBook:
    job_id: str
    sha256: str
    author_dir: str
    title: str
    npages: int
    review_frac: float
    era: str
    quality: str
    length: str
    proof_available: bool

    @property
    def cell(self) -> tuple:
        return (self.era, self.quality, self.length)


def _sha(*parts) -> str:
    return hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()


def _quality(review_frac: float) -> str:
    if review_frac <= 0:
        return "clean"
    return "mixed" if review_frac < 0.5 else "noisy"


def _length(npages: int, median: int) -> str:
    return "short" if npages <= median else "long"


def _era(author_dir: str) -> str:
    return "pre1947" if author_dir in PRE1947_AUTHORS else "modern"


def _author_of(source: str) -> str:
    parts = Path(source).parts
    if "data" in parts:
        index = parts.index("data")
        if index + 1 < len(parts):
            return parts[index + 1]
    return "unknown"


def _non_keeper_shas(catalogue_db: Path) -> set:
    if not catalogue_db.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{catalogue_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = conn.execute(
            "SELECT d.sha256 FROM catalogue_duplicate_members m "
            "JOIN catalogue_local_documents d ON d.id = m.document_id "
            "WHERE m.is_keeper = 0 AND d.sha256 IS NOT NULL").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return {row[0] for row in rows}


def read_raw_frame(queue_db: Path, jobs_dir: Path) -> list[dict]:
    """One dict per done job with >= 6 OCR pages and a raw artifact on disk."""
    conn = sqlite3.connect(f"file:{queue_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, sha256, source, title FROM jobs "
            "WHERE state = 'done'").fetchall()
    finally:
        conn.close()
    frame = []
    for job_id, sha256, source, title in rows:
        try:
            summary = json.loads(
                (jobs_dir / str(job_id) / "summary.json").read_text(
                    encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pages = summary.get("pages") or []
        if len(pages) < 6:
            continue
        raw = summary.get("raw")
        if not raw or not Path(raw).is_file():
            continue
        review = summary.get("ocr_review") or []
        proof = summary.get("proofreading")
        frame.append({
            "job_id": str(job_id),
            "sha256": summary.get("source_sha256") or sha256,
            "author_dir": _author_of(source or ""),
            "title": title or Path(source or "").stem,
            "npages": len(pages),
            "review_frac": len(review) / max(1, len(pages)),
            "proof_available": bool(proof) and proof != "not run",
        })
    return frame


def build_frame(raw_frame: list[dict], *, exclude_shas: set,
                exclude_authors: set, non_keeper_shas: set) -> list[FrameBook]:
    kept = [
        book for book in raw_frame
        if book["author_dir"] != COMICS_DIR
        and book["sha256"] not in exclude_shas
        and book["sha256"] not in non_keeper_shas
        and book["author_dir"] not in exclude_authors
    ]
    if not kept:
        return []
    median = sorted(book["npages"] for book in kept)[len(kept) // 2]
    books = []
    for book in kept:
        books.append(FrameBook(
            job_id=book["job_id"], sha256=book["sha256"],
            author_dir=book["author_dir"], title=book["title"],
            npages=book["npages"], review_frac=round(book["review_frac"], 4),
            era=_era(book["author_dir"]),
            quality=_quality(book["review_frac"]),
            length=_length(book["npages"], median),
            proof_available=book["proof_available"]))
    return books


_ALL_CELLS = tuple(
    (era, quality, length)
    for era in ("pre1947", "modern")
    for quality in ("clean", "mixed", "noisy")
    for length in ("short", "long"))


def _relaxations(cell: tuple) -> list[tuple]:
    """Fallback cells for an under-filled target, in order.

    Era is relaxed last: a pre-1947 cell is filled from other pre-1947 cells
    (length, then quality) before ever borrowing a modern book, so the
    pre-1947 stratum stays pre-1947 for as long as the corpus allows.
    """
    era, quality, length = cell
    other_len = "long" if length == "short" else "short"
    other_era = "modern" if era == "pre1947" else "pre1947"
    other_quals = [q for q in ("clean", "mixed", "noisy") if q != quality]
    same_era = [(era, quality, other_len)]
    for q in other_quals:
        same_era += [(era, q, length), (era, q, other_len)]
    cross_era = [(other_era, quality, length), (other_era, quality, other_len)]
    return same_era + cross_era


def pick_families(books: list[FrameBook], *, seed: int, per_cell: int = 5,
                  max_per_author: int = 2) -> dict:
    """Deterministic family pick: up to 5 per cell.

    Each author directory may back at most ``max_per_author`` families across
    the whole draw. A cell that cannot reach ``per_cell`` even after
    relaxation is left short and reported -- the pre-1947 strata are
    genuinely thin in the corpus.
    """
    by_cell: dict[tuple, list[FrameBook]] = {}
    for book in books:
        by_cell.setdefault(book.cell, []).append(book)
    for bucket in by_cell.values():
        bucket.sort(key=lambda b: _sha(seed, b.cell, b.sha256))

    author_count: dict[str, int] = {}
    used_shas: set = set()
    picked: dict[tuple, list] = {}
    relaxations: dict[str, list] = {}

    for cell in _ALL_CELLS:
        chosen: list[FrameBook] = []
        sources = [cell] + _relaxations(cell)
        for source_index, source_cell in enumerate(sources):
            for book in by_cell.get(source_cell, []):
                if len(chosen) >= per_cell:
                    break
                if book.sha256 in used_shas or book.author_dir == "unknown":
                    continue
                if author_count.get(book.author_dir, 0) >= max_per_author:
                    continue
                chosen.append(book)
                author_count[book.author_dir] = author_count.get(
                    book.author_dir, 0) + 1
                used_shas.add(book.sha256)
                if source_index > 0:
                    relaxations.setdefault("/".join(cell), []).append(
                        {"filled_from": "/".join(source_cell),
                         "sha256": book.sha256})
            if len(chosen) >= per_cell:
                break
        picked["/".join(cell)] = chosen
    return {"picked": picked, "relaxations": relaxations}


def pick_pages(book: FrameBook, *, seed: int, k: int = 10) -> dict:
    pages = list(range(1, book.npages + 1))
    core = pages[2:-2] if len(pages) >= 12 else pages
    take = min(k, len(core))
    rng = random.Random(int(_sha(seed, book.sha256), 16))
    chosen = sorted(rng.sample(core, take))
    prob = min(1.0, k / len(core)) if core else 0.0
    return {"pages": chosen, "selection_probability": round(prob, 4),
            "core_page_count": len(core)}


def build(seed: int, out_path: Path) -> dict:
    pilot = json.loads(PILOT_JSON.read_text(encoding="utf-8"))
    pilot_shas = {f["sha256"] for f in pilot["families"]}
    pilot_authors = {
        f["author_dir"] for f in pilot["families"]
        if f["author_dir"] not in COLLECTION_BUCKETS}

    raw_frame = read_raw_frame(QUEUE_DB, JOBS)
    books = build_frame(
        raw_frame, exclude_shas=pilot_shas, exclude_authors=pilot_authors,
        non_keeper_shas=_non_keeper_shas(CATALOGUE_DB))

    result = pick_families(books, seed=seed)
    picked = result["picked"]

    families = []
    for cell_key, chosen in picked.items():
        for book in chosen:
            families.append(splits.Family(
                family_id=book.job_id[:16], members=(book.job_id[:16],),
                content_hashes=(book.sha256,),
                processed_members=(book.job_id[:16],), needs_confirmation=()))
    split_of = splits.assign_splits(
        families, {"train": 24, "calibration": 12, "test": 24}, seed=seed)

    proposal_families = []
    total_pages = 0
    for cell_key, chosen in picked.items():
        for book in chosen:
            fam_id = book.job_id[:16]
            page_pick = pick_pages(book, seed=seed)
            total_pages += len(page_pick["pages"])
            proposal_families.append({
                "family_id": fam_id,
                "cell": cell_key,
                "job_id": book.job_id,
                "sha256": book.sha256,
                "author_dir": book.author_dir,
                "title": book.title,
                "npages": book.npages,
                "era": book.era, "quality": book.quality,
                "length": book.length,
                "proof_available": book.proof_available,
                "split": split_of.get(fam_id, "unassigned"),
                "pages": page_pick["pages"],
                "selection_probability": page_pick["selection_probability"],
            })
    proposal_families.sort(key=lambda f: (f["cell"], f["sha256"]))

    split_counts: dict[str, int] = {}
    for family in proposal_families:
        split_counts[family["split"]] = split_counts.get(family["split"], 0) + 1

    proposal = {
        "seed": seed,
        "target": "60 families x 10 pages = 600 pages (PROVISIONAL, not frozen)",
        "allocation": {"train": 24, "calibration": 12, "test": 24},
        "frame_size": len(books),
        "length_median_pages": (
            sorted(b.npages for b in books)[len(books) // 2] if books else None),
        "relaxations": result["relaxations"],
        "split_counts": split_counts,
        "total_pages": total_pages,
        "families": proposal_families,
    }
    PROPOSAL_JSON.write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n", encoding="utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        **proposal,
        "eligible_books": [
            {"job_id": b.job_id, "sha256": b.sha256,
             "author_dir": b.author_dir, "title": b.title, "npages": b.npages,
             "cell": "/".join(b.cell)} for b in books],
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"frame: {len(books)} eligible books "
          f"(median {proposal['length_median_pages']} pages)")
    print(f"families: {len(proposal_families)}  pages: {total_pages}")
    print(f"split counts: {split_counts}")
    under = [k for k, v in picked.items() if len(v) < 5]
    print(f"cells with < 5 families: {under or 'none'}")
    print(f"cells that needed relaxation: "
          f"{sorted(result['relaxations']) or 'none'}")
    print(f"proposal -> {PROPOSAL_JSON}")
    print(f"full frame -> {out_path}")
    return proposal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    build(args.seed, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
