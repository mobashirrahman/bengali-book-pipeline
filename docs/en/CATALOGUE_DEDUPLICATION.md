# Catalogue deduplication

Safe whole-corpus deduplication for the `data/` collection. The workflow
analyzes first and moves files only through an explicit apply step driven by
a specific report. Original files are never deleted; quarantine moves are
reversible via a manifest.

## Phases

All phases live under `pdf-craft-catalogue dedupe` and operate on an
explicit **side database** (`--db`, no default). Never pass the live
catalogue database unless that is exactly what you intend.

1. `dedupe inventory --db SIDE --data ROOT`
   Walks every current file, hashes supported files (PDF/EPUB) by content,
   and snapshots one path identity per file. Identical bytes share one
   document row while every path keeps its alias. Unreadable entries
   (dangling symlinks, I/O errors) are recorded with a failure row; one
   failure never aborts the run. Creates run N.
2. `dedupe analyze --db SIDE [--run-id N]`
   Reuses cached page fingerprints and text signatures only when both the
   content SHA-256 and the algorithm version match, then persists edges,
   clusters, keeper scorecards and failures. Prints a summary and marks
   the run completed.
3. `dedupe report --db SIDE --json-out R.json --csv-out R.csv`
   Reconciles discovered paths, unique contents, failures, cluster counts,
   removable candidates and estimated bytes; exits nonzero on any
   contradiction. Emits the full JSON report plus a CSV of removable
   candidates.
4. `dedupe apply --db SIDE --report R.json --quarantine-root DIR [--dry-run]`
   Requires the report and a nonempty quarantine root. Before each move it
   rechecks the source SHA-256 and skips missing, changed, recently-active
   or already-quarantined files. Writes a durable manifest
   (`manifest-run-N.json`) before and after moving. Destination names are
   collision-safe (`<sha-prefix>/<sha>_<name>`, suffixed on clash) and
   nothing is ever overwritten or deleted. `--dry-run` plans without
   moving or writing.
5. `dedupe rollback --manifest manifest-run-N.json`
   Restores exactly the entries the manifest records as moved, and only
   when the quarantine copy exists and the original path is absent;
   anything else is reported as a conflict, never forced.

## What counts as a duplicate

- `duplicate` -- same book, same format. Only this relation is ever
  removable, and only from `auto` clusters. Byte-identical aliases
  (several paths, one SHA-256) keep the earliest sorted path. Visual
  duplicates need similar page counts, at least two informative agreeing
  interior pages, and boilerplate rejection; every removable member must
  have a *direct* `sha256`/`page_image` edge to the keeper, so transitive
  chains never make an unrelated file removable.
- `format_variant` -- same book as PDF and EPUB (e.g. via text MinHash).
  Both copies are kept.
- `same_work` -- same title/author by metadata or same-format text match.
  Both copies are kept and linked; text alone never authorizes removal.

Oversized clusters (`--max-auto-cluster`, default 8) stay `review`, as do
human `confirmed`/`rejected` decisions, which reruns never overwrite.

## Caching and reruns

Fingerprint rows carry `FINGERPRINT_ALGO_VERSION` /
`TEXT_SIGNATURE_ALGO_VERSION` next to the content hash. Unchanged files are
not re-rendered on reruns; changed files hash differently and miss the
cache; bumping a version constant invalidates it. Rebuilding a report from
the same run reproduces it exactly.
