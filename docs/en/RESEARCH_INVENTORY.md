# Corpus inventory snapshot + pilot duplicate check

Regenerate (numbers drift — the OCR and dedupe workers run continuously):

```bash
.venv/bin/python research/refresh_inventory.py
```

Read-only over `pdf-craft-output/cluster/queue.sqlite3`,
`pdf-craft-output/catalogue/catalogue.db`, the job `summary.json` files and
`pdf-craft-output/extracted-metadata.csv`. Writes
`pdf-craft-output/research/inventory/inventory-report.json` and
`pilot-family-groups.json`. Companion to `RESEARCH_PROTOCOL.md` §5.

## 1. Snapshot — 2026-09-09 (indicative; re-run for current)

| Counter | Value | Notes |
| --- | --- | --- |
| Jobs `done` | ~1,778 | ~1,748 at pilot-prep; **~24,700 pending** — the OCR queue is live |
| Jobs `failed` / `running` | 5 / 1 | |
| `done` raw artifacts present on disk | = done count | every done job has its `raw.pcex` |
| `done` proofread artifacts present | 690 | older profile `3e954be3…` only |
| OCR pages total (done jobs) | ~103,000 | sum of `summary.json` `pages` lengths |
| Distinct content SHA-256 (catalogue) | ~22,200 | catalogue-wide, grows with ingest |
| Duplicate clusters | ~2,470 | churns as the dedupe worker re-runs |
| Candidate work groups | ~2,415 | clusters whose `relation` names work/edition |

`unknowns` in the report: the metadata CSV columns don't match the coverage
probe's expected names, so title/author/year coverage is `null` (not 0); and
no inventory source carries a redistribution-rights field, so rights coverage
is `unknown` for every row — an external determination, per §8.

## 2. Pilot family duplicate check

Do any of the 12 frozen pilot families duplicate each other or another book?

- **8 / 12** pilot source PDFs sit in a `same_work` cluster of size 2.
- In every case the sibling is an **EPUB in `catalogue/epub-staging/`** or a
  **download-stub PDF** (`Unknown Author - … is waiting to be download!!!`),
  never a second independent scan. The pilot always uses the scanned PDF, and
  the catalogue currently marks it the keeper.
- **No two pilot families are grouped together** — the 12 stay 12 distinct
  works under `splits.group_families` with catalogue `same_work` edges;
  `pilot_family_pairs_to_confirm` is empty.
- One earlier run saw হুমায়ুন আজাদ (*মুখোমুখি*) marked **non-keeper** behind a
  download-stub keeper; a later dedupe re-run corrected it. Re-run this script
  before freezing the split to confirm it has stayed corrected.

The 4 families with no cluster: কাজী নজরুল — চক্রবাক, incoming-scraped — ১৫০টি
কবিতা, সেবা — Cowboy, আহসান হাবীব — Unnmad.

## 3. What a human must still confirm (§5)

1. Confirm every pilot PDF is still its cluster's keeper (re-run the script).
2. When the **main study** frame is drawn, exclude the EPUB-staging and
   download-stub siblings of these 8 clusters so the same work is not sampled
   twice across pilot + main study. `research/build_main_study_frame.py`
   already excludes the pilot books by sha + author dir.
3. Edition-level near-duplicate detection across independent *scans* is not
   built (`catalogue_works` grouping is metadata-driven). If two different
   scanned editions of one work both enter the corpus, this check will not
   catch them — a manual pass over the main-study families is still required.
