"""Combine per-engine/split scores -> summary.csv + report.md + run_meta.json."""

import argparse
import csv
import datetime
import json
import statistics
import subprocess
from pathlib import Path


def percentile(values: list[float], pct: float):
    if not values:
        return None
    ordered = sorted(values)
    rank = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[rank]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scores", nargs="+", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--engines", nargs="+", required=True)
    parser.add_argument("--splits", nargs="+", required=True)
    parser.add_argument("--normalization", required=True)
    parser.add_argument("--license-reid", required=True)
    parser.add_argument("--license-mozhi", required=True)
    parser.add_argument("--output-report", required=True)
    parser.add_argument("--output-summary", required=True)
    parser.add_argument("--output-meta", required=True)
    args = parser.parse_args()

    scores = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.scores]
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    by_key = {(s["engine"], s["split"]): s for s in scores}

    rows = []
    for engine in args.engines:
        for split in args.splits:
            s = by_key[(engine, split)]
            items = s["items"]
            cers = [r["cer"] for r in items]
            wers = [r["wer"] for r in items]
            secs = [r["seconds"] for r in items if r.get("seconds") is not None]
            err = sum(1 for r in items if r.get("error"))
            rows.append(
                {
                    "engine": engine,
                    "split": split,
                    "model_rev": s.get("model_rev", ""),
                    "n": s["n"],
                    "missing": s["missing"],
                    "errors": err,
                    "cer_micro": round(s["cer"], 5),
                    "wer_micro": round(s["wer"], 5),
                    "cer_macro": round(statistics.mean(cers), 5) if cers else "",
                    "wer_macro": round(statistics.mean(wers), 5) if wers else "",
                    "mean_sec": round(statistics.mean(secs), 3) if secs else "",
                    "median_sec": round(statistics.median(secs), 3) if secs else "",
                    "p95_sec": round(percentile(secs, 95), 3) if secs else "",
                    "ins": sum(r["character_operations"]["insert"] for r in items),
                    "del": sum(r["character_operations"]["delete"] for r in items),
                    "rep": sum(r["character_operations"]["replace"] for r in items),
                }
            )

    summary_path = Path(args.output_summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        gpu = "unknown"

    run_meta = {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "inputs": meta,
        "normalization": args.normalization,
        "licenses": {"reid": args.license_reid, "mozhi-test": args.license_mozhi},
        "hardware": {"gpu": gpu},
        "engines": {
            f"{s['engine']}/{s['split']}": s.get("engine_meta", {})
            for s in scores
            if s.get("engine_meta")
        },
    }
    Path(args.output_meta).write_text(json.dumps(run_meta, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Bengali OCR benchmark",
        "",
        f"Engines: {', '.join(args.engines)}. Splits are never pooled: `reid` = historical "
        "page-level end-to-end (detection+recognition+reading order); `mozhi-test` = "
        "modern word recognition only.",
        "",
        "| engine | split | n | missing | errors | CER micro | WER micro | CER macro | WER macro | mean s | median s | p95 s | ins/del/rep |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['engine']} | {r['split']} | {r['n']} | {r['missing']} | {r['errors']} | "
            f"{r['cer_micro']} | {r['wer_micro']} | {r['cer_macro']} | {r['wer_macro']} | "
            f"{r['mean_sec']} | {r['median_sec']} | {r['p95_sec']} | {r['ins']}/{r['del']}/{r['rep']} |"
        )
    lines += ["", "## Per-split lowest-agreement items (spot-check)", ""]
    for engine in args.engines:
        for split in args.splits:
            s = by_key[(engine, split)]
            worst = sorted(s["items"], key=lambda r: (r["cer"], r["wer"]), reverse=True)[:3]
            lines.append(f"### {engine} / {split} (model: {s.get('model_rev', '')})")
            for w in worst:
                lines.append(f"- `{w['id']}` CER={w['cer']:.3f} WER={w['wer']:.3f} err={w.get('error')}")
            lines.append("")
    Path(args.output_report).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {summary_path} + report")


if __name__ == "__main__":
    main()
