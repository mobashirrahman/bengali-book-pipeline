#!/usr/bin/env python3
"""EasyOCR line runner: items JSON -> resume-safe JSONL lines.

Runs inside the bnch-easyocr env (cuda or cpu). Detection boxes come from
``reader.readtext(detail=1)`` and are converted by run_line_engine.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_items, selected  # noqa: E402
from run_line_engine import load_done_ids, run_easyocr, write_row  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--quantize-mode", default="off", choices=["on", "off"])
    parser.add_argument("--canvas-size", type=int, default=2560)
    parser.add_argument("--mag-ratio", type=float, default=1.0)
    parser.add_argument("--model-rev", default="unknown")
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()

    import easyocr

    os.environ["EASYOCR_CANVAS_SIZE"] = str(args.canvas_size)
    os.environ["EASYOCR_MAG_RATIO"] = str(args.mag_ratio)
    model_dir = os.environ.get("EASYOCR_MODEL_DIR") or None
    reader_kwargs: dict = {"lang_list": ["bn"], "gpu": (args.device == "cuda"),
                           "model_storage_directory": model_dir}
    if args.quantize_mode == "on":
        reader_kwargs["quantize"] = True
    reader = easyocr.Reader(**{k: v for k, v in reader_kwargs.items() if v is not None})
    rev = (args.model_rev if args.model_rev != "unknown" else
           f"easyocr-{easyocr.__version__}-bn-{args.device}-default"
           f"{'-q' if args.quantize_mode == 'on' else ''}"
           f"-canvas{args.canvas_size}-mag{args.mag_ratio}")

    items = selected(load_items(args.items), args.max_items)
    done = set() if args.fresh else load_done_ids(args.output)
    if args.fresh:
        Path(args.output).unlink(missing_ok=True)
    ok = errors = skipped = 0
    for item in items:
        if item["id"] in done:
            skipped += 1
            continue
        lines, error, elapsed = run_easyocr(item.get("image", ""), reader,
                                            args.timeout)
        ok += error is None
        errors += error is not None
        write_row(args.output, {"id": item["id"], "lines": lines,
                                "seconds": elapsed, "error": error,
                                "model_rev": rev})
    print(f"easyocr-lines items={len(items)} ok={ok} errors={errors} "
          f"skipped={skipped} -> {args.output}")


if __name__ == "__main__":
    main()
