# Catalogue backend

This document records the database work for the scanned-book collection in
`data/`. The catalogue is being built as a backend for a future website. The
OCR, proofreading, publication, and cluster queues remain separate systems.

## Current design

The catalogue has two layers:

- The legacy tables (`books`, `authors`, `files`, and the reading and review
  tables) keep the existing `/v1` API and older callers working.
- The normalized tables use `catalogue_` prefixes. They separate a work from
  an edition, people, identifiers, source records, local documents, document
  matches, assets, and generated artifacts. Source snapshots and metadata
  assertions preserve where each value came from.

The normalized ingestion flow is:

1. Index local PDFs, EPUBs, and supported artifacts by SHA-256 without
   modifying anything under `data/`.
2. Stage source records from explicit Rokomari JSONL, Google Books JSON, or
   Open Library dump files.
3. Materialize source records into works, editions, people, identifiers, and
   provenance records.
4. Generate document match candidates for review before accepting a match.
5. Register cover candidates, fetch bounded copies into content-addressed
   storage, and record attribution, rights, source, and selection history.

The database currently uses SQLite for the compatibility and unit-test path.
The normalized API is read-only and is available under `/v2`:

- `/v2/health` and `/v2/stats`
- `/v2/search?q=...&limit=...&after=...`
- `/v2/works`, `/v2/works/{id}`
- `/v2/editions/{id}`
- `/v2/documents/{id}`
- `/v2/assets/{id}`

Requests open and close their own SQLite connection. `/v1` remains available
for existing clients.

## Local PostgreSQL environment

System `sudo` is not available on the development host, so PostgreSQL is
installed in user space rather than as an Ubuntu service:

```bash
PGROOT=/scratch/mdra00001/conda/envs/pdf-craft-postgres
PGDATA=/scratch/pdf-craft/pdf-craft-output/catalogue/postgres

"$PGROOT/bin/pg_ctl" -D "$PGDATA" \
  -o "-p 55432 -h 127.0.0.1" \
  -l "$PGDATA/server.log" start

"$PGROOT/bin/pg_isready" -h 127.0.0.1 -p 55432
```

The current local cluster is PostgreSQL 18.6 and listens only on loopback.
Its data directory is generated output and must not be committed. Stop it
with:

```bash
"$PGROOT/bin/pg_ctl" -D "$PGDATA" stop
```

Install the optional backend dependencies with:

```bash
pip install 'pdf-craft[catalogue]'
```

PostgreSQL commands take `--dsn` or read `CATALOGUE_POSTGRES_DSN`. A missing
DSN is an error; PostgreSQL commands never fall back to SQLite. The normalized
schema is migrated under a PostgreSQL advisory lock and records checksums in
`catalogue_schema_migrations`.

Initialize and inspect the schema:

```bash
export CATALOGUE_POSTGRES_DSN='postgresql://USER:PASSWORD@127.0.0.1:55432/DBNAME'
python -m pdf_craft.catalogue.cli postgres-init
python -m pdf_craft.catalogue.cli postgres-status
```

The API selects PostgreSQL explicitly with `--postgres-dsn`, or by calling
`init_app(postgres_dsn=...)`. With a PostgreSQL backend, `/v2` is available for
normalized reads and `/v1` returns an explicit SQLite-only error. Every API
request opens and closes its own connection; PostgreSQL connections apply a
5-second statement timeout and roll back before closing.

## Source and metadata policy

Rokomari, Google Books, and Open Library are treated as source systems, not as
one merged truth. Values retain source provenance and can be reviewed before
they become the selected display value. ISBNs are normalized and validated;
title and author matching is a candidate-generation step with explicit
accept/reject decisions.

The repository does not contain a licensed 300k-plus Rokomari export. A bulk
import must receive an explicit local export or an authorized API response.
The importers are resumable and preserve rejected or malformed records for
reporting. They must not silently fabricate records or scrape an unavailable
dataset.

Cover URLs are registered as candidates first. Downloads are bounded and
stored by content hash, duplicate bytes are linked to their provenance, and a
selected cover records the reason and actor. Copyright and attribution fields
remain part of the catalogue record.

## Transfer from SQLite

SQLite remains authoritative for this phase. Transfer only the normalized
`catalogue_` tables; legacy v1 tables are not copied. Rows are sent in bounded
batches in dependency order, preserving primary keys and provenance. Each
batch commits its PostgreSQL checkpoint together with its upserts, so an
interrupted transfer resumes safely. A repeated transfer is idempotent and
resets PostgreSQL identity sequences after completion.

```bash
python -m pdf_craft.catalogue.cli transfer-postgres \
  --db pdf-craft-output/catalogue/catalogue.db \
  --dsn "$CATALOGUE_POSTGRES_DSN" --batch-size 500
```

Transfer metadata is stored in PostgreSQL. PostgreSQL data and local cluster
files belong under `pdf-craft-output/`; do not place them in `data/` or commit
them.

The clean local database used for the current generated collection is
`pdf_craft_catalogue`:

```bash
PGROOT=/scratch/mdra00001/conda/envs/pdf-craft-postgres
"$PGROOT/bin/createdb" -h 127.0.0.1 -p 55432 pdf_craft_catalogue
python -m pdf_craft.catalogue.cli postgres-init \
  --dsn postgresql://127.0.0.1:55432/pdf_craft_catalogue
python -m pdf_craft.catalogue.cli transfer-postgres \
  --db pdf-craft-output/catalogue/catalogue.db \
  --dsn postgresql://127.0.0.1:55432/pdf_craft_catalogue
```

The transferred database currently contains 2,987 indexed local documents.

## Useful SQLite commands

Run the CLI as a module from the repository root:

```bash
python -m pdf_craft.catalogue.cli init --db pdf-craft-output/catalogue/catalogue.db
python -m pdf_craft.catalogue.cli ingest-local \
  --db pdf-craft-output/catalogue/catalogue.db --data data
python -m pdf_craft.catalogue.cli stats \
  --db pdf-craft-output/catalogue/catalogue.db
python -m pdf_craft.catalogue.cli serve \
  --db pdf-craft-output/catalogue/catalogue.db
```

Stage supplied source files before materializing them:

```bash
python -m pdf_craft.catalogue.cli stage-rokomari \
  --db pdf-craft-output/catalogue/catalogue.db --file rokomari.jsonl
python -m pdf_craft.catalogue.cli stage-google \
  --db pdf-craft-output/catalogue/catalogue.db --file google-response.json
python -m pdf_craft.catalogue.cli stage-openlibrary-dump \
  --db pdf-craft-output/catalogue/catalogue.db --file works.dump.gz
python -m pdf_craft.catalogue.cli materialize-source \
  --db pdf-craft-output/catalogue/catalogue.db
```

The remaining data work is to load any supplied Rokomari export, stage Google
Books and Open Library records in bounded batches, resolve matches with review
thresholds, and materialize covers and metadata with their provenance. Full
write parity for the legacy `/v1` API is outside this phase and remains
SQLite-only.
