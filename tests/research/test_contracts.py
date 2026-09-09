"""Contract tests for the S0 research record schema (unittest, stdlib only)."""

import copy
import tempfile
import unittest
from pathlib import Path

from pdf_craft_tool.research.config import StudyConfig
from pdf_craft_tool.research.schema import (
    Candidate,
    ContractError,
    Decision,
    Evaluation,
    Geometry,
    GoldLine,
    GoldPage,
    Manifest,
    Prediction,
    SamplePage,
    SourcePage,
    record_hash,
    software_baseline,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_CONFIG = REPO_ROOT / "research" / "configs" / "pilot.json"

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def make_geometry():
    return Geometry(x0=0, y0=0, x1=10, y1=20)


def make_source_page():
    return SourcePage(
        source_sha256=HASH_A,
        work_id="w1",
        edition_id="e1",
        overlap_group="",
        page_number=1,
        page_number_convention="pdf_index",
        width=100,
        height=200,
        image_sha256=HASH_B,
        original_location="/loc/p1",
        rights_basis="public_domain",
        rights_evidence="",
    )


def make_sample_page():
    return SamplePage(
        page_id=HASH_C,
        source_sha256=HASH_A,
        work_id="w1",
        edition_id="e1",
        overlap_group="",
        split="pilot",
        stratum="s1",
        selection_probability=0.5,
        seed=1,
        manifest_version=1,
        schema_version=1,
        file_hashes={"a.png": HASH_D},
        legacy_gold_ids=(),
    )


def make_gold_page(**overrides):
    lines = (
        GoldLine(line_index=0, geometry=make_geometry(), text="line one",
                 unreadable=False),
        GoldLine(line_index=1, geometry=make_geometry(), text="line two",
                 unreadable=False),
    )
    kwargs = {
        "page_id": HASH_C,
        "source_sha256": HASH_A,
        "image_sha256": HASH_B,
        "lines": lines,
        "reading_order": (0, 1),
        "annotator_ids": ("ann-a", "ann-b"),
        "adjudicator_id": "judge-1",
        "status": "final",
        "guideline_version": "v1",
        "disagreement_rate": 0.1,
    }
    kwargs.update(overrides)
    return GoldPage(**kwargs)


def make_prediction():
    return Prediction(
        page_id=HASH_C,
        system_id="sys-1",
        config_hash="cfg-1",
        model_id="model-1",
        prompt_hash="prompt-1",
        crop_hash="crop-1",
        raw_output="",
        parsed_text="text",
        failure_state="ok",
        timing_ms=5,
        resource={"pages": 1},
    )


def make_candidate():
    return Candidate(
        candidate_id="cand-1",
        page_id=HASH_C,
        base="prediction.parsed_text",
        start=0,
        end=1,
        before="a",
        after="b",
        proposer_id="prop-1",
        proposal_bank_hash="bank-1",
    )


def make_decision(**overrides):
    kwargs = {
        "candidate_id": "cand-1",
        "gate_id": "gate-1",
        "gate_version": "v1",
        "threshold": 0.5,
        "action": "accept",
        "reason": "looks good",
        "original_retained": False,
    }
    kwargs.update(overrides)
    return Decision(**kwargs)


def make_evaluation():
    return Evaluation(
        prediction_hash=HASH_A,
        gold_hash=HASH_B,
        manifest_hash=HASH_C,
        metric_version="m1",
        tokenization_version="t1",
        unicode_version="u1",
        denominators={"chars": 5},
        per_family={"fam1": {"cer": 0.1}},
        statistical_protocol={"method": "paired_bootstrap"},
    )


class ContractTests(unittest.TestCase):
    def test_roundtrip_each_record(self):
        fixtures = [
            make_geometry(), make_source_page(), make_sample_page(),
            GoldLine(line_index=0, geometry=make_geometry(), text="t",
                     unreadable=False),
            make_gold_page(), make_prediction(), make_candidate(),
            make_decision(), make_evaluation(),
        ]
        for fixture in fixtures:
            with self.subTest(record=type(fixture).__name__):
                self.assertEqual(
                    type(fixture).from_dict(fixture.to_dict()), fixture)

    def test_reject_unknown_split(self):
        data = make_sample_page().to_dict()
        data["split"] = "holdout"
        with self.assertRaises(ContractError):
            SamplePage.from_dict(data)

    def test_reject_unknown_gold_status(self):
        data = make_gold_page().to_dict()
        data["status"] = "blessed"
        with self.assertRaises(ContractError):
            GoldPage.from_dict(data)

    def test_reject_missing_or_bad_hashes(self):
        data = make_source_page().to_dict()
        data["source_sha256"] = "abc"
        with self.assertRaises(ContractError):
            SourcePage.from_dict(data)
        eval_data = make_evaluation().to_dict()
        eval_data["gold_hash"] = "a" * 63
        with self.assertRaises(ContractError):
            Evaluation.from_dict(eval_data)

    def test_reject_invalid_geometry(self):
        with self.assertRaises(ContractError):
            Geometry(x0=5, y0=0, x1=3, y1=10)
        with self.assertRaises(ContractError):
            Geometry(x0=-1, y0=0, x1=3, y1=10)

    def test_reject_gold_fields_in_inference(self):
        pred_data = make_prediction().to_dict()
        pred_data["verified_text"] = "x"
        with self.assertRaises(ContractError):
            Prediction.from_dict(pred_data)
        cand_data = make_candidate().to_dict()
        cand_data["gold_text"] = "x"
        with self.assertRaises(ContractError):
            Candidate.from_dict(cand_data)

    def test_canonical_hash_key_order_independent(self):
        first = {"a": 1, "b": {"x": 1, "y": 2}}
        second = {"b": {"y": 2, "x": 1}, "a": 1}
        self.assertEqual(record_hash(first), record_hash(second))

    def test_identical_records_identical_hash(self):
        self.assertEqual(record_hash(make_gold_page().to_dict()),
                         record_hash(make_gold_page().to_dict()))

    def test_manifest_immutable(self):
        first = Manifest.build("gold", [make_gold_page().to_dict()])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.json"
            first.write(path)
            other_page = make_gold_page(disagreement_rate=0.2).to_dict()
            second = Manifest.build("gold", [other_page])
            self.assertNotEqual(first.manifest_hash, second.manifest_hash)
            with self.assertRaises(ContractError):
                second.write(path)
            first.write(path)

    def test_manifest_roundtrip_and_baseline(self):
        manifest = Manifest.build("gold", [make_gold_page().to_dict()])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.json"
            manifest.write(path)
            self.assertEqual(Manifest.load(path), manifest)
        baseline = software_baseline()
        self.assertIsInstance(baseline["git_dirty"], bool)
        self.assertIn("python_version", baseline)

    def test_decision_original_retained_rule(self):
        with self.assertRaises(ContractError):
            make_decision(action="abstain", original_retained=False)
        decision = make_decision(action="accept", original_retained=False)
        self.assertFalse(decision.original_retained)

    def test_gold_final_requires_two_annotators(self):
        with self.assertRaises(ContractError):
            make_gold_page(annotator_ids=("a",))
        page = make_gold_page(annotator_ids=("a", "b"), adjudicator_id="j")
        self.assertTrue(page.is_final_export_eligible())

    def test_config_loads_pilot(self):
        config = StudyConfig.load(PILOT_CONFIG)
        self.assertTrue(config.draft is True)
        self.assertEqual(config.primary_endpoint["contrast"], "B5_minus_B0")
        self.assertIn("B5", config.baseline_candidates)

    def test_config_rejects_bad_endpoint_direction(self):
        config = StudyConfig.load(PILOT_CONFIG)
        data = copy.deepcopy(config.to_dict())
        data["primary_endpoint"]["direction"] = "minimise"
        with self.assertRaises(ContractError):
            StudyConfig(
                study_id=data["study_id"],
                schema_version=data["schema_version"],
                draft=data["draft"],
                primary_endpoint=data["primary_endpoint"],
                baseline_candidates=tuple(data["baseline_candidates"]),
                sampling=data["sampling"],
                split_allocation=data["split_allocation"],
                uncertainty=data["uncertainty"],
                calibration_screen=data["calibration_screen"],
            )


if __name__ == "__main__":
    unittest.main()
