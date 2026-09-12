#!/usr/bin/env python3
"""Surya OCR 2 line runner: items JSON -> resume-safe JSONL lines.

Runs inside the bnch-surya env (llamacpp backend; see workflow/run.sh
exports). Full-page recognition; line parsing lives in run_line_engine.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_items, selected  # noqa: E402
from run_line_engine import load_done_ids, run_surya, write_row  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--backend", default="llamacpp")
    parser.add_argument("--model-rev", default="unknown")
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("SURYA_INFERENCE_BACKEND", args.backend)
    os.environ.setdefault("VLLM_DTYPE", "float16")
    if args.backend == "llamacpp":
        binary = os.environ.get("LLAMA_CPP_BINARY", "")
        libdir = os.path.dirname(binary)
        if libdir:
            old = os.environ.get("LD_LIBRARY_PATH", "")
            os.environ["LD_LIBRARY_PATH"] = libdir if not old else libdir + ":" + old

    from importlib.metadata import version as _pkg_version

    from surya.inference import SuryaInferenceManager
    from surya.recognition import RecognitionPredictor

    print("surya-lines: starting inference manager", flush=True)
    try:
        manager = SuryaInferenceManager(method=args.backend)
    except TypeError:
        manager = SuryaInferenceManager()
    print("surya-lines: creating recognition predictor", flush=True)
    predictor = RecognitionPredictor(manager)
    print("surya-lines: predictor ready", flush=True)
    rev = (args.model_rev if args.model_rev != "unknown" else
           f"surya-ocr-{_pkg_version('surya-ocr')}-surya-ocr-2-{args.backend}")

    def recognizer(images, layouts=None, full_page=None):
        if layouts is None:
            return predictor(images)
        return predictor(images, layouts, full_page=False)

    items = selected(load_items(args.items), args.max_items)
    done = set() if args.fresh else load_done_ids(args.output)
    if args.fresh:
        Path(args.output).unlink(missing_ok=True)
    ok = errors = skipped = 0
    try:
        for item in items:
            if item["id"] in done:
                skipped += 1
                continue
            lines, error, elapsed = run_surya(item.get("image", ""), None,
                                              recognizer, args.timeout)
            ok += error is None
            errors += error is not None
            write_row(args.output, {"id": item["id"], "lines": lines,
                                    "seconds": elapsed, "error": error,
                                    "model_rev": rev})
            print(f"surya-lines {item['id'][:8]} nlines={len(lines)} "
                  f"sec={elapsed} err={error}", flush=True)
    finally:
        try:
            manager.stop()
        except Exception:  # noqa: BLE001 - best effort shutdown
            pass
    print(f"surya-lines items={len(items)} ok={ok} errors={errors} "
          f"skipped={skipped} -> {args.output}")


if __name__ == "__main__":
    main()
