"""Tests for research/refresh_inventory.py (S1 refresh + pilot dup check)."""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "refresh_inventory",
    Path(__file__).resolve().parents[2] / "research" / "refresh_inventory.py")
refresh_inventory = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(refresh_inventory)

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


def _catalogue(path: Path, families) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE catalogue_local_documents (id INTEGER PRIMARY KEY,
            sha256 TEXT, source_path TEXT);
        CREATE TABLE catalogue_duplicate_clusters (id INTEGER PRIMARY KEY,
            relation TEXT, size INTEGER);
        CREATE TABLE catalogue_duplicate_members (cluster_id INTEGER,
            document_id INTEGER, is_keeper INTEGER);
    """)
    for doc_id, sha, cluster, is_keeper in families:
        conn.execute("INSERT INTO catalogue_local_documents VALUES(?,?,?)",
                     (doc_id, sha, f"data/book-{doc_id}.pdf"))
        if cluster is not None:
            conn.execute(
                "INSERT OR IGNORE INTO catalogue_duplicate_clusters "
                "VALUES(?,?,?)", (cluster, "same_work", 2))
            conn.execute(
                "INSERT INTO catalogue_duplicate_members VALUES(?,?,?)",
                (cluster, doc_id, is_keeper))
    conn.commit()
    conn.close()


def _provenance(path: Path, shas) -> None:
    path.write_text(json.dumps({
        "families": [
            {"job_id": f"job{index}" + "0" * 60, "source_sha256": sha,
             "author_dir": f"author-{index}", "title": f"title-{index}"}
            for index, sha in enumerate(shas)
        ]}), encoding="utf-8")


class PilotDuplicateCheck(unittest.TestCase):
    def test_two_pilot_families_in_one_cluster_are_grouped(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cat = tmp / "catalogue.db"
            # A and B share cluster 7; C is alone.
            _catalogue(cat, [
                (1, HEX_A, 7, 1),
                (2, HEX_B, 7, 0),
                (3, HEX_C, None, 1),
                (99, "d" * 64, 7, 1),  # non-pilot sibling in the cluster
            ])
            prov = tmp / "pilot_provenance.json"
            _provenance(prov, [HEX_A, HEX_B, HEX_C])

            result = refresh_inventory.pilot_family_duplicate_check(
                str(cat), prov)

            self.assertEqual(result["summary"]["pilot_families"], 3)
            self.assertEqual(result["summary"]["in_a_duplicate_cluster"], 2)
            self.assertEqual(len(result["summary"]["marked_non_keeper"]), 1)
            pairs = result["summary"]["pilot_family_pairs_to_confirm"]
            self.assertEqual(pairs, [sorted(["job0" + "0" * 12,
                                             "job1" + "0" * 12])])
            # group_families keeps them separate but flags the pair.
            flagged = [g for g in result["grouped"] if g["needs_confirmation"]]
            self.assertTrue(flagged)

    def test_no_catalogue_still_returns_families(self):
        with tempfile.TemporaryDirectory() as d:
            prov = Path(d) / "p.json"
            _provenance(prov, [HEX_A, HEX_B])
            result = refresh_inventory.pilot_family_duplicate_check(None, prov)
            self.assertEqual(result["summary"]["pilot_families"], 2)
            self.assertEqual(result["summary"]["in_a_duplicate_cluster"], 0)


class MainEntryPoint(unittest.TestCase):
    def test_writes_report_and_groups(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            queue = tmp / "queue.sqlite3"
            conn = sqlite3.connect(queue)
            conn.executescript("""
                CREATE TABLE sources (sha256 TEXT, path TEXT, present INTEGER);
                CREATE TABLE jobs (id TEXT, state TEXT, profile TEXT);
                INSERT INTO jobs VALUES ('j1','done','p'),('j2','pending','p');
            """)
            conn.commit()
            conn.close()
            cat = tmp / "catalogue.db"
            _catalogue(cat, [(1, HEX_A, None, 1)])
            prov = tmp / "pilot_provenance.json"
            _provenance(prov, [HEX_A])
            config = tmp / "inventory.json"
            config.write_text(json.dumps({
                "queue_db": str(queue), "catalogue_db": str(cat),
            }), encoding="utf-8")
            out = tmp / "out"

            # _resolve() joins relative paths to the repo; here the config
            # already holds absolute paths, so patch only the catalogue path
            # the check hard-codes.
            original = refresh_inventory._resolve
            refresh_inventory._resolve = lambda value: (
                str(cat) if value == "pdf-craft-output/catalogue/catalogue.db"
                else original(value))
            try:
                code = refresh_inventory.main([
                    "--config", str(config), "--out-dir", str(out),
                    "--provenance", str(prov)])
            finally:
                refresh_inventory._resolve = original

            self.assertEqual(code, 0)
            self.assertTrue((out / "inventory-report.json").is_file())
            self.assertTrue((out / "pilot-family-groups.json").is_file())
            report = json.loads(
                (out / "inventory-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["job_state_counts"]["done"], 1)


if __name__ == "__main__":
    unittest.main()
