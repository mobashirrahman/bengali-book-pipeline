"""S5 calibration — leakage discipline, determinism, the event screen."""

import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research import calibration, schema
from pdf_craft_tool.research.calibration import (
    CalibrationScreen,
    GateClassifier,
    LabelledCandidate,
)


def _item(cid, family, split, benef, size, conf):
    return LabelledCandidate(
        candidate_id=cid,
        family_id=family,
        split=split,
        features={"edit_size": float(size), "ocr_confidence": float(conf)},
        outcome="beneficial" if benef else "harmful",
    )


def _train_set(n=40):
    items = []
    for i in range(n):
        benef = i % 2 == 0
        # beneficial candidates: small edits, low confidence; harmful: large, high
        items.append(_item(f"t{i}", f"fam{i % 5}", "train", benef,
                           1 if benef else 6, 40 if benef else 95))
    return items


class LeakageDiscipline(unittest.TestCase):
    def test_test_split_never_fits(self):
        items = _train_set() + [_item("x", "famT", "test", True, 1, 30)]
        with self.assertRaises(schema.ContractError):
            GateClassifier().fit(items)
        clf = GateClassifier().fit(_train_set())
        calib = [_item("c", "famC", "calibration", True, 1, 30),
                 _item("x", "famT", "test", False, 6, 95)]
        with self.assertRaises(schema.ContractError):
            clf.choose_threshold(calib)

    def test_threshold_uses_calibration_only(self):
        clf = GateClassifier().fit(_train_set())
        mixed = [_item("c", "famC", "calibration", True, 1, 30),
                 _item("t", "famX", "train", False, 6, 95)]
        with self.assertRaises(schema.ContractError):
            clf.choose_threshold(mixed)

    def test_fit_is_deterministic(self):
        a = GateClassifier().fit(_train_set(), l2=1.0, iters=200)
        b = GateClassifier().fit(_train_set(), l2=1.0, iters=200)
        self.assertEqual(a.weights, b.weights)
        self.assertEqual(a.bias, b.bias)

    def test_fit_needs_training_items(self):
        calib_only = [_item("c", "f", "calibration", True, 1, 30)]
        with self.assertRaises(schema.ContractError):
            GateClassifier().fit(calib_only)


class Screen(unittest.TestCase):
    def _calib(self, beneficial, harmful, families):
        items = []
        fam_cycle = [f"fam{i}" for i in range(families)]
        for i in range(beneficial):
            items.append(_item(f"b{i}", fam_cycle[i % families], "calibration",
                               True, 1, 30))
        for i in range(harmful):
            items.append(_item(f"h{i}", fam_cycle[i % families], "calibration",
                               False, 6, 95))
        return items

    def test_screen_blocks_on_insufficient_support(self):
        result = CalibrationScreen().evaluate(self._calib(3, 2, 4))
        self.assertFalse(result["supported"])
        self.assertEqual(result["fallback"], "rule_based_policy")

    def test_screen_passes_with_enough(self):
        result = CalibrationScreen().evaluate(self._calib(120, 120, 12))
        self.assertTrue(result["supported"])
        self.assertGreaterEqual(result["families"], 10)


class FrozenPolicy(unittest.TestCase):
    def test_frozen_policy_is_immutable(self):
        clf = GateClassifier().fit(_train_set())
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "policy.json"
            calibration.freeze_policy(clf, 0.5, "bankhash", path)
            # identical rewrite ok
            calibration.freeze_policy(clf, 0.5, "bankhash", path)
            # different threshold -> refused
            with self.assertRaises(schema.ContractError):
                calibration.freeze_policy(clf, 0.7, "bankhash", path)

    def test_policy_roundtrip(self):
        clf = GateClassifier().fit(_train_set())
        restored = GateClassifier.from_dict(clf.to_dict())
        features = {"edit_size": 1.0, "ocr_confidence": 30.0}
        self.assertAlmostEqual(clf.predict_proba(features),
                               restored.predict_proba(features), places=12)

    def test_predict_proba_rejects_missing_feature(self):
        clf = GateClassifier().fit(_train_set())
        with self.assertRaises(schema.ContractError):
            clf.predict_proba({"edit_size": 1.0})


if __name__ == "__main__":
    unittest.main()
