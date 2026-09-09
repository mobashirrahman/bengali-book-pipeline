# Research protocol: trustworthy Bengali OCR correction (pilot phase)

Status: **draft decisions, to be finalised after the pilot and before any
final-test access.** This document freezes the analysis intent so that later
choices are visible as changes, not silent tuning. It is the protocol companion
to [`RESEARCH_ROADMAP.md`](RESEARCH_ROADMAP.md) (science, literature, sampling,
metrics, decision gates) and [`references/research-implementation-plan.md`](../../references/research-implementation-plan.md)
(software packets S0–S8). Where this file and the roadmap disagree, the roadmap
is authoritative and this file is a bug.

Schema and record contracts are implemented in
`pdf_craft_tool/research/schema.py` (`SCHEMA_VERSION = 1`). The machine-readable
form of the decisions below is `research/configs/pilot.json`
(`StudyConfig`, `draft = true`).

## 1. Question and claim ceiling

Primary question (roadmap RQ2): **does source-image evidence plus an
empirically calibrated abstention policy reduce harmful OCR corrections on
unseen Bengali book pages at a fixed, useful correction/review budget, relative
to unrestricted correction and to simple gates?**

The accepted fallback outcome is a **resource + empirical-analysis paper**: an
independently verified full-page benchmark with a failure taxonomy. A no-edit
system that merely matches the production Tesseract baseline is **not** an OCR
improvement and will not be reported as one. Machine drafts (Tesseract, Qwen,
any VLM) are never gold.

## 2. Primary endpoint (frozen contrast, draft magnitude)

- **Metric:** macro-averaged per-work/edition-family character error rate (CER)
  under the strict-NFC text policy (`metrics.py`, `text_policy="nfc_strict"`;
  no quote folding, no spelling modernisation).
- **Contrast:** `B5 − B0` (proposed evidence-based gate with abstention minus the
  unchanged production Tesseract configuration), paired within family, across
  every eligible test page.
- **Direction:** lower is better. An unchanged output scores zero improvement,
  never a benefit.
- **Budget:** a fixed inference budget (model calls / tokens / wall time). The
  numeric budget and the smallest useful CER reduction are **declared after the
  pilot** from observed variance and annotator capacity, before final-test
  labels are unsealed. They are deliberately left as
  `fixed_inference_budget_to_be_set_after_pilot` in the config.

Confidence intervals: paired bootstrap resampling **whole work/edition
families** (`statistics.py`, `resample_unit="work_edition_family"`), never
per-character. Report event counts, not only intervals; zero observed harms do
not license a distribution-free safety claim.

## 3. Secondary endpoints (frozen list)

Reported for every arm, even when an arm fails or accepts no edits:

1. Damage to initially-correct text (count and rate of correct aligned units
   made wrong), fixed unit and denominator across methods.
2. Correction coverage = accepted candidates / all proposed candidates; and
   residual error after correction. Abstaining everywhere = coverage 0.
3. Beneficial-edit fraction (distance-reducing accepted edits / all accepted
   edits) **and** exact-correction precision (fully correct replacements / all
   accepted edits) — reported separately; both undefined when no edit is
   accepted (not 100%).
4. Sensitive-span preservation: exact preservation on the bounded
   source-spelling, name and numeral span sets.
5. Omission and reading-order errors, scored against the independent full-page
   census, reported separately from substitution errors.
6. Cost: wall time, CPU/GPU time, model calls, tokens, memory, and measured
   annotation minutes. Code-agent cost is accounted separately from
   experimental model cost.

## 4. Baseline and ablation arms (frozen roster)

| ID | Arm | Notes |
| --- | --- | --- |
| B0 | Unchanged production Tesseract configuration | Mandatory do-nothing baseline |
| B1 | Reproducible Bengali-specific recogniser (e.g. bbOCR) | Record availability/compatibility failures; unavailable ≠ silently replaced |
| B2 | One current locally-feasible open document/VLM recogniser | Chosen on development data only, then digest+prompt frozen |
| B3 | B0 + text-only correction | Context-only benefit and damage |
| B4 | Existing conservative exact-span + Tesseract-crop gate | The current repository policy (`ConservativeProofreader` + `TesseractCropVerifier`) |
| B5 | Proposed evidence-based gate with abstention | The hypothesis under test |
| Ablations | edit-size rule; OCR-confidence threshold; alternate-recogniser agreement; calibrated gate without image features | Isolates which evidence helps |

For RQ2, candidate corrections are **frozen once per proposer** into an
immutable candidate bank (`Candidate.proposal_bank_hash`). Every gate arm is
scored on byte-identical candidates. A second Tesseract PSM mode is a correlated
signal, not an independent oracle. Missing evidence → explicit abstention.

