# Research implementation — running results log

Baseline: `baseline.txt` (was HEAD dc91280; concurrent catalogue work has since
committed 2b912e5 + 87045aa on the same branch — my commits sit on top and touch
only research paths). Tracked-file diff outside my scope: unchanged relative to
baseline. All my work is new files under `pdf_craft_tool/research/`,
`tests/research/`, `research/`, `docs/en/RESEARCH_*`.

Commits on `feature/agent-swarm-16-backends` (a concurrent process moved HEAD to
this branch mid-run; my commits stack on top and touch only research paths).
Pushed to `origin/feature/agent-swarm-16-backends` for backup.
- `796dd4f` S0–S4 framework + pilot prep + docs (95 tests)
- `312d9b9` S5 gates + calibration + S8 skeleton (111 tests)
- `af5f28a` S6 external evaluation + report tables (125 tests)
- `65e4f43` S7 CLI + export; S4–S6 review fixes (142 tests)

## Agent workflow outcome

Coordinator (Sonnet) + architect (reused saved plan) + reviewer subagent +
OpenCode free backends. Delegation scorecard:
- muse-coder: S0 ✓, S2 ✓, S4 ✓ (3/4 completed cleanly)
- glm-coder (TokenRouter): S1 stalled after ~8 min with no output → re-run on muse ✓
- orca-coder (OrcaRouter): S3 blocked by content-moderation ("sensitive_words_detected") → in-house
- muse-coder: S5 stalled after exploration, no files → in-house

Free backends are reliable for ~1-module bounded tasks, unreliable for the
larger 4–5 file stages. S3 and S5 (correctness-critical evaluation core) taken
in-house; this is more token-efficient than repeated failed delegation cycles.

## Stage status vs acceptance criteria

| Stage | Status | Tests | Acceptance |
| --- | --- | --- | --- |
| S0 contracts/config | DONE (muse) | test_contracts 14 | MET — round-trip, unknown-key/hash/geometry rejection, gold-free inference, identical-hash determinism, immutable manifest, `git status` scope clean |
| S1 inventory/splits | DONE (muse) | test_inventory 5, test_splits 9 | MET — distinct counters on synthetic corpus, related texts never cross splits, seed/order stable, unknown stays unknown, unprocessed still sampled. Coordinator fixed real-data artifact path (summary.json nesting) + unassigned-family guard |
| S2 census/annotation | DONE (muse) | test_census 4, test_annotation 16 | MET — OCR-missed line kept in gold, annotators blind to drafts + each other, conflicts need adjudication, provisional/flagged never export, stale revision fails, image-hash mismatch blocks, disagreement trigger blocks promotion. Coordinator fixes: terminal flagged/provisional, NFC edit-distance disagreement, GET/POST split for conflict detection |
| S3 metrics/statistics | DONE (in-house) | test_metrics 14, test_statistics 10 | MET — hand-calc insert/delete/substitute/quote/Bengali/missing-line/repeated-line/swap/empty/zero-edit, identical → identical, gold-free rejected not perfect-scored, whole-family resample, zero harms ≠ safety claim |
| S4 runners/adapters/candidates | DONE (muse) | test_runners 8, test_candidates 8 | MET — retry/resume no dup + no prompt change, identity invalidation, injected gold rejected pre-invocation, spy verifies crop, uniform schema or explicit failure, immutable candidate-bank hash |
| S5 gates/calibration | DONE (in-house) | test_gates 7, test_calibration 9 | MET — held-out never fits/tunes, insufficient support blocks calibrated method + rule fallback, unknown evidence → abstain, do-nothing byte-exact, deterministic overlap policy, gold unreadable by gate, frozen policy immutable, reject-all ≠ improvement |
| S6 external/report | DONE (muse) | test_external 6, test_report 8 | MET — native never falls back to legacy scorer, common_text_metrics labelled + non-comparable, word/page not pooled, failed arm keeps a row, zero-coverage precision "undefined". Fix: per-page failure accounting |
| S7 CLI/export | DONE (muse) | test_cli 5, test_export 5 | MET — offline toy study runs inventory→…→report, inference export lacks gold, unapproved excluded with reason, path traversal rejected, every checksum matches, unknown command fails; `pdf_craft_tool/cli.py` untouched (verified byte-identical to baseline) |
| S8 audit/manuscript | skeleton + reviewer passes done | — | software-completable parts done (regeneration recipe, audit checklist, manuscript skeleton). Final table awaits real predictions/gold |

Full `tests/research`: **142 passing** (`.venv/bin/python -m unittest discover -s tests/research -p 'test_*.py'`).

Reviewer subagent, pass 1 (S0–S3): substantially meets acceptance, 6 minor
findings — ALL FIXED (`tests/research/test_review_fixes.py`).
Reviewer subagent, pass 2 (S4–S6): 2 major + 4 minor + 1 cosmetic.
- MAJOR per-page failure accounting in report → FIXED (ArmResult per-page
  fields; failure table + rebuild_from_records count per page).
- MAJOR calibration screen not structurally enforced → FIXED
  (`promote_calibrated_gate`; `freeze_policy` refuses an unscreened calibrated
  policy).
- MINOR reconcile dead code / degenerate threshold accept-all / proposal_bank_hash
  not content-addressed / native evaluator version → first two FIXED; the last
  two accepted as noted (bank_hash is content-strict and is what gates cite;
  native path is only reachable via an explicitly version-registered evaluator).
- COSMETIC unused `seed` on `GateClassifier.fit` — left (fit is deterministic).

## Deliverables

- `docs/en/RESEARCH_PROTOCOL.md`, `RESEARCH_ANNOTATION.md` (annot-1), `RESEARCH_RESULTS.md`
- `research/configs/{pilot,baselines,gates}.json`
- `research/proposals/development-sampling-60.md` + `.json` — concrete reproducible 12-family × 5-page pilot sample (seed 20260909) with human sign-offs listed
- `research/paper/{README,outline}.md` — manuscript skeleton, no numbers

## Results found (experimental)

**None yet, by design.** No model run, no annotation collected, no external
download, no cluster allocation. `RESEARCH_RESULTS.md` §2 is the regeneration
recipe for when a result exists. Prior local Sarat ~0.799% CER is agreement with
an imperfect EPUB, measured before this framework — not a result of this study.

## Human / rights inputs required to execute the pilot

1. Two Bengali-fluent annotators + one adjudicator for the 60-page pilot.
2. Rights determination (scans / transcriptions / metadata) per pilot family.
3. Confirm the 12 proposed strata represent observed corpus variation.
4. Confirm S1 `needs_confirmation` duplicate/edition groups.
5. Set the post-pilot inference budget and smallest useful CER reduction.
6. Approve cluster capacity for baseline runs (S4) beyond one reserved worker.
