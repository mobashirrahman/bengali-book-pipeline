# Research roadmap: trustworthy Bengali book digitization

Prepared 2026-09-09. This is a research proposal and implementation handoff, not a completed experiment. The current request reopens planning of the dataset topic deferred in `FUTURE_PLANS.md`; it does not start annotation, training, downloads or cluster changes.

**Recommendation:** build one paper around the question **“When should an OCR correction be trusted?”** Use a carefully verified sample of Bengali book scans to evaluate transcription fidelity and a correction policy that uses source-image evidence and can leave uncertain text unchanged. A useful first output is an independently annotated benchmark; the methodological contribution must be earned through experiments.

Detailed Claude tasks are in [the implementation plan](../../references/research-implementation-plan.md). Acceptance is never guaranteed by a project plan.

## 1. What the collection makes possible

The read-only inventory on 2026-09-09 found 24,586 PDFs and 123 EPUBs under `data/`, among 25,285 files occupying approximately 382 GB in apparent size. These are **file counts, not distinct literary works**, and include material whose languages, dates, editions, scan quality and redistribution rights still need verification. The continuously changing queue and generated artifacts are a separate inventory.

The audit found about 1,700 completed raw-extraction artifacts and 690 earlier-profile proofread artifacts. The current cluster configuration includes `--no-proofread`; historical correction artifacts and present production outputs therefore need separate provenance. Metadata is also sparse: the inspected 22,717-row metadata CSV had only 310 nonempty year fields, which are not necessarily verified publication dates.

A coordinator check at approximately 02:31–02:32 UTC found 216 review rows in `book-pipeline-sarat-10/gold/gold.sqlite3`: 182 `manual`, 27 `tesseract`, 4 `flag` and 3 `skip`. Thus there are **209 accepted review decisions from one book**, not zero reviewed labels. The database alone does not establish who supplied those decisions or independent adjudication. Reuse them as review candidates and preserve their history; they do not establish a representative held-out benchmark.

Existing research infrastructure is unusually helpful:

- Raw `.pcex` extraction, proofreading audits, source-image crops, and Markdown chunks with page/box provenance.
- `pdf_craft_tool/benchmark.py` and `external_eval.py` for initial scoring and external references.
- `gold_tasks.py`, `gold_store.py` and `gold_server.py` for line tasks, decisions and verified exports.
- `ConservativeProofreader` and `TesseractCropVerifier` for exact-span corrections and corroboration.

These components are in the current dirty worktree; availability is not evidence of a stable released research framework. The current annotation workflow exposes OCR/Qwen drafts and does not implement independent double annotation, adjudication, or work/edition-family test separation. Its line proposals derive from OCR boxes, so missing lines can disappear before evaluation.

The earlier Sarat result—approximately 0.799% CER and 1.235% WER on ten pages—measures agreement with an imperfect EPUB. It is neither human-verified collection accuracy nor evidence of an LLM improvement. Guarded runs made no changes; that observation motivates the study but cannot establish an effective correction method. See [the deployment evidence](../../references/cluster-pipeline.md).

## 2. Five plausible papers

| Direction | Concrete research question | Required evidence | Recommendation |
| --- | --- | --- | --- |
| Faithful OCR and selective correction | Can image evidence reduce harmful edits while fixing enough real errors on unseen books? | Blind gold transcriptions, strong baselines, error/coverage/cost comparisons | Best first project; reuses the most infrastructure |
| Search under real OCR errors | Which correction and retrieval choices improve Bengali evidence retrieval, particularly names, quotations and historical spelling? | Human-written queries, relevant source pages, fixed indexes and controlled OCR variants | Strong follow-up; start with retrieval before adding answer generation |
| Variation across editions and scans | Can a system distinguish OCR corruption from authentic textual variants? | Confirmed paired editions/scans, aligned passages and expert variant labels | Distinctive if the inventory contains enough pairs; feasibility currently unknown |
| Annotation efficiency | Which regions should humans review to reduce corpus errors per hour? | Timed annotation study, random-sampling baseline and untouched evaluation books | A useful extension to the first paper; needs real annotators |
| Bengali literary/cultural history | How does one specified linguistic or cultural phenomenon vary across a defensible period/genre sample? | Reliable dates/editions, a domain collaborator, confound controls and close reading | Worth pursuing when there is a substantive humanities question |

Examples for the last direction include changes in address forms, orthographic variation, or portrayals of urban life. Choose one question with a Bengali literature/history specialist. Folder names and a convenience collection cannot support claims about all Bengali literature, and OCR errors can mimic linguistic change.

