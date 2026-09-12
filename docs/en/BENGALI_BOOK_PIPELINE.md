# Bengali book pipeline

The opt-in `book` command converts a PDF into a raw `.pcex`, a separately
proofread `.pcex`, EPUB 3, a complete Markdown book, per-chapter Markdown,
source-linked JSONL chunks, and correction/recovery logs. Existing conversion
commands and the library's default DeepSeek configuration are unchanged.

## Run

For author/publisher metadata, cover overrides, title pages, and re-publishing
saved extraction without OCR, see [Publication metadata and covers](PUBLICATION.md).

Install the package's normal dependencies and Poppler, then install Tesseract
and Bengali **tessdata_best** language data. This OCR path does not require CUDA
or `pdf-craft[local]`. Tesseract models are not downloaded automatically.
The isolated Tesseract executable and language data already used in this workspace
can be selected explicitly:

```bash
.venv/bin/python -m pdf_craft_tool book \
  "sample_book/sarat upannays.pdf" \
  --work-dir pdf-craft-output/my-bengali-book \
  --pages 4-13 \
  --tesseract pdf-craft-output/benchmark/tesseract-env/bin/tesseract \
  --tessdata models-cache/tesseract-best \
  --title "অরক্ষণীয়া — নমুনা" \
  --author "শরৎচন্দ্র চট্টোপাধ্যায়"
```

For another machine, use `--tesseract tesseract` and the installed language-data
directory. Remove `--pages` to process the entire PDF. Page numbers are **physical,
1-based PDF pages**, not printed page labels. Ranges such as `4-13,100,200-205`
are supported. Nonconsecutive pages are not joined into a single paragraph.

Proofreading expects an already running local Ollama server with `qwen3.5:4b`
installed. No server is started and no LLM is downloaded by this command.
Use `--model` and `--ollama-url` to select another installed model/server;
the server must support Ollama's JSON chat API. The model's installed digest
is recorded. An explicitly configured remote server receives selected OCR text.
Default operation stays on `127.0.0.1`.

For the cached installation in this workspace, start the server in a separate
terminal before running the example (stop it with Ctrl-C when finished):

```bash
OLLAMA_MODELS=/scratch/pdf-craft/models-cache/ollama \
OLLAMA_HOST=127.0.0.1:11434 \
  pdf-craft-output/benchmark/ollama/bin/ollama serve
```

Useful switches:

- `--no-proofread`: generate the reading/retrieval outputs directly from OCR.
- `--review-only`: collect proposals, but apply none.
- `--proofread-all`: inspect all eligible text blocks, not only suspicious ones.
- `--protected-word NAME`: repeat for names or spellings that must not change.
- `--chunk-tokens 800`: Markdown chunk ceiling, measured with `cl100k_base`.
- `--epubcheck /path/to/epubcheck.jar`: require EPUBCheck to pass; requires Java.
- `--easyocr-fallback`: collect a second reading of weak pages. EasyOCR is optional
  and must be installed separately; its first use may download its own models.
  `--easyocr-model-path` selects its cache. Alternate confidence is not compared
  numerically with Tesseract confidence or used to overwrite the primary text.

## Stages and recovery

Use `--stage extract`, `--stage proofread`, and `--stage render` to run stages
separately, keeping the same source/OCR arguments. `--stage all` is the default.
The latter two stages require the earlier extraction; `render` also requires
the matching proofread extraction unless `--no-proofread` is set. Currently stage
selection still preflights Tesseract and, when applicable, Ollama for model identity.

Each stage has a content/settings fingerprint. OCR fingerprints include the PDF,
selected pages, language data, executable and OCR settings. Proofreading also
includes the raw extraction hash, model digest, prompt and application policy.
Changing these creates a new stage directory rather than contaminating old caches.
Existing extracted files are validated and protected by SHA-256 sidecars. A work
directory lock prevents simultaneous writers. LLM responses are cached by input,
prompt and model identity; interrupted OCR resumes from completed page XMLs.

Read `WORK_DIR/run-summary.json` for the current output paths and review counts.
The main artifacts are:

```text
WORK_DIR/
  ocr/<fingerprint>/raw.pcex
  ocr/<fingerprint>/analysis/ocr/page_N.{xml,json}
  proofread/<fingerprint>/proofread.pcex
  proofread/<fingerprint>/audit/         # original, proposal, decision, output
  proofread/<fingerprint>/evidence/      # source-image word crops
  render/<fingerprint>/book/
    book.epub
    book.md
    chapters/chapter-NNNNN.md
    assets/
    chunks.jsonl
    source-map.json
```

