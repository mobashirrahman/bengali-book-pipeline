# Manuscript workspace (S8)

Skeleton only. No numbers until real predictions and gold exist
(`docs/en/RESEARCH_RESULTS.md` §2). Nothing here is submission-ready.

- `outline.md` — section-by-section outline mapped to the roadmap's
  "minimum paper package" (§8) and the required experiment artifacts.
- `tables/` — table specs; each names the artifact that fills it and the
  `report.py` function that generates it.
- `figures/` — figure specs (harm-vs-coverage, per-family CER).

Authors own interpretation, attribution, rights and submission decisions. Every
numeric claim in a draft must name its source artifact (prediction JSONL hash,
gold manifest hash, metric version). A later bug fix is a versioned correction
with a transparent rerun of every affected arm, never silent test-driven tuning.
