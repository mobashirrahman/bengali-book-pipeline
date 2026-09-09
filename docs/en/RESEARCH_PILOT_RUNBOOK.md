# Pilot runbook — 60-page blind annotation

Operational companion to [`RESEARCH_PROTOCOL.md`](RESEARCH_PROTOCOL.md) and
[`RESEARCH_ANNOTATION.md`](RESEARCH_ANNOTATION.md). It covers standing up the
blind double-annotation pilot on the 60 development pages, running it with two
annotators and one adjudicator, and exporting the pilot gold + disagreement
report. Everything here is offline and loopback-only; nothing trains a model,
downloads, allocates cluster capacity, or publishes.

Team confirmed: **two Bengali-fluent annotators working blind to each other,
plus one adjudicator who does not annotate**. Rights status: **internal
research use only, not yet formalised** — the pilot runs as internal method
development; no artifact leaves the machine until rights are recorded, and
`export` stays blocked for release bundles (§7).

---

## 0. One-time: freeze the study root

Run on bio10 from the repo root, in the project venv:

```bash
.venv/bin/python research/build_pilot.py \
    --out pdf-craft-output/research/pilot-study
```

This is deterministic and re-runnable. It:

- reads `research/proposals/development-sampling-60.json` (the signed-off draw),
- checks each book's proposal sha256 against the sha256 the OCR pipeline
  recorded (`<job>/summary.json`),
- renders the 5 chosen pages of each of the 12 books to PNG with `pdftoppm`
  (poppler) at the DPI that matches the OCR pixel space,
- seeds a **draft** page census from the book's `.pcex` OCR regions,
- writes, under the study root:
  - `sample_manifest.json` — immutable `schema.Manifest` (kind `sample`), 60
    `SamplePage` records, `split = "pilot"`, real per-page selection
    probability, `seed = 20260909`;
  - `annotation_pages.json` — the seed file for the annotation store;
  - `pilot_provenance.json` — per-page job id, canonical `source_sha256`, the
    sha256 the page was actually rendered from, a `pdf_bytes_drifted_from_ocr_source`
    flag, OCR-seeded region count;
  - `census/<page_id>.json` — the draft census per page;
  - `pages/<page_id>.png` — the 60 scans.

> **PDF drift.** Some `data/` PDFs were re-saved in place (metadata embedded)
> after the OCR ran, so their bytes no longer match `summary.json`. The
> canonical `source_sha256` in the manifest is the **OCR-time** hash — the key
> every future B0/baseline prediction is also keyed to. The rendered page is
> visually identical; `pilot_provenance.json` records both hashes. This is
> expected, not an error.

Record the printed `sample_manifest.json` hash in the pilot log.

---

## 0b. Materialise the B0 baseline (no humans, no models)

B0 is the unchanged production Tesseract configuration. Its output for the 60
pages already exists in the OCR `.pcex` artifacts, so it can be assembled now,
offline:

```bash
S=pdf-craft-output/research/pilot-study
.venv/bin/python -m pdf_craft_tool.research ocr-pages --study-root $S
.venv/bin/python -m pdf_craft_tool.research run \
    --study-root $S --manifest $S/sample_manifest.json \
    --config research/configs/baselines.json \
    --pages $S/ocr_pages.json --out $S/predictions.jsonl
```

- `ocr-pages` reads `pilot_provenance.json`, pulls the raw OCR text for each
  page from its job's `.pcex`, and writes `ocr_pages.json` (`page_id` /
  `image_ref` / `ocr_text`). No model call.
- `run` (dry, `allow_execution` off) then produces 60 real B0 `Prediction`
  records; B1–B5 come back `unsupported` — they need models / execution not
  authorised here.
- The B0 predictions are keyed to the OCR-time `source_sha256`, so they line
  up with `sample_manifest.json` and, later, with `gold.json`.

`predictions.jsonl` cannot be **scored** until pilot gold exists (`evaluate`
refuses a page with no gold). It is ready to score the moment §6 produces
`gold_pages.json`.

---

## 1. Mandatory: human census review

The seeded census is **OCR regions**, not an independent enumeration. Protocol
§6 requires a human to enumerate every readable region *including text the OCR
missed* before transcription. Do this once, per page, against the scan:

1. Open each `pages/<page_id>.png` beside `census/<page_id>.json`.
2. Add a region for any readable text block the OCR dropped (page numbers,
   running heads, footnotes, marginalia, caption text, verse lines).
3. Remove regions that are not text (bare rule lines, smudges) or merge/split
   to match real reading units.
4. Keep `region_index` dense `0..n-1`; keep `reading_order` a permutation.
5. For a page that is a full-page illustration or blank, leave the single
   seeded region and set its `note`.

Re-write `annotation_pages.json` from the corrected `census/*.json` (a small
script: replace each page's `census` field by the reviewed file's contents).
Set `pilot_provenance.json → census_status` to `human_reviewed` and record who
did the review and when. The census reviewer **may** be one of the annotators
(the census is structure, not transcription) but note it in the log.

---

## 2. Start the annotation server (bio10, loopback)

```bash
set -a; source .env; set +a   # only if the venv needs it; server itself needs nothing

.venv/bin/python -m pdf_craft_tool.research.annotation_server \
    --db  pdf-craft-output/research/pilot-study/annotation.sqlite3 \
    --pages pdf-craft-output/research/pilot-study/annotation_pages.json \
    --images pdf-craft-output/research/pilot-study/pages \
    --annotators anno-1,anno-2 \
    --adjudicators judge-1 \
    --host 127.0.0.1 --port 8767
```