Never delete the source PDF: all page coordinates and image evidence refer to it.
Page diagnostics contain TSV word confidences/boxes, quality flags and attempted
readings. Low-confidence, low-Bengali-ratio or low-ink-coverage pages trigger
alternate PSM and autocontrast retries. Unresolved pages retain their text **and
an original-page image** in the extraction and rendered outputs. OCR errors may
also fall back to page images. A successfully rendered book can therefore still
need review; consult the summary, not merely the exit code.

## What automatic proofreading means

The LLM proposes JSON word replacements, not a rewritten chapter. Validation
requires exact unique source text, whole Bengali words, disjoint spans, a maximum
two-character change per word, and a small paragraph edit budget. Digits,
punctuation, protected words and structural markup cannot change. Malformed,
oversized, uncertain or mixed-content requests remain unchanged and are logged.
Headings are not corrected; long blocks are currently routed to review.

**An LLM confidence value is not evidence of accuracy.** Before any structurally
valid edit is applied, the original word box is cropped from the PDF and OCRed
again at 2× scale using PSM 8 and 13. Both readings must reproduce the proposed
replacement with confidence at least 80. The crop and readings are logged.
These are correlated readings of one engine, not a guarantee of correctness.
Use review-only mode for a new edition until its held-out transcription benchmark
supports enabling automatic changes.

The exported `ConservativeProofreader` accepts a model request callable, optional
diagnostics and a `verify_edit` callable; without corroboration it never applies
edits. `PDFCraft.translate_extraction(raw, target, proofreader)` composes it with
the existing extraction transformer. `render_markdown_bundle` is also exported
for callers with an existing `.pcex`; renderers never depend on OCR caches.

## Reading and retrieval output

EPUB uses reflowable XHTML, reader-theme-friendly CSS, navigation and `bn`
language metadata. Existing extracted images and linked notes are preserved.
No font is embedded by default; the reader supplies a Bengali-capable font.
The installed EPUB generator's hardcoded language metadata is repaired in the
generated archive, without modifying the external package.

Markdown chunks carry source-document SHA-256, stable chapter/segment IDs,
physical pages, OCR-pixel bounding boxes and per-segment Unicode offsets. A chunk
does not split a Bengali word or combining sequence. An indivisible word longer
than the token ceiling is allowed to exceed it; its actual token count is stored.
Chunks are paragraph-first, without overlap or forced padding to the ceiling.
Joining chunks of a segment reproduces its Markdown exactly. Asset links are
relative to the accompanying chapter Markdown file, identified in each record.

## Benchmark

Use explicit, matching EPUB chapters rather than fuzzy matching the hypothesis
to whichever passage scores best:

```bash
.venv/bin/python -m pdf_craft_tool.benchmark \
  --reference "sample_book/sarat upannays.epub" \
  --epub-items index_split_002.html index_split_003.html \
  --raw /path/from/run-summary/raw.pcex \
  --proofread /path/from/run-summary/proofread.pcex \
  --output pdf-craft-output/benchmark.json
```

The scorer requires optional `rapidfuzz`. It reports Levenshtein character/word
error rates, insertions/deletions/substitutions, denominators and artifact hashes.
Normalization is NFC, collapsed whitespace and folded curly apostrophes; word
tokens contain Unicode letters, marks and numbers. Reference text never enters
OCR or proofreading prompts.

On Sarat pages 4–13 (12,758 reference characters, 2,105 words):

| Stage | CER | WER |
|---|---:|---:|
| Earlier plain Tesseract benchmark | 0.941% | 1.235% |
| Integrated Tesseract + deterministic normalization | 0.799% | 1.235% |
| Initial LLM edits without crop corroboration (rejected design) | 0.839% | 1.378% |
| Final crop-corroborated proofreader | 0.799% | 1.235% |

The final proofreader applied no changes on those ten pages. The CER improvement
over plain Tesseract is normalization, **not an LLM accuracy gain**. The EPUB
reference contains its own errors, so these are agreement measurements, not
verified transcription accuracy. Forty additional pages, spaced every 120 pages
from page 100 to 4780, also completed OCR without page-quality flags. That is a
recovery smoke test, not a held-out accuracy score. Their final proofreading pass
inspected 146 blocks, applied no changes and marked 145 blocks for review.

The supplied PDF has clean rasterized glyphs but a broken text mapping. Results
must not be extrapolated to skewed, stained or torn scans. Automatic dewarping,
Surya integration, dependable footnote detection from TSV, embedded fonts and
a manually aligned held-out accuracy corpus are not implemented here. Footnote
text is retained as ordinary text when Tesseract cannot classify it.

Sources: [Tesseract best models](https://github.com/tesseract-ocr/tessdata_best),
[Ollama chat API](https://docs.ollama.com/api/chat),
[EPUBCheck 5.3.0](https://github.com/w3c/epubcheck/releases/tag/v5.3.0).
