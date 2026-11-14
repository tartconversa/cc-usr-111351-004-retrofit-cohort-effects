"""单元构建与对照资格的回归检查。"""
import json
import unittest
from pathlib import Path
from src.retrofit_cohort_effects_input import load_bundle
from src.retrofit_cohort_effects_design import (
    build_cells,
    normalize_config,
    validate_adoption,
)
from src.retrofit_cohort_effects_vintage import build_snapshot

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def cells_of(name):
    bundle = load_bundle(FIXTURES / name)
    config = normalize_config(bundle["analysis"])
    warnings = []
    adoption = validate_adoption(bundle, config, warnings)
    snapshot = build_snapshot(bundle, config["cutoff"])
    return build_cells(snapshot, adoption, config)


def find_cell(cells, cohort, event_month):
    for cell in cells:
        if cell["cohort"] == cohort and cell["event_month"] == event_month:
            return cell
    raise AssertionError("单元不存在：%s %s" % (cohort, event_month))


class DesignTests(unittest.TestCase):
    def test_anticipation_window_excludes_control(self):
        cells = cells_of("case_invalid_control.json")
        cell = find_cell(cells, "2026-09", 0)
        excluded = {item["park"]: item["reason"] for item in cell["controls_excluded"]}
        self.assertEqual(excluded.get("B"), "in_anticipation")
        self.assertEqual([e["park"] for e in cell["controls"]], ["C"])

    def test_already_treated_never_control(self):
        cells = cells_of("case_invalid_control.json")
        cell = find_cell(cells, "2026-09", 1)
        excluded = {item["park"]: item["reason"] for item in cell["controls_excluded"]}
        self.assertEqual(excluded.get("B"), "already_treated")
        for other in cells:
            for entry in other["controls"]:
                pass
        later = find_cell(cells, "2026-10", 0)
        excluded = {item["park"]: item["reason"] for item in later["controls_excluded"]}
        self.assertEqual(excluded.get("A"), "already_treated")

    def test_last_cohort_has_no_legal_control(self):
        cells = cells_of("case_last_cohort.json")
        cell = find_cell(cells, "2026-12", 0)
        self.assertEqual(cell["status"], "not_identified")
        self.assertIn("no_legal_controls", cell["reasons"])
        reasons = {item["reason"] for item in cell["controls_excluded"]}
        self.assertEqual(reasons, {"already_treated"})
        self.assertIsNone(cell["effect"])

    def test_later_cohort_is_legal_control_for_earlier(self):
        cells = cells_of("case_last_cohort.json")
        cell = find_cell(cells, "2026-08", 2)
        self.assertEqual(cell["status"], "identified")
        self.assertEqual([e["park"] for e in cell["controls"]], ["P3"])
        excluded = {item["park"]: item["reason"] for item in cell["controls_excluded"]}
        self.assertEqual(excluded.get("P2"), "already_treated")

    def test_attrition_marks_missing_current(self):
        cells = cells_of("case_attrition.json")
        cell = find_cell(cells, "2026-09", 2)
        excluded = {item["park"]: item["reason"] for item in cell["treated_excluded"]}
        self.assertEqual(excluded.get("T2"), "missing_current")
        self.assertEqual([e["park"] for e in cell["treated"]], ["T1"])

    def test_base_months_shift_with_anticipation(self):
        cells = cells_of("case_invalid_control.json")
        cell = find_cell(cells, "2026-09", 0)
        self.assertEqual(cell["base_months"], ["2026-07"])
        later = find_cell(cells, "2026-10", 0)
        self.assertEqual(later["base_months"], ["2026-08"])

    def test_missing_base_rejects_treated(self):
        bundle = {
            "schema_version": 1,
            "panel": [
                {"park": "A", "month": "2026-09", "published": "2026-10-05", "revision": 1, "energy": 8},
                {"park": "B", "month": "2026-08", "published": "2026-09-05", "revision": 1, "energy": 20},
                {"park": "B", "month": "2026-09", "published": "2026-10-05", "revision": 1, "energy": 19},
            ],
            "adoption": {"A": "2026-09", "B": None},
            "analysis": {"cutoff": "2026-11-01", "target_weights": {"A": 1}},
        }
        config = normalize_config(bundle["analysis"])
        warnings = []
        adoption = validate_adoption(bundle, config, warnings)
        snapshot = build_snapshot(bundle, config["cutoff"])
        cells = build_cells(snapshot, adoption, config)
        cell = find_cell(cells, "2026-09", 0)
        self.assertEqual(cell["status"], "not_identified")
        self.assertIn("no_legal_treated", cell["reasons"])
        self.assertEqual(cell["treated_excluded"][0]["reason"], "missing_base")

    def test_not_in_target_excluded_from_treated(self):
        bundle = {
            "schema_version": 1,
            "panel": [
                {"park": "A", "month": "2026-08", "published": "2026-09-05", "revision": 1, "energy": 10},
                {"park": "A", "month": "2026-09", "published": "2026-10-05", "revision": 1, "energy": 8},
                {"park": "B", "month": "2026-08", "published": "2026-09-05", "revision": 1, "energy": 20},
                {"park": "B", "month": "2026-09", "published": "2026-10-05", "revision": 1, "energy": 19},
            ],
            "adoption": {"A": "2026-09", "B": "2026-09"},
            "analysis": {"cutoff": "2026-11-01", "target_weights": {"A": 1}},
        }
        config = normalize_config(bundle["analysis"])
        warnings = []
        adoption = validate_adoption(bundle, config, warnings)
        snapshot = build_snapshot(bundle, config["cutoff"])
        cells = build_cells(snapshot, adoption, config)
        cell = find_cell(cells, "2026-09", 0)
        self.assertEqual([e["park"] for e in cell["treated"]], ["A"])
        self.assertEqual(cell["treated_excluded"][0]["reason"], "not_in_target")
        excluded = {item["park"]: item["reason"] for item in cell["controls_excluded"]}
        # B是同队列处理园区，按已改造处理，绝不进入对照侧
        self.assertEqual(excluded.get("B"), "already_treated")
        self.assertEqual(cell["status"], "not_identified")
        self.assertIn("no_legal_controls", cell["reasons"])

    def test_adoption_must_cover_all_parks(self):
        bundle = {
            "schema_version": 1,
            "panel": [
                {"park": "A", "month": "2026-08", "published": "2026-09-05", "revision": 1, "energy": 10},
            ],
            "adoption": {"B": None},
            "analysis": {"cutoff": "2026-11-01", "target_weights": {"B": 1}},
        }
        config = normalize_config(bundle["analysis"])
        with self.assertRaises(ValueError):
            validate_adoption(bundle, config, [])

    def test_config_rejects_outcome_covariate_without_note(self):
        analysis = {
            "cutoff": "2026-11-01",
            "target_weights": {"A": 1},
            "estimator": "did_ipw",
            "covariates": ["output"],
        }
        with self.assertRaises(ValueError):
            normalize_config(analysis)
        analysis["allow_outcome_adjustment"] = True
        with self.assertRaises(ValueError):
            normalize_config(analysis)
        analysis["adjustment_note"] = "使用改造前一年平均产量作为协变量"
        config = normalize_config(analysis)
        self.assertEqual(config["adjustment_note"], "使用改造前一年平均产量作为协变量")

    def test_config_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            normalize_config({"cutoff": "2026-11-01", "target_weights": {"A": 1}, "typo": 1})


if __name__ == "__main__":
    unittest.main()
