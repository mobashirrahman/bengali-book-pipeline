# Publication metadata and covers

The `book` command now supports an editable metadata sidecar. Changing it affects
the render cache only, not OCR or proofreading. For `book.pdf`, the default is
`book.metadata.json`; `--metadata /path/to/metadata.json` selects another file.
Files are read, never rewritten. `--title` and nonempty `--author` CLI values take
precedence. Unknown bibliographic fields stay absent; the filename is only a
fallback title, not a verified title-page transcription.

```json
{
  "title": "বইয়ের নাম",
  "authors": ["লেখকের নাম"],
  "editors": [],
  "translators": [],
  "publisher": "প্রকাশকের নাম",
  "source_date": "১৯২০",
  "edition": "প্রথম সংস্করণ",
  "subjects": ["বাংলা সাহিত্য"],
  "description": "A manually verified description.",
  "cover_page": 1,
  "evidence": {
    "authors": {"page": 3, "note": "Verified against the scanned title page"}
  }
}
```

Additional optional strings: `isbn`, `rights`, `identifier`. Do not infer public
domain status or invent ISBNs. `source_date` and `edition` describe the scanned
edition and appear in its bibliographic citation and title page. They do not
replace the digital EPUB modification timestamp. `publisher` is transcribed from
the source; conversion provenance explicitly identifies pdf-craft separately.
Metadata follows [EPUB 3.3](https://www.w3.org/TR/epub-33/).

## Covers and navigation

- Existing extracted cover is reused when no override is specified.
- `cover_page` selects a physical, 1-based page of the original PDF, rendered at
  150 DPI. Verify the page visually: the software does not classify covers yet.
- Alternatively set `"cover": "images/cover.jpg"`, relative to the metadata file.
  Do not specify both. Raster replacements are normalized to PNG; their contents
  participate in render cache identity. No image generation or download occurs.
- A reflowable title page contains the title, people and available source-edition
  information. Cover, title page, contents and main text are navigation landmarks.
  The extracted hierarchical chapter table of contents is retained.
- Bengali styling remains reader-theme-friendly. No unlicensed fonts are embedded.

## Re-publish without OCR or LLM calls

```bash
.venv/bin/python -m pdf_craft_tool publish saved-proofread.pcex \
  --source original.pdf \
  --metadata original.metadata.json \
  --output-dir pdf-craft-output/my-new-publication
```

Use the PDF matching the extraction; `.pcex` does not currently prove their
association. The output directory must be new. The command exports EPUB,
Markdown, source-mapped chunks, effective `metadata.json`, publication settings,
SHA-256 seals and `validation.json`. Chunking needs the existing tokenizer cache;
on this deployment set
`TIKTOKEN_CACHE_DIR=/path/to/pdf-craft-worker/tokenizer-cache`.
No OCR runtime, Ollama server, GPU or model download is required.
Pass `--epubcheck /path/to/epubcheck.jar` to require conformance checking before
the new output directory is published.

The lightweight validator checks XML, ZIP mimetype, resource/spine references and
local links/fragments. It is not full conformance or visual accessibility testing.
Run EPUBCheck for full structural checking:

```bash
java -jar /path/to/epubcheck.jar pdf-craft-output/my-new-publication/book.epub
```

## Continuous fleet publication

The separate `pdf-craft-publications.service` runs on bio10. It reads completed
OCR jobs through a read-only SQLite connection and publishes their collected
proofread `.pcex` files locally. Worker releases and OCR job identities are unchanged.
EPUBCheck is enabled for each publication; a failing book is recorded as an error
without stopping other books. No corrected metadata is automatically invented.

Metadata lookup, highest precedence first:

1. `pdf-craft-output/cluster/publications/metadata/<job-id>.json`
2. `data/.../original-book.metadata.json`, adjacent to the original PDF
3. Filename title, with unknown fields absent

The central override is useful when you want to keep `data/` untouched. Relative
cover paths resolve relative to whichever metadata file is selected. The central
file replaces the adjacent file rather than partially merging with it. Identical
PDF aliases share the OCR job's selected source path; use a central override to
avoid ambiguity between aliases.

The watcher checks every 60 seconds and waits for inputs/metadata/cover files to
settle for 120 seconds. Changed metadata or replacement cover bytes create a new
publication, without repeating OCR or proofreading. Prior revisions remain.
Malformed metadata and validation failures retry after five minutes. Source PDFs
are hash-checked before a new publication; a changed source is not paired with
the old OCR text. Do not modify generated files in place.

Outputs: `pdf-craft-output/cluster/publications/books/<job-id>/<revision>/`.
The current output for each job is recorded in `publications/records/<job-id>.json`.
`publications/status.json` gives the latest completed scan counts; during a scan,
records and `cluster/publications.log` show newly completed books.

```bash
systemctl --user status pdf-craft-publications.service
tail -n 20 pdf-craft-output/cluster/publications.log
```

To run a single scan manually, first stop the publication service, then run:

```bash
TIKTOKEN_CACHE_DIR=/path/to/pdf-craft-worker/tokenizer-cache \
  .venv/bin/python -m pdf_craft_tool.publication_queue \
  --config pdf-craft-output/cluster-config.json --once
```

Restart the service afterward for continuous updates. A flock prevents concurrent
publishers. Stopping this service does not stop OCR. The service uses an immutable
snapshot under `pdf-craft-output/publication-releases/`; inspect its unit for the
active release. A different `--release` value creates new publication revisions.
Do not reinitialize the OCR fleet solely for publication changes: its processing
profile includes renderer code and could unnecessarily requeue OCR for every book.

Automatic cover classification, title-page metadata proposals, font embedding,
EPUB page-list mapping and improved poetry/header recognition are not implemented
by this increment. Existing Markdown source maps and image/caption/footnote
rendering remain available; no extracted text is silently removed or rewritten.
