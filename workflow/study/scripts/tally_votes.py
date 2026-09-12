#!/usr/bin/env python3
"""Align the 5 voters' lines to census regions, record blind votes, triage.

Inputs (study root): annotation_pages.json, lines_tesseract/easyocr/surya.jsonl
({id, lines:[{text,bbox,conf}], error}), predictions.vendors.jsonl (GV/AZ raw
responses in raw_output). Output: votes.sqlite3 (StudyVoteStore) + triage.json.

Quorum lives in StudyVoteStore.triage: 5/5 ok + equal normalised texts is
provisional_accept, everything else is a conflict for the human adjudicator.
"""

import argparse
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from pdf_craft_tool.research import study_adjudication, study_align  # noqa: E402

VOTERS = ("tesseract", "easyocr", "surya", "google", "azure")


def norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def load_lines(path: Path) -> dict:
    rows = {}
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=None, help="triage JSON")
    args = parser.parse_args()

    study = Path(args.study)
    pages = json.loads((study / "annotation_pages.json").read_text(encoding="utf-8"))
    local = {v: load_lines(study / f"lines_{v}.jsonl")
             for v in ("tesseract", "easyocr", "surya")}
    vendor_preds = {}
    for line in (study / "predictions.vendors.jsonl").read_text(
            encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            vendor_preds[(row["system_id"], row["page_id"])] = row

    db_path = Path(args.db) if args.db else study / "votes.sqlite3"
    store = study_adjudication.StudyVoteStore(db_path, voter_ids=VOTERS)
    report = {"pages": {}, "totals": Counter(), "failures": Counter(),
              "pairwise": {}, "align_quality": Counter()}
    pair_agree = Counter()
    pair_total = Counter()

    for page in pages:
        page_id = page["page_id"]
        entries = page.get("census", {}).get("entries", [])
        region_ids = sorted({e.get("region_index") for e in entries
                             if isinstance(e.get("region_index"), int)})
        texts: dict[str, dict] = {}
        states: dict[str, str] = {}
        # Vendor voters: align raw responses.
        for voter, system in (("google", "GV"), ("azure", "AZ")):
            pred = vendor_preds.get((system, page_id))
            if pred is None:
                texts[voter] = {}
                states[voter] = "missing"
            elif pred.get("failure_state") != "ok":
                texts[voter] = {}
                states[voter] = pred.get("failure_state", "error")
            else:
                engine = "google" if voter == "google" else "azure"
                alignment = study_align.align_page(engine, pred.get("raw_output", ""), entries)
                texts[voter] = alignment.region_texts
                states[voter] = "ok"
                report["align_quality"][f"{voter}_unassigned"] += len(alignment.unassigned)
                report["align_quality"][f"{voter}_ambiguous"] += len(alignment.ambiguous)
        # Local voters: align {text,bbox} rows.
        for voter in ("tesseract", "easyocr", "surya"):
            row = local[voter].get(page_id)
            if row is None:
                texts[voter] = {}
                states[voter] = "missing"
            elif row.get("error"):
                texts[voter] = {}
                states[voter] = "error"
            else:
                alignment = study_align.align_page("records", row.get("lines", []), entries)
                texts[voter] = alignment.region_texts
                states[voter] = "ok"
                report["align_quality"][f"{voter}_unassigned"] += len(alignment.unassigned)
                report["align_quality"][f"{voter}_ambiguous"] += len(alignment.ambiguous)

        for voter, state in states.items():
            if state != "ok":
                report["failures"][f"{voter}_{state}"] += 1
        for idx in region_ids:
            votes = {}
            for voter in VOTERS:
                if states[voter] == "ok":
                    votes[voter] = {"text": texts[voter].get(idx, ""),
                                    "failure_state": "ok"}
                else:
                    votes[voter] = {"text": "", "failure_state": states[voter]}
            store.record_votes(page_id, idx, votes)
        # Pairwise agreement on mutually-ok regions.
        ok_voters = [v for v in VOTERS if states[v] == "ok"]
        for i, a in enumerate(ok_voters):
            for b in ok_voters[i + 1:]:
                for idx in region_ids:
                    pair_total[(a, b)] += 1
                    if norm(texts[a].get(idx, "")) == norm(texts[b].get(idx, "")):
                        pair_agree[(a, b)] += 1
        tri = store.triage(page_id)
        report["pages"][page_id] = tri
        report["totals"]["provisional_accept"] += tri["provisional_accept"]
        report["totals"]["conflict"] += tri["conflict"]
        report["totals"]["regions"] += tri["regions"]

    report["pairwise"] = {
        f"{a}+{b}": round(pair_agree[(a, b)] / pair_total[(a, b)], 4)
        for (a, b) in sorted(pair_total)}
    report["totals"] = dict(report["totals"])
    report["failures"] = dict(report["failures"])
    report["align_quality"] = dict(report["align_quality"])
    out = Path(args.out) if args.out else study / "triage.json"
    out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1),
                   encoding="utf-8")
    store.close()
    t = report["totals"]
    print(f"tally pages={len(report['pages'])} regions={t.get('regions', 0)} "
          f"accept={t.get('provisional_accept', 0)} conflict={t.get('conflict', 0)} "
          f"failures={json.dumps(report['failures'], sort_keys=True)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
