"""按月滚动重放的回归检查：截止收紧、防泄漏与确定性。"""
import json
import unittest
from pathlib import Path
from src.retrofit_cohort_effects_input import load_bundle
from src.retrofit_cohort_effects_replay import replay_cutoff, run_replay

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def replay(name, months, day=None):
    return run_replay(load_bundle(FIXTURES / name), months, replay_day=day)


def entry_of(result, month):
    for entry in result["entries"]:
        if entry["month"] == month:
            return entry
    raise AssertionError("缺少重放月份：" + month)


class ReplayTests(unittest.TestCase):
    def test_cutoff_defaults_to_month_end(self):
        self.assertEqual(replay_cutoff("2026-10"), "2026-10-31")
        self.assertEqual(replay_cutoff("2026-02"), "2026-02-28")
        self.assertEqual(replay_cutoff("2026-10", 5), "2026-10-05")
        self.assertEqual(replay_cutoff("2026-02", 31), "2026-02-28")

    def test_replay_tightens_information_set(self):
        result = replay("case_staggered_publication.json", ["2026-10", "2026-11"])
        october = entry_of(result, "2026-10")
        self.assertEqual(october["cutoff"], "2026-10-31")
        self.assertEqual(october["report"]["event_study"]["coverage"], [])
        future = october["report"]["data_trace"]["excluded_future_month"]
        self.assertIn({"park": "T", "month": "2026-11", "revision": 1,
                       "published": "2026-10-28"}, future)
        november = entry_of(result, "2026-11")
        coverage = november["report"]["event_study"]["coverage"]
        self.assertEqual([(p["event_month"], p["effect"]) for p in coverage], [(0, -1.0)])
        revisions = november["report"]["data_trace"]["revisions"]
        chosen = {(r["park"], r["month"]): r["chosen"]["revision"] for r in revisions}
        self.assertEqual(chosen[("T", "2026-09")], 2)

    def test_early_published_future_month_never_leaks(self):
        result = replay("case_staggered_publication.json", ["2026-10"])
        october = entry_of(result, "2026-10")
        cells = october["report"]["cells"]
        self.assertEqual(cells, [])
        self.assertEqual(result["pivot"]["coverage"]["2026-10"], {})

    def test_replay_capped_at_analysis_cutoff(self):
        result = replay("case_staggered_publication.json", ["2026-12"])
        december = entry_of(result, "2026-12")
        self.assertTrue(december["capped_at_analysis_cutoff"])
        self.assertEqual(december["cutoff"], "2026-12-10")

    def test_replay_deterministic(self):
        first = replay("case_staggered_publication.json", ["2026-11", "2026-12"])
        second = replay("case_staggered_publication.json", ["2026-11", "2026-12"])
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_pivot_reflects_information_arrival(self):
        result = replay("case_staggered_publication.json", ["2026-10", "2026-11", "2026-12"])
        pivot = result["pivot"]["coverage"]
        self.assertEqual(pivot["2026-10"], {})
        self.assertEqual(pivot["2026-11"], {"0": -1.0})
        self.assertEqual(pivot["2026-12"], {"0": -1.0, "1": -3.0})

    def test_no_data_month_recorded_not_raised(self):
        result = replay("case_staggered_publication.json", ["2026-08"])
        august = entry_of(result, "2026-08")
        self.assertEqual(august["status"], "no_data")


if __name__ == "__main__":
    unittest.main()
