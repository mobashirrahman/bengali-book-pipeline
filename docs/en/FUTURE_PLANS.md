# Deferred plans

## Bengali OCR dataset and Tesseract fine-tuning

**Status: deferred by the user. Not current work.**
Recorded 2026-09-07. Do not start dataset collection, annotation, model training,
downloads, or server allocation for this proposal without a new user request.
This does not change the existing OCR/proofreading or EPUB publication services.

### Motivation and limits

The book collection could support a verified Bengali text-recognition dataset,
particularly for historical typefaces, conjuncts, vowel signs and recurring glyph
confusions. A dataset would also support comparisons with other OCR engines.
Fine-tuning might help recognition, but does not directly solve omitted lines,
reading order, page segmentation or poor preprocessing.

The earlier approximately 0.80% character-error result used a small, relatively
clean Sarat sample and an imperfect EPUB reference. It is not a collection-wide
baseline or evidence that fine-tuning will improve accuracy.

### Proposed workflow, if revisited

1. Sample diverse books, publishers, periods, fonts and scan quality. Include
   poetry and mixed Bengali/English text. Combine random samples with suspicious
   OCR regions; do not select only errors or only high-confidence text.
2. Extract full text-line images, checking that vowel marks are included and
   neighbouring lines are excluded. Retain page images and crop coordinates.
3. Human-verify every ground-truth transcription against the scan. OCR and Qwen
   outputs are draft labels, not ground truth. Preserve original spelling,
   punctuation and numbers; flag unreadable material rather than guessing.
4. Record PDF hash, page, bounding box, original OCR, verified transcription,
   reviewer status and dataset version. Define a consistent Unicode transcription
   policy without silently modernizing historical spelling.
5. Split training, validation and test sets by book; group duplicate pages and
   editions to prevent leakage. Keep the final test set out of model selection.
6. Export paired line images and UTF-8 labels, for example
   `book123_p004_line07.png` and `book123_p004_line07.gt.txt`.
7. Fine-tune the existing Bengali `tessdata_best` model using the maintained
   `tesstrain` workflow, rather than starting from scratch.
8. Compare against the unchanged baseline on held-out books before LLM
   proofreading. Measure character/word errors, omitted text, and regressions by
   book type. Promote a model only after demonstrated improvement.

### Tentative pilot size

- Around 1,000 human-verified evaluation lines from held-out books.
- Around 3,000–5,000 training lines plus separate validation material.

These are proposed starting annotation budgets, not guaranteed requirements or
accuracy promises. Establish the evaluation dataset first. Tesseract training is
CPU-based; servers could parallelize preparation and evaluation rather than act
as one distributed GPU training job. Decide any resource allocation separately
from the running book-processing workload.

### References

