# Research results — status and reproducibility record

Status as of 2026-09-09: **software framework only. No experimental results
exist yet.** No model has been run for this study, no annotation has been
collected, no external dataset has been downloaded, no cluster capacity has been
allocated to it. This file is the place those results will land; today it
records what has been built and what regenerates a result once one exists.

## 1. What has been built (S0–S7)

| Stage | Module(s) | What it does | Tests |
| --- | --- | --- | --- |
| S0 | `research/schema.py`, `config.py` | Immutable record contracts (source page, sample, gold, prediction, candidate, decision, evaluation), versioned manifests, gold-free inference serialisation, `software_baseline()` environment identity, `StudyConfig` | `test_contracts` |
| S1 | `research/inventory.py`, `splits.py` | Read-only corpus audit; duplicate/overlap grouping; deterministic work/edition-family splits; frozen probability sample + separate challenge sample | `test_inventory`, `test_splits` |
| S2 | `research/census.py`, `annotation.py`, `annotation_server.py` | Full-page census (captures OCR-missed lines); blind independent double annotation; adjudication; final gold export; loopback-only annotation server; promotion trigger | `test_census`, `test_annotation` |
| S3 | `research/metrics.py`, `statistics.py` | raw / strict-NFC / search text policies; CER/WER/grapheme; omission & reading-order; before/after edit accounting; sensitive-span preservation; paired **family-level** bootstrap; harm report that never asserts safety | `test_metrics`, `test_statistics` |
| S4 | `research/runners.py`, `adapters.py`, `candidates.py` | Frozen baseline adapters (B0–B5) that degrade to an explicit `unsupported` failure offline; prediction cache with strict identity; immutable candidate bank | `test_runners`, `test_candidates` |
| S5 | `research/gates.py`, `calibration.py` | Acceptance-policy gates over one frozen candidate bank; hand-written L2 logistic gate; calibration/threshold on calibration split only; roadmap calibration-event screen with rule-based fallback | `test_gates`, `test_calibration` |
| S6 | `research/external.py`, `report.py` | External evaluation with a hard `native` vs `common_text_metrics` split; baseline/ablation tables; primary contrast B5−B0; harm-vs-coverage points; offline table rebuild | `test_external`, `test_report` |
| S7 | `research/__main__.py`, `research/export.py` | Separate research CLI (`python -m pdf_craft_tool.research`); reproducible export bundles with path-traversal and gold-leak guards | `test_cli`, `test_export` |
| S2/S4 pilot | `research/pcex.py`, `research/build_pilot.py`, `annotation_server.py` `--images` | Read OCR region geometry + text from `.pcex`; freeze the 60-page pilot study root; serve hash-checked page scans in the blind UI; assemble the B0 input (`research ocr-pages`) | `test_pcex`, `test_annotation` |

All tests are standard-library `unittest`, fully offline, no CUDA, no network,
no model download, no whole-book conversion:
`.venv/bin/python -m unittest discover -s tests/research -p 'test_*.py'` —
**155 passing**.

Three independent reviewer passes were run (S0–S3, S4–S6, S7). Pass 1: 6 minor,
all fixed. Pass 2: 2 major (per-page failure accounting in the report;
structural enforcement of the calibration-event screen) + 4 minor — the majors
and two minors fixed; two minors accepted with rationale (`proposal_bank_hash`
is superseded by the content-strict `CandidateBank.bank_hash` gates actually
cite; `register_native_evaluator` is reachable only via an explicit versioned
registration). Pass 3 (S7 export/CLI): 5 major + 3 minor, all fixed —
`ExportRefused` now a `ValueError` so the CLI reports it cleanly; non-empty
bundle destination refused; inference bundles reject gold-shaped content by
value not just filename; release decisions fail closed; the `evaluate`
subcommand no longer fabricates the harm endpoint (coverage/harm reported
unmeasured, never a fake zero); symlinks skipped; bundle manifest integrity
hash. Regression tests: `tests/research/test_review_fixes.py`,
`test_calibration.py`.

