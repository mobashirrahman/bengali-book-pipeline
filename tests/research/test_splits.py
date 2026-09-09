"""Tests for S1 grouping, splitting and sampling (synthetic only)."""

import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research import schema
from pdf_craft_tool.research.splits import (
    Document,
    Family,
    GroupEdge,
    assign_splits,
    challenge_sample,
    group_families,
    probability_sample,
    write_sample_manifest,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def doc(doc_id, digest, processed=False):
    return Document(document_id=doc_id, content_sha256=digest,
                    processed=processed)


def fam(family_id, members, hashes, processed=()):
    return Family(family_id=family_id, members=tuple(members),
                  content_hashes=tuple(hashes),
                  processed_members=tuple(processed),
                  needs_confirmation=())


class GroupingTests(unittest.TestCase):
    def test_byte_identical_grouped(self):
        docs = [doc("d1", SHA_A), doc("d2", SHA_A), doc("d3", SHA_B)]
        edges = [GroupEdge(a="d1", b="d2", relation="byte_identical",
                           method="sha", score=0.0, confirmed=False)]
        families = group_families(docs, edges)
        self.assertEqual(len(families), 2)
        by_member = {}
        for family in families:
            for member in family.members:
                by_member[member] = family.family_id
        self.assertEqual(by_member["d1"], by_member["d2"])
        self.assertNotEqual(by_member["d1"], by_member["d3"])

    def test_alternate_scan_and_anthology_overlap(self):
        docs = [doc("d1", SHA_A), doc("d2", SHA_B),
                doc("d3", SHA_C), doc("d4", SHA_D)]
        edges = [GroupEdge(a="d1", b="d2", relation="same_work",
                           method="title", score=0.7, confirmed=True),
                 GroupEdge(a="d2", b="d3", relation="anthology_overlap",
                           method="toc", score=0.75, confirmed=True)]
        families = group_families(docs, edges)
        self.assertEqual(len(families), 2)
        sizes = sorted(len(family.members) for family in families)
        self.assertEqual(sizes, [1, 3])

    def test_unconfirmed_low_score_is_advisory(self):
        docs = [doc("d1", SHA_A), doc("d2", SHA_B)]
        edges = [GroupEdge(a="d1", b="d2", relation="same_work",
                           method="fuzzy", score=0.5, confirmed=False)]
        families = group_families(docs, edges)
        self.assertEqual(len(families), 2)
        for family in families:
            self.assertIn(("d1", "d2"), family.needs_confirmation)


class SplitSamplingTests(unittest.TestCase):
    def setUp(self):
        self.families = [
            fam("fam-a", ("m1",), (SHA_A,), ("m1",)),
            fam("fam-b", ("m2",), (SHA_B,), ("m2",)),
            fam("fam-c", ("m3",), (SHA_C,), ("m3",)),
            fam("fam-d", ("m4",), (SHA_D,), ("m4",)),
            fam("fam-e", ("m5",), (SHA_E,), ("m5",)),
            fam("fam-f", ("m6",), (SHA_F,), ("m6",)),
        ]
        self.pages = {"m1": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                      "m2": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                      "m3": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                      "m4": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                      "m5": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                      "m6": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}
        self.split_of = {fam.family_id: "pilot" for fam in self.families}
        self.stratum_of = {fam.family_id: "s1" for fam in self.families}

    def sample(self, seed=7, families=None):
        return probability_sample(
            families=self.families if families is None else families,
            split_of=self.split_of, pages_available=self.pages,
            pages_per_family=2, seed=seed, stratum_of=self.stratum_of)

    def test_assign_splits_order_independent(self):
        allocation = {"calibration": 1, "test": 2, "train": 2}
        first = assign_splits(list(self.families), allocation, seed=11)
        second = assign_splits(list(reversed(self.families)), allocation,
                               seed=11)
        third = assign_splits(self.families[2:] + self.families[:2],
                              allocation, seed=11)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        counts = {}
        for split in first.values():
            counts[split] = counts.get(split, 0) + 1
        self.assertEqual(counts.get("train"), 2)
        self.assertEqual(counts.get("test"), 2)
        self.assertEqual(counts.get("calibration"), 1)
        self.assertEqual(counts.get("unassigned"), 1)

    def test_related_never_cross_split(self):
        mapping = assign_splits(self.families, {"pilot": 6}, seed=3)
        by_doc = {}
        for family in self.families:
            for member in family.members:
                by_doc.setdefault(member, set()).add(
                    mapping[family.family_id])
        for member, splits in by_doc.items():
            self.assertEqual(len(splits), 1, member)

    def test_unprocessed_book_still_sampled(self):
        lonely = fam("fam-u", ("mu",), (SHA_A,))
        split_of = {"fam-u": "pilot"}
        result = probability_sample(
            families=[lonely], split_of=split_of, pages_available={},
            pages_per_family=3, seed=5, stratum_of={})
        self.assertEqual(len(result.pages), 3)
        for page in result.pages:
            self.assertTrue(page["stratum"].endswith("+unprocessed"))

    def test_manifest_frozen(self):
        result = self.sample(seed=7)
        other = self.sample(seed=8)
        self.assertNotEqual(result.manifest.manifest_hash,
                            other.manifest.manifest_hash)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample-manifest.json"
            write_sample_manifest(result, path)
            with self.assertRaises(schema.ContractError):
                write_sample_manifest(other, path)
            write_sample_manifest(result, path)

    def test_challenge_disjoint(self):
        result = self.sample()
        prob_ids = {page["page_id"] for page in result.pages}
        overlap = dict(result.pages[0])
        novel = schema.SamplePage(
            page_id="0" * 64, source_sha256=SHA_E, work_id="unknown",
            edition_id="unknown", overlap_group="fam-x", split="pilot",
            stratum="s1", selection_probability=1.0, seed=9,
            manifest_version=schema.MANIFEST_VERSION,
            schema_version=schema.SCHEMA_VERSION, file_hashes={},
            legacy_gold_ids=()).to_dict()
        challenge = challenge_sample(
            candidates=[overlap, novel], exclude_page_ids=prob_ids, seed=1)
        challenge_ids = {page["page_id"] for page in challenge}
        self.assertFalse(challenge_ids & prob_ids)
        self.assertIn("0" * 64, challenge_ids)
        for page in challenge:
            self.assertEqual(page["split"], "challenge")

    def test_selection_probability_recorded(self):
        result = self.sample()
        self.assertTrue(result.pages)
        for page in result.pages:
            self.assertGreater(page["selection_probability"], 0)
            self.assertLessEqual(page["selection_probability"], 1)


if __name__ == "__main__":
    unittest.main()
