# Development sampling proposal — 12 families × 5 pages = 60 pages

Prepared 2026-09-09 from **read-only** metadata only (no PDF rendering, no
hashing of `data/`, all SQLite opened `mode=ro`). This is a **proposal that
needs human sign-off** on strata representativeness and rights before any page
is annotated (roadmap §5, stage 1). It is not a committed manifest; the frozen
manifest is produced by `python -m pdf_craft_tool.research sample` once the
choices below are confirmed.

Machine-readable form: `pdf-craft-output/agents/research-impl-20260909/pilot-selection.json`.
Sampling-frame table: `pdf-craft-output/agents/research-impl-20260909/done-books.csv`.

## 1. Frame

- Source of truth: `pdf-craft-output/cluster/queue.sqlite3` (`jobs` where
  `state='done'`) joined to `<job>/summary.json` for OCR'd page lists and
  artifact paths, plus `catalogue.db` duplicate clusters and
  `extracted-metadata.csv`.
- 1,726 completed books at audit time (drifts upward; the queue is live). All
  have a raw `.pcex`; 690 (older profile `3e954be3…`) additionally have
  proofread artifacts + audit JSON.
- **Eligibility filter:** `>= 6` OCR'd pages AND raw artifact present on local
  disk → 1,498 books across 214 source directories.
- **Exclusions:** non-keeper members of `catalogue_duplicate_members` clusters
  (4,155 sha256 values); `Chacha Chowdhury Comics` (comics, not book prose).
  Frame after exclusions: **1,181 candidate books**.