## 1b. Pilot preparation done without humans (2026-09-09)

- **Study root frozen.** `research/build_pilot.py` built
  `pdf-craft-output/research/pilot-study/`: immutable `sample_manifest.json`
  (60 `SamplePage`, `split="pilot"`, `seed=20260909`), 60 rendered page scans,
  a draft OCR-seeded census per page, and `pilot_provenance.json`. Keyed to the
  OCR-time `source_sha256` (some `data/` PDFs drifted after in-place metadata
  embedding; recorded, not an error).
- **B0 baseline materialised.** `research ocr-pages` + `research run` (dry)
  produced 60 real B0 `Prediction` records from the existing `.pcex` OCR — 54
  `ok`, 6 `empty` (blank/figure pages). B1–B5 return `unsupported` offline.
  Not scorable until pilot gold exists.
- **Annotation server** now serves the page scan (`--images`, hash-checked)
  alongside the blind line slots.
- Still pending humans: the census review, the two blind annotators + one
  adjudicator, and rights formalisation. See
  [`RESEARCH_PILOT_RUNBOOK.md`](RESEARCH_PILOT_RUNBOOK.md).

## 2. Primary result table — regeneration recipe (for when results exist)

1. Freeze the sample manifest (`python -m pdf_craft_tool.research sample`, or
   `research/build_pilot.py` for the pilot), the gold export from the
   annotation pipeline, and the candidate bank.
2. `python -m pdf_craft_tool.research ocr-pages` assembles the B0 input from
   existing OCR; `python -m pdf_craft_tool.research run` for each baseline arm
   on the pilot pages (`--allow-execution` + a fixed budget for B1–B5) into a
   prediction cache.
3. `python -m pdf_craft_tool.research fit-gate` on the training split;
   `choose_threshold` on the calibration split; `freeze_policy`.
4. `python -m pdf_craft_tool.research evaluate` then `report` →
   `report.render_markdown` produces the baseline table and the harm-vs-coverage
   figure data.
5. The **primary endpoint** is `report.primary_contrast(a="B0", b="B5")`:
   paired-family strict-NFC macro CER, bootstrap over whole families. Lower is
   better; zero change is zero improvement.
6. `python -m pdf_craft_tool.research export` builds the reproducible bundle;
   `export.rebuild_tables_offline` must reproduce step 4's numbers from the
   saved predictions alone.

Every number in a future manuscript must name the artifact it came from
(prediction JSONL hash, gold manifest hash, metric version, config hash — all
embedded in the report header).

## 3. Prior local observations (NOT results of this study)

- Sarat pp. 4–13, raw OCR vs. the project's imperfect EPUB:
  ~0.799 % CER / ~1.235 % WER. This is *agreement with an imperfect reference*,
  measured before this framework existed; it is not human-verified collection
  accuracy and not an LLM-improvement result. Guarded proofreading made zero
  edits on that run.
- 216 review rows exist in `book-pipeline-sarat-10/gold/gold.sqlite3`
  (182 manual, 27 tesseract, 4 flag, 3 skip). These are single-pass, crop-first
  candidates — **not** independent double-annotated gold — and enter this study
  only as re-annotation candidates.

## 4. Audit checklist (S8)

Before any result is reported:

- [ ] An independent person regenerates the primary table from the released
      predictions + gold with a clean environment.
- [ ] Every manuscript number is traced to a named artifact.
- [ ] The stated contribution is re-checked against the roadmap's adjacent-work
      matrix (REID2019, BaDLAD, Gold Standard Bangla, bbOCR, OHRBench, …).
- [ ] Reviewer subagent has seen the actual scoped diff and test evidence; a
      free backend was not the sole reviewer of its own code.
- [ ] A randomised set of claimed corrections is inspected against the scan.
- [ ] Nulls, exclusions and limitations are in the write-up, not dropped.
- [ ] Rights are documented per released artifact class.

## 5. Manuscript

`research/paper/` holds the manuscript skeleton and the table/figure
placeholders. It stays a skeleton until step 2 above has produced real
predictions. No speculative numbers.
