from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .database import CatalogueDB
from .matching import infer_author_from_path, infer_title_from_path
from .models import FileRecord
from .search import get_book_stats, rebuild_fts_index

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def cmd_init(args: argparse.Namespace) -> None:
    db = CatalogueDB(args.db)
    print(f"Initialized catalogue database at {args.db}")
    db.close()


def cmd_postgres_init(args: argparse.Namespace) -> None:
    from .postgres import initialize_postgres

    migrations = initialize_postgres(args.dsn)
    version = migrations[-1].version if migrations else 0
    print(f"Initialized PostgreSQL catalogue schema version {version}")


def cmd_postgres_status(args: argparse.Namespace) -> None:
    import json

    from .postgres import postgres_status

    print(json.dumps(postgres_status(args.dsn), ensure_ascii=False, indent=2))


def cmd_transfer_postgres(args: argparse.Namespace) -> None:
    from .transfer import transfer_sqlite_to_postgres

    report = transfer_sqlite_to_postgres(args.db, args.dsn, batch_size=args.batch_size)
    print("Transferred normalized catalogue: " + ", ".join(
        f"{key}={value}" for key, value in report.items() if key != "tables"
    ))
    for table, count in report["tables"].items():
        print(f"  {table}: {count}")


def cmd_import_rokomari(args: argparse.Namespace) -> None:
    from .importers.rokomari import import_rokomari, import_rokomari_from_file

    db = CatalogueDB(args.db)
    if args.file:
        count = import_rokomari_from_file(db, args.file)
    else:
        count = import_rokomari(db, cache_dir=args.cache_dir)
    print(f"Imported {count} books from Rokomari")
    db.close()


def cmd_stage_rokomari(args: argparse.Namespace) -> None:
    from .importers.rokomari import stage_rokomari_from_file

    db = CatalogueDB(args.db)
    staged, skipped = stage_rokomari_from_file(
        db, args.file, snapshot_key=args.snapshot_key, batch_size=args.batch_size,
    )
    print(f"Staged {staged} Rokomari records; skipped {skipped}")
    db.close()


def cmd_stage_google(args: argparse.Namespace) -> None:
    import json

    from .importers.google_books import stage_google_books_response

    db = CatalogueDB(args.db)
    response = json.loads(Path(args.file).read_text(encoding="utf-8"))
    staged, skipped = stage_google_books_response(
        db, response, snapshot_key=args.snapshot_key,
        request={"file": str(args.file)},
    )
    print(f"Staged {staged} Google Books records; skipped {skipped}")
    db.close()


def cmd_stage_openlibrary(args: argparse.Namespace) -> None:
    from .importers.open_library import stage_open_library_dump

    db = CatalogueDB(args.db)
    staged, skipped = stage_open_library_dump(db, args.file, batch_size=args.batch_size)
    print(f"Staged {staged} Open Library records; skipped {skipped}")
    db.close()


def cmd_materialize_source(args: argparse.Namespace) -> None:
    from .materialization import materialize_source_records

    db = CatalogueDB(args.db)
    report = materialize_source_records(
        db, source=args.source, dry_run=args.dry_run, batch_size=args.batch_size,
    )
    print("Materialization: " + ", ".join(f"{key}={value}" for key, value in report.items()))
    db.close()


def cmd_repair_people(args: argparse.Namespace) -> None:
    from .materialization import repair_edition_people

    db = CatalogueDB(args.db)
    report = repair_edition_people(db, batch_size=args.batch_size, dry_run=args.dry_run)
    status = "People repair (dry run): " if args.dry_run else "People repair: "
    print(status + ", ".join(f"{key}={value}" for key, value in report.items()))
    db.close()


def cmd_materialize_ratings(args: argparse.Namespace) -> None:
    from .ratings import materialize_external_ratings

    db = CatalogueDB(args.db)
    report = materialize_external_ratings(
        db, source=args.source, batch_size=args.batch_size,
    )
    print("Rating materialization: " + ", ".join(f"{key}={value}" for key, value in report.items()))
    db.close()