- [Tesseract training guidance and hardware requirements](https://tesseract-ocr.github.io/tessdoc/tess5/TrainingTesseract-5.html)
- [Maintained tesstrain workflow and ground-truth format](https://github.com/tesseract-ocr/tesstrain)
- [Image-quality and preprocessing guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)

## DeepSeek-OCR-2 vs Tesseract fine-tuning comparison

**Status: deferred by the user. Not current work.**
Recorded 2026-09-10. User asked to save the analysis as future scope and
revisit later. Do not start dataset collection, annotation, model training,
downloads, or server allocation for this proposal without a new user request.
This does not change the existing OCR/proofreading or EPUB publication services.
User context at recording time: goal is exploration only (no accuracy target
committed); human-annotation budget undecided; if revisited, compare both
model families on the same held-out set.

### Feasibility (as assessed 2026-09-10)

- DeepSeek-OCR-2 (`deepseek-ai/DeepSeek-OCR-2`, Apache-2.0, 3B, BF16 ~6-7GB)
  is fine-tunable today: 35 finetunes + 8 adapters on HuggingFace, Unsloth
  free fine-tuning notebook (1.4x faster, 40% less VRAM, 5x context), and a
  community DGX LoRA recipe. Published two-stage recipe (MolSeek-OCR,
  arXiv:2604.03476): direct full-parameter SFT was unstable; LoRA first, then
  selective full SFT with split learning rates, on 96-192k pairs. Unsloth's
  small Persian demo (n=10) is directional only, not evidence for Bengali.
- Production path is Tesseract-first plus Qwen3.5-4B proofreading
  (`pdf_craft_tool/book.py`, `cluster_worker.py`); OCR backends are a closed
  config surface (`pdf_craft/ocr_config.py`,
  `pdf_craft/pdf/page_extractor.py:PageExtractorNode` via upstream
  `doc-page-extractor` factories). New weights require an upstream factory
  entry, a new local config, a processing-hash/model-identity bump, and an
  immutable release; never requeue the collection for a trial.
- Fleet constraint: documented 8GB-class hosts fit Qwen Q4 but DeepSeek
  weights (~6.7GB) are tight and PaddleOCR-VL has OOM'd there. LoRA-4bit with
  gradient checkpointing may fit one host for a pilot; full SFT wants
  DGX-class. Train off the book-processing allocation.
- Existing per-book artifacts (`pdf-craft-output/cluster/jobs/<id>/work/`:
  `raw.pcex`, `page_N.{xml,json}`, `proofread.pcex`, `audit/`, `evidence/`
  crops; `gold_tasks.py`/`gold_store.py` line grouping) support weak-pair
  mining and zero-shot eval, not training. Raw/Qwen text is draft labels, not
  ground truth. Eval harness: `pdf_craft_tool/benchmark.py` (CER/WER),
  `external_eval.py` plus Snakemake plan. Same caveat as above: fine-tuning
  does not fix omissions, reading order, segmentation, or preprocessing.

### Proposed workflow, if revisited

1. Freeze a held-out Bengali eval set first (~1,000 human-verified lines,
   book-split, duplicates/editions grouped, test books excluded from any
   later train pool). New manifest could live under
   `pdf_craft_tool/eval_sets/` with a freeze test (e.g.
   `tests/test_bench_freeze.py`); no package changes.
2. Zero-shot comparison on the frozen set: current Tesseract vs
   DeepSeek-OCR-2 (`Free OCR` and `<|grounding|>Convert the document to
   markdown` prompts) vs Tesseract+Qwen chain. Report CER/WER plus omissions
   and regressions by book type. Refs never enter prompts.
3. Decision gate: train only if step 2 shows a learnable pattern (e.g.
   systematic conjunct/vowel-sign confusions rather than layout/omissions).
   Then run a LoRA pilot (Unsloth, off-fleet GPU, 3-5k verified train lines)
   in parallel with a CPU `tesstrain` pilot on the same splits.
4. Promote only after both candidates beat the frozen baseline on held-out
   books; integrate via canary release, never a full-collection requeue.

### References

- [DeepSeek-OCR-2 model card](https://huggingface.co/deepseek-ai/DeepSeek-OCR-2)
- [DeepSeek-OCR 2 paper (arXiv:2601.20552)](https://arxiv.org/abs/2601.20552)
- [Unsloth DeepSeek-OCR-2 run and fine-tune guide](https://unsloth.ai/docs/models/tutorials/deepseek-ocr-2)
- [MolSeek fine-tuning recipe (arXiv:2604.03476)](https://arxiv.org/abs/2604.03476)

## Full multi-engine OCR benchmark (remainder)

**Status: deferred by the user. Not current work.**
Recorded 2026-09-10. User asked to save the full all-model benchmarking as
future scope alongside the fine-tuning plan. Do not run engines, download
weights, or allocate GPU hosts for this without a new user request. This is
evaluation only — no dataset creation, no model training (those stay deferred
above). Execution plan, if resumed:
`references/bengali-ocr-benchmark-plan.md` (6 engines × REID historical pages
+ Mozhi modern word crops, Snakemake `workflow/`, report under
`pdf-craft-output/external-eval/runs/<date>/`).

### Completion snapshot (as of 2026-09-10)

- Inputs staged, git-ignored: `pdf-craft-output/external-eval/` (`REID2019.zip`
  + `reid/`, `mozhi-test.zip`/`mozhi-val.zip` + `mozhi/`, `mozhi/test`,
  `mozhi/val`). Reference access via `pdf_craft_tool/external_eval.py`
  (`tests/test_external_eval.py`).
- Phase 1 scored in `runs/full/`: Tesseract (`ben`) both splits; EasyOCR
  (`bn`) both splits; bbOCR (APSIS-Net) both splits; Surya OCR 2 Mozhi only —
  Surya REID hypotheses/scores absent, needs completion or failure triage.
- No summary report yet in `runs/full/` (scores + hypotheses JSONL only).
- Phase 2 not started: DeepSeek-OCR-2 adapter (`run_deepseekocr.py` + env +
  rules; VRAM spike first, abort to v1 fallback) and EasyOCR-CRNN
  (`Sarjinkhan2003/bengali-crnn-easyocr` weights staged under
  `external-eval/easyocr-crnn-run/`:
  `bengali_crnn.pth`, `bengali_crnn.py`, `bengali_crnn.yaml`,
  `craft_mlt_25k.pth`; rule + smoke + full splits pending).
- Excluded with rationale (see reference plan §2): PaddleOCR-VL-1.6 (8GB OOM,
  documented in `pdf-craft-output/benchmark/ocr-engines/`), classic
  PP-OCRv3-v6 (no Bengali model), GOT-OCR 2.0 (EN/ZH only), Bangla TrOCR (no
  public checkpoint), Baidu AI Cloud OCR (metered/account-bound), ERNIE key-info
  extraction (not recognition).

### Proposed workflow, if revisited

1. Complete Surya REID (or record abort cause), then generate the Phase-1
   report: per engine × per split micro/macro CER/WER, n/missing/error rates,
   timing (mean/median/p95), char-op breakdown, per-item JSONL, failure
   taxonomy sample, provenance appendix — REID and Mozhi reported separately,
   never pooled.
2. Phase 2: DeepSeek-OCR-2 VRAM spike → adapter → smoke (2 REID pages + 20
   Mozhi words) → full splits; EasyOCR-CRNN weights → rule → smoke → full
   splits; rerun `report` only (never recompute finished engines).
3. Non-gating follow-ups: `baidu/Unlimited-OCR` Bengali smoke test once its
   multilingual release lands (`UnlimitedOCRLocalConfig` path already wired);
   BCD3 third split; Ali-corpus outreach (`docs/en/BANGLA_OCR_GOLD_STANDARD_DATASETS.md`).
4. Acceptance: `snakemake --dryrun` graphs all rules; reruns reproduce scores;
   `pytest tests/test_external_eval.py` passes; report covers all 7 items for
   all 6 engines × both splits with prompts, peak VRAM, and CRNN provenance.

## Intended-text overlay for worn print (Phase C)

**Status: deferred by the user (recorded 2026-09-11). Not current work.**
Do not start without a new user request. Prerequisite: the diplomatic gold
transcript must be frozen first, so intent can never leak back into
pixel judgments.

### Motivation and limits

Historical print degrades ambiguous glyphs (e.g. worn র dots reading as ব).
The study gold is diplomatic — what the pixels show — which is correct for
scoring OCR engines but leaves the author's intended text unresolved. This
overlay recovers intended readings as a separate, evidence-cited layer; it
never overwrites gold and is never scored by the OCR CER.

### Proposed workflow, if revisited

1. Collect flagged ambiguous spans from adjudication (worn/confusable glyphs).
2. For each span, assemble an evidence bundle automatically: (a) lexical check
   of both readings (morphology-aware, not headwords only); (b) cleanest
   same-book instances of the same typeface for dot-region ink comparison;
   (c) same-page known-glyph geometry baseline; (d) LM sentence scores as a
   weak signal only.
3. A human adjudicates from the bundle (never from gut or silent LLM
   correction), recording intended reading + cited evidence + confidence into
   an overlay JSONL keyed by (page, region, span). Unresolvable spans stay
   flagged; the residual rate is a reported number.
4. Report separate agreement stats for the intended layer (expect lower kappa
   than diplomatic gold) and add worn-type cases to the failure taxonomy as a
   print-degradation class.
