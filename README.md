# Bengali book pipeline

A local pipeline for digitizing scanned Bengali/Bangla books: OCR (Tesseract,
via a [pdf-craft fork](https://github.com/mobashirrahman/pdf-craft)) plus
conservative LLM proofreading, a catalogue for a large scanned-book
collection, gold-standard dataset tooling for evaluating OCR engines, and a
blinded, multi-voter research study comparing OCR engines on Bengali text.

This is one person's local processing pipeline, not a hosted service — every
piece runs on your own machine or SSH-reachable hosts, and there's no
included book data (a scanned personal collection is explicitly gitignored;
see [Data](#data)).

## Components

| Component | What it does | Docs |
| --- | --- | --- |
| `pdf_craft_tool.book` | PDF → Tesseract OCR → conservative LLM proofreading → EPUB + Markdown/chunks, restartable by stage | [Bengali book pipeline](docs/en/BENGALI_BOOK_PIPELINE.md), [batch workflow](docs/en/BANGLA_BATCH_WORKFLOW.md) |
| `pdf_craft_tool.cluster` / `cluster_worker` | SSH book farm: one coordinator, durable per-host workers, incremental discovery | [Cluster pipeline](docs/en/CLUSTER_PIPELINE.md) |
| `pdf_craft_tool.publish` / `publication_queue` | Re-publish a saved extraction (no OCR/GPU/LLM) with editable metadata and covers | [Publication](docs/en/PUBLICATION.md) |
| `catalogue` | Book catalogue: dedup, title/author resolution, covers, ratings, a small API + [frontend](frontend/) | [Catalogue backend](docs/en/CATALOGUE_BACKEND.md), [dedup](docs/en/CATALOGUE_DEDUPLICATION.md), [cover verification](docs/en/CATALOGUE_COVER_VERIFICATION.md), [metadata](docs/en/CATALOGUE_METADATA.md) |
| `pdf_craft_tool.gold_server` | Gamified line-level gold-label adjudication website for building an OCR benchmark reference | [Gold-standard datasets](docs/en/BANGLA_OCR_GOLD_STANDARD_DATASETS.md) |
| `pdf_craft_tool.review_server` | Manual review website for completed cluster OCR jobs | [Bengali book pipeline](docs/en/BENGALI_BOOK_PIPELINE.md) |
| `pdf_craft_tool.benchmark` / `external_eval` | CER/WER scoring against gold labels and third-party reference splits (BL REID2019, Mozhi) | [OCR engine benchmark](docs/en/OCR_ENGINE_BENCHMARK.md) |
| `pdf_craft_tool.research` + `workflow/` | Blinded 5-voter OCR-comparison research study (Tesseract/EasyOCR/Surya/vendor engines), Snakemake-driven | [Research protocol](docs/en/RESEARCH_PROTOCOL.md), [annotation](docs/en/RESEARCH_ANNOTATION.md), [pilot runbook](docs/en/RESEARCH_PILOT_RUNBOOK.md), [roadmap](docs/en/RESEARCH_ROADMAP.md), [results](docs/en/RESEARCH_RESULTS.md), [implementation status](research/IMPLEMENTATION_STATUS.md) |

Deferred/future scope (not currently running) is tracked in
[FUTURE_PLANS.md](docs/en/FUTURE_PLANS.md), including a DeepSeek-OCR-2 vs.
Tesseract fine-tuning feasibility writeup
([DEEPSEEK_OCR_BANGLA_FINETUNE.md](docs/en/DEEPSEEK_OCR_BANGLA_FINETUNE.md)).

## Installation

```bash
git clone https://github.com/mobashirrahman/bengali-book-pipeline.git
cd bengali-book-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

This installs [pdf-craft](https://github.com/mobashirrahman/pdf-craft) (this
project's fork, with the Tesseract backend and conservative proofreader) plus
the catalogue's SQLite backend. Add `[postgres]` for the Postgres catalogue
backend, or `[local]` to also pull pdf-craft's local-GPU OCR extra:

```bash
pip install -e ".[postgres,local]"
```

Tesseract itself is a system dependency: install it and the Bengali
(`ben`) traineddata separately (see the
[OCR Backend Guide](https://github.com/mobashirrahman/pdf-craft/blob/main/docs/en/OCR_BACKENDS.md)
in the pdf-craft fork). Copy `.env.template` to `.env` and fill in what each
component you're using needs — see that file's own comments; nothing in it
is required for components you don't run.

## Quick start: one book

```bash
python -m pdf_craft_tool book "your-book.pdf" \
  --work-dir pdf-craft-output/your-book \
  --tessdata /path/to/tessdata_best \
  --pages 1-10
```

See [BENGALI_BOOK_PIPELINE.md](docs/en/BENGALI_BOOK_PIPELINE.md) for the full
command reference, model recommendations, and the metadata/cover sidecar
format. For processing a whole collection across multiple machines, see
[CLUSTER_PIPELINE.md](docs/en/CLUSTER_PIPELINE.md).

## Data

No book files, scans, or the catalogue database are included in this
repository (`data/`, `sample_book/`, `pdf-craft-output/`, and
`models-cache/` are gitignored). This pipeline processes your own scanned
collection; nothing here downloads or bundles book content.

## Development

```bash
pip install -e ".[postgres]" pytest
pytest tests/
```

`tests/catalogue/` and `tests/research/` need no external services by
default; Postgres-backed tests are marked `integration` and skip unless
`CATALOGUE_POSTGRES_DSN` is set.

## Relationship to pdf-craft

This pipeline depends on
[mobashirrahman/pdf-craft](https://github.com/mobashirrahman/pdf-craft), a
fork of [oomol-lab/pdf-craft](https://github.com/oomol-lab/pdf-craft) (MIT
license) that adds a CPU-only Tesseract OCR backend, a conservative
exact-span proofreader, reading-structure recovery, and EPUB publication
metadata. Everything in *this* repository — the catalogue, cluster
coordinator, gold/review websites, benchmarking, and the research study — is
pipeline-level tooling built on top of that library, not part of pdf-craft
itself.

## License

MIT — see [LICENSE](./LICENSE). Depends on pdf-craft (MIT, Tao Zeyu and the
oomol-lab team; Tesseract backend and other additions by Md Mobashir Rahman).
