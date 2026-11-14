"""整次分析的回归检查：worked example、汇总口径、确定性与区间。"""
import json
import unittest
from pathlib import Path
from src.retrofit_cohort_effects_input import load_bundle
from src.retrofit_cohort_effects_analysis import run_analysis

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def analyze(name, **kwargs):
    return run_analysis(load_bundle(FIXTURES / name), **kwargs)


def coverage_point(report, event_month):
    for point in report["event_study"]["coverage"]:
        if point["event_month"] == event_month:
            return point
    return None


class AnalysisTests(unittest.TestCase):
    def test_worked_example_did_is_minus_one(self):
        report = analyze("sample.json")
        cell = report["cells"][0]
        self.assertEqual(cell["effect"], -1.0)
        self.assertEqual(cell["treated_term"], -2.0)
        self.assertEqual(cell["control_term"], -1.0)
        point = coverage_point(report, 0)
        self.assertEqual(point["effect"], -1.0)
        self.assertEqual(point["coverage"], 1.0)

    def test_invalid_control_case(self):
        report = analyze("case_invalid_control.json")
        self.assertEqual(report["event_grid"]["2026-09"]["0"], -2.0)
        self.assertEqual(report["event_grid"]["2026-09"]["1"], -3.0)
        self.assertEqual(report["event_grid"]["2026-10"]["0"], -5.0)
        self.assertEqual(coverage_point(report, 0)["effect"], -3.5)
        self.assertEqual(coverage_point(report, 1)["coverage"], 0.5)
        balanced = report["event_study"]["balanced"]
        self.assertEqual(balanced["parks"], ["A"])
        self.assertEqual([p["effect"] for p in balanced["series"]], [-2.0, -3.0])

    def test_last_cohort_coverage_shrinks_to_zero(self):
        report = analyze("case_last_cohort.json")
        self.assertEqual(coverage_point(report, 0)["effect"], -4.5)
        self.assertAlmostEqual(coverage_point(report, 0)["coverage"], 2.0 / 3.0)
        self.assertAlmostEqual(coverage_point(report, 3)["coverage"], 1.0 / 3.0)
        self.assertIsNone(coverage_point(report, 4))
        self.assertIsNone(report["event_grid"]["2026-12"]["0"])
        balanced = report["event_study"]["balanced"]
        self.assertEqual(balanced["parks"], ["P1"])
        self.assertEqual([p["effect"] for p in balanced["series"]], [-5.0] * 4)

    def test_attrition_composition_vs_same_composition(self):
        report = analyze("case_attrition.json")
        effects = [coverage_point(report, e)["effect"] for e in range(4)]
        self.assertEqual(effects, [-2.5, -2.5, -1.0, -1.0])
        balanced = report["event_study"]["balanced"]
        self.assertEqual(balanced["parks"], ["T1"])
        self.assertEqual([p["effect"] for p in balanced["series"]], [-1.0] * 4)

    def test_staggered_publication_single_cutoff(self):
        report = analyze("case_staggered_publication.json")
        self.assertEqual(report["event_grid"]["2026-10"]["0"], -1.0)
        self.assertEqual(report["event_grid"]["2026-10"]["1"], -3.0)
        revisions = report["data_trace"]["revisions"]
        chosen = {(r["park"], r["month"]): r["chosen"]["energy"] for r in revisions}
        self.assertEqual(chosen[("T", "2026-09")], 29)
        self.assertEqual(chosen[("T", "2026-11")], 25)
        unpublished = report["data_trace"]["excluded_unpublished"]
        self.assertIn({"park": "C2", "month": "2026-11", "revision": 1,
                       "published": "2026-12-20"}, unpublished)

    def test_determinism_same_cutoff_same_result(self):
        first = analyze("case_attrition.json")
        second = analyze("case_attrition.json")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_fingerprint_changes_with_cutoff(self):
        early = analyze("case_staggered_publication.json", cutoff="2026-11-30")
        late = analyze("case_staggered_publication.json", cutoff="2026-12-10")
        self.assertNotEqual(early["fingerprint"], late["fingerprint"])
        self.assertEqual(early["event_grid"]["2026-10"]["0"], -1.0)
        self.assertIsNone(early["event_grid"]["2026-10"]["1"])

    def test_bootstrap_is_park_level_and_seeded(self):
        report = analyze("case_attrition.json")
        inference = report["inference"]
        self.assertEqual(inference["method"], "park_cluster_bootstrap")
        self.assertEqual(sorted(inference["treated_pool"]), ["T1", "T2"])
        self.assertEqual(inference["control_pool"], ["C"])
        point = coverage_point(report, 0)
        self.assertIn("ci", point)
        self.assertLessEqual(point["ci"]["lo"], point["ci"]["hi"])
        again = analyze("case_attrition.json")
        self.assertEqual(point["ci"], coverage_point(again, 0)["ci"])

    def test_bootstrap_can_be_disabled(self):
        bundle = load_bundle(FIXTURES / "sample.json")
        bundle["analysis"]["bootstrap"] = {"reps": 0, "seed": 1, "level": 0.9}
        report = run_analysis(bundle)
        self.assertIsNone(report["inference"]["method"])
        self.assertNotIn("ci", coverage_point(report, 0))

    def test_no_legal_control_never_falls_back(self):
        report = analyze("case_last_cohort.json")
        for cell in report["cells"]:
            for entry in cell["controls"]:
                adoption = load_bundle(FIXTURES / "case_last_cohort.json")["adoption"]
                other = adoption[entry["park"]]
                if other is not None:
                    self.assertGreater(other, cell["month"])
        last = [c for c in report["cells"] if c["cohort"] == "2026-12"][0]
        self.assertEqual(last["status"], "not_identified")
        self.assertIsNone(last["effect"])

    def test_weights_trace_complete(self):
        report = analyze("case_attrition.json")
        trace = report["weights_trace"]
        self.assertEqual(trace["target_weights"], {"T1": 1, "T2": 1})
        self.assertEqual(trace["treated_target_total"], 2)
        per_event = {p["event_month"]: p["coverage"] for p in trace["per_event"]}
        self.assertEqual(per_event[0], 1.0)
        self.assertEqual(per_event[2], 0.5)
        self.assertEqual(trace["balanced"]["parks"], ["T1"])


if __name__ == "__main__":
    unittest.main()
