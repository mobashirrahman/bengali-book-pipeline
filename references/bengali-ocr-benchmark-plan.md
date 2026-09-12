# Bengali OCR Benchmark Plan (final)

**Status:** approved for implementation. **Scope:** evaluation only — no dataset
creation, no model training (Bengali OCR dataset + Tesseract fine-tuning stay
deferred per `docs/en/FUTURE_PLANS.md`).

**Objective:** Reproducibly benchmark 6 OCR engines — Tesseract (`ben`),
EasyOCR (`bn`), EasyOCR-CRNN variant, Surya OCR 2, bbOCR/APSIS-Net,
DeepSeek-OCR v2 — on two fixed third-party references, using a new Snakemake
workflow under `workflow/`.

## 1. Eval splits (already staged, no downloads needed)

All inputs live under git-ignored `pdf-craft-output/external-eval/` (staged
2026-09-07; zips are the pinned copies — record sha256 in
`workflow/config.yaml`):

| Split | Path | Content / license | Use |
|---|---|---|---|
| REID2019 | `reid/REID2019/Competition_dataset_ImagesPAGEXML` (`REID2019.zip`, ~1 GB) | Historical printed Bengali pages ~1713–1914 + PAGE XML; public domain (heiDATA DOI `10.11588/DATA/AIQSXL`) | Page-level end-to-end: detection + recognition + reading order |
| Mozhi test | `mozhi/test/{images,test_gt.txt}` (`mozhi-test.zip`) | 10,113 modern book word crops; CC BY 4.0 (`https://ilocr.iiit.ac.in/public/printed/phase-0/v0.5/bengali/akshara/{train,test,val}.zip`) | Word recognition only |
| Mozhi val | `mozhi/val/` (`mozhi-val.zip`) | 9,787 crops | Sanity/threshold checks only, never test |

Reference access: `pdf_craft_tool/external_eval.py` (`reid_references`,
`mozhi_references`, `score_hypotheses`), tested by
`tests/test_external_eval.py`. REID and Mozhi results are **never pooled** —
report as "historical page-level" and "modern word recognition" separately.

Ali et al. EMNLP 2023 corpus (4.1M images, kappa 0.91/0.93/0.78) has **no
public download** (ICT Division property, confidentiality caveat in the
paper). Pursue via parallel, non-blocking outreach: Apurba authors
(`hasmot_ali@apurba.com.bd`, `info@apurba.com.bd`, contract "SD 08") +
EBLICT/BCC (`pdeblict@bcc.net.bd`, +88-02-55006869) + watch HF org
`banglagov` and `corpus.bangla.gov.bd`. If granted, it becomes a third eval
split. The benchmark proceeds regardless. (Background:
`docs/en/BANGLA_OCR_GOLD_STANDARD_DATASETS.md`.)

## 2. Engine lineup (final: 6 engines)

| Engine | Model/lang | Status |
|---|---|---|
| Tesseract | `ben` tessdata | Done (in `runs/full/`) |
| EasyOCR | `bn` reader, GPU | Done (in `runs/full/`) |
| Surya OCR 2 | 650M VLM, llamacpp/Vulkan backend (no docker on host); pages full-page mode, Mozhi crops block mode w/ synthetic full-image Text layout | Done (in `runs/full/`) |
| bbOCR | APSIS-Net via `apsisocr==0.0.7` (+ `fastdeploy` detector for pages), CPU | Done (in `runs/full/`) |
| DeepSeek-OCR v2 | `deepseek-ai/DeepSeek-OCR-2` (~3B, Apache-2.0) via `doc-page-extractor` v2 model class, fp16 | Phase 2: VRAM spike first (abort → v1 fallback), then adapter |
| EasyOCR-CRNN | `Sarjinkhan2003/bengali-crnn-easyocr` (MIT, ResNet34+BiLSTM+CTC) as EasyOCR `recog_network` swap, same env | Phase 2: download weights, extend `run_easyocr.py`, label third-party/unvetted |

**PaddleOCR-VL-1.6 is excluded.** No official quantized PaddlePaddle
checkpoint exists (PaddleSlim covers only PP-OCRv3 CNNs — DIY training, out
of scope); the official GGUF (`PaddlePaddle/PaddleOCR-VL-1.6-GGUF`) runs only
as a generic chat VLM, not the document pipeline (not like-for-like); local
RTX 2060 Super 8 GB OOMs in the native runtime (fully documented in
`pdf-craft-output/benchmark/ocr-engines/results_paddleocr_vl_SKIPPED.md`).

**Also excluded (assessed 2026-09-10):** classic PaddleOCR PP-OCRv3/v4/v5/v6
(no Bengali rec model exists); GOT-OCR 2.0 (English+Chinese only per paper,
fine-tune required); Bangla TrOCR (no public checkpoint — would need
training); Baidu AI Cloud OCR API/SDK (metered, account-bound, no
server-Linux build); ERNIE/PP-ChatOCR (key-info extraction, not recognition).