Two evaluation tracks are never pooled: **recognition on shared gold line
crops** and **end-to-end page transcription**. Word-crop scores are never
compared numerically to full-page scores.

## 5. Sampling decisions (draft; roadmap §5)

- **Pilot / development:** 12 work/edition families × 5 full pages = **60 pages**.
  Families chosen across observed typography, scan quality, prose/poetry and
  mixed-script strata with a reproducible rule (`splits.py`); pilot families may
  enter **training only**. The concrete pilot family list and its rationale are
  in `research/proposals/development-sampling-60.md`.
- **Provisional full study:** 60 families × 10 probability-sampled pages =
  600 pages; allocation 24 train / 12 calibration / 24 locked test. These are
  planning numbers, rescaled from pilot variance before test lock — not a
  sample-size guarantee.
- Probability sample and the error-enriched **challenge sample** are stored and
  reported separately; challenge pages never enter the prevalence estimate.
- Selection probabilities are stored per page (`SamplePage.selection_probability`).
  A weighted collection estimate is reported only if the sampling frame supports
  one.
- Duplicate / edition / overlapping-anthology grouping happens **before**
  splitting; related text never crosses splits; the manifest is frozen, not
  silently regrown as the corpus changes.

## 6. Annotation decisions (draft; roadmap §5, software S2)

- For every sampled page, humans first enumerate all readable text
  regions/lines **including text OCR missed** (the full-page census,
  `census.py`), before any transcription.
- Two Bengali-fluent annotators transcribe independently from the image, blind
  to OCR text, model identity, candidate corrections and each other
  (`annotation_server.py` enforces blindness in the server payload and
  assignment state, not only in the browser).
- Disagreements are adjudicated; reviewer IDs, revisions and decision reasons
  are recorded. Existing "verified" Sarat selections are **candidates** for this
  process, not automatic gold.
- Diplomatic transcription: preserve source spelling, punctuation, Bengali
  digits, visible historical variants. A separate normalised search layer may
  exist. Policies for conjuncts, vowel signs, nukta, joiners, hyphenation, verse
  line breaks, footnotes and unreadable spans are specified in
  [`RESEARCH_ANNOTATION.md`](RESEARCH_ANNOTATION.md). Illegible text is marked,
  never guessed and never model-filled.
- **Pilot quality triggers** (roadmap §5): mean pairwise NFC character
  disagreement > 1%, exact-line agreement < 95%, or any systematic policy
  ambiguity. A trigger requires guideline revision, annotator calibration and a
  blind 10-page recheck **before** scaling to main-study annotation. All
  disagreements are adjudicated regardless of triggers. Revised quality targets
  are frozen before main-study annotation.
- Independent spot audits inspect agreements as well as disagreements.

## 7. Leakage and provenance rules (enforced in software)

- Gold text is a scoring input only. Inference manifests and evaluation
  manifests are **separate exports** (`schema.assert_no_gold_fields`,
  `Prediction.from_dict` / `Candidate.from_dict` reject gold keys).
- Reference EPUBs and any hidden-test labels never enter OCR/correction prompts,
  classifier fitting, retrieval indexes used for correction, or threshold
  selection. Thresholds and calibration are fit on calibration families only;
  classifier parameters on training families only.
- Stable `page_id` is derived from source hash + page number + convention, never
  from mutable OCR text. Legacy gold entry IDs are retained as provenance links
  (`SamplePage.legacy_gold_ids`).
- Every record carries: source SHA-256 and location, page-number convention,
  image hash, coordinate unit / render scale / orientation, work/edition/overlap
  group, split, selection probability, raw OCR, model/config identities,
  annotation state, provenance and rights evidence.
- Foundation-model pretraining contamination cannot be excluded by our split;
  this limitation is stated in any write-up.

## 8. Rights (external decision, not made here)

Distribution rights are assessed separately for scans, transcriptions, metadata
and any borrowed labels. The public resource is built only from material with a
documented release basis (`SourcePage.rights_basis` ≠ `unknown`/`excluded`).
Unknown-rights books may be annotated for internal method development but
excluded from public release. Rights are confirmed before heavy annotation
investment in a release candidate.

## 9. Software baseline identity

Every manifest embeds `software_baseline()` output: git commit, git-dirty flag,
a SHA-256 of `git status --porcelain`, Python version, platform and key package
versions. A git commit alone does not identify this dirty working tree; the
status digest does. Baseline recorded for this implementation:
`pdf-craft-output/agents/research-impl-20260909/baseline.txt`.

## 10. What is NOT authorised by this document

No model training, no large downloads, no new cluster allocation, no external
model spending, no public dataset upload. Human annotators, adjudication
capacity and rights determinations are real external prerequisites; if they are
unavailable the software work is completed and the missing inputs are reported,
never substituted with model-generated gold or fabricated numbers.
