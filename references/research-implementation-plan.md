# Claude implementation handoff: Bengali OCR research

Date: 2026-09-09. Read [the research roadmap](../docs/en/RESEARCH_ROADMAP.md) for the scientific question, literature, sampling, annotation, metrics and decision gates. This file specifies proposed work; none of the new modules or commands below has been implemented by this planning task.

## Operating contract

Follow `AGENTS.md` and `references/agent-workflow.md`. Claude Code coordinates on Sonnet; use its architect/reviewer roles for judgment and the pinned OpenCode backends for bounded implementation and investigation. The one-shot architecture packet and result for this plan are saved in `pdf-craft-output/agents/research-20260909/`.

Preserve the existing dirty worktree. Several required gold/evaluation/proofreading modules are currently uncommitted; a fresh checkout of HEAD alone would omit them. Before implementation, record the exact baseline, existing staged/unstaged changes, required source hashes and artifact provenance. Use a dedicated feature branch/worktree with the required baseline explicitly carried over if needed; never reset, stash, clean or absorb unrelated changes. Acceptance means **no new changes outside assigned scope relative to that baseline**, not “the production directory is clean.”

Experimental code belongs under `pdf_craft_tool/research/`, tests under `tests/research/`, configuration under `research/configs/`, and generated data/results under `pdf-craft-output/research/<study-id>/`. Existing package APIs are inputs to research adapters. Do not change `pdf_craft/__init__.py`, package dependencies or deployed behavior to create the experiment. New experimental dependencies, if necessary, belong in an explicitly documented isolated research environment, not the library's runtime requirements.

Read the cluster handoff before inspecting live state. Open live databases read-only; never change production queue rows, services, worker releases or model tags. Run experiments against frozen source/config/artifact identities. Existing completed jobs are reuse candidates, not a random evaluation sample, and profiles may differ. An artifact path recorded in a job result must exist and match provenance before it is used.

The first implementation milestone is a reviewed pilot-ready research framework. Human transcriptions, rights decisions and resource allocation are real dependencies. If they are unavailable, finish independent software work and report the missing input; do not substitute model-generated gold or fabricate outcomes. No training, large downloads, external model spending, public dataset upload or additional cluster allocation is implied by this planning document alone.

## Shared interfaces to freeze first

Keep these versioned records small and validated. Prefer JSON/JSONL and existing/standard-library tools; do not add a database service.

| Record | Required contract |
| --- | --- |
| Source/page | Source SHA-256, work/edition/overlap group, page number convention, dimensions, image hash, original location, rights/provenance evidence |
| Sample | Stable page ID, split, sampling stratum/probability, random seed, manifest/schema version, file hashes |
| Gold | Page/line geometry and order, diplomatic text, unreadable regions, independent annotators, revisions, adjudicator, status and guideline version |
| Prediction | Sample ID, system/config/model/prompt/crop hashes, exact raw output, parsed text, failure state, timing and resource counters |
| Candidate | Stable original span coordinates, before/after strings, proposer ID, evidence references, proposal-bank hash; no gold fields |
| Decision | Candidate ID, gate/version/threshold, accept/abstain/reason and evidence; unchanged original retained |
| Evaluation | Prediction/gold/manifest hashes, metric/tokenization/Unicode versions, denominators, per-family results and statistical protocol |

Gold texts are scoring inputs only. Produce separate inference and evaluation manifests. Stable source/page IDs must not depend on mutable OCR text; keep legacy gold entry IDs as provenance links. Coordinates must name units, render scale and orientation. No implicit page-offset guessing or silently repaired out-of-bounds crops.

## Ordered implementation packets

Every path listed here is either explicitly existing or a proposed new file. Use standard-library `unittest` for the proposed small deterministic tests; the command prefix is `.venv/bin/python -m unittest discover -s tests/research -p`. A test filename below supplies the final argument. No normal test should download a model, access the network, require CUDA or convert an entire book.

### S0. Baseline, records and study configuration

Own proposed `pdf_craft_tool/research/{__init__,schema,config}.py`, `tests/research/{__init__,test_contracts}.py`, `research/configs/pilot.json` and `docs/en/RESEARCH_PROTOCOL.md`. Claude owns the scientific protocol; one coder owns the Python/config files. No other worker edits shared initialization or schema files.

