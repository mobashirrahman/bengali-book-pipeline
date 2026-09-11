"""Score hypotheses JSONL against references -> scores JSON."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vendor import load  # noqa: E402

_external_eval = load()
mozhi_references = _external_eval.mozhi_references
reid_references = _external_eval.reid_references
score_hypotheses = _external_eval.score_hypotheses


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--split", required=True)
    parser.add_argument("--reid-dir", required=True)
    parser.add_argument("--mozhi-test-dir", required=True)
    parser.add_argument("--items", required=True)
    parser.add_argument("--hyp", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.split == "reid":
        items = reid_references(Path(args.reid_dir))
    elif args.split == "mozhi-test":
        items = mozhi_references(Path(args.mozhi_test_dir), "test")
    else:
        raise ValueError(f"unknown split: {args.split}")

    wanted = {row["id"] for row in json.loads(Path(args.items).read_text(encoding="utf-8"))}
    items = [row for row in items if row["id"] in wanted]

    hypotheses, timing, errors = {}, {}, {}
    model_rev = "unknown"
    for raw in Path(args.hyp).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        hypotheses[row["id"]] = row.get("text", "")
        timing[row["id"]] = row.get("seconds")
        errors[row["id"]] = row.get("error")
        model_rev = row.get("model_rev", model_rev)

    result = score_hypotheses(items, hypotheses)
    for row in result["items"]:
        row["seconds"] = timing.get(row["id"])
        row["error"] = errors.get(row["id"])
    result.update(
        {
            "engine": args.engine,
            "split": args.split,
            "model_rev": model_rev,
            "scorer": "pdf_craft_tool.external_eval.score_hypotheses over pdf_craft_tool.benchmark.score",
        }
    )
    meta_path = Path(str(args.hyp) + ".meta.json")
    if meta_path.is_file():
        try:
            result["engine_meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"engine={args.engine} split={args.split} n={result['n']} cer={result['cer']:.4f} wer={result['wer']:.4f}")


if __name__ == "__main__":
    main()