**Watchlist:** `baidu/Unlimited-OCR` (MIT, DeepSeek-OCR-based, EN/ZH only as
of Jul 2026; multilingual release announced) — 10-minute Bengali smoke test
once its multilingual release lands. `pdf_craft` already wires an
`UnlimitedOCRLocalConfig` path, so integration would be cheap.

## 3. Results to report (per engine × per split)

1. Micro CER / micro WER (primary, as `score_hypotheses` computes) + macro
   mean CER/WER.
2. `n` scored, `missing` count, error/timeout count + rate.
3. Timing: mean / median / p95 sec per page (REID), per 1k words (Mozhi).
4. Char-op breakdown (insert/delete/replace) from `benchmark.score`.
5. Per-item JSONL `{id, cer, wer, seconds, error}` retained for audit;
   lowest-agreement examples listed for spot-check.
6. Failure taxonomy sample (conjuncts, vowel signs, archaic forms,
   segmentation/reading-order on REID).
7. Provenance appendix, auto-recorded by Snakemake: dataset sha256 + item
   counts, engine + model/weight revision, normalization string, dataset
   licenses, hardware (GPU/VRAM or CPU), date.

Scoring reuses `pdf_craft_tool/benchmark.score` (NFC, curly-quote fold,
whitespace collapse; L/M/N word tokens; true Levenshtein via rapidfuzz), so
external numbers are directly comparable with in-domain gold-split numbers.
Caveat (per `pdf_craft_tool/research/metrics.py`): this is repo-normalized
CER/WER, not a native published metric — state it in the report. The
in-domain proxy pipeline (`benchmark/ocr-engines/`, `aggregate.py`
Bangla-ratio/similarity proxies, `qualitative_notes.md`) stays as-is and is
not part of the scored results.

## 4. Snakemake workflow (new `workflow/` dir)

`pdf_craft/` package and existing `scripts/ocr_benchmark/` scripts stay
untouched.

- `workflow/Snakefile` rules:
  - `verify_inputs` — sha256 + item counts of staged zips/dirs (fetch URLs
    recorded in config; no re-download by default).
  - `run_{tesseract,easyocr,easyocr_crnn,surya,bbocr,deepseekocr}` — thin
    adapters with a common CLI: references in, hypotheses JSONL
    `{id, text, seconds, error, model_rev}` out, using `reid:*` /
    `mozhi-test:*` ids from `external_eval`. GPU engines declare
    `resources: gpu=1`; launch with `--resources gpu=1` so they serialize on
    the 8 GB card while CPU jobs parallelize (`CORES` env, `run.sh`).
  - `score` — `external_eval.score_hypotheses` per engine × split.
  - `report` — summary CSV/JSON + results-table markdown under
    `pdf-craft-output/external-eval/runs/<date>/` (git-ignored).
- `workflow/config.yaml` — relative paths only (replacing the hardcoded
  `/scratch/...` absolutes in current scripts), model revisions, timeouts,
  device selection.
- `workflow/envs/*.yaml` — per-engine conda envs (isolates
  paddle/torch/easyocr/surya/apsisocr conflicts); snakemake + rapidfuzz live
  in the workflow env (`benchmark.score` requires rapidfuzz, currently only
  in the `catalogue` extra).
- Smoke rule: `smoke` target runs all 4 adapters on 2 REID pages + 20 Mozhi
  words for fast green-check.

## 5. Acceptance checks

- `snakemake --dryrun` graphs all rules; reruns reproduce scores (pinned envs;
  GPU engines may vary by ms in timing only).
- `pytest tests/test_external_eval.py` (+ benchmark scorer test in
  `tests/test_book_pipeline.py`) passes.
- Report contains all 7 items from §3 for all 6 engines × both splits, with
  DeepSeek-OCR prompts + peak VRAM and CRNN third-party labeling in provenance.

## 6. Execution order

1. (User, parallel/non-blocking) Ali-corpus outreach emails.
2. Phase 1 (done): 4-engine Snakemake benchmark → `runs/full/` report.
3. Phase 2: DeepSeek-OCR v2 VRAM spike (abort → v1 fallback) → `run_deepseekocr.py`
   + env + rules → smoke → full splits; EasyOCR-CRNN weights + rule → smoke →
   full splits; `report` rerun (existing four engines never recomputed).
4. Independent review of the `workflow/` diff only.

Optional, non-gating follow-ups: `baidu/Unlimited-OCR` Bengali smoke test
after its multilingual release; BCD3 (`bengaliai.github.io/bbocr`) third
split; Ali-corpus access; transformers+flash-attn PaddleOCR-VL spike.
