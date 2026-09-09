"""Tests for the S1 research inventory audit (synthetic fixtures only)."""

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research.inventory import InventorySources, build_inventory

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def write_queue_db(path: Path, data_root: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE sources (path TEXT, size INTEGER, "
                     "sha256 TEXT, profile TEXT, present INTEGER)")
        conn.execute("CREATE TABLE jobs (id TEXT, sha256 TEXT, "
                     "profile TEXT, source TEXT, title TEXT, state TEXT, "
                     "result TEXT)")
        present_files = []
        for index in range(1, 7):
            target = data_root / f"book{index}.pdf"
            target.write_bytes(b"%PDF-1.4 fixture\n")
            present_files.append(str(target))
        shas = [SHA_A, SHA_A, SHA_B, SHA_C, SHA_D, SHA_E]
        for index, digest in enumerate(shas, start=1):
            conn.execute("INSERT INTO sources VALUES (?,?,?,?,?)",
                         (present_files[index - 1], 100 + index, digest,
                          "full" if index % 2 else "fast", 1))
        jobs = [("j1", SHA_A, "full", "done"),
                ("j2", SHA_B, "full", "done"),
                ("j3", SHA_C, "fast", "done"),
                ("j4", SHA_D, "fast", "done"),
                ("j5", SHA_E, "full", "failed")]
        for job_id, digest, profile, state in jobs:
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                         (job_id, digest, profile, "scanner",
                          "title-" + job_id, state, ""))
        conn.commit()
    finally:
        conn.close()


def write_result_dirs(jobs_dir: Path) -> None:
    raw_ok = jobs_dir / "raw-ok.pdf"
    raw_ok.write_bytes(b"raw")
    specs = {
        "j1": {"pages": [1, 2, 3], "raw": str(raw_ok),
               "proofreading": "not run"},
        "j2": {"pages": [1], "raw": str(jobs_dir / "raw2.pdf"),
               "proofreading": str(jobs_dir / "missing-proof.pcex")},
        "j3": {"pages": [2, 3], "raw": str(jobs_dir / "missing-raw.pdf"),
               "proofreading": "not run"},
        "j4": {"pages": [], "raw": str(jobs_dir / "missing-raw.pdf"),
               "proofreading": None},
    }
    (jobs_dir / "raw2.pdf").write_bytes(b"raw2")
    for job_id, payload in specs.items():
        dest = jobs_dir / job_id
        dest.mkdir(parents=True, exist_ok=True)
        payload = dict(payload,
                       source_sha256=SHA_A if job_id == "j1" else SHA_B)
        (dest / "result.json").write_text(json.dumps(payload),
                                          encoding="utf-8")


def write_metadata_csv(path: Path) -> None:
    rows = [
        {"doc_id": "d1", "extracted_title": "T1",
         "extracted_authors": "A1", "year": "1990"},
        {"doc_id": "d2", "extracted_title": "T2",
         "extracted_authors": "", "year": ""},
        {"doc_id": "d3", "extracted_title": "T3",
         "extracted_authors": "A3", "year": "2001"},
        {"doc_id": "d4", "extracted_title": "T4",
         "extracted_authors": "A4", "year": ""},
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["doc_id",
                                                    "extracted_title",
                                                    "extracted_authors",
                                                    "year"])
        writer.writeheader()
        writer.writerows(rows)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.queue = self.root / "queue.sqlite3"
        write_queue_db(self.queue, self.root / "data")
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir()
        write_result_dirs(self.jobs_dir)
        self.meta = self.root / "extracted-metadata.csv"
        write_metadata_csv(self.meta)

    def make_sources(self, **overrides):
        kwargs = {"queue_db": self.queue, "catalogue_db": None,
                  "dedupe_report": None, "metadata_csv": self.meta,
                  "jobs_dir": self.jobs_dir,
                  "data_root": self.root / "data"}
        kwargs.update(overrides)
        return InventorySources(**kwargs)

    def test_distinct_counts(self):
        report = build_inventory(self.make_sources())
        self.assertEqual(report.file_count, 6)
        self.assertEqual(report.distinct_content_sha256, 5)
        self.assertEqual(report.job_state_counts.get("failed"), 1)
        self.assertEqual(report.job_state_counts.get("done"), 4)
        self.assertEqual(len(report.processing_profiles), 2)

    def test_artifact_presence(self):
        report = build_inventory(self.make_sources())
        self.assertEqual(report.done_raw_artifacts_present, 2)
        self.assertEqual(report.done_proofread_artifacts_present, 0)

    def test_ocr_page_total(self):
        report = build_inventory(self.make_sources())
        self.assertEqual(report.ocr_page_total, 6)

    def test_unknown_metadata_stays_unknown(self):
        report = build_inventory(self.make_sources(catalogue_db=None))
        self.assertIsNone(report.candidate_work_groups)
        self.assertTrue(any("catalogue" in entry or "work" in entry
                            or "group" in entry
                            for entry in report.unknowns))
        self.assertEqual(report.metadata_coverage["year"], 2)

    def test_rights_unknown(self):
        report = build_inventory(self.make_sources())
        self.assertEqual(report.rights_coverage["with_rights_basis"], 0)
        self.assertEqual(report.rights_coverage["unknown"],
                         report.rights_coverage["rows"])
        self.assertEqual(report.rights_coverage["rows"], 4)


if __name__ == "__main__":
    unittest.main()
