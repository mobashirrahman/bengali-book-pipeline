# Catalogue metadata, covers, and safe embedding

Finishes the bio10-only corpus workflow: high-confidence title/author
metadata for current PDFs and generated OCR EPUBs, field provenance,
staged embedded metadata, one cover image or explicit error per PDF,
and reversible deduplication ordering. All commands live under
`pdf-craft-catalogue metadata` and run on bio10 only.

## Order of operations

Embedding changes file hashes, so embedding always comes **after**
deduplication. While the publication queue, catalogue API, or
coordinator are running, use `prepare` + `apply --dry-run` only; defer
the real `apply` and any quarantine moves until no active consumer can
see a mid-write file.

## Phases

1. `metadata manifest --data ROOT --out MANIFEST.json`
   Resolves one manifest entry per PDF/EPUB: accepted `title`/`authors`
   plus unresolved candidates and per-signal evidence
   (`filename_*`, `embedded_*`, `cover_*`, `signals`, `precedence`).
   Reruns are resumable: unchanged files (same
   SHA-256/size/mtime) are reused, changed files are re-resolved.
   `--dry-run` counts without writing; `--force` re-resolves
   everything; `--limit N` bounds pilots.
   `--defer-list FILE` (one path per line) records files under active
   consumers as `deferred` instead of resolving them; `--cover-index
   FILE` loads cover-OCR `{title_candidates, author_candidates}` per
   path so three-signal corroboration works without a GPU at manifest
   time.
2. `metadata cover-export --manifest MANIFEST.json --output-dir DIR`
   Renders page 0 of every PDF into content-addressed `<sha256>.jpg`
   files plus `index.json`, labelled truthfully
   `first-page-extraction`. Unrenderable PDFs keep an explicit `error`
   row; non-PDFs are `skipped`. Reruns skip completed rows whose
   output still exists. A verification summary (`present` /
   `missing_output` / `no_record`) prints after every run.
3. `metadata prepare --manifest MANIFEST.json --staging DIR --plan PLAN.json`
   Copies accepted-metadata sources into `DIR` and writes
   `/Title`/`/Author` (PDF) or `dc:title`/`dc:creator` (EPUB OPF) into
   the **copies**. Sources are only read. Entries without accepted
   metadata are recorded as `skipped_<status>` with a reason.
   Verification re-reads every staged copy through the catalogue
   extractors and reports `verified` / `mismatch` / `missing`.
4. `metadata apply --plan PLAN.json`
   Atomically copies staged files over their sources (temp file +
   `os.replace`), but only when the live source still matches the
   `source_sha256` recorded at prepare time. Changed sources are
   rejected (`rejected_changed`), already-applied sources are skipped
   (`already_applied`), so apply is resumable and doubles as recovery.
   `--dry-run` counts without writing.
5. `metadata status --manifest MANIFEST.json --plan PLAN.json`
   Prints manifest accounting (`completed` / `ambiguous` / `deferred` /
   `damaged` / `empty` / `not_a_book`) and plan recovery state
   (`staged_ok` / `staged_missing` / `source_changed` /
   `already_applied`) without changing anything.

## Acceptance rules

- Only `title` and `authors` are ever accepted or embedded.
  Publisher/year are never inferred.
- An accepted value needs corroboration: it appears in at least two
  trusted signals (filename, embedded, cover OCR), or is the only
  non-empty trusted signal. A bare filename slug that matches no
  scraper template is untrusted: it stays a candidate but cannot
  compete, so cover OCR can rescue it and OPF metadata outranks it.
  Disagreement between trusted signals means `status="ambiguous"`
  with candidates preserved and only the contested field cleared.
- EPUBs rank the OPF first (`embedded_first`); PDFs rank a matched
  filename template first (`filename_first`), because PDF `/Info` is
  usually a scanner or download-site watermark.
- Staged copies stay valid containers: PDFs are cloned whole
  (outlines, forms, page labels, XMP stream) with only
  `/Title`/`/Author` replaced; EPUBs keep member order, compression
  and bytes with `mimetype` first and uncompressed.
- Publication sidecars (`<source>.metadata.json`, central
  `metadata/<job>.json`) remain the output owner for generated EPUBs;
  embedding only touches container metadata, never sidecar content.
