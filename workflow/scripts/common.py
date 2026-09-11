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
