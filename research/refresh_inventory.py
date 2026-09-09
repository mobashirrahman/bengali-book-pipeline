#!/usr/bin/env python3
"""S1 corpus inventory refresh + pilot-family duplicate check (read-only).

Re-runs `inventory.build_inventory` against the live cluster/catalogue state
and, separately, checks whether any of the 12 frozen pilot families is a
duplicate / edition of another book (or of another pilot family) that a human
must confirm before the split is locked (roadmap / `RESEARCH_PROTOCOL.md` §5).

Offline, read-only: every SQLite handle is opened `mode=ro`; nothing under
`data/` is written or hashed; no network, no model.

Usage::

    .venv/bin/python research/refresh_inventory.py
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
from pathlib import Path

from pdf_craft_tool.research import inventory, splits

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO / "research" / "configs" / "inventory.json"
DEFAULT_OUT = REPO / "pdf-craft-output" / "research" / "inventory"
PILOT_PROVENANCE = (REPO / "pdf-craft-output" / "research" / "pilot-study"
                    / "pilot_provenance.json")


def _resolve(value) -> str | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = REPO / path
    return str(path) if path.exists() else None


def _load_sources(config_path: Path) -> inventory.InventorySources:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise SystemExit(f"{config_path} must be a JSON object")
    return inventory.InventorySources(
        queue_db=_resolve(config.get("queue_db")),
        catalogue_db=_resolve(config.get("catalogue_db")),
        dedupe_report=_resolve(config.get("dedupe_report")),
        metadata_csv=_resolve(config.get("metadata_csv")),
        jobs_dir=_resolve(config.get("jobs_dir")),
        data_root=_resolve(config.get("data_root")),
    )


def pilot_family_duplicate_check(catalogue_db: str | None,
                                 provenance_path: Path) -> dict:
    """Group the 12 pilot families and attach catalogue duplicate evidence.

    For each pilot family we (a) look up its source PDF in
    ``catalogue_local_documents`` by sha256, (b) list every duplicate cluster
    it belongs to and every sibling member of those clusters, and (c) run
    ``splits.group_families`` over the 12 families plus catalogue
    ``same_work`` / ``same_edition`` edges between them, so two pilot families
    that the catalogue links land in one group with a ``needs_confirmation``
    pair.
    """
    provenance = json.loads(Path(provenance_path).read_text(encoding="utf-8"))
    families_in = provenance["families"]

    documents: list[splits.Document] = []
    per_family: list[dict] = []
    sha_to_family: dict[str, str] = {}
    doc_to_family: dict[int, str] = {}

    conn = None
    if catalogue_db:
        try:
            conn = sqlite3.connect(f"file:{catalogue_db}?mode=ro", uri=True)
        except sqlite3.Error:
            conn = None

    for entry in families_in:
        sha = entry["source_sha256"]
        fam_id = entry["job_id"][:16]
        sha_to_family[sha] = fam_id
        documents.append(splits.Document(
            document_id=fam_id, content_sha256=sha,
            work_id="", edition_id="", processed=True,
            metadata={"author_dir": entry["author_dir"],
                      "title": entry["title"]}))
        record = {
            "family_id": fam_id,
            "author_dir": entry["author_dir"],
            "title": entry["title"],
            "source_sha256": sha,
            "catalogue_document_id": None,
            "is_non_keeper": False,
            "clusters": [],
        }
        if conn is not None:
            row = conn.execute(
                "SELECT id FROM catalogue_local_documents WHERE sha256=?",
                (sha,)).fetchone()
            if row is not None:
                doc_id = int(row[0])
                record["catalogue_document_id"] = doc_id
                doc_to_family[doc_id] = fam_id
                for cid, is_keeper, relation, size in conn.execute(
                        "SELECT m.cluster_id, m.is_keeper, cl.relation, cl.size "
                        "FROM catalogue_duplicate_members m "
                        "JOIN catalogue_duplicate_clusters cl "
                        "ON cl.id = m.cluster_id WHERE m.document_id=?",
                        (doc_id,)):
                    if not is_keeper:
                        record["is_non_keeper"] = True
                    members = []
                    for m_doc, m_keep, m_sha, m_path in conn.execute(
                            "SELECT m.document_id, m.is_keeper, d.sha256, "
                            "d.source_path FROM catalogue_duplicate_members m "
                            "JOIN catalogue_local_documents d "
                            "ON d.id = m.document_id WHERE m.cluster_id=?",
                            (cid,)):
                        members.append({
                            "document_id": int(m_doc),
                            "is_keeper": bool(m_keep),
                            "sha256": m_sha,
                            "source_path": m_path,
                        })
                    record["clusters"].append({
                        "cluster_id": int(cid), "relation": relation,
                        "size": int(size) if size is not None else None,
                        "members": members,
                    })
        per_family.append(record)

    # Advisory edges between two pilot families the catalogue clusters
    # together. Catalogue clustering is a signal, not a confirmed merge:
    # group_families keeps such families separate but flags the pair in
    # needs_confirmation for a human.
    links: list[list[str]] = []
    edges: list[splits.GroupEdge] = []
    for record in per_family:
        for cluster in record["clusters"]:
            for member in cluster["members"]:
                other = doc_to_family.get(member["document_id"])
                if other and other != record["family_id"]:
                    pair = sorted([record["family_id"], other])
                    if pair not in links:
                        links.append(pair)
                    edges.append(splits.GroupEdge(
                        a=record["family_id"], b=other,
                        relation=cluster["relation"], method="catalogue_cluster",
                        score=0.5, confirmed=False))
    if conn is not None:
        conn.close()

    grouped = splits.group_families(documents, edges)
    groups = [{
        "family_id": fam.family_id,
        "members": list(fam.members),
        "needs_confirmation": [list(pair) for pair in fam.needs_confirmation],
    } for fam in grouped]

    non_keepers = [r["family_id"] for r in per_family if r["is_non_keeper"]]
    in_any_cluster = [r["family_id"] for r in per_family if r["clusters"]]
    return {
        "pilot_families": per_family,
        "grouped": groups,
        "summary": {
            "pilot_families": len(per_family),
            "in_a_duplicate_cluster": len(in_any_cluster),
            "marked_non_keeper": non_keepers,
            "pilot_family_pairs_to_confirm": links,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--provenance", type=Path, default=PILOT_PROVENANCE)
    args = parser.parse_args(argv)

    sources = _load_sources(args.config)
    report = inventory.build_inventory(sources)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.out_dir / "inventory-report.json"
    report_path.write_text(
        json.dumps(dataclasses.asdict(report), ensure_ascii=False, indent=2,
                   sort_keys=True) + "\n", encoding="utf-8")

    groups_path = args.out_dir / "pilot-family-groups.json"
    if args.provenance.is_file():
        dup = pilot_family_duplicate_check(
            _resolve("pdf-craft-output/catalogue/catalogue.db"),
            args.provenance)
        groups_path.write_text(
            json.dumps(dup, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n", encoding="utf-8")
    else:
        dup = None

    print(f"inventory report -> {report_path}")
    print(f"  done jobs (job_state_counts): "
          f"{report.job_state_counts.get('done')}")
    print(f"  distinct content sha256: {report.distinct_content_sha256}")
    print(f"  done raw artifacts present: {report.done_raw_artifacts_present}")
    print(f"  done proofread artifacts present: "
          f"{report.done_proofread_artifacts_present}")
    print(f"  OCR page total: {report.ocr_page_total}")
    print(f"  duplicate clusters: {report.duplicate_cluster_count}")
    print(f"  candidate work groups: {report.candidate_work_groups}")
    if dup is not None:
        s = dup["summary"]
        print(f"pilot family groups -> {groups_path}")
        print(f"  pilot families in a duplicate cluster: "
              f"{s['in_a_duplicate_cluster']} / {s['pilot_families']}")
        print(f"  marked non-keeper: {s['marked_non_keeper'] or 'none'}")
        print(f"  pilot family pairs to confirm: "
              f"{s['pilot_family_pairs_to_confirm'] or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