Observed spread in the candidate frame: era proxy — 152 pre-1947-author books /
1,029 modern; provenance — 846 curated author dirs / 335 `incoming-scraped`;
OCR-quality proxy (fraction of OCR'd pages the pipeline flagged for review) —
137 clean / 886 mixed / 158 noisy; length — 467 short (≤30 pp) / 555 medium /
159 long (>120 pp); 55 books with a poetry keyword in the title.

## 2. Reproducible selection rule

- **Family = one completed book** (deduplicated). Edition-level near-duplicate
  detection is not yet built (`catalogue` has `same_work_clusters = 0`), so a
  family here is a single work/edition; S1 `group_families` + human confirmation
  will merge any that turn out related before the split is frozen.
- **Strata:** the crossing `era ∈ {pre1947, modern} × quality ∈ {clean, mixed,
  noisy} × length ∈ {short, long}` = **12 cells**, one family per cell. This
  deliberately spans the axes the roadmap names (typography/era, scan quality,
  prose vs. verse, length) using only proxies available from read-only
  metadata. `era` is an author-name proxy; `quality` is the pipeline's own
  `ocr_review` flag fraction (0 / <0.5 / ≥0.5); `length` is the OCR'd page
  count.
- **Family pick within a cell:** `seed = 20260909` (matches
  `research/configs/pilot.json`). Rank eligible books in the cell by
  `sha256(f"{seed}\0{cell}\0{book_sha256}")` and take the minimum, skipping any
  author directory already used (→ 12 distinct authors). If a cell is empty,
  relax `length`, then `era`, then take any book of that `quality`.
- **Page pick within a family:** drop the first 2 and last 2 OCR'd pages when
  the book has ≥ 12 (front/back matter), then draw 5 page numbers with
  `random.Random(sha256(f"{seed}\0{book_sha256}"))`.
  `selection_probability = 5 / (core page count)`, stored per page.

Re-running the rule on the same frame + seed reproduces the exact list below.
A different seed produces a different, equally valid draw — and, because the
manifest is immutable (`schema.Manifest`), it produces a *new* manifest rather
than silently mutating this one.

## 3. Proposed 12 families

| # | Stratum (era/quality/length) | Author dir | Title | job / sha256 (12) | Profile | OCR pp | Pages | p(page) | Proofread artifact |
|---|---|---|---|---|---|---|---|---|---|
| 1 | pre1947 / clean / short | রবীন্দ্রনাথ ঠাকুর | Choraidhon (চোরাইধন) | `1ab051000aac` / `4060ede4fd04` | 3e954be3 | 7 | 1,2,3,4,5 | 0.71 | yes |
| 2 | pre1947 / clean / long | শরৎচন্দ্র চট্টোপাধ্যায় | দেবদাস | `353387a71b7d` / `888507bfc58a` | 9f23c684 | 73 | 25,42,45,50,51 | 0.07 | no |
| 3 | pre1947 / mixed / short | কাজী নজরুল ইসলাম | চক্রবাক (verse) | `60df28fc1c6a` / `09858b1c0836` | 3e954be3 | 23 | 9,15,16,17,21 | 0.26 | yes |
| 4 | pre1947 / mixed / long | মাইকেল মধুসূদন দত্ত (compiler গুপ্ত) | মাইকেল (নাটক / drama) | `a926ea673a17` / `dee1bacf1685` | 3e954be3 | 122 | 22,30,46,103,110 | 0.04 | yes |
| 5 | pre1947 / noisy / short | incoming-scraped | ১৫০টি কবিতার বই (verse anthology) | `a91ccd5e0695` / `a9d5d0a05290` | 9f23c684 | 23 | 4,16,17,20,21 | 0.26 | no |
| 6 | pre1947 / noisy / long | ছোটদের বই | এসো রঙ করি (children's) | `9987cedf2841` / `5aaca7f6f2ac` | 3e954be3 | 114 | 4,60,64,71,108 | 0.05 | yes |
| 7 | modern / clean / short | ওয়েস্টার্ন বই – সেবা প্রকাশনী | Cowboy (pulp western) | `56eece6b0dd2` / `0b561cd68443` | 3e954be3 | 14 | 6,7,9,11,12 | 0.50 | yes |
| 8 | modern / clean / long | তসলিমা নাসরিন | আমি ভালো নেই, তুমি ভাল থেকো প্রিয় দেশ | `589727f2d355` / `9d520529f8e9` | 3e954be3 | 444 | 68,133,280,341,368 | 0.01 | yes |
| 9 | modern / mixed / short | হুমায়ুন আজাদ | মুখোমুখি | `83af5471d78b` / `579c0fa57820` | 3e954be3 | 14 | 3,6,7,10,11 | 0.50 | yes |
| 10 | modern / mixed / long | অনুবাদ সাহিত্য (translation) | ইতালীর সেরা গল্প (Italir Sera Galpa) | `4d2f85011e55` / `0659bc675340` | 9f23c684 | 164 | 64,74,87,102,140 | 0.03 | no |
| 11 | modern / noisy / short | আহসান হাবীব | "154" (title needs confirmation) | `d4ba4a581c39` / `b9a2cedb1322` | 3e954be3 | 27 | 10,13,14,16,24 | 0.22 | yes |
| 12 | modern / noisy / long | সঙ্গীত (music treatise) | সঙ্গীত রত্নাকর (Sangit Ratnakar) | `cf2173c3ec3d` / `7ded2dabb452` | 9f23c684 | 348 | 62,108,172,211,232 | 0.01 | no |

60 pages, 12 distinct authors, both processing profiles, 8/12 with a proofread
artifact for reuse-as-review-candidate. Genres represented: novel, short verse,
drama, verse anthology, children's picture book, pulp western, memoir/essay,
translated short fiction, music treatise.

## 4. Known limitations — require a human decision before annotation

1. **Strata are proxies.** `era` is inferred from the author directory name;
   `quality` is the pipeline's own review flag, not a human quality judgement;
   genre/verse is a title keyword. A Bengali-literature specialist must confirm
   these 12 are a defensible spread of *observed* typography, scan quality and
   register — and swap any that are not. Do not invent publication dates to fill
   a cell.
2. **Rights unknown for all 12.** No inventory source records redistribution
   rights. `incoming-scraped` (family 5) has unknown provenance and is the most
   likely public-release exclusion. Rights must be assessed per family (scan,
   transcription, metadata) before annotation of any release candidate.
3. **Duplicate/overlap not human-confirmed.** Non-keeper duplicates were
   excluded automatically. S1 `group_families` will surface `needs_confirmation`
   pairs; a person must confirm none of these 12 are editions of each other or
   of a main-study family before the split is frozen (pilot families may enter
   *training only*).
4. **Titles 11 ("154") and a few compiler/author attributions are noisy**
   scraper strings; confirm the actual work and author.
5. **Page count = OCR'd pages, not printed pages.** Some books were only
   partially processed; the census + annotation will use the real page images,
   and a page that turns out blank/duplicate is replaced by re-drawing with the
   same seeded RNG and the substitution recorded.
6. **This is development data only.** Under the protocol these 12 families may
   enter the training split and never calibration or test.
