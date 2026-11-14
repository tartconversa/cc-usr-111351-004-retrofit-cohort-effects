"""信息截止快照的回归检查：修订选择、截止排除、未来月份防泄漏。"""
import unittest
from src.retrofit_cohort_effects_vintage import build_snapshot


def bundle(panel):
    return {
        "schema_version": 1,
        "panel": panel,
        "adoption": {"A": None},
        "analysis": {"cutoff": "2026-12-31", "target_weights": {"A": 1}},
    }


def record(park, month, published, revision, energy):
    return {"park": park, "month": month, "published": published,
            "revision": revision, "energy": energy}


class VintageTests(unittest.TestCase):
    def test_latest_published_revision_wins(self):
        data = bundle([
            record("A", "2026-09", "2026-10-05", 1, 10),
            record("A", "2026-09", "2026-11-05", 2, 9),
        ])
        snap = build_snapshot(data, "2026-10-31")
        self.assertEqual(snap["values"][("A", "2026-09")]["energy"], 10)
        snap = build_snapshot(data, "2026-12-31")
        self.assertEqual(snap["values"][("A", "2026-09")]["energy"], 9)
        trace = snap["trace"]["revisions"]
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["chosen"]["revision"], 2)
        self.assertEqual(trace[0]["superseded"][0]["energy"], 10)

    def test_unpublished_excluded_and_traced(self):
        data = bundle([
            record("A", "2026-09", "2026-10-05", 1, 10),
            record("A", "2026-10", "2026-12-01", 1, 8),
        ])
        snap = build_snapshot(data, "2026-10-31")
        self.assertNotIn(("A", "2026-10"), snap["values"])
        excluded = snap["trace"]["excluded_unpublished"]
        self.assertEqual([(e["park"], e["month"]) for e in excluded], [("A", "2026-10")])

    def test_future_month_excluded_even_if_published_early(self):
        data = bundle([
            record("A", "2026-09", "2026-10-05", 1, 10),
            record("A", "2026-11", "2026-10-28", 1, 7),
        ])
        snap = build_snapshot(data, "2026-10-31", max_month="2026-10")
        self.assertNotIn(("A", "2026-11"), snap["values"])
        future = snap["trace"]["excluded_future_month"]
        self.assertEqual([(e["park"], e["month"]) for e in future], [("A", "2026-11")])
        self.assertEqual(snap["trace"]["excluded_unpublished"], [])

    def test_duplicate_revision_rejected(self):
        data = bundle([
            record("A", "2026-09", "2026-10-05", 1, 10),
            record("A", "2026-09", "2026-10-06", 1, 11),
        ])
        with self.assertRaises(ValueError):
            build_snapshot(data, "2026-12-31")

    def test_bad_records_rejected(self):
        with self.assertRaises(ValueError):
            build_snapshot(bundle([record("A", "2026-9", "2026-10-05", 1, 10)]), "2026-12-31")
        with self.assertRaises(ValueError):
            build_snapshot(bundle([record("A", "2026-09", "2026-10-5", 1, 10)]), "2026-12-31")
        with self.assertRaises(ValueError):
            build_snapshot(bundle([record("A", "2026-09", "2026-10-05", 1, True)]), "2026-12-31")
        with self.assertRaises(ValueError):
            build_snapshot(bundle([record("A", "2026-09", "2026-10-05", 0, 10)]), "2026-12-31")

    def test_missing_stays_missing(self):
        data = bundle([record("A", "2026-09", "2026-10-05", 1, 10)])
        snap = build_snapshot(data, "2026-12-31")
        self.assertNotIn(("A", "2026-10"), snap["values"])


if __name__ == "__main__":
    unittest.main()
