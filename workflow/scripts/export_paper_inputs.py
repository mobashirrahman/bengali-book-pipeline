#!/usr/bin/env python3
"""Assemble the input directory the showyourwork paper archives.

The paper repo (github.com/mobashirrahman/bengali-ocr-benchmark-paper) packs
one directory, ``$PDF_CRAFT_RUN_DIR``, into its cached ``benchmark.tar.gz``.
This script builds that directory from a finished run: score/hypothesis/meta
files, the config that produced them, the in-collection proxy study
(manifest reduced to doc ids and page indexes -- collection paths and titles
never leave this machine), and a provenance record of the pdf-craft commit.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

_RUN_GLOBS = ("scores_*.json", "hypotheses_*.jsonl", "*.meta.json", "run_meta.json",
              "inputs_meta.json", "summary.csv", "report.md")
_INHOUSE_FILES = ("proxy_scores.json", "summary.csv")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--inhouse-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.output.exists():
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True)
    for pattern in _RUN_GLOBS:
        for path in sorted(args.run_dir.glob(pattern)):
            shutil.copy2(path, args.output / path.name)
    shutil.copy2(args.config, args.output / "config.yaml")

    if args.inhouse_dir and args.inhouse_dir.is_dir():
        dest = args.output / "inhouse"
        dest.mkdir()
        for name in _INHOUSE_FILES:
            if (args.inhouse_dir / name).is_file():
                shutil.copy2(args.inhouse_dir / name, dest / name)
        manifest = json.loads((args.inhouse_dir / "manifest.json").read_text(encoding="utf-8"))
        safe = [{"doc_id": d.get("doc_id"), "pages": [{"index": p.get("index")} for p in d.get("pages", [])]}
                for d in manifest]
        (dest / "manifest.json").write_text(json.dumps(safe, indent=1) + "\n", encoding="utf-8")

    provenance = {
        "pdf_craft_commit": _git("rev-parse", "HEAD"),
        "pdf_craft_dirty": bool(_git("status", "--porcelain", "--", "workflow", "pdf_craft_tool")),
        "pdf_craft_branch": _git("branch", "--show-current"),
        "run_dir": str(args.run_dir),
    }
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"paper inputs: {sum(1 for _ in args.output.rglob('*') if _.is_file())} files -> {args.output}")


if __name__ == "__main__":
    main()
