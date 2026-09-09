"""Offline checks for immutable candidate banks and proposals (S4)."""

import ast
import sys
import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research import candidates, schema

HEX_A = "a" * 64
HEX_B = "b" * 64


def _record(candidate_id, page_id=HEX_A, start=0, end=1,
            before="ক", after="খ"):
    return {
        "candidate_id": candidate_id,
        "page_id": page_id,
        "base": "ocr",
        "start": start,
        "end": end,
        "before": before,
        "after": after,
        "proposer_id": "disagree-v0",
        "evidence_refs": [],
        "proposal_bank_hash": "seed-hash",
    }


def _two_records():
    return [
        _record("cand-2", page_id=HEX_B, start=0, end=1,
                before="গ", after="ঘ"),
        _record("cand-1", page_id=HEX_A, start=2, end=3,
                before="ক", after="খ"),
    ]


class CandidateBankTests(unittest.TestCase):
    def test_bank_hash_stable(self):
        records = _two_records()
        first = candidates.CandidateBank.build("disagree-v0", records)
        second = candidates.CandidateBank.build(
            "disagree-v0", list(reversed(records)))
        self.assertEqual(first.bank_hash, second.bank_hash)
        self.assertEqual(first.bank_id, second.bank_id)
        self.assertEqual(
            [r["candidate_id"] for r in first.candidates],
            [r["candidate_id"] for r in second.candidates])

    def test_bank_immutable_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.json"
            bank = candidates.CandidateBank.build("disagree-v0", _two_records())
            bank.write(path)
            # Identical rewrite succeeds.
            candidates.CandidateBank.load(path).write(path)
            other = candidates.CandidateBank.build(
                "disagree-v0",
                [_record("cand-9", start=0, end=1, before="চ", after="ছ")])
            with self.assertRaises(schema.ContractError):
                other.write(path)

    def test_gold_field_in_candidate_rejected(self):
        bad = dict(_record("cand-bad"), gold_text="must not enter")
        with self.assertRaises(schema.ContractError):
            schema.Candidate.from_dict(bad)
        with self.assertRaises(schema.ContractError):
            candidates.CandidateBank.build("disagree-v0", [bad])

    def test_for_page_filters(self):
        bank = candidates.CandidateBank.build("disagree-v0", _two_records())
        only_a = bank.for_page(HEX_A)
        self.assertEqual(len(only_a), 1)
        self.assertTrue(all(r["page_id"] == HEX_A for r in only_a))
        self.assertEqual(len(bank.for_page(HEX_B)), 1)

    def test_all_gate_arms_cite_same_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.json"
            candidates.CandidateBank.build(
                "disagree-v0", _two_records()).write(path)
            arm_a = candidates.CandidateBank.load(path)
            arm_b = candidates.CandidateBank.load(path)
            self.assertEqual(arm_a.stamp(), arm_b.stamp())
            self.assertEqual(arm_a.stamp(), arm_a.bank_hash)


class ProposalTests(unittest.TestCase):
    def test_propose_from_disagreement_deterministic(self):
        kwargs = dict(
            page_id=HEX_A, base_field="ocr",
            ocr_text="আমি বই পড়ি", alt_text="আমি খই পড়ি",
            proposer_id="disagree-v0", bank_seed="seed-1")
        first = candidates.propose_from_disagreement(**kwargs)
        second = candidates.propose_from_disagreement(**kwargs)
        self.assertEqual(first, second)
        self.assertTrue(first)
        for record in first:
            schema.Candidate.from_dict(record)  # validates; raises on gold
            schema.assert_no_gold_fields(record, context="proposal")
            self.assertNotIn("gold_text", record)

    def test_propose_identical_texts_empty(self):
        result = candidates.propose_from_disagreement(
            page_id=HEX_A, base_field="ocr",
            ocr_text="আমি বই পড়ি", alt_text="আমি বই পড়ি",
            proposer_id="disagree-v0", bank_seed="seed-1")
        self.assertEqual(result, [])

    def test_propose_no_model_used(self):
        forbidden = ("torch", "transformers", "ollama", "requests", "PIL")
        for name in forbidden:
            self.assertNotIn(name, sys.modules)
        repo_root = Path(__file__).resolve().parents[2]
        for module in ("adapters.py", "runners.py", "candidates.py"):
            source = (
                repo_root / "pdf_craft_tool" / "research" / module
            ).read_text(encoding="utf-8")
            tree = ast.parse(source)
            top_imports = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    top_imports.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        top_imports.add(node.module.split(".")[0])
            for name in forbidden:
                self.assertNotIn(name, top_imports, module)
        candidates.propose_from_disagreement(
            page_id=HEX_A, base_field="ocr",
            ocr_text="আমি বই পড়ি", alt_text="আমি খই পড়ি",
            proposer_id="disagree-v0", bank_seed="seed-1")
        for name in forbidden:
            self.assertNotIn(name, sys.modules)


if __name__ == "__main__":
    unittest.main()
