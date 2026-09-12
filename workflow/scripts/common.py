"""Shared helpers for benchmark engine adapters.

Common hypotheses schema (JSONL, one row per reference item):
    {"id": str, "text": str, "seconds": float, "error": str | None,
     "model_rev": str}
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path


def load_items(path: str | Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def add_common_args(parser) -> None:
    parser.add_argument("--items", required=True, help="items JSON [{id, image}]")
    parser.add_argument("--output", required=True, help="hypotheses JSONL output")
    parser.add_argument("--split", required=True, help="reid | mozhi-test")
    parser.add_argument("--max-items", type=int, default=0, help="0 = all")
    parser.add_argument("--timeout", type=float, default=120.0, help="sec/item")
    parser.add_argument("--model-rev", default="unknown")


def selected(items: list[dict], max_items: int) -> list[dict]:
    return items if max_items <= 0 else items[:max_items]


def assign_word_lines(word_refs, line_refs, localize):
    """Assign each word box ``[x1, y1, x2, y2]`` to a line index.

    ``localize(word, line_refs)`` returns a line index or ``None`` when the
    word overlaps no line (apsisocr's ``localize_box`` does this). Upstream
    ``ApsisOCR.process_boxes`` (0.0.7) crashes on those with
    ``ValueError: cannot convert float NaN to integer``. Words with no
    overlap fall back to the nearest line by vertical-center distance; when
    no lines were detected at all, words are ordered top-to-bottom, each on
    its own line. Hits are returned unchanged, so behavior is identical to
    upstream whenever every word overlaps a line.
    """
    if not line_refs:
        order = sorted(
            range(len(word_refs)),
            key=lambda i: (
                (word_refs[i][1] + word_refs[i][3]) / 2.0,
                (word_refs[i][0] + word_refs[i][2]) / 2.0,
            ),
        )
        lines = [0] * len(word_refs)
        for rank, i in enumerate(order):
            lines[i] = rank
        return lines
    assigned = []
    for word in word_refs:
        lid = localize(word, line_refs)
        if lid is not None:
            assigned.append(int(lid))
            continue
        center = (word[1] + word[3]) / 2.0
        best, best_dist = 0, None
        for idx, (_x1, y1, _x2, y2) in enumerate(line_refs):
            dist = abs(center - (y1 + y2) / 2.0)
            if best_dist is None or dist < best_dist:
                best, best_dist = idx, dist
        assigned.append(best)
    return assigned


def run_sequential(
    items: list[dict],
    infer,
    timeout: float,
    model_rev: str,
    grace: float = 30.0,
) -> list[dict]:
    """Run infer(image_path) -> str over items with a per-item timeout.

    infer must raise on failure; "missing file" is reported as an error row.
    The worker-thread join gets ``timeout + grace`` so engine-level timeouts
    (e.g. subprocess) fire first with their own error message.
    """

    def guarded(image: str) -> tuple[str, str | None, float]:
        start = time.monotonic()
        try:
            if not image or not Path(image).is_file():
                return "", f"missing-file:{image}", time.monotonic() - start
            return infer(image) or "", None, time.monotonic() - start
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            return "", f"{type(exc).__name__}:{str(exc)[:120]}", time.monotonic() - start

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for item in items:
            future = pool.submit(guarded, item.get("image", ""))
            try:
                text, error, elapsed = future.result(timeout=timeout + grace)
            except FuturesTimeoutError:
                text, error, elapsed = "", "timeout", timeout + grace
            rows.append(
                {
                    "id": item["id"],
                    "text": text,
                    "seconds": round(elapsed, 3),
                    "error": error,
                    "model_rev": model_rev,
                }
            )
    return rows


def write_rows(rows: list[dict], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def summarize(rows: list[dict]) -> str:
    total = len(rows)
    errors = sum(1 for row in rows if row["error"])
    ok = [row["seconds"] for row in rows if not row["error"]]
    mean = sum(ok) / len(ok) if ok else 0.0
    return f"items={total} ok={len(ok)} errors={errors} mean_sec={mean:.2f}"