def cmd_cover(args: argparse.Namespace) -> None:
    if args.dry_run:
        print(f"Would perform cover action {args.action}")
        return

    from .assets import (
        fetch_remote_cover,
        register_local_file,
        register_remote_cover,
        select_cover,
        verify_cover_batch,
    )

    db = CatalogueDB(args.db)
    if args.action == "register-url":
        result = register_remote_cover(db, args.edition_id, args.url, source_record_id=args.source_record_id, attribution=args.attribution, rights=args.rights)
    elif args.action == "register-file":
        result = register_local_file(db, args.edition_id, args.file, args.asset_root, source_record_id=args.source_record_id, attribution=args.attribution, rights=args.rights)
    elif args.action == "fetch":
        result = fetch_remote_cover(db, args.candidate_id, args.asset_root, max_bytes=args.max_bytes, timeout=args.timeout)
    elif args.action == "verify-batch":
        result = verify_cover_batch(
            db, args.asset_root, limit=args.limit, after_id=args.after_id,
            max_bytes=args.max_bytes, timeout=args.timeout,
        )
        print(
            f"Verified cover batch: examined={result.examined} "
            f"validated={result.validated} rejected={result.rejected} "
            f"selected={result.selected} "
            f"next_after_id={result.next_after_id} complete={result.complete}"
        )
        db.close()
        return
    else:
        result = select_cover(db, args.edition_id, candidate_id=args.candidate_id, manual=args.manual, selected_by=args.selected_by)
    print(f"Cover asset {result.id}: {result.storage_uri}")
    db.close()


def cmd_review(args: argparse.Namespace) -> None:
    from .resolution import review_match

    db = CatalogueDB(args.db)
    review_match(db, args.match_id, reviewer=args.reviewer,
                 decision=args.decision, reason=args.reason)
    print(f"Match {args.match_id}: {args.decision}")
    db.close()


def cmd_resolve_local(args: argparse.Namespace) -> None:
    from .resolution import generate_candidates

    if args.limit is not None and args.limit < 0:
        raise ValueError("limit must be non-negative")
    db = CatalogueDB(args.db)
    where = ""
    parameters: list[object] = []
    if args.only_unmatched:
        where = "WHERE NOT EXISTS (SELECT 1 FROM catalogue_document_matches m WHERE m.document_id=d.id)"
    limit = " LIMIT ?" if args.limit is not None else ""
    if args.limit is not None:
        parameters.append(args.limit)
    rows = db.conn.execute(
        "SELECT d.id FROM catalogue_local_documents d "
        + where
        + " ORDER BY d.id"
        + limit,
        tuple(parameters),
    ).fetchall()
    total_candidates = 0
    for index, row in enumerate(rows, start=1):
        total_candidates += len(generate_candidates(db, int(row[0])))
        if index % 100 == 0:
            logger.info("Resolved candidate queues for %d / %d local documents", index, len(rows))
    pending = db.conn.execute(
        "SELECT COUNT(*) FROM catalogue_document_matches WHERE status='candidate'"
    ).fetchone()[0]
    print(
        f"Resolved {len(rows)} local documents; observed {total_candidates} candidate rows; "
        f"pending candidates {pending}"
    )
    db.close()


def cmd_enrich_google(args: argparse.Namespace) -> None:
    from .importers.google_books import enrich_book_from_google

    db = CatalogueDB(args.db)
    count = enrich_book_from_google(
        db,
        api_key=args.api_key,
        max_enrichments=args.max,
        rate_limit_delay=args.delay,
    )
    print(f"Enriched {count} books from Google Books")
    db.close()


