#!/usr/bin/env python3
"""Surya OCR 2 adapter: images -> hypotheses JSONL (common schema).

No docker on this host: runs the transformers backend (SURYA_INFERENCE_BACKEND).
Each Mozhi word crop is fed as its own single-block "page" (Surya has no
word/line mode switch). REID pages go through full-page OCR in reading order.
"""

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import add_common_args, load_items, selected, summarize, write_rows  # noqa: E402

_TAG = re.compile(r"<[^>]+>")


def page_text(page) -> str:
    blocks = sorted(page.blocks, key=lambda b: (b.reading_order is None, b.reading_order))
    parts = []
    for block in blocks:
        if getattr(block, "skipped", False) or getattr(block, "error", False):
            continue
        html = getattr(block, "html", "") or ""
        text = _TAG.sub("", html).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    parser.add_argument("--backend", default="transformers")
    parser.add_argument("--batch-pages", type=int, default=1)
    parser.add_argument("--batch-crops", type=int, default=8)
    args = parser.parse_args()

    os.environ.setdefault("SURYA_INFERENCE_BACKEND", args.backend)
    os.environ.setdefault("VLLM_DTYPE", "float16")
    if args.backend == "llamacpp":
        # Snakemake/conda activation scrubs LD_LIBRARY_PATH, which the
        # llama-server binary needs for its sibling libggml-*.so libs.
        # Re-derive it from the binary location (libs ship alongside it).
        binary = os.environ.get("LLAMA_CPP_BINARY", "")
        libdir = os.path.dirname(binary)
        if libdir:
            old = os.environ.get("LD_LIBRARY_PATH", "")
            os.environ["LD_LIBRARY_PATH"] = libdir if not old else libdir + ":" + old

    from PIL import Image

    import surya  # noqa: F401  (version stamp via metadata below)
    from importlib.metadata import version as _pkg_version

    from surya.inference import SuryaInferenceManager
    from surya.recognition import RecognitionPredictor

    try:
        manager = SuryaInferenceManager(method=args.backend)
    except TypeError:
        manager = SuryaInferenceManager()
    predictor = RecognitionPredictor(manager)
    rev = (
        args.model_rev
        if args.model_rev != "unknown"
        else f"surya-ocr-{_pkg_version('surya-ocr')}-surya-ocr-2-{args.backend}"
    )

    items = selected(load_items(args.items), args.max_items)
    crops = args.split == "mozhi-test"
    batch_size = args.batch_crops if crops else args.batch_pages
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]

    def predict(images: list) -> list:
        if not crops:
            return predictor(images)
        from surya.layout.schema import LayoutBox, LayoutResult

        layouts = []
        for img in images:
            w, h = img.size
            layouts.append(
                LayoutResult(
                    bboxes=[
                        LayoutBox(
                            polygon=[0, 0, w, h],
                            label="Text",
                            raw_label="Text",
                            position=0,
                        )
                    ],
                    image_bbox=[0, 0, w, h],
                )
            )
        return predictor(images, layouts, full_page=False)

    def run_batch(batch: list[dict]) -> list[tuple[str, str | None, float]]:
        start = time.monotonic()
        try:
            images = []
            for item in batch:
                path = item.get("image", "")
                if not path or not Path(path).is_file():
                    images.append(None)
                    continue
                with Image.open(path) as handle:
                    if getattr(handle, "n_frames", 1) > 1:
                        print(f"note: multipage tif, using first frame: {path}", flush=True)
                    images.append(handle.convert("RGB"))
            pages = predict([img if img is not None else Image.new("RGB", (32, 32)) for img in images])
            elapsed = time.monotonic() - start
            out = []
            for item, image, page in zip(batch, images, pages):
                if image is None:
                    out.append(("", f"missing-file:{item.get('image', '')}", 0.0))
                else:
                    out.append((page_text(page), None, round(elapsed / len(batch), 3)))
            return out
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - start
            return [("", f"{type(exc).__name__}:{str(exc)[:120]}", round(elapsed, 3)) for _ in batch]

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for batch in batches:
            future = pool.submit(run_batch, batch)
            try:
                results = future.result(timeout=args.timeout * len(batch))
            except FuturesTimeoutError:
                results = [("", "timeout", args.timeout) for _ in batch]
            for item, (text, error, elapsed) in zip(batch, results):
                rows.append({"id": item["id"], "text": text, "seconds": elapsed, "error": error, "model_rev": rev})

    manager.stop()
    write_rows(rows, args.output)
    print(f"surya split={args.split} {summarize(rows)}")


if __name__ == "__main__":
    main()
