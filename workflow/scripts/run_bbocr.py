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
from common import add_common_args, assign_word_lines, load_items, run_sequential, selected, summarize, write_rows  # noqa: E402


def _install_line_assignment_fix(ocr_cls, localize_box):
    """Patch ``ApsisOCR.process_boxes`` against its NaN line crash.

    Upstream 0.0.7 does ``data.lines.apply(lambda x: int(x))`` where
    ``localize_box`` returned ``None`` for word boxes overlapping no line
    box (22/51 REID pages) -> ``ValueError: cannot convert float NaN to
    integer``. The patched copy is identical except line assignment goes
    through :func:`common.assign_word_lines`, which keeps every upstream
    hit and only reroutes the ``None`` cases to the nearest line.
    """
    import copy

    import numpy as np
    import pandas as pd

    def process_boxes(self, word_boxes, line_boxes):
        line_orgs = []
        line_refs = []
        for bno in range(len(line_boxes)):
            tmp_box = copy.deepcopy(line_boxes[bno])
            tmp_box = np.array(tmp_box).reshape(4, 2)
            x2, x1 = int(max(tmp_box[:, 0])), int(min(tmp_box[:, 0]))
            y2, y1 = int(max(tmp_box[:, 1])), int(min(tmp_box[:, 1]))
            line_orgs.append([x1, y1, x2, y2])
            line_refs.append([x1, y1, x2, y2])

        # merge
        for lidx, box in enumerate(line_refs):
            if box is not None:
                for nidx in range(lidx + 1, len(line_refs)):
                    x1, y1, x2, y2 = box
                    x1n, y1n, x2n, y2n = line_orgs[nidx]
                    dist = min([abs(y2 - y1), abs(y2n - y1n)])
                    if abs(y1 - y1n) < dist and abs(y2 - y2n) < dist:
                        x1, x2, y1, y2 = (
                            min([x1, x1n]),
                            max([x2, x2n]),
                            min([y1, y1n]),
                            max([y2, y2n]),
                        )
                        box = [x1, y1, x2, y2]
                        line_refs[lidx] = None
                        line_refs[nidx] = box

        line_refs = [lr for lr in line_refs if lr is not None]
        # sort line refs based on Y-axis
        line_refs = sorted(line_refs, key=lambda x: x[1])
        # word_boxes
        word_refs = []
        for bno in range(len(word_boxes)):
            tmp_box = copy.deepcopy(word_boxes[bno])
            tmp_box = np.array(tmp_box).reshape(4, 2)
            x2, x1 = int(max(tmp_box[:, 0])), int(min(tmp_box[:, 0]))
            y2, y1 = int(max(tmp_box[:, 1])), int(min(tmp_box[:, 1]))
            word_refs.append([x1, y1, x2, y2])

        data = pd.DataFrame(
            {"words": word_refs, "word_ids": [i for i in range(len(word_refs))]}
        )
        # detect line-word (NaN-tolerant: see docstring)
        data["lines"] = assign_word_lines(word_refs, line_refs, localize_box)
        # register as crop
        text_dict = []
        for line in data.lines.unique():
            ldf = data.loc[data.lines == line]
            _boxes = ldf.words.tolist()
            _bids = ldf.word_ids.tolist()
            _, bids = zip(*sorted(zip(_boxes, _bids), key=lambda x: x[0][0]))
            for idx, bid in enumerate(bids):
                _dict = {
                    "line_no": line,
                    "word_no": idx,
                    "crop_id": bid,
                    "poly": word_boxes[bid],
                }
                text_dict.append(_dict)
        data = pd.DataFrame(text_dict)
        return data

    ocr_cls.process_boxes = process_boxes


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
        from apsisocr.utils import localize_box

        _install_line_assignment_fix(ApsisOCR, localize_box)
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
