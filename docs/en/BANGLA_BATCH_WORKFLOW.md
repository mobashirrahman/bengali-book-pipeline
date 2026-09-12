# Bangla scanned-book workflow

This workflow converts a directory of image-only PDFs into Markdown, EPUB, or
both, then proofreads the OCR text without translating or rewriting it. It is
designed to resume safely after interruption and to isolate a failed book from
the rest of a large collection.

## Recommended proofreading model

Start with **Qwen 3.5 4B, Q4_K_M**, served by Ollama as `qwen3.5:4b`.

- Qwen's official model card describes Qwen 3.5 4B as a 4B-parameter model with
  support for 201 languages and dialects. That breadth makes it a stronger
  starting point for Bangla than small English-centric models.
- Ollama's `qwen3.5:4b` artifact is a 3.4 GB Q4_K_M model. It leaves useful room
  in the RTX 2060 Super's 8 GB VRAM for the KV cache and runtime buffers.
- `qwen3.5:9b` is 6.6 GB in Ollama. It may run with a small context or CPU
  offload, but it leaves too little margin to be the reliable unattended
  default on this GPU.
- Models around 2B are faster, but their tendency to guess, normalize, or miss
  contextual OCR errors makes them a poor default for archival text.

The relevant primary sources are the
[Qwen 3.5 4B model card](https://huggingface.co/Qwen/Qwen3.5-4B),
[Ollama's 4B artifact details](https://ollama.com/library/qwen3.5:4b), and
[NVIDIA's RTX 20-series specifications](https://www.nvidia.com/en-gb/geforce/graphics-cards/compare/).

This is a hardware-appropriate starting point, not proof that it is the best
model for a particular collection. There is no published result in the model
card that isolates historical Bangla OCR proofreading. Before a full run,
compare Qwen 3.5 4B against manually corrected pages from the actual books.

## Important OCR limitation

Proofreading and OCR are separate jobs. Qwen corrects text only after another
model has read each scanned page.

This repository currently supports DeepSeek OCR, DeepSeek OCR 2, and Unlimited
OCR through its extraction dependency. DeepSeek's published OCR weights are
about 6.7 GB before runtime overhead, so local OCR is tight on an 8 GB card.
Bangla quality must be tested rather than assumed. Run a varied 20–50 page pilot
containing old fonts, conjuncts, damaged pages, poetry, footnotes, and mixed
Bangla/English text.

For a future Bangla-specific OCR adapter, PaddleOCR-VL 1.5/1.6 is the most
promising current candidate: PaddleOCR documents a 0.9B document parser with
Bengali among its supported languages, and its direct PaddlePaddle/Transformers
path supports NVIDIA compute capability 7.0 or newer. It is not yet a pdf-craft
backend, so this repository does not silently substitute it. See the official
[PaddleOCR-VL documentation](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md)
and [PaddleOCR project releases](https://github.com/PaddlePaddle/PaddleOCR).

## Install

Use Python 3.11 and install Poppler, Poetry, the local OCR extra, and Ollama.
The package commands vary by Linux distribution; on Debian or Ubuntu the
project setup is:

```shell
cd /scratch/pdf-craft
sudo apt-get install poppler-utils
poetry config virtualenvs.in-project true
poetry install --with dev -E local
cp .env.template .env

ollama pull qwen3.5:4b
OLLAMA_CONTEXT_LENGTH=4096 OLLAMA_NUM_PARALLEL=1 ollama serve
```

Ollama's OpenAI-compatible API is exposed at
`http://localhost:11434/v1`; its required local API key is ignored. The checked-in
`.env.template` already maps the `proofread` profile to this endpoint. Ollama's
[OpenAI compatibility documentation](https://docs.ollama.com/api/openai-compatibility)
explains that contract. A 4K context is intentional: larger contexts consume
more VRAM, while pdf-craft sends smaller structure-preserving groups. See
[Ollama's context-length guidance](https://docs.ollama.com/context-length).

Configure one OCR block in `.env`. For the repository's validated OCR 2 local
path, select:

```dotenv
PDF_CRAFT_OCR_MODE=deepseek-ocr2-local
PDF_CRAFT_DEEPSEEK_OCR2_LOCAL_MODELS_CACHE_PATH=./models-cache
PDF_CRAFT_DEEPSEEK_OCR2_LOCAL_ONLY=false
```

Allow downloading for the first run. After the models are cached, change
`PDF_CRAFT_DEEPSEEK_OCR2_LOCAL_ONLY` to `true` for reproducible offline runs.

## Pilot before processing the collection

First inspect discovery without loading any model:

```shell
poetry run python -m pdf_craft_tool batch /path/to/books \
  --output-dir /path/to/converted --dry-run --limit 10
```

Then OCR a few representative pages. `--pages` uses 1-based page numbers:

```shell
ollama stop qwen3.5:4b
poetry run python -m pdf_craft_tool batch /path/to/books \
  --output-dir /path/to/pilot --stage extract --limit 3 \
  --pages 1,2,10,25 --ocr-mode deepseek-ocr2-local --ocr-size base
```

Start Ollama, then proofread and render those cached extractions without rerunning
OCR:

```shell
OLLAMA_CONTEXT_LENGTH=4096 OLLAMA_NUM_PARALLEL=1 ollama serve
poetry run python -m pdf_craft_tool batch /path/to/books \
  --output-dir /path/to/pilot --stage proofread --limit 3 --format both
```

For each sample, compare OCR and proofread text to a manual transcription. Track
character error rate separately before and after proofreading, and inspect all
LLM changes. Reject a setup that improves spelling while changing names,
quotations, numbers, old orthography, or the author's voice.

Collection-specific conservatism can be added with `--prompt`, for example:

```shell
--prompt 'Preserve pre-1971 spelling and Sanskrit-derived names exactly.'
```

## Full resumable run

The safest operation on one 8 GB GPU is two explicit phases:

```shell
# Phase 1: OCR. Stop any resident Ollama model first.
ollama stop qwen3.5:4b
poetry run python -m pdf_craft_tool batch /path/to/books \
  --output-dir /path/to/converted --stage extract \
  --ocr-mode deepseek-ocr2-local --ocr-size base

# Phase 2: local LLM proofreading and rendering.
OLLAMA_CONTEXT_LENGTH=4096 OLLAMA_NUM_PARALLEL=1 ollama serve
poetry run python -m pdf_craft_tool batch /path/to/books \
  --output-dir /path/to/converted --stage proofread --format both
```

`--stage all` performs the same two phases in one invocation. Explicit phases
are preferable for a multi-month corpus because they make GPU ownership and
maintenance windows obvious.

The output tree mirrors the source tree. Internal state is retained under
`OUTPUT/.work/RELATIVE_BOOK_NAME/`:

- `analysis/ocr/page_*.xml`: page-level OCR resume cache;
- `book.pcex`: immutable extracted document used for rerendering;
- `proofread.pcex`: proofread structured document;
- `proofreading-cache/`: deterministic LLM response cache;
- `proofreading-logs/`: request metadata without prompt text;
- `batch-report.json`: success or failure status for every book.

Rerunning the same command validates and reuses completed `.pcex` artifacts,
skips existing final outputs, retries incomplete work, and never overwrites a
completed book. Keep the `.work` directory until the collection has passed
quality review and backups.

## Capacity reality for 6,000 books

Measure pages per second on the pilot before estimating completion. For example,
6,000 books at 300 pages each is 1.8 million pages. At 10 seconds per page, OCR
alone is about 208 continuous days; at 30 seconds per page, it is about 625 days.
Proofreading adds a second, token-dependent pass. These are illustrative
calculations, not performance claims.

For a collection this large:

1. Establish a gold set and acceptance threshold before scaling.
2. Keep source PDFs, `.pcex` files, and final outputs on backed-up storage.
3. Run one OCR worker and one Ollama request at a time on the 2060 Super.
4. Use `--limit` for staged batches and inspect `batch-report.json` after each.
5. Consider additional GPUs or a remote OCR service after measuring the pilot;
   model selection cannot compensate for years of single-GPU throughput.

