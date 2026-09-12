# AI-Assisted Bangla Transcript Study Plan (locked)

Status: approved design, phased execution. Complements (never alters) the
human-human pilot (`pdf-craft-output/research/pilot-study/`, 60 pages,
2 annotators + 1 adjudicator, protocol in `docs/en/RESEARCH_PROTOCOL.md`).

## Objective

Build a 1000-page in-domain Bangla transcript dataset with AI annotators and
solo human adjudication, as a second paper artefact next to the pilot:
(a) human-human adjudicated pilot, (b) AI-annotated + human-adjudicated main
set. Report cross-dataset agreement; never pool the two.

## Voter set (5, full voters from the start)

| # | Engine | Type | Notes |
|---|---|---|---|
| 1 | Tesseract `ben` (frozen config = B0) | local CPU | cheap baseline, verbatim `ocr_text` path exists |
| 2 | EasyOCR `bn` | local GPU | |
| 3 | Surya OCR 2 (llamacpp/Vulkan) | local GPU | best page engine in `workflow/runs/full/` (CER 0.157) |
| 4 | Google Cloud Vision `DOCUMENT_TEXT_DETECTION` | vendor | 1000 pages/mo free; Bengali supported |
| 5 | Azure AI Vision Read (F0) | vendor | 5000 transactions/mo free, 20/min throttle; page PNGs only (never PDFs — F0 reads first 2 PDF pages only) |

Non-voting reviewers: bbOCR (joins after fastdeploy-detector fix; votes only
on fully processed pages, no backfill) and Qwen text corrector (conditioned
models never vote; feeds the suspicious-agreement queue).

Excluded: AWS Textract (Bengali support unconfirmed, 3-month trial only),
OCR.space (Tesseract-derived — not independent), expiring commercial trials.

## Safeguards (pre-registered, non-optional)

1. **Quorum:** unanimous = 5/5 successful votes after NFC normalization →
   provisional accept. Everything else (4-1, 3-2, 2-2-1, any failure) → human
   conflict queue. Majority is never truth (correlated failures: conjuncts,
   vowel signs, archaic print, shared modern-Bengali training data).
2. **Failures are rows, not gaps:** per `runners.py`, failed engines yield
   failure-state predictions; agreement denominator recorded per line.
3. **Vendor drift canary:** pinned 20-page set re-runs monthly; on output
   drift, that vendor's votes version-split from that date — old/new never pooled.
4. **Blinded adjudication:** resolver sees anonymized A/B/C/D/E, engine
   identities stripped server-side (extend `_assert_blind` pattern).
   Resolver is never an annotator on that page; every resolution records a reason.
5. **Blind-spot defense (unanimous-wrong + unanimous-omission):**
   - coverage audit (~10% of agreements, rate calibrated in Phase A);
   - risk-weighted suspicious-agreement queue (low confidence, rare conjuncts,
     archaic vocab, lexicon misses; any reviewer disagreeing with a unanimous
     vote routes the line to human);
   - omission check: every census region ends with verified text or explicit
     "no text" + missed-text proposals + full-page spot reads.
6. **Leakage:** `assert_no_gold_fields` on all inference inputs; vendors receive
   page images only, never reference text. Keys in gitignored `/.env`
   (`GOOGLE_VISION_API_KEY`, `AZURE_VISION_KEY`, `AZURE_VISION_ENDPOINT`);
   never in repo, packets, or chat.
7. **Rights gate:** no vendor call until per-book third-party-disclosure review
   is recorded; vendor DPA answers logged in provenance; third-party processing
   disclosed in the paper. Export stays blocked for release until rights are
   recorded (runbook §7).

## Phases

**Phase A (60 pages, shared inputs, separate study root).** New study root
(e.g. `pdf-craft-output/research/main-study-1000/`) with own manifest/DB/users;
the 60 pilot page-images referenced sha-pinned, no pilot rows touched. Full
5-engine loop + adjudication + audit. Outputs: measured disagreement rate,
adjudicator lines/hour, calibrated audit fraction, human-human vs AI-assisted
agreement on identical inputs, vendor Bengali verification. Gate for Phase B.

**Phase B (+940 pages, gated on Phase A numbers).** 40–60 families × 15–25
pages (wider, not deeper — family-bootstrap CIs need families), new seed,
recorded selection probabilities, rights cleared per book first. No commitment
beyond 1000 without a second adjudicator.

## Reproducibility (Snakemake)

Study workflow (new `workflow/study/` or extended `Snakefile`): freeze/sample →
per-engine inference (local conda envs as in `workflow/envs/`, vendor adapters
via env keys) → `PredictionCache`-backed resume → triage/conflicts →
adjudication DB → coverage audit → `export.py` gold bundle + agreement/score
report. GPU engines serialize (`resources: gpu=1`); vendor calls carry quota
budgets (`max_model_calls` = free-tier cap) and version/date provenance.
Reruns reproduce--except recorded vendor-drift splits, which are explicit.

## Reporting

Per engine × split: micro CER/WER (`benchmark.score`, comparable with
`workflow/runs/full/`), n/missing/errors, timing, failure taxonomy; study
level: pairwise AI agreement, human-AI agreement, kappa-style stats, estimated
residual unanimous-error rate with family-bootstrap CIs, canary drift log.
