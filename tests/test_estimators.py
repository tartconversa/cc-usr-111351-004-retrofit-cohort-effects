"""估计器与诊断的回归检查。"""
import unittest
from src.retrofit_cohort_effects_design import normalize_config
from src.retrofit_cohort_effects_estimators import cell_effect


def config(**overrides):
    analysis = {"cutoff": "2026-11-01", "target_weights": {"A": 1}}
    analysis.update(overrides)
    return normalize_config(analysis)


def entry(park, change, weight=None):
    result = {"park": park, "change": change, "base_mean": 0.0, "current": change}
    if weight is not None:
        result["weight"] = weight
    return result


class EstimatorTests(unittest.TestCase):
    def test_did_arithmetic(self):
        cfg = config()
        result = cell_effect([entry("A", -2.0, 1.0)], [entry("B", -1.0)], cfg, {})
        self.assertEqual(result["effect"], -1.0)
        self.assertEqual(result["treated_term"], -2.0)
        self.assertEqual(result["control_term"], -1.0)

    def test_did_target_weights(self):
        cfg = config()
        treated = [entry("A", -2.0, 3.0), entry("B", -6.0, 1.0)]
        result = cell_effect(treated, [entry("C", 0.0)], cfg, {})
        self.assertEqual(result["treated_term"], -3.0)
        self.assertEqual(result["effect"], -3.0)

    def test_min_controls_refusal(self):
        cfg = config(min_controls=2)
        self.assertIsNone(cell_effect([entry("A", -2.0, 1.0)], [entry("B", -1.0)], cfg, {}))

    def test_ipw_weights_follow_similarity(self):
        attrs = {"T1": {"x": 0.0}, "T2": {"x": 0.2},
                 "C1": {"x": 0.1}, "C2": {"x": 0.3}, "C3": {"x": 5.0}}
        cfg = config(estimator="did_ipw", covariates=["x"], support_epsilon=0.02)
        treated = [entry("T1", -2.0, 1.0), entry("T2", -2.0, 1.0)]
        controls = [entry("C1", 0.0), entry("C2", 1.0), entry("C3", 4.0)]
        result = cell_effect(treated, controls, cfg, attrs)
        self.assertIsNotNone(result)
        weights = result["control_weights"]
        self.assertGreater(weights["C1"], weights["C2"])
        self.assertGreater(weights["C2"], weights["C3"])
        self.assertIn("ess", result["diagnostics"])
        self.assertIn("max_weight_share", result["diagnostics"])

    def _support_scenario(self):
        attrs = {"T%d" % i: {"x": v} for i, v in
                 enumerate([10.0, 10.1, 10.2, 9.9, 10.05])}
        attrs.update({"C1": {"x": 0.0}, "C2": {"x": 0.1}, "C3": {"x": -0.1},
                      "C4": {"x": 0.2}, "C5": {"x": 11.0}})
        treated = [entry("T%d" % i, -2.0, 1.0) for i in range(5)]
        controls = [entry("C1", 0.0), entry("C2", 0.1), entry("C3", -0.1),
                    entry("C4", 0.2), entry("C5", 9.0)]
        return attrs, treated, controls

    def test_ipw_support_excludes_extreme_control(self):
        attrs, treated, controls = self._support_scenario()
        cfg = config(estimator="did_ipw", covariates=["x"], support_epsilon=0.2)
        result = cell_effect(treated, controls, cfg, attrs)
        self.assertIsNotNone(result)
        self.assertEqual(result["off_support"], ["C5"])
        self.assertNotIn("C5", result["control_weights"])
        self.assertAlmostEqual(result["effect"], -2.05, delta=0.05)

    def test_ipw_insufficient_controls_after_support_refuses(self):
        attrs, treated, controls = self._support_scenario()
        cfg = config(estimator="did_ipw", covariates=["x"],
                     support_epsilon=0.2, min_controls=5)
        self.assertIsNone(cell_effect(treated, controls, cfg, attrs))

    def test_balance_table_reported(self):
        attrs = {"T": {"x": 1.0}, "C1": {"x": 1.0}, "C2": {"x": 3.0}}
        cfg = config(covariates=["x"])
        treated = [entry("T", -2.0, 1.0)]
        controls = [entry("C1", 0.0), entry("C2", 0.0)]
        result = cell_effect(treated, controls, cfg, attrs)
        self.assertEqual(len(result["balance"]), 1)
        row = result["balance"][0]
        self.assertEqual(row["covariate"], "x")
        self.assertIsNotNone(row["smd_after"])


if __name__ == "__main__":
    unittest.main()