- The DB filename must **not** be `gold.sqlite3` or `queue.sqlite3` (the store
  refuses those). Keep it on bio10 local disk.
- `--pages` is read only on first run (the store is immutable after seeding).
  Re-running after a census fix needs a **fresh DB file** — delete the old one
  only if no annotator has submitted against it.
- `--images` serves each `<page_id>.png`, hash-checked against the frozen page;
  a swapped file is rejected with HTTP 409.
- The server prints one token per annotator and per adjudicator on startup.
  **Give each person only their own token, over a private channel.** Tokens are
  in memory only — every restart mints new ones.
- The server binds `127.0.0.1` and refuses any non-loopback `--host`.

### How the team connects

The team log into bio10 directly. Each person, in their own bio10 session,
opens an SSH port-forward from their laptop and browses locally:

```bash
ssh -N -L 8767:127.0.0.1:8767 <user>@bio10.studcs.uni-saarland.de
# then open http://127.0.0.1:8767/ in a normal browser
```

Only the coordinator runs the server; annotators never start their own.

---

## 3. Annotator workflow (each of the two, independently)

1. Open `http://127.0.0.1:8767/`, paste your token and a `page_id`
   (the coordinator hands out the page list from `sample_manifest.json`).
2. The scan loads above the transcription boxes. Confirm the **frozen image
   sha256** shown matches the one the coordinator gave you. If the scan does
   not load, stop — do not transcribe from memory; tell the coordinator.
3. Transcribe each region into its box, following
   [`RESEARCH_ANNOTATION.md`](RESEARCH_ANNOTATION.md) (guideline `annot-1`):
   diplomatic transcription, source spelling, Bengali digits, mark illegible
   text, never guess, never paste from any OCR or model output.
4. Submit. You never see the other annotator's text, any OCR draft, any
   candidate correction, or any agreement statistic — the server payload does
   not carry them.
5. Work through the full 60. The `progress` line shows counts only.

You do **not** need to finish all 60 in one sitting; assignments persist. Each
submit is optimistic-locked on a revision number; a stale submit is rejected
and you reload.

---

## 4. Adjudicator workflow (third person)

For each page once **both** annotators have submitted:

1. `POST /api/conflicts/<page_id>/detect` (adjudicator token) builds the
   per-line conflict list and the disagreement stats. The UI exposes this;
   or `curl`:
   ```bash
   curl -s -X POST -H "X-Annotation-Token: $JUDGE" \
     http://127.0.0.1:8767/api/conflicts/<page_id>/detect
   ```
2. For every differing region, `POST /api/adjudicate/<page_id>` with
   `region_index`, `resolved_text`, a non-empty `reason`, and the current
   `revision`. You must not be either annotator (the store enforces this).
3. A page is done when it has zero unresolved conflicts.

The adjudicator sees both transcriptions and the stats — that is the whole
job — but still never sees OCR or model text.

---

## 5. Quality triggers (protocol §6, checked after the pilot)

Collect `disagreement_stats` for all 60 pages and run:

```python
from pdf_craft_tool.research.annotation import promotion_blocked
promotion_blocked([stats_for_each_page])   # -> {"blocked": bool, "reasons": [...]}
```

Blocked when **mean pairwise NFC character disagreement > 1%** or **mean
exact-line agreement < 95%**. A block means: revise `RESEARCH_ANNOTATION.md`,
recalibrate the annotators, and redo a blind 10-page recheck **before** any
main-study annotation. All 60 disagreements are adjudicated either way.

---

## 6. Export the pilot gold

Once every page is `adjudicated`:

```python
from pdf_craft_tool.research.annotation import AnnotationStore
store = AnnotationStore("…/pilot-study/annotation.sqlite3", pages=[])
gold = store.export_final(all_page_ids, adjudicator_id="judge-1")
```

Each entry is a `schema.GoldPage` with `status="final"` (≥2 distinct
annotators + adjudicator). Write them to
`…/pilot-study/gold_pages.json` (a `GoldPage` list). `FLAGGED` / `PROVISIONAL`
pages are terminal and never exported — list them in the pilot log with the
reason.

This gold is **pilot / training-split only**. It never enters calibration or
test (protocol §5).

---

## 7. What is NOT unlocked by having the annotation team

- **No release bundle.** Rights are "internal, not formalised", so
  `python -m pdf_craft_tool.research export --kind gold …` must not be run for
  distribution. Internal method-development use of the gold is fine.
- **No baseline model runs** beyond the one reserved worker without capacity
  approval; `run` stays dry (`--allow-execution` off) until then.
- **No inference-budget or effect-size numbers** — those are set *after* this
  pilot from its observed variance and measured annotation minutes.

---

## 8. Pilot log — fill this in

| Item | Value |
| --- | --- |
| `sample_manifest.json` hash | |
| `build_pilot.py` git commit | |
| census review — who / when | |
| census_status | |
| annotators (ids) | anno-1 = … , anno-2 = … |
| adjudicator (id) | judge-1 = … |
| server started (bio10 time) | |
| pages submitted by both | / 60 |
| pages adjudicated | / 60 |
| FLAGGED / PROVISIONAL pages | |
| mean pairwise char disagreement | |
| mean exact-line agreement | |
| `promotion_blocked` result | |
| gold export path + count | |
