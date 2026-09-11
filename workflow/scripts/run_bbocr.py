#!/usr/bin/env python3
"""bbOCR / APSIS-Net adapter: images -> hypotheses JSONL (common schema).

- mozhi-test (word crops): ApsisNet.infer on RGB crops, pre-batched.
- reid (full pages): ApsisOCR full pipeline (needs fastdeploy detector).
CPU onnxruntime; GPU optional via --device flag passed to detectors.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import add_common_args, load_items, run_sequential, selected, summarize, write_rows  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    args = parser.parse_args()

    import apsisocr  # noqa: F401
    from importlib.metadata import version

    rev = (
        args.model_rev
        if args.model_rev != "unknown"
        else f"apsisocr-{version('apsisocr')}-{args.device}"
    )
    items = selected(load_items(args.items), args.max_items)

    if args.split == "mozhi-test":
        import cv2
        from apsisocr import ApsisNet

        recognizer = ApsisNet()
        batches = [items[i : i + args.batch] for i in range(0, len(items), args.batch)]
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            for batch in batches:
                future = pool.submit(_infer_crops, recognizer, batch)
                try:
                    results = future.result(timeout=args.timeout * len(batch))
                except FuturesTimeoutError:
                    results = [("", "timeout", args.timeout) for _ in batch]
                for item, (text, error, elapsed) in zip(batch, results):
                    rows.append(
                        {"id": item["id"], "text": text, "seconds": elapsed, "error": error, "model_rev": rev}
                    )
    else:
        from apsisocr import ApsisOCR

        ocr = ApsisOCR()

        def infer(image: str) -> str:
            result = ocr(image)
            if isinstance(result, dict):
                return result.get("text", "")
            return str(result)

        rows = run_sequential(items, infer, args.timeout, rev)

    write_rows(rows, args.output)
    print(f"bbocr split={args.split} {summarize(rows)}")


def _infer_crops(recognizer, batch: list[dict]) -> list[tuple[str, str | None, float]]:
    import cv2

    start = time.monotonic()
    images, valid = [], []
    for item in batch:
        path = item.get("image", "")
        image = cv2.imread(path) if path else None
        if image is None:
            images.append(None)
        else:
            images.append(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        valid.append(image is not None)
    try:
        crops = [img for img in images if img is not None]
        texts = recognizer.infer(crops, normalize_unicode=True) if crops else []
        elapsed = time.monotonic() - start
        out, it = [], iter(texts)
        for item, ok in zip(batch, valid):
            if not ok:
                out.append(("", f"missing-file:{item.get('image', '')}", 0.0))
            else:
                out.append((next(it) or "", None, round(elapsed / len(batch), 3)))
        return out
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - start
        return [("", f"{type(exc).__name__}:{str(exc)[:120]}", round(elapsed, 3)) for _ in batch]


if __name__ == "__main__":
    main()