def cmd_match(args: argparse.Namespace) -> None:
    db = CatalogueDB(args.db)
    data_dir = Path(args.data)
    if not data_dir.is_dir():
        print(f"Error: {data_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    matched = 0
    unmatched = 0
    pdf_files = list(data_dir.rglob("*.pdf"))
    logger.info("Found %d PDF files in %s", len(pdf_files), data_dir)

    for pdf_path in pdf_files:
        rel_path = pdf_path.relative_to(data_dir)
        author_name = infer_author_from_path(str(rel_path))
        title = infer_title_from_path(str(rel_path))

        if not title:
            unmatched += 1
            continue

        books = db.find_books_by_title(title, limit=5)
        best_match = None
        best_score = 0.0

        for book in books:
            book_authors = db.get_book_authors(book.id)  # type: ignore[arg-type]
            book_author = book_authors[0].name if book_authors else ""

            if author_name and book_author:
                from .matching import fuzzy_match_score
                title_sim = fuzzy_match_score(title, book.title)
                author_sim = fuzzy_match_score(author_name, book_author)
                score = 0.6 * title_sim + 0.4 * author_sim
            else:
                from .matching import fuzzy_match_score
                score = fuzzy_match_score(title, book.title)

            if score > best_score:
                best_score = score
                best_match = book

        if best_match and best_score >= 0.50:
            sha256 = _compute_sha256(pdf_path)
            existing_file = db.get_file_by_sha256(sha256)
            if not existing_file:
                file_record = FileRecord(
                    book_id=best_match.id,
                    source_path=str(pdf_path),
                    sha256=sha256,
                    file_size=pdf_path.stat().st_size,
                )
                db.create_file(file_record)
            matched += 1
            if matched % 100 == 0:
                logger.info("Matched %d / %d PDFs", matched, len(pdf_files))
        else:
            unmatched += 1

    print(f"Matching complete: {matched} matched, {unmatched} unmatched out of {len(pdf_files)} PDFs")
    db.close()


def cmd_ingest_local(args: argparse.Namespace) -> None:
    from .foundation import ingest_local_documents

    db = CatalogueDB(args.db)
    extensions = {f".{extension.lstrip('.')}" for extension in args.extensions}
    indexed, skipped = ingest_local_documents(
        db, args.data, include_extensions=extensions,
    )
    inventory = db.conn.execute(
        "SELECT status, COUNT(*) FROM catalogue_local_inventory GROUP BY status"
    ).fetchall()
    counts = ", ".join(f"{row[0]}={row[1]}" for row in inventory)
    print(f"Indexed {indexed} local documents; skipped {skipped}; inventory {counts}")
    db.close()


def _compute_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_rebuild_fts(args: argparse.Namespace) -> None:
    db = CatalogueDB(args.db)
    rebuild_fts_index(db)
    print("FTS index rebuilt")
    db.close()


def cmd_dedupe_inventory(args: argparse.Namespace) -> None:
    from .dedupe import inventory_corpus

    db = CatalogueDB(args.db)
    try:
        run_id = inventory_corpus(
            db.conn, args.data,
            extensions=tuple(args.extensions),
        )
        rows = db.conn.execute(
            "SELECT status, COUNT(*) FROM dedupe_path_identities "
            "WHERE run_id=? GROUP BY status", (run_id,),
        ).fetchall()
        counts = ", ".join(f"{row[0]}={row[1]}" for row in rows)
        print(f"Dedupe inventory run {run_id}: {counts}")
    finally:
        db.close()


def cmd_dedupe_analyze(args: argparse.Namespace) -> None:
    from .dedupe import analyze_run

    db = CatalogueDB(args.db)
    try:
        if args.run_id is None:
            row = db.conn.execute(
                "SELECT id FROM dedupe_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("Error: no dedupe runs; run dedupe inventory first",
                      file=sys.stderr)
                sys.exit(1)
            run_id = int(row[0])
        else:
            run_id = args.run_id
        summary = analyze_run(
            db.conn, run_id, with_metadata=not args.no_metadata,
            max_auto_cluster=args.max_auto_cluster,
        )
        print(f"Dedupe analyze run {run_id}: " + ", ".join(
            f"{key}={value}" for key, value in summary.items()))
    finally:
        db.close()


def cmd_dedupe_report(args: argparse.Namespace) -> None:
    from .dedupe import build_report, reconcile_report, write_report_files

    db = CatalogueDB(args.db)
    try:
        if args.run_id is None:
            row = db.conn.execute(
                "SELECT id FROM dedupe_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("Error: no dedupe runs; run dedupe inventory first",
                      file=sys.stderr)
                sys.exit(1)
            run_id = int(row[0])
        else:
            run_id = args.run_id
        report = build_report(db.conn, run_id)
        problems = reconcile_report(report)
        if problems:
            for problem in problems:
                print(f"Reconciliation problem: {problem}", file=sys.stderr)
            sys.exit(1)
        write_report_files(report, args.json_out, args.csv_out)
        print(f"Dedupe report for run {run_id}: "
              f"{len(report['removable'])} removable candidates, "
              f"{report['estimated_bytes_saved']} bytes; "
              f"wrote {args.json_out} and {args.csv_out}")
    finally:
        db.close()


def cmd_dedupe_apply(args: argparse.Namespace) -> None:
    from .dedupe import apply_report, load_report_file

    if not str(args.quarantine_root or "").strip():
        print("Error: --quarantine-root is required and must be nonempty",
              file=sys.stderr)
        sys.exit(1)
    report = load_report_file(args.report)
    db = CatalogueDB(args.db)
    try:
        summary = apply_report(
            db.conn, report, quarantine_root=args.quarantine_root,
            dry_run=args.dry_run, manifest_path=args.manifest_out,
        )
    finally:
        db.close()
    skipped = ", ".join(f"{key}={value}"
                        for key, value in summary["skipped"].items()) or "none"
    if summary["dry_run"]:
        print(f"Dedupe apply dry run: would move {summary['planned']}; "
              f"skipped ({skipped})")
    else:
        print(f"Dedupe apply: moved {summary['moved']}; skipped ({skipped}); "
              f"manifest {summary['manifest']}")


def cmd_dedupe_rollback(args: argparse.Namespace) -> None:
    from .dedupe import rollback_manifest

    summary = rollback_manifest(args.manifest)
    if summary["conflicts"]:
        for conflict in summary["conflicts"]:
            print(f"Rollback conflict at {conflict['src']}: "
                  f"{conflict['reason']}", file=sys.stderr)
    print(f"Dedupe rollback: restored {summary['restored']}, "
          f"conflicts {summary['conflict_count']}")


def cmd_stats(args: argparse.Namespace) -> None:
    db = CatalogueDB(args.db)
    stats = get_book_stats(db)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    db.close()


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api.app import init_app

    app = init_app(
        args.db,
        postgres_dsn=args.postgres_dsn,
        content_root=args.content_root,
        asset_root=args.asset_root,
    )
    uvicorn.run(app, host=args.host, port=args.port)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pdf-craft-catalogue", description="Book catalogue management")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialize catalogue database")
    p_init.add_argument("--db", default="catalogue.db", help="Database file path")
    p_init.set_defaults(func=cmd_init)

    p_pg_init = sub.add_parser("postgres-init", help="Initialize PostgreSQL catalogue schema")
    p_pg_init.add_argument("--dsn", help="PostgreSQL DSN (or CATALOGUE_POSTGRES_DSN)")
    p_pg_init.set_defaults(func=cmd_postgres_init)

    p_pg_status = sub.add_parser("postgres-status", help="Show PostgreSQL migration status")
    p_pg_status.add_argument("--dsn", help="PostgreSQL DSN (or CATALOGUE_POSTGRES_DSN)")
    p_pg_status.set_defaults(func=cmd_postgres_status)

    p_transfer = sub.add_parser(
        "transfer-postgres", help="Transfer normalized tables from SQLite to PostgreSQL"
    )
    p_transfer.add_argument("--db", required=True, help="Authoritative SQLite database path")
    p_transfer.add_argument("--dsn", help="PostgreSQL DSN (or CATALOGUE_POSTGRES_DSN)")
    p_transfer.add_argument("--batch-size", type=int, default=500)
    p_transfer.set_defaults(func=cmd_transfer_postgres)

    p_rokomari = sub.add_parser("import-rokomari", help="Import Rokomari dataset")
    p_rokomari.add_argument("--db", default="catalogue.db")
    p_rokomari.add_argument("--file", help="Local JSONL file instead of HuggingFace download")
    p_rokomari.add_argument("--cache-dir", help="HuggingFace cache directory")
    p_rokomari.set_defaults(func=cmd_import_rokomari)

    p_stage_rokomari = sub.add_parser(
        "stage-rokomari", help="Preserve Rokomari JSONL records for later matching"
    )
    p_stage_rokomari.add_argument("--db", default="catalogue.db")
    p_stage_rokomari.add_argument("--file", required=True)
    p_stage_rokomari.add_argument("--snapshot-key")
    p_stage_rokomari.add_argument("--batch-size", type=int, default=500)
    p_stage_rokomari.set_defaults(func=cmd_stage_rokomari)

    p_stage_google = sub.add_parser(
        "stage-google", help="Stage a saved Google Books API JSON response"
    )
    p_stage_google.add_argument("--db", default="catalogue.db")
    p_stage_google.add_argument("--file", required=True)
    p_stage_google.add_argument("--snapshot-key", default="response")
    p_stage_google.set_defaults(func=cmd_stage_google)

    p_stage_openlibrary = sub.add_parser(
        "stage-openlibrary-dump", help="Stage a plain or gzip Open Library line dump"
    )
    p_stage_openlibrary.add_argument("--db", default="catalogue.db")
    p_stage_openlibrary.add_argument("--file", required=True)
    p_stage_openlibrary.add_argument("--batch-size", type=int, default=500)
    p_stage_openlibrary.set_defaults(func=cmd_stage_openlibrary)

    p_materialize = sub.add_parser("materialize-source", help="Materialize staged source records")
    p_materialize.add_argument("--db", default="catalogue.db")
    p_materialize.add_argument("--source")
    p_materialize.add_argument("--batch-size", type=int, default=500)
    p_materialize.add_argument("--dry-run", action="store_true")
    p_materialize.set_defaults(func=cmd_materialize_source)

    p_repair = sub.add_parser(
        "repair-people", help="Remove Rokomari sidebar authors from edition people"
    )
    p_repair.add_argument("--db", default="catalogue.db")
    p_repair.add_argument("--batch-size", type=int, default=500)
    p_repair.add_argument("--dry-run", action="store_true")
    p_repair.set_defaults(func=cmd_repair_people)

    p_ratings = sub.add_parser(
        "materialize-ratings", help="Materialize external ratings from staged source records"
    )
    p_ratings.add_argument("--db", default="catalogue.db")
    p_ratings.add_argument("--source", choices=("rokomari", "google_books", "goodreads"))
    p_ratings.add_argument("--batch-size", type=int, default=500)
    p_ratings.set_defaults(func=cmd_materialize_ratings)

    p_cover = sub.add_parser("cover", help="Register, verify, fetch, or select cover assets")
    p_cover.add_argument("action", choices=("register-url", "register-file", "fetch", "verify-batch", "select"))
    p_cover.add_argument("--db", default="catalogue.db")
    p_cover.add_argument("--edition-id", type=int)
    p_cover.add_argument("--candidate-id", type=int)
    p_cover.add_argument("--url")
    p_cover.add_argument("--file")
    p_cover.add_argument("--asset-root", default="assets")
    p_cover.add_argument("--source-record-id", type=int)
    p_cover.add_argument("--attribution")
    p_cover.add_argument("--rights")
    p_cover.add_argument("--manual", action="store_true")
    p_cover.add_argument("--selected-by")
    p_cover.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    p_cover.add_argument("--timeout", type=float, default=20.0)
    p_cover.add_argument("--limit", type=int, default=100)
    p_cover.add_argument("--after-id", type=int)
    p_cover.add_argument("--dry-run", action="store_true")
    p_cover.set_defaults(func=cmd_cover)

    p_review = sub.add_parser("review-match", help="Accept or reject a pending v2 match")
    p_review.add_argument("--db", default="catalogue.db")
    p_review.add_argument("--match-id", type=int, required=True)
    p_review.add_argument("--reviewer", required=True)
    p_review.add_argument("--reason", required=True)
    p_review.add_argument("decision", choices=("accepted", "rejected"))
    p_review.set_defaults(func=cmd_review)

    p_resolve = sub.add_parser(
        "resolve-local", help="Generate reviewable canonical edition candidates for local documents"
    )
    p_resolve.add_argument("--db", default="catalogue.db")
    p_resolve.add_argument(
        "--only-unmatched", action="store_true",
        help="Skip documents that already have any candidate or decision",
    )
    p_resolve.add_argument(
        "--limit", type=_non_negative_int,
        help="Process at most this many documents",
    )
    p_resolve.set_defaults(func=cmd_resolve_local)

    p_google = sub.add_parser("enrich-google", help="Enrich books via Google Books API")
    p_google.add_argument("--db", default="catalogue.db")
    p_google.add_argument("--api-key", required=True, help="Google Books API key")
    p_google.add_argument("--max", type=int, default=500, help="Max books to enrich")
    p_google.add_argument("--delay", type=float, default=0.1, help="Rate limit delay in seconds")
    p_google.set_defaults(func=cmd_enrich_google)

    p_match = sub.add_parser("match", help="Match PDFs against catalogue")
    p_match.add_argument("--db", default="catalogue.db")
    p_match.add_argument("--data", required=True, help="Directory containing PDFs")
    p_match.set_defaults(func=cmd_match)

    p_ingest = sub.add_parser(
        "ingest-local", help="Index local PDFs and document artifacts by SHA-256"
    )
    p_ingest.add_argument("--db", default="catalogue.db")
    p_ingest.add_argument("--data", required=True, help="Directory containing local files")
    p_ingest.add_argument(
        "--extensions", nargs="+", default=["pdf", "epub", "pcex"],
        help="File extensions to index (default: pdf epub pcex)",
    )
    p_ingest.set_defaults(func=cmd_ingest_local)

    p_fts = sub.add_parser("rebuild-fts", help="Rebuild full-text search index")
    p_fts.add_argument("--db", default="catalogue.db")
    p_fts.set_defaults(func=cmd_rebuild_fts)

    p_stats = sub.add_parser("stats", help="Show catalogue statistics")
    p_stats.add_argument("--db", default="catalogue.db")
    p_stats.set_defaults(func=cmd_stats)

    p_dedupe = sub.add_parser(
        "dedupe",
        help="Reproducible whole-corpus deduplication (analyze first, move only via apply)",
    )
    dedupe_sub = p_dedupe.add_subparsers(dest="dedupe_action")

    # --db has no default here on purpose: analysis must use an explicit side
    # database (or a catalogue copy), never the live catalogue by accident.
    p_inv = dedupe_sub.add_parser(
        "inventory", help="Hash every file and snapshot path identities")
    p_inv.add_argument("--db", required=True, help="Side database path")
    p_inv.add_argument("--data", required=True, help="Corpus root directory")
    p_inv.add_argument("--extensions", nargs="+", default=["pdf", "epub"])
    p_inv.set_defaults(func=cmd_dedupe_inventory)

    p_analyze = dedupe_sub.add_parser(
        "analyze", help="Match inventoried files into evidence clusters")
    p_analyze.add_argument("--db", required=True, help="Side database path")
    p_analyze.add_argument("--data", required=False,
                           help="Corpus root (informational; run holds the paths)")
    p_analyze.add_argument("--run-id", type=int, default=None,
                           help="Inventory run to analyze (default: latest)")
    p_analyze.add_argument("--no-metadata", action="store_true",
                           help="Skip the metadata same-work signal")
    p_analyze.add_argument("--max-auto-cluster", type=int, default=8)
    p_analyze.set_defaults(func=cmd_dedupe_analyze)

    p_report = dedupe_sub.add_parser(
        "report", help="Emit the reconciled JSON/CSV report for a run")
    p_report.add_argument("--db", required=True, help="Side database path")
    p_report.add_argument("--run-id", type=int, default=None,
                          help="Run to report on (default: latest)")
    p_report.add_argument("--json-out", required=True)
    p_report.add_argument("--csv-out", required=True)
    p_report.set_defaults(func=cmd_dedupe_report)

    p_apply = dedupe_sub.add_parser(
        "apply",
        help="Quarantine report-listed duplicates (requires report + quarantine root)",
    )
    p_apply.add_argument("--db", required=True, help="Side database path")
    p_apply.add_argument("--report", required=True, help="Report JSON path")
    p_apply.add_argument("--quarantine-root", required=True,
                         help="Destination directory; originals are moved, never deleted")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="Plan without moving files or writing a manifest")
    p_apply.add_argument("--manifest-out", default=None)
    p_apply.set_defaults(func=cmd_dedupe_apply)

    p_rollback = dedupe_sub.add_parser(
        "rollback", help="Restore exactly the moves one manifest records")
    p_rollback.add_argument("--manifest", required=True)
    p_rollback.set_defaults(func=cmd_dedupe_rollback)

    p_serve = sub.add_parser("serve", help="Start FastAPI server")
    p_serve.add_argument("--db", default="catalogue.db")
    p_serve.add_argument("--postgres-dsn", help="Use PostgreSQL for normalized /v2 reads")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument(
        "--content-root",
        help="Approved root for serving local PDF/EPUB documents (also CATALOGUE_CONTENT_ROOT)",
    )
    p_serve.add_argument(
        "--asset-root",
        help="Approved root for serving selected local raster assets (also CATALOGUE_ASSET_ROOT)",
    )
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