Implement validation for the records above, path/provenance rules, versioned immutable manifests, and serialization without gold in inference records. Freeze the protocol's main endpoint, baseline candidates, sampling decisions and uncertainty rules as draft decisions to finalize after the pilot. Record exact software baseline and environment identities; a Git commit alone is insufficient for a dirty source tree.

Acceptance (`test_contracts.py`): round-trip fixture records; reject unknown split/state, missing hashes, invalid geometry and evaluation-only fields in inference input; identical canonical records produce identical hashes. Repository status comparison confirms only owned paths changed.

### S1. Corpus audit, grouping and sampling

Depends on S0. Own `pdf_craft_tool/research/{inventory,splits}.py` and `tests/research/{test_inventory,test_splits}.py`.

Read existing manifests and databases without writes. Distinguish files, content hashes, works/editions, processing profiles, unique pages, and available artifacts. Report missing metadata/rights and paths that no longer resolve. Build candidate duplicate/overlap groups; expose uncertain matches for human confirmation. Reuse cached fingerprints; do not hash/render the entire 382 GB collection merely to select a pilot. Generate a frozen probability sample and a separately identified challenge sample.

Acceptance (`test_inventory.py`, `test_splits.py`): a synthetic corpus with identical bytes, alternate scans, collected-work overlap and a failed job yields correct distinct counters; related texts never cross splits; seed/order changes do not silently mutate an existing manifest; unknown metadata remains unknown; a fixture containing unprocessed books remains eligible for sampling. Human check: confirm uncertain groups and release eligibility.

### S2. Full-page census and independent annotation

Depends on S0; integrates with S1 manifests. Own `pdf_craft_tool/research/{census,annotation,annotation_server}.py`, `pdf_craft_tool/research/static/`, `tests/research/{test_census,test_annotation}.py`, and `docs/en/RESEARCH_ANNOTATION.md`.

Reuse `GoldDataset`, `GoldStore` exports and existing source-crop validation through an isolated research adapter. Keep research annotations separate from legacy decisions. Do not rebuild the whole production review system. Implement page census, independent assignments, revision conflicts, adjudication and a final gold export. Existing crop-first tasks alone cannot supply the census: reviewers need the whole page and a way to add OCR-missed regions. Blindness is enforced in server payloads/assignment state, not just by hiding controls in the browser. Preserve existing loopback/token/origin protections.

Acceptance (`test_census.py`, `test_annotation.py`): a fixture with an OCR-missed line retains that line in gold; first and second annotators cannot receive machine drafts or each other's text; conflicts require adjudication; provisional/flagged labels cannot enter final export; stale revisions fail; source-image/hash mismatch blocks annotation; exceeding the configured disagreement trigger blocks promotion to main-study annotation. Human check: independently transcribe the pilot, apply the roadmap's guideline/recheck decision rule and audit a sample of agreements as well as disagreements.

### S3. Metrics and statistical protocol

Depends on S0; develop in parallel with S1/S2 using shared fixtures. Own `pdf_craft_tool/research/{metrics,statistics}.py` and `tests/research/{test_metrics,test_statistics}.py`.

Reuse low-level edit-distance machinery, but do not call legacy `benchmark.score()` a strict scorer: it folds curly quotes. Add explicit raw/NFC text policies, word-unit rules, omission/order scoring, before/after error accounting, exact correction precision versus beneficial-edit fraction, sensitive-span preservation, coverage and paired family-level intervals. Version any extended-grapheme segmentation implementation and its Unicode tables; do not approximate graphemes by stripping combining marks. If a supported segmenter is unavailable, label that additional metric unavailable pending an isolated dependency decision. Include all failures and denominators.

Acceptance (`test_metrics.py`, `test_statistics.py`): hand-calculated insert/delete/substitute, quote, Bengali combining-mark/joiner/numeral, missing-line, repeated-line, swapped-region, empty/truncated prediction and zero-edit cases; identical predictions reproduce the same metrics; gold-free inference fixture rejected by scorer for missing gold rather than assigned a perfect score; resampling draws complete families and pairs systems; zero harms do not produce an unsupported safety assertion. Test statistical invariants with an independently derived toy example, not a hard-coded output copied from the implementation.

