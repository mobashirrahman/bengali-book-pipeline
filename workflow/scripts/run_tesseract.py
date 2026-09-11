#!/usr/bin/env python3
"""Tesseract adapter: images -> hypotheses JSONL (common schema)."""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import add_common_args, load_items, run_sequential, selected, summarize, write_rows  # noqa: E402


def model_rev(bin_path: str, tessdata_dir: str, lang: str) -> str:
    version = subprocess.run([bin_path, "--version"], capture_output=True, text=True)
    first = version.stdout.splitlines()[0] if version.stdout else "tesseract-?"
    digest = hashlib.md5((Path(tessdata_dir) / f"{lang}.traineddata").read_bytes()).hexdigest()[:12]
    return f"{first} {lang}@{digest}"


def main() -> None:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    parser.add_argument("--tesseract-bin", required=True)
    parser.add_argument("--tessdata-dir", required=True)
    parser.add_argument("--lang", default="ben")
    parser.add_argument("--oem", default="1")
    parser.add_argument("--psm-reid", default="3")
    parser.add_argument("--psm-mozhi", default="8")
    args = parser.parse_args()

    items = selected(load_items(args.items), args.max_items)
    psm = args.psm_mozhi if args.split == "mozhi-test" else args.psm_reid
    rev = args.model_rev if args.model_rev != "unknown" else model_rev(
        args.tesseract_bin, args.tessdata_dir, args.lang
    )

    def infer(image: str) -> str:
        result = subprocess.run(
            [args.tesseract_bin, image, "stdout", "-l", args.lang,
             "--tessdata-dir", args.tessdata_dir,
             "--oem", args.oem, "--psm", psm],
            capture_output=True, text=True, timeout=args.timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"exit_{result.returncode}:{result.stderr.strip()[:100]}")
        return result.stdout

    rows = run_sequential(items, infer, args.timeout, rev)
    write_rows(rows, args.output)
    print(f"tesseract split={args.split} {summarize(rows)}")


if __name__ == "__main__":
    main()
