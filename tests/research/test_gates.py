"""S5 gate behaviour — every arm on one frozen candidate bank."""

import unittest

from pdf_craft_tool.research import gates, schema
from pdf_craft_tool.research.candidates import CandidateBank

_PAGE = "a" * 64


def _cand(cid, start, end, before, after):
    return schema.Candidate(
        candidate_id=cid,
        page_id=_PAGE,
        base="ocr",
        start=start,
        end=end,
        before=before,
        after=after,
        proposer_id="proposer-1",
        proposal_bank_hash="bank-seed-hash",
    ).to_dict()


def _bank(*records):
    return CandidateBank.build("proposer-1", list(records))


class GateBehaviour(unittest.TestCase):
    def test_unchanged_gate_touches_nothing(self):
        base = "the quick brown fox"
        bank = _bank(_cand("c1", 4, 9, "quick", "quikc"))
        gate = gates.UnchangedGate().bind(bank)
        decisions = gate.decide_bank(bank, evidence_by_candidate={})
        self.assertTrue(all(d.action == "abstain" for d in decisions))
        self.assertTrue(all(d.original_retained for d in decisions))
        result = gates.apply_decisions(base, bank.candidates, decisions)
        self.assertEqual(result["text"], base)
        self.assertTrue(result["unchanged"])

    def test_missing_evidence_abstains(self):
        bank = _bank(_cand("c1", 0, 3, "abc", "abd"))
        gate = gates.ExistingPolicyGate().bind(bank)
        decision = gate.decide(bank.candidates[0], evidence={"edit_size": 1})
        self.assertEqual(decision.action, "abstain")
        self.assertIn("verifier_pass", decision.reason)

    def test_overlapping_edits_deterministic(self):
        bank = _bank(
            _cand("c1", 0, 5, "abcde", "abcdX"),
            _cand("c2", 3, 8, "defgh", "DEFGH"),
        )
        gate = gates.EditSizeGate(max_edit_size=9).bind(bank)
        ev = {"c1": {"edit_size": 1}, "c2": {"edit_size": 3}}
        d1 = gate.decide_bank(bank, evidence_by_candidate=ev)
        # reverse the candidate order in a fresh bank -> same outcome
        bank2 = _bank(
            _cand("c2", 3, 8, "defgh", "DEFGH"),
            _cand("c1", 0, 5, "abcde", "abcdX"),
        )
        g2 = gates.EditSizeGate(max_edit_size=9).bind(bank2)
        d2 = {d.candidate_id: d.action for d in g2.decide_bank(bank2, evidence_by_candidate=ev)}
        first = {d.candidate_id: d.action for d in d1}
        self.assertEqual(first, d2)
        self.assertEqual(first["c1"], "accept")
        self.assertEqual(first["c2"], "reject")
        self.assertIn("overlaps", next(d.reason for d in d1 if d.candidate_id == "c2"))

    def test_all_gates_cite_bank_hash(self):
        bank = _bank(_cand("c1", 0, 3, "abc", "abd"))
        for name, cls in gates.GATES.items():
            gate = cls().bind(bank) if name != "edit_size" else cls().bind(bank)
            self.assertEqual(gate.bank_hash(), bank.stamp())

    def test_gate_cannot_read_gold(self):
        bank = _bank(_cand("c1", 0, 3, "abc", "abd"))
        gate = gates.EditSizeGate().bind(bank)
        with self.assertRaises(schema.ContractError):
            gate.decide(bank.candidates[0],
                        evidence={"edit_size": 1, "verified_text": "abc"})

    def test_reject_all_is_not_improvement(self):
        # A gate that rejects every candidate accepts nothing: coverage 0,
        # bytes unchanged -- must be distinguishable from a real error reduction.
        base = "abcxyz"
        bank = _bank(_cand("c1", 0, 3, "abc", "abd"))
        gate = gates.EditSizeGate(max_edit_size=0).bind(bank)
        decisions = gate.decide_bank(bank, evidence_by_candidate={"c1": {"edit_size": 3}})
        result = gates.apply_decisions(base, bank.candidates, decisions)
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["text"], base)  # no error was actually corrected

    def test_accept_applies_edit(self):
        base = "abcxyz"
        bank = _bank(_cand("c1", 0, 3, "abc", "ABC"))
        gate = gates.EditSizeGate(max_edit_size=3).bind(bank)
        decisions = gate.decide_bank(bank, evidence_by_candidate={"c1": {"edit_size": 3}})
        result = gates.apply_decisions(base, bank.candidates, decisions)
        self.assertEqual(result["text"], "ABCxyz")
        self.assertEqual(result["applied"], 1)


if __name__ == "__main__":
    unittest.main()
