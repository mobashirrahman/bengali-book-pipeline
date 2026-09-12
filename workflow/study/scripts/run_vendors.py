#!/usr/bin/env python3
"""Run the frozen vendor voters (GV + AZ) over study pages with discipline.

Quota ledger + prediction cache + per-adapter budgets from
research/configs/study-baselines.json. Rights gate reads the study's
rights.json (default-deny: no recorded user confirmation, no network).
Keys come from the environment (loaded from repo .env if unset); values are
never printed or logged.
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from pdf_craft_tool.research import adapters, runners, vendor_transports  # noqa: E402

ENV_KEYS = ("GOOGLE_VISION_API_KEY", "AZURE_VISION_KEY", "AZURE_VISION_ENDPOINT")


def load_dotenv() -> None:
    import os
    env_file = REPO / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in ENV_KEYS:
            os.environ.setdefault(key, value.strip().strip("'\""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True)
    parser.add_argument("--adapters", default="GV,AZ")
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--out", required=True, help="predictions JSONL")
    parser.add_argument("--cache", default=None)
    parser.add_argument("--ledger", default=None)
    args = parser.parse_args()

    import os

    load_dotenv()
    study = Path(args.study)
    rights = json.loads((study / "rights.json").read_text(encoding="utf-8"))
    confirmed = rights.get("status") == "user-confirmed"
    gate = lambda page_id: confirmed  # noqa: E731 - deny by default

    items = json.loads((study / "items.json").read_text(encoding="utf-8"))
    if args.max_pages > 0:
        items = items[: args.max_pages]
    pages = [{"page_id": item["id"], "image_ref": item["image"],
              "ocr_text": ""} for item in items]

    wanted = [a.strip() for a in args.adapters.split(",") if a.strip()]
    specs = [s for s in adapters.load_adapter_specs(
        "research/configs/study-baselines.json") if s.adapter_id in wanted]
    if {s.adapter_id for s in specs} != set(wanted):
        raise SystemExit(f"spec mismatch for {wanted}")
    instances = {s.adapter_id: adapters.make_adapter(s) for s in specs}
    frozen = json.loads((REPO / "research/configs/study-baselines.json").read_text())
    per_adapter = {aid: dict(frozen["budgets"][aid]) for aid in wanted}
    plan = runners.RunPlan(
        study_id=study.name, adapter_ids=tuple(s.adapter_id for s in specs),
        sample_manifest=str(study / "sample_manifest.json"),
        budget={"max_model_calls": len(pages) * len(specs) + 5,
                "max_wall_seconds": 7200, "per_adapter": per_adapter})

    env = {key: os.environ.get(key, "") for key in ENV_KEYS}
    clients = {}
    if "GV" in instances:
        clients["GV"] = vendor_transports.make_google_vision_transport(
            rights_gate=gate, env=env)
    if "AZ" in instances:
        clients["AZ"] = vendor_transports.make_azure_read_transport(
            rights_gate=gate, env=env)

    cache_path = Path(args.cache) if args.cache else study / "predictions.vendor-cache.jsonl"
    ledger_path = Path(args.ledger) if args.ledger else study / "quota-ledger.jsonl"
    result = runners.run_baselines(
        plan, pages=pages, adapters=instances, allow_execution=True,
        clients=clients, cache=runners.PredictionCache(cache_path),
        ledger=runners.QuotaLedger(ledger_path))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for record in result["predictions"]:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    by_adapter = result.get("by_adapter", {})
    print(f"vendors pages={len(pages)} predictions={len(result['predictions'])} "
          f"by_adapter={json.dumps(by_adapter, sort_keys=True)} "
          f"quota_exhausted={json.dumps(result.get('quota_exhausted', {}), sort_keys=True)} "
          f"budget_exhausted={result.get('budget_exhausted')} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