A chatbot, a large text dump, or a fine-tuned model can support these projects, but none alone establishes a research contribution. Prioritize annotation and evaluation before spending on large training runs.

## 3. What would be new—and what already exists

This is a targeted literature check, not an exhaustive novelty determination. Recheck adjacent work before preregistration and submission.

| Prior work | What it already establishes | Implication for this project |
| --- | --- | --- |
| [REID2019](https://primaresearch.org/www/assets/papers/ICDAR2019_Clausner_REID2019.pdf) | Historical Bengali printed-document recognition and layout evaluation with PAGE ground truth | Do not claim the first historical Bengali OCR benchmark; include a compatible external evaluation |
| [BaDLAD, 2023](https://arxiv.org/abs/2303.05325) | A large Bengali document-layout dataset across multiple domains | Layout annotation alone is insufficient differentiation |
| [Gold Standard Bangla OCR Dataset, EMNLP 2023](https://aclanthology.org/2023.emnlp-industry.44/) | Millions of human-annotated character/word images across document types | Differentiate full-page fidelity, documented splits and correction behavior; verify actual dataset access separately |
| [bbOCR, 2023](https://arxiv.org/abs/2308.10647) | An open Bengali OCR pipeline | Include a Bengali-specific recognizer if reproducible, not only generic LLMs |
| [Levchenko, 2025](https://aclanthology.org/2025.lm4dh-1.7/) | Historical transcription evaluation, orthographic biases and correction regressions in Russian | “LLMs sometimes damage historical text” is already known |
| [Reading or Guessing?, 2026 preprint](https://arxiv.org/abs/2605.27750) | Visual grounding failures in Ancient Greek OCR and controlled perturbation analysis | Image-faithfulness claims need more than fluent output or another model's agreement |
| [ExtractConf, 2026 preprint](https://arxiv.org/abs/2606.24420) | Confidence estimation combining multiple signals for document-field extraction | Confidence gating itself is not new; compare simple alternatives and show what transfers to free-text correction |
| [BanglaWild, August 2026 preprint](https://arxiv.org/abs/2608.03884) | Bengali scene-text evaluation with OCR/VLM baselines and detailed errors | A generic Bengali VLM benchmark is crowded; scene text and historical book pages are distinct tasks |
| [KhatianDoc, September 2026 preprint](https://arxiv.org/abs/2609.03597) | Human-verified Bengali land-record tasks and diagnostic model evaluation | Language coverage alone is not a novelty claim; this project concerns book transcription |
| [OHRBench, ICCV 2025](https://openaccess.thecvf.com/content/ICCV2025/html/Zhang_OCR_Hinders_RAG_Evaluating_the_Cascading_Impact_of_OCR_on_ICCV_2025_paper.html) | OCR errors affect retrieval and generated answers | A follow-up needs Bengali/book-specific evidence or a new intervention beyond repeating that OCR affects RAG |

The proposed contribution is a **combination to investigate**: independent full-page ground truth, separation of OCR errors from authentic spelling, unseen work/edition families, harmful-edit measurements, and an empirically calibrated policy with explicit cost and coverage. If the policy is merely a rearrangement of existing heuristics, frame the paper as a resource and empirical study. Do not manufacture a method novelty claim.

Working title: *When Should OCR Corrections Be Trusted? Evaluating Faithful Transcription of Bengali Books*.

## 4. Research questions and experiment

1. **RQ1—fidelity:** Where do conventional OCR and vision-language models fail across observed typography, scan quality, genre and historical spelling?
2. **RQ2—correction:** Can a policy informed by the image improve transcription while reducing harmful edits relative to unrestricted correction and simple gates?
3. **RQ3—efficiency:** At a fixed inference or human-review budget, how much useful correction can the policy deliver on unseen books?

For RQ2, freeze candidate corrections once per proposer. Compare different acceptance policies on exactly those candidates; otherwise a better proposer can be mistaken for a better gate. Also report complete pipelines separately, including their proposal costs and failure rates.

The proposed gate is deliberately small: a rule-based baseline followed, if warranted, by a regularized classifier predicting whether a candidate lowers transcription error. Possible features are edit size, OCR confidence, agreement with a separately pinned recognizer, crop quality, and changes to names/numerals or known historical forms. Fit on training books, choose calibration/thresholds on separate calibration books, then lock the policy. A second Tesseract mode is a correlated signal, not an independent oracle. Missing evidence causes an explicit abstention.

The primary endpoint is the paired change in **macro per-family CER under the strict NFC policy**, B5 minus B0, across every eligible test page at a fixed inference budget. Lower is better; an unchanged output has zero improvement. Predeclare the smallest useful reduction and the budget after the pilot, before final-test access. Key secondary endpoints are damage to initially correct text, correction coverage, historical/name/numeral preservation and cost. Compare harmful-edit behavior to text-only correction and simple gates at comparable coverage/budgets; lower average CER alone does not justify a safety claim.

### Baselines and ablations

| Arm | Purpose |
| --- | --- |
| B0: unchanged production Tesseract configuration | Mandatory do-nothing correction baseline |
| B1: a reproducible Bengali-specific recognizer such as bbOCR | Specialist comparator; record availability/compatibility failures |
| B2: one current, locally feasible open document/VLM recognizer | Strong independent modern comparator; choose on development data and freeze digest/prompt |
| B3: B0 plus text-only correction | Measures context-only benefit and damage |
| B4: existing conservative exact-span and Tesseract-crop gate | Measures the existing repository policy |
| B5: proposed evidence-based gate with abstention | Tests incremental value |
| Ablations: edit-size rule, OCR-confidence threshold, alternate-recognizer agreement, calibrated gate without image features | Determines which evidence actually helps |

Use the already recorded Qwen profiles for initial reproducibility where feasible, but verify the exact architecture, digest, prompt and crop attachment. Do not assume all available models support Bengali equally. Baseline admission cannot depend on final-test scores. A model that cannot run is reported as unavailable, not silently replaced by a paid endpoint. Do not call a candidate an independent recognizer if it is the same model/weights in another prompt.

Separate two tracks: **recognition on shared gold line crops** isolates recognition; **end-to-end page transcription** includes detection, missing text and ordering. Never compare a word-crop score against a full-page score as though they were equivalent. The correction method may not fix segmentation errors; report that limit explicitly.

## 5. Dataset and annotation protocol

Start with **12 development work/edition families × 5 full pages = 60 pages**. Choose them across observed typography, quality, prose/poetry and mixed-script strata, with a reproducible sampling rule. Do not invent historical dates to fill strata. The pilot estimates error prevalence, disagreement, annotation time, candidate yield and variance across books.

A provisional full-study budget is **60 families × 10 probability-sampled pages = 600 pages**. Allocate 24 families to training/development, 12 to calibration and 24 to the locked test set. Pilot families may enter training only. Scale this budget using pilot variance, the smallest useful effect and actual annotator capacity **before opening test results**. These are planning numbers, not a sample-size or acceptance guarantee. Prioritize more independent books over many adjacent pages from a few books.

The 12-family calibration allocation is provisional. As an initial feasibility screen, require at least 100 beneficial and 100 harmful candidate outcomes spread across at least 10 calibration families, plus stable leave-one-family-out threshold behavior, before presenting a learned/calibrated gate as supported. These counts are an operational minimum, not statistical power or a low-risk guarantee; pilot variance and the harm tolerance may require substantially more evidence. If support is insufficient, enlarge development/calibration data before test lock or retain a simple rule-based policy and make an empirical/resource claim. If the pilot yields almost no candidate corrections, do not force a classifier experiment.

Create a separately reported challenge set of degraded pages or suspicious spans if useful. Do not merge error-enriched samples into the main prevalence estimate. Store selection probabilities for the main sample; report both per-book results and a properly weighted collection estimate only if the sampling frame supports one. Avoid sampling only successfully processed jobs, which would omit difficult failures.

Group byte duplicates, repeated scans, editions of the same work, and overlapping anthologies/collected works before splitting. Use metadata and candidate near-duplicate detection followed by human confirmation. Retain distinct editions for analysis, but keep related text in one split. Hashes alone cannot detect editions or overlapping content. Split consistency must survive input order and later corpus growth; freeze a manifest rather than silently moving books between versions.

For every sampled full page, humans first enumerate all readable text regions/lines, including text missed by OCR. Two Bengali-fluent annotators then transcribe independently from the image without seeing OCR, model identity, candidate corrections or each other's text. Resolve disagreements through adjudication and record reviewer IDs, revisions and the reason for decisions. Existing “verified” selections are candidates for this process, not automatically independent gold.

Preserve source spelling, punctuation, Bengali digits and visible historical variants. Keep a separate normalized search layer if needed. Specify policies for conjuncts, vowel signs, nukta, joiners, hyphenation, verse line breaks, footnotes and unreadable spans. Record uncertainty explicitly; do not have a model guess illegible text. Report exclusions by category and denominator. Independent spot audits must also inspect agreements, since both annotators can make the same error.

Use proposed pilot quality triggers of mean pairwise NFC character disagreement above 1%, exact-line agreement below 95%, or any systematic policy ambiguity. A trigger requires guideline revision, annotator calibration and a blind 10-page recheck before scaling. Adjudicate all disagreements regardless of these thresholds. Freeze any revised quality targets before main-study annotation; report actual agreement and audits rather than treating a passed threshold as proof of correctness.

Each record needs source hash and location, page convention, image hash, coordinate system/crop geometry, work/edition/overlap group, split, selection probability, raw OCR, model/config identities, annotation state, provenance and rights evidence. Gold and inference inputs must be separate exports; reference EPUBs and test labels never enter OCR/correction prompts, fitting, retrieval indexes used for correction, or threshold selection. Foundation-model pretraining contamination cannot be ruled out by our split; state that limitation.

Assess distribution rights for the chosen benchmark subset, separately for scans, transcriptions, metadata and any borrowed labels. Build the public resource from material with a documented release basis. Unknown-rights books may be excluded from release; a private-only collection does not satisfy a public-dataset paper's reproducibility claim. Confirm rights before investing heavily in annotation of a release candidate.

## 6. Metrics that prevent misleading success

| Measure | Definition / required qualification |
| --- | --- |
| CER and WER | Edit distance divided by reference length; micro and macro per book. Preserve a strict raw view and a documented NFC view; do not silently fold quotes or modernize spelling |
| Grapheme error rate | An additional explicitly versioned Unicode extended-grapheme view; not automatically equivalent to Bengali linguistic syllables/aksharas |
| Omitted lines and reading order | Score against the independent full-page census; report region omissions, text deletions and order errors separately |
| Beneficial / neutral / harmful edit counts | For a fixed aligned evaluation unit, classify the actual distance change after applying an edit; ambiguous/overlapping candidates need a deterministic joint-unit policy |
| Beneficial-edit fraction and exact correction precision | Report both distance-reducing accepted edits / all accepted edits and fully correct accepted replacements / all accepted edits; expose neutral/harmful counts; both undefined when no edits are accepted |
| Damage to initially correct text | Number/rate of initially correct aligned units made wrong; use a fixed character/token unit and denominator across methods |
| Coverage and residual error | Accepted candidates / all proposed candidates; also corrected raw errors, remaining errors, document changes and human-review burden. Abstaining everywhere has zero correction coverage |
| Cost | Wall time, CPU/GPU time, model calls/tokens, memory and measured annotation minutes. Correctly separate code-agent costs from experimental model costs |

Confidence intervals must use paired resampling of independent work/edition families, not millions of characters treated as independent observations. Lock the primary endpoint/contrast above, practical effect and harm tolerance in the protocol; exploratory slices remain exploratory. Report denominators and failure cases, including empty/truncated model output. Macro CER should not hide book length; provide micro CER as well.

Annotate a bounded set of source-spelling, name and numeral spans and report exact preservation on those subsets. A lower overall edit distance can conceal a newly damaged name or an authentic historical form replaced with a modern spelling; inspect these changes directly against the source.

Bootstrap intervals alone cannot establish extremely low harmful-edit risk when there are zero observed harms. Report event counts and uncertainty honestly; where useful, also report the fraction of independent books with any harm and an interval for that endpoint. A gate with no accepted corrections has undefined edit precision, not 100% precision. Avoid “guaranteed safe” or distribution-free claims: calibration books do not guarantee performance under new fonts or scan conditions.

Add one external benchmark. Existing `external_eval.py` imports references but uses the repository's normalized CER/WER scorer; it does **not** implement all published REID/Mozhi metrics. Reuse reference import where compatible, then either implement/use the versioned published evaluator or label the result a **common text-metrics evaluation**, with its normalization fully specified. Never compare the latter numerically to published native scores. Identify exact dataset version/split, provenance, licenses and any overlap with development data. If adaptation occurs, create a separate development partition and preserve an untouched evaluation portion. External comparisons must not reuse the imperfect Sarat EPUB as gold.

## 7. Step-by-step research schedule

| Stage | Claude delivers | Human/research decision | Finish condition |
| --- | --- | --- | --- |
| 0. Frame and audit | Reproducible inventory, source/artifact availability, metadata/rights coverage and related-work matrix | Select target question; identify annotators and feasible release subset | A concrete sampling frame, known unknowns and an experiment protocol |
| 1. Freeze pilot | Versioned schema, duplicate groups and 60-page development manifest | Confirm representative observed strata and source rights | No cross-split relatives; every selected page traceable |
| 2. Prepare annotation | Blind workflow, full-page census, adjudication and export validation | Two transcribers plus an adjudicator complete the pilot | Source-based labels, disagreement/audit report and measured time |
| 3. Validate scoring | Strict/normalized scores, omission checks, paired uncertainty and failure handling | Choose practical effect, harm tolerance and main-study budget | Hand-calculated fixtures and independent metric review pass |
| 4. Establish baselines | Pinned, resumable baseline runs on the pilot; error taxonomy and cost report | Decide whether the correction hypothesis merits a larger study | No gold leakage; reliable runs; sufficient real errors/candidates |
| 5. Freeze main study | Family splits, sampled pages, protocol/config hashes and locked test export | Fund/complete annotation; preregister analysis | Final-test labels remain sealed from development |
| 6. Implement and select policy | Shared candidate bank, simple gate baselines and calibrated gate/ablations | Select one final method using development/calibration only | Policy and model digests frozen before test |
| 7. Run final evaluation | Paired results, subgroup errors, independent external evaluation and costs | Interpret gains, null results and regressions | All preregistered rows reported; no selective dropping |
| 8. Write and release | Reproducible tables/figures, dataset card, release manifest and manuscript draft | Authors verify claims, rights and venue fit; submit | An independent person can reproduce the main table |

Allow roughly **12–16 weeks with annotation and engineering in parallel**, assuming a dedicated engineering lead, two annotators and part-time adjudication. Weeks 1–3 cover framing, software and the pilot; weeks 4–9 expand annotation and baselines; weeks 9–12 cover frozen experiments; weeks 12–16 cover analysis and writing. Annotation can dominate: 600 pages at an illustrative 10–20 minutes per page per annotator already require 200–400 person-hours for two passes, before adjudication, layout census and administration. Replace that illustration with pilot timings and actual weekly staff hours; part-time staffing can make the schedule substantially longer. If the budget is smaller, finish a smaller well-defined empirical study and narrow the claims.

If new annotation cannot be staffed yet, use the existing external benchmark labels for an initial reproducible feasibility study after checking their versions and terms. That can test the method and scoring infrastructure, but it cannot support claims of a newly verified collection benchmark.

A single reserved research worker may suffice for the pilot; measure before expanding. Do not change active OCR/publication releases or consume all bio01–bio24 capacity for an experiment. Additional training and allocation are subsequent execution decisions.

## 8. What makes the result submission-ready

The strongest outcome combines an independently verified releasable benchmark with a policy that beats credible simple baselines on unseen families, at useful coverage and a measured cost. A null method result can still support a valuable resource/analysis paper if the benchmark fills a demonstrated gap and the failure analysis is substantial. A no-edit system matching Tesseract is not evidence of improved OCR.

[IJDAR](https://link.springer.com/journal/10032/aims-and-scope) is a plausible document-analysis target: its dataset-paper policy explicitly requires a new publicly available dataset plus experiments demonstrating usefulness. A stronger generalizable method or carefully framed NLP empirical contribution could fit a future ACL/EMNLP venue through [ACL Rolling Review](https://aclrollingreview.org/cfp). Check the actual future call, track, timing and requirements before selecting a submission; these are fit assessments, not acceptance predictions. A focused cultural-heritage NLP workshop is another possible first venue.

Minimum paper package: a precise question and novelty comparison; collection/sampling/annotation documentation; main baseline table; harmful-edit versus coverage figure; per-family/subgroup and omission analysis; ablations; external evaluation; compute/annotation costs; reproducibility files and limitations. Every numeric manuscript claim must trace to an experiment artifact.

Keep search/QA as a follow-up unless the first paper is already complete. Its pilot could use 150–300 human-authored Bengali queries with page-level evidence, evaluate BM25/dense/hybrid retrieval on identical raw/corrected/gold sample variants, and score Recall@k and nDCG. Keep chunk boundaries and query sources controlled. Only then add generated answers, citation correctness and unanswerable questions. Those counts are proposed budgets, and a gold-index comparison must stay within the actually transcribed subset.
