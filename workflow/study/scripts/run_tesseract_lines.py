#!/usr/bin/env python3
"""Tesseract TSV line runner: items JSON -> resume-safe JSONL lines.

Runs under the repo .venv python (tesseract is an external binary).
Grouping/parsing lives in run_line_engine (unit-tested, stdlib-only).
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from common import load_items, selected  # noqa: E402
from run_line_engine import load_done_ids, run_tesseract, write_row  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--tesseract-bin", required=True)
    parser.add_argument("--tessdata-dir", required=True)
    parser.add_argument("--lang", default="ben")
    parser.add_argument("--oem", default="1")
    parser.add_argument("--psm", default="3")
    parser.add_argument("--model-rev", default="unknown")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore existing output rows and rerun all items")
    args = parser.parse_args()

    if args.model_rev != "unknown":
        rev = args.model_rev
    else:
        version = subprocess.run([args.tesseract_bin, "--version"],
                                 capture_output=True, text=True)
        first = version.stdout.splitlines()[0] if version.stdout else "tesseract-?"
        digest = hashlib.md5((Path(args.tessdata_dir) / f"{args.lang}.traineddata")
                             .read_bytes()).hexdigest()[:12]
        rev = f"{first} {args.lang}@{digest}"

    items = selected(load_items(args.items), args.max_items)
    done = set() if args.fresh else load_done_ids(args.output)
    if args.fresh:
        Path(args.output).unlink(missing_ok=True)
    ok = errors = skipped = 0
    for item in items:
        if item["id"] in done:
            skipped += 1
            continue
        lines, error, elapsed = run_tesseract(
            item.get("image", ""), args.tesseract_bin, args.tessdata_dir,
            args.lang, args.oem, args.psm, args.timeout)
        ok += error is None
        errors += error is not None
        write_row(args.output, {"id": item["id"], "lines": lines,
                                "seconds": elapsed, "error": error,
                                "model_rev": rev})
    print(f"tesseract-lines items={len(items)} ok={ok} errors={errors} "
          f"skipped={skipped} -> {args.output}")


if __name__ == "__main__":
    main()
