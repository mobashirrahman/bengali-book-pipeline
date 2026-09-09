# Outline — "When Should OCR Corrections Be Trusted? Evaluating Faithful Transcription of Bengali Books"

Working title (roadmap §3). Frame as a **resource + empirical study** unless the
gate demonstrably beats credible simple baselines on unseen families; do not
manufacture a method-novelty claim.

## 1. Introduction
- The problem: OCR-correction systems can damage historical text while fixing
  errors; when is an edit trustworthy?
- Contribution *to investigate* (roadmap §3): independent full-page ground
  truth + separation of OCR error from authentic spelling + unseen
  work/edition families + harmful-edit measurement + empirically calibrated
  policy with explicit cost/coverage. Null method result still yields a
  resource/analysis paper.

## 2. Related work
Fill from the roadmap §3 matrix (REID2019, BaDLAD, Gold Standard Bangla OCR,
bbOCR, Levchenko 2025, Reading or Guessing 2026, ExtractConf 2026, BanglaWild,
KhatianDoc, OHRBench). Re-check adjacent work before preregistration.

## 3. The benchmark
- Collection and sampling frame (S1). Selection probabilities.
- Duplicate / edition / overlap grouping; split construction; no cross-split
  relatives (frozen manifest).
- Full-page census + blind double annotation + adjudication (S2). Guideline
  version `annot-1`. Agreement, audit of agreements, exclusion by category.
- Diplomatic transcription policy (conjuncts, matras, nukta, joiners,
  Bengali digits, historical spelling, verse breaks, footnotes, illegible).
- Rights basis per released artifact class.
- Table: benchmark composition by stratum. → `report` record counts.

## 4. Metrics and protocol
- raw / strict-NFC / search text views; grapheme view (versioned).
- Two tracks never pooled: recognition on shared line crops vs end-to-end page.
- Omission / reading-order scored against the census.
- Beneficial / neutral / harmful edit accounting; damage to initially-correct
  text; exact-correction precision vs beneficial-edit fraction (both undefined
  at zero coverage).
- Sensitive-span preservation (name / numeral / historical spelling).
- Paired bootstrap over whole work/edition families. Event counts for harm, not
  just intervals. Predeclared primary contrast: strict-NFC macro-family CER,
  B5 − B0, at a fixed budget.

## 5. Systems
- B0 unchanged Tesseract; B1 Bengali-specific recogniser (record availability
  failures); B2 open VLM (frozen digest+prompt, chosen on dev only); B3
  text-only correction; B4 existing conservative gate; B5 evidence gate with
  abstention. Ablations: edit-size, OCR-confidence, alt-recogniser agreement,
  calibrated gate without image features.
- Frozen candidate bank; every gate scored on byte-identical candidates.
- Calibration-event screen (≥100 beneficial / ≥100 harmful / ≥10 families +
  LOFO stability) with an explicit rule-based fallback.

## 6. Results
- Table 1: baseline/ablation table, every arm present (failures shown).
- Figure 1: harmful-edit rate vs coverage.
- Table 2: per-family / subgroup CER, omission analysis.
- Table 3: ablations — which evidence helps.
- External evaluation (labelled `common_text_metrics` unless a versioned native
  evaluator is available; never compared numerically to published native
  scores).
- Costs: model calls / tokens / wall / annotation minutes; code-agent cost
  separated from experimental model cost.

## 7. Discussion / limitations
- Foundation-model pretraining contamination not excludable by the split.
- Calibration books do not guarantee performance under new fonts/scans.
- Segmentation errors the correction method cannot fix.
- Convenience collection: no claims about "all Bengali literature".

## 8. Reproducibility appendix
- Release manifest, dataset card, config/protocol hashes, regeneration recipe
  (`RESEARCH_RESULTS.md` §2). Independent reproduction of Table 1 required.
