"""Vendor OCR smoke check on synthetic Bangla pages (dry run by default).

Renders 5 SYNTHETIC Bangla pages with PIL (hard-coded sentences written in
this script -- no book content; page 5 is blank), writes PNGs plus a truth
JSON under ``pdf-craft-output/research/main-study-1000/vendor-smoke/``, then
runs the GV and AZ vendor adapters via ``run_baselines`` with a
``PredictionCache`` and ``QuotaLedger`` in that directory.

Default is a dry run (``allow_execution=False``): every row is ``unsupported``
and no network, key or endpoint is touched. Pass ``--execute`` to perform
real vendor calls (requires keys in ``.env`` and recorded rights review).
Only ``--execute`` was deliberately NOT run by automation; run it by hand.
Prints one table per vendor: page_id, failure_state, n_chars, CER vs truth
(simple Levenshtein), model_version. Never prints keys, endpoints or raw
responses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from pdf_craft_tool.research import (  # noqa: E402
    adapters,
    runners,
    vendor_transports,
)

STUDY_DIR = (
    REPO_ROOT / "pdf-craft-output" / "research" / "main-study-1000"
    / "vendor-smoke"
)

# Hard-coded synthetic sentences (original, no book content). Page 5 blank.
SENTENCES = [
    "ঢাকার আকাশে আজ সাদা মেঘ ভেসে বেড়াচ্ছে।",
    "বইয়ের পাতায় কালো কালির অক্ষর জ্বলজ্বল করছে।",
    "শিশুটি মাঠে দৌড়ে প্রজাপতির পেছনে ছুটল।",
    "বর্ষার নদীতে নতুন পানির স্রোত বইছে।",
    "",
]


def load_dotenv(path: Path) -> None:
    """Parse KEY=VALUE lines into os.environ (no overrides, no printing)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"').strip()
        if key and key not in os.environ:
            os.environ[key] = value


def find_font(preferred: str) -> str | None:
    """Resolve a font file via fc-match (None when unavailable)."""
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", preferred],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    candidate = (result.stdout or "").strip().splitlines()
    if result.returncode == 0 and candidate and Path(candidate[0]).is_file():
        return candidate[0]
    return None


def synthetic_page_id(label: str) -> str:
    """Content-hashed page id for a synthetic label.

    ``schema.Prediction.page_id`` must be sha256 hex, so synthetic pages use
    ``sha256("vendor-smoke:<label>")`` rather than the bare label.
    """
    return hashlib.sha256(f"vendor-smoke:{label}".encode("utf-8")).hexdigest()


def render_pages(pages_dir: Path) -> list[dict]:
    """Render synthetic PNGs; return [{page_id, label, image_ref, text}]."""
    from PIL import Image, ImageDraw, ImageFont

    pages_dir.mkdir(parents=True, exist_ok=True)
    font_path = find_font("Noto Sans Bengali") or find_font(
        "Noto Serif Bengali"
    )
    font = (
        ImageFont.truetype(font_path, 40) if font_path
        else ImageFont.load_default()
    )
    pages = []
    for index, sentence in enumerate(SENTENCES, start=1):
        label = f"synthetic-{index:02d}"
        page_id = synthetic_page_id(label)
        image_path = pages_dir / f"{label}.png"
        image = Image.new("RGB", (1200, 400), "white")
        drawer = ImageDraw.Draw(image)
        if sentence:
            drawer.text((60, 140), sentence, fill="black", font=font)
        image.save(image_path)
        pages.append(
            {"page_id": page_id, "label": label,
             "image_ref": str(image_path), "text": sentence}
        )
    return pages


def levenshtein(left: str, right: str) -> int:
    """Simple char-level Levenshtein distance."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            )
        previous = current
    return previous[-1]


def cer(truth: str, hypothesis: str) -> float:
    """Character error rate (blank truth scores 0.0 when empty, else 1.0)."""
    if not truth:
        return 0.0 if not hypothesis else 1.0
    return levenshtein(truth, hypothesis) / len(truth)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform real vendor calls (default: dry run)",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    # Defence in depth: dry-run and live state (pages, truth, cache,
    # ledger) live in separate subdirectories so a dry run can never
    # populate -- or read -- live results and vice versa.
    state_dir = STUDY_DIR / ("live" if args.execute else "dry")
    state_dir.mkdir(parents=True, exist_ok=True)

    rendered = render_pages(state_dir / "pages")
    truth = {entry["page_id"]: entry["text"] for entry in rendered}
    (state_dir / "truth.json").write_text(
        json.dumps(
            [
                {"page_id": entry["page_id"], "text": entry["text"]}
                for entry in rendered
            ],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    specs = adapters.load_adapter_specs("research/configs/study-baselines.json")
    instances = {spec.adapter_id: adapters.make_adapter(spec)
                 for spec in specs}
    config = {
        spec.adapter_id: dict(spec.config)
        for spec in specs
    }
    budgets = json.loads(
        (REPO_ROOT / "research" / "configs" / "study-baselines.json")
        .read_text(encoding="utf-8")
    )["budgets"]

    synthetic_ids = {entry["page_id"] for entry in rendered}

    def rights_gate(page_id: str) -> bool:
        # Synthetic pages carry content-hashed ids (schema mandates sha256
        # hex, so the "synthetic-" label cannot itself be a page id); the
        # gate allows exactly this run's synthetic ids. Everything else
        # (notably any book page) is refused without a network call.
        return page_id in synthetic_ids

    clients: dict = {}
    if args.execute:
        clients = {
            "GV": vendor_transports.make_google_vision_transport(
                rights_gate=rights_gate
            ),
            "AZ": vendor_transports.make_azure_read_transport(
                rights_gate=rights_gate
            ),
        }

    plan = runners.RunPlan(
        study_id="vendor-smoke",
        adapter_ids=tuple(spec.adapter_id for spec in specs),
        sample_manifest="synthetic",
        budget={
            "max_model_calls": 10,
            "max_wall_seconds": 600,
            "per_adapter": {
                adapter_id: {
                    "max_calls_per_month": budgets[adapter_id][
                        "max_calls_per_month"
                    ],
                    "min_interval_seconds": budgets[adapter_id][
                        "min_interval_seconds"
                    ],
                }
                for adapter_id in instances
            },
        },
    )
    pages = [
        {"page_id": entry["page_id"], "image_ref": entry["image_ref"],
         "ocr_text": ""}
        for entry in rendered
    ]
    result = runners.run_baselines(
        plan,
        pages=pages,
        adapters=instances,
        allow_execution=bool(args.execute),
        clients=clients,
        cache=runners.PredictionCache(state_dir / "cache.jsonl"),
        ledger=runners.QuotaLedger(state_dir / "quota.jsonl"),
    )

    by_page = {}
    for record in result["predictions"]:
        by_page[(record["system_id"], record["page_id"])] = record
    for adapter_id in plan.adapter_ids:
        print(f"vendor {adapter_id}")
        print("page_id failure_state n_chars cer model_version")
        for entry in rendered:
            record = by_page.get((adapter_id, entry["page_id"]), {})
            failure = record.get("failure_state", "missing")
            hypothesis = record.get("parsed_text", "")
            print(
                f"{entry['page_id']} {failure} {len(hypothesis)} "
                f"{cer(truth[entry['page_id']], hypothesis):.3f} "
                f"{config[adapter_id].get('model_version', '')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