### S4. Frozen baseline runners and candidate bank

Depends on S0/S1; S2 gold is needed for pilot scoring, not inference. Own `pdf_craft_tool/research/{runners,adapters,candidates}.py`, `research/configs/baselines.json`, and `tests/research/{test_runners,test_candidates}.py`.

Wrap the existing OCR/proofreading facilities, preserving raw/proofread separation. Implement explicit model/config identities, inference-only inputs, prediction/proposal caching, resume and budget limits. Unsupported baseline adapters must fail clearly; no placeholder predictions. Finish mocked/offline integration first, then run only the allocated pilot. Save raw responses, parsing failures and image-attachment identities. Baseline B2 selection uses development results and feasibility, then freezes.

Acceptance (`test_runners.py`, `test_candidates.py`): retry/resume neither duplicates outputs nor silently changes prompts; model/prompt/crop changes invalidate cache identity; a deliberately injected gold field is rejected before invocation; a spy client verifies the intended crop is attached; all registered baselines produce the same output schema or explicit failures; immutable candidate-bank hashes match across gate arms. Human check: inspect a bounded real sample against the scan before scaling.

### S5. Acceptance policies and calibration

Depends on S2–S4. Own `pdf_craft_tool/research/{gates,calibration}.py`, `research/configs/gates.json`, and `tests/research/{test_gates,test_calibration}.py`.

Implement unchanged/raw, existing-policy adapter, simple threshold/size/agreement baselines, and the proposed small classifier only after pilot evidence warrants it. Fit classifier parameters on training books; fit/select calibration and operating thresholds on calibration books only. Enforce the roadmap's calibration-event and family-support screen, with an explicit simple-policy fallback if it fails; do not present the screen as a statistical guarantee. Separate fitting, prediction and scoring APIs. Reuse the same candidates for gate ablations and account for verifier cost. Freeze conflict resolution for overlapping edits, abstentions and malformed proposals. Store the frozen policy artifact before final test.

Acceptance (`test_gates.py`, `test_calibration.py`): held-out IDs/labels cannot enter fitting or tuning; insufficient class/family support blocks a calibrated-method promotion; unknown evidence leads to abstention; do-nothing leaves exact original bytes intact; incompatible/overlapping edits follow the declared deterministic policy; beneficial/neutral/harmful fixture decisions are scored independently; inference cannot read gold files; all policies identify their exact proposal-bank hash. Scientific check: distinguish reduced error from merely rejecting every proposal.

### S6. External evaluation and study reports

Depends on S3–S5. Own `pdf_craft_tool/research/{external,report}.py`, `tests/research/{test_external,test_report}.py` and `research/configs/final.json`.

Reuse `external_eval.py` reference import for the exact available REID/Mozhi version, preserving external splits and reporting overlap/rights limitations. Its legacy scorer does not provide the full published native metrics. Implement/use the versioned published evaluator for a `native` mode if feasible; otherwise provide an explicitly labeled `common_text_metrics` mode and prohibit comparisons to published native scores. Produce baseline and ablation tables, error/coverage/cost plots, failure denominators and per-family paired intervals from saved predictions. Record the primary strict-NFC macro-CER contrast B5 minus B0 and secondary harm/coverage endpoints. Freeze configuration before scoring the final test. A later bug fix requires a versioned correction and transparent rerun of all affected methods, not silent test-driven tuning.

Acceptance (`test_external.py`, `test_report.py`): fixture import preserves reference IDs and split; unavailable `native` evaluation fails rather than calling the legacy scorer; word/crop/page or differently normalized scores cannot be pooled under one unlabeled metric; all configured arms appear even if they fail; zero-edit precision is undefined with coverage zero; table totals reconcile to record counts; rerunning a report from the same records reproduces numeric tables. External data downloads and real runs await the experiment's execution/resource scope.

### S7. Research CLI integration and reproducible export

