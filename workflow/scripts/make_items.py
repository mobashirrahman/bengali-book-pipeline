"""Dump reference items [{id, image}] for one split -> items JSON."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vendor import load  # noqa: E402

_external_eval = load()
mozhi_references, reid_references = _external_eval.mozhi_references, _external_eval.reid_references


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, help="reid | mozhi-test")
    parser.add_argument("--reid-dir", required=True)
    parser.add_argument("--mozhi-test-dir", required=True)
    parser.add_argument("--max-items-reid", type=int, default=0)
    parser.add_argument("--max-items-mozhi", type=int, default=0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.split == "reid":
        items = reid_references(Path(args.reid_dir))
        max_items = args.max_items_reid
    elif args.split == "mozhi-test":
        items = mozhi_references(Path(args.mozhi_test_dir), "test")
        max_items = args.max_items_mozhi
    else:
        raise ValueError(f"unknown split: {args.split}")
    if max_items > 0:
        items = items[:max_items]

    slim = [{"id": item["id"], "image": item["image"]} for item in items]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"split={args.split} items={len(slim)} -> {out}")


if __name__ == "__main__":
    main()