Depends on S1–S6. Own `pdf_craft_tool/research/{__main__,export}.py`, `tests/research/{test_cli,test_export}.py` and `research/README.md`. This is a research-specific module entry point; do not enlarge the shared production CLI during parallel tasks.

Provide planned subcommands `inventory`, `sample`, `validate`, `annotate`, `run`, `fit-gate`, `evaluate`, `report`, and `export`. Accept explicit study root/config/manifest arguments; keep annotation, inference and scoring roles separate. Export only records/assets with a documented release decision; keep secrets, private source paths, unapproved books and hidden-test labels out of public inference bundles. Rebuild tables offline from saved predictions.

Acceptance (`test_cli.py`, `test_export.py`): offline toy study completes from validated fixtures to final table; inference export lacks gold; unapproved content causes exclusion with a manifest reason; path traversal/out-of-root asset references are rejected; every exported asset checksum matches; unknown commands/invalid configs fail with actionable errors. These example subcommands are proposed, not commands users can run today.

### S8. Independent audit and manuscript package

Depends on S6/S7. Claude owns `docs/en/RESEARCH_RESULTS.md` and `research/paper/` (proposed). Tables/plots and model/data artifacts remain generated outputs until an explicit release selection. Human authors own interpretation, attribution, rights and submission decisions.

Review annotation protocol, metrics and actual scoped implementation diffs with Claude's reviewer. Capture commands and real test results for it; do not let a backend be the sole reviewer of its own code. Have an independent person reproduce the main table and inspect a randomized set of claimed corrections. Draft the manuscript from frozen artifacts, including nulls, exclusions and limitations. No speculative result numbers.

Acceptance: a clean, documented research environment regenerates the main table from the released predictions/gold; every numerical manuscript claim names its source artifact; the stated contribution survives comparison to the roadmap's adjacent papers. Select the venue and submission cycle on the strength of the actual result.

## Execution order and delegation

S0 fixes interfaces. Then S1, S2 and S3 have disjoint owned scopes and can run concurrently on Muse, GLM/TokenRouter and GLM/OrcaRouter respectively, using the exact pinned free models in `AGENTS.md`. S4 follows a usable manifest; S5 follows usable labels and candidates; S6→S7→S8 complete evaluation and handoff. S2 and real annotation will usually be the critical path. Shared contract changes return to the S0 owner at a synchronization point, never through competing worker edits.

For each bounded packet: state files, dependencies, acceptance cases and forbidden mutations; save packet, complete log and session ID under `pdf-craft-output/agents/`. Use `--pure`. Put the positional message before `--file` as documented in the operational reference, since `--file` consumes an array. Example:

```bash
opencode run --pure --agent muse-coder --model opencode/muse-spark-1.3-contributor-free "Implement the attached task" --file /absolute/path/to/handoff.md
```

One worker per backend. Use the three pinned free routes before the metered B.AI route; do not silently change models or wire up paid fallbacks. Honor quota/retry errors. Return a concrete defect to the same coder session for one focused repair; never reuse a coder session as the independent reviewer.

Run relevant new tests through an investigator, plus existing `test_gold.py`, `test_external_eval.py`, `test_proofreading.py` or `test_review.py` only when their boundaries are touched. Claude independently validates anything gating a commit, compares against the initial dirty baseline, and stages only intended paths. Do not run all OCR conversions to validate ordinary Python changes.

## Copy-paste starting prompt for Claude

> Read AGENTS.md, docs/en/RESEARCH_ROADMAP.md and references/research-implementation-plan.md. Implement S0 and the pilot-preparation software in S1–S3, preserving all existing work and running services. Start by recording the current source/artifact baseline and reuse the saved architecture; clarify only choices that materially block a task. Follow the pinned agent workflow and disjoint ownership. Use synthetic fixtures for development, and prepare a concrete 60-page development sampling proposal from read-only metadata. Do not start model training, large downloads, new cluster allocation, public release, or claim that machine drafts are human gold. Complete and verify the independent software work; then hand over the pilot manifest/protocol, tests, annotation interface and the specific human/rights inputs needed to execute the pilot. Report each implemented stage and any remaining gaps against its acceptance criteria.
