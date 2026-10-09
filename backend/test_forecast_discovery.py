from __future__ import annotations

import copy
import math
import unittest
from datetime import date, timedelta

from backend.forecast_discovery import build_discovery_pool, select_discovery_seeds


def board(index: int, **changes) -> dict:
    return {"kind": "concept", "code": f"BK{index:04d}", "name": f"方向{index}",
            "pctChg": 20 - index, "breadth": 70 - index, "amount": 1e9 - index, **changes}


def bars(closes: list[float], amounts: list[float] | None = None, start: str = "2026-09-01") -> list[dict]:
    first = date.fromisoformat(start)
    return [{"date": (first + timedelta(days=index)).isoformat(), "close": close,
             "amount": amounts[index] if amounts is not None else 10.0}
            for index, close in enumerate(closes)]


class DiscoveryTests(unittest.TestCase):
    def test_reversal_and_relative_turnover_recalled_outside_daily_leaders(self):
        rows = [board(index) for index in range(1, 41)]
        histories = {
            "BK0031": bars([120, 118, 116, 114, 112, 110, 110, 110, 112, 116, 124]),
            "BK0032": bars([100] * 11, [10] * 5 + [20] * 5 + [1e9]),
        }
        original = copy.deepcopy((rows, histories))
        selected, metadata = build_discovery_pool(rows, histories, "2026-09-11")
        codes = {row["code"] for row in selected}
        self.assertEqual(len(selected), 12)
        self.assertTrue({"BK0031", "BK0032"} <= codes)
        ratio = next(row for row in selected if row["code"] == "BK0032")
        self.assertEqual(ratio["discoveryFeatures"]["amountRatio5d"], 2.0)
        reversal = next(row for row in selected if row["code"] == "BK0031")
        self.assertLess(reversal["discoveryFeatures"]["previousChange5d"], 0)
        self.assertGreater(reversal["discoveryFeatures"]["momentumChange3d"], 0)
        self.assertEqual(metadata["historyCheckedCount"], 2)
        self.assertEqual(metadata["historyUnseenCount"], 38)
        self.assertEqual(metadata["historyFeatureReadyCount"], 2)
        self.assertEqual((rows, histories), original)

    def test_history_gaps_and_future_rows_remain_visible(self):
        rows = [board(index) for index in range(1, 6)]
        valid = bars([100] * 11)
        selected, metadata = build_discovery_pool(rows, {
            "BK0001": valid + [{"date": "2026-09-12", "close": 10000, "amount": 1e9}],
            "BK0002": valid[:-1], "BK0003": valid[-3:], "BK0004": [],
        }, "2026-09-11")
        self.assertEqual(metadata["ignoredFutureBars"], 1)
        self.assertEqual(metadata["historyCheckedCount"], 4)
        self.assertEqual(metadata["historyMissingOrStaleCount"], 2)
        self.assertEqual(metadata["historyInsufficientCount"], 1)
        self.assertEqual(metadata["historyUnseenCount"], 1)
        features = {row["code"]: row["discoveryFeatures"] for row in selected}
        self.assertEqual(features["BK0001"]["lastClose"], 100)
        self.assertIsNone(features["BK0002"]["lastClose"])
        self.assertIsNone(features["BK0003"]["momentumChange5d"])

    def test_malformed_history_is_not_a_valid_zero(self):
        history = bars([100] * 11)
        history[-1]["close"] = True
        history.extend([None, {"date": "bad", "close": 100},
                        {"date": "2026-09-11", "close": math.inf},
                        {"date": "2026-09-11", "close": 100, "code": "BK0999"}])
        selected, metadata = build_discovery_pool([board(1)], {"BK0001": history}, "2026-09-11")
        self.assertEqual(metadata["ignoredInvalidBars"], 5)
        self.assertIsNone(selected[0]["discoveryFeatures"]["lastClose"])
        self.assertEqual(metadata["channels"]["momentum3d"]["availableCount"], 0)

    def test_event_channel_can_recall_before_price_and_rejects_future_or_undated(self):
        rows = [board(index) for index in range(1, 41)]
        news = {"id": "event-1", "title": "待核验产业事件", "publishedAt": "2026-09-12T09:00:00+08:00",
                "url": "https://example.org/event", "source": "测试公告"}
        selected, metadata = build_discovery_pool(rows, {}, "2026-09-11", {
            "BK0031": [news], "BK0032": [{**news, "publishedAt": "2026-09-13T12:00:00+08:00"}],
            "BK0033": [{**news, "publishedAt": "unknown"}],
        }, decision_at="2026-09-13T10:00:00+08:00")
        self.assertIn("BK0031", [row["code"] for row in selected])
        self.assertEqual(metadata["channels"]["eventClue"]["availableCount"], 1)
        event = next(row for row in selected if row["code"] == "BK0031")["eventClues"][0]
        self.assertIn("不证明利好", event["note"])
        self.assertEqual(event["timePrecision"], "timestamp")
        _, old = build_discovery_pool(rows, {}, "2026-09-11", {"BK0031": [news]})
        self.assertEqual(old["channels"]["eventClue"]["availableCount"], 0)

    def test_event_time_precision_utc_and_invalid_links(self):
        news = {"title": "事件", "publishedAt": "2026-09-13", "url": "https://example.org/a"}
        selected, metadata = build_discovery_pool([board(1), board(2)], {}, "2026-09-11", {
            "BK0001": [news, {**news, "url": "file:///secret"}, {**news, "url": "https://user:pass@example.org/a"},
                       {**news, "publishedAt": "2026-08-01"}],
            "BK0002": [{**news, "publishedAt": "2026-09-13T03:00:00Z"}],
        }, decision_at="2026-09-13T10:00:00+08:00")
        self.assertEqual(metadata["channels"]["eventClue"]["availableCount"], 1)
        clue = next(row for row in selected if row["code"] == "BK0001")["eventClues"]
        self.assertEqual(len(clue), 1)
        self.assertEqual(clue[0]["timePrecision"], "date")

    def test_normalized_date_only_event_preserves_original_precision(self):
        event = {"title": "只有发布日期的事件", "publishedAt": "2026-09-13T00:00:00+08:00",
                 "publishedAtPrecision": "date", "url": "https://example.org/news"}
        selected, metadata = build_discovery_pool([board(1)], {}, "20260911", {"BK0001": [event]},
                                                   decision_at="2026-09-13T10:00:00+08:00")
        clue = selected[0]["eventClues"][0]
        self.assertEqual(clue["timePrecision"], "date")
        self.assertEqual(clue["publishedAtPrecision"], "date")
        self.assertEqual(metadata["channels"]["eventClue"]["availableCount"], 1)

    def test_relative_strength_requires_both_valid_aligned_benchmarks(self):
        rows = [board(index) for index in range(1, 41)]
        relative = {"tradeDate": "2026-09-11", "excessShanghai5d": 3.5, "excessStar505d": 1.5}
        selected, metadata = build_discovery_pool(rows, {}, "2026-09-11", relative_strength={
            "BK0031": relative,
            "BK0032": {**relative, "tradeDate": "2026-09-10"},
            "BK0033": {**relative, "excessStar505d": None},
            "BK0034": {**relative, "excessShanghai5d": -1},
        })
        self.assertIn("BK0031", [row["code"] for row in selected])
        self.assertEqual(metadata["channels"]["relativeStrength5d"]["availableCount"], 1)
        signal = next(signal for row in selected if row["code"] == "BK0031" for signal in row["candidateSignals"]
                      if signal["metric"] == "relativeStrength5d")
        self.assertEqual(signal["value"], 1.5)

    def test_negative_current_return_and_snapshot_only_fallback(self):
        rows = [board(index, pctChg=-index, breadth=None, amount=None) for index in range(1, 20)]
        selected, metadata = build_discovery_pool(rows, {}, "2026-09-11")
        self.assertEqual([row["code"] for row in selected], [f"BK{index:04d}" for index in range(1, 13)])
        self.assertEqual(metadata["baseline"]["dailyTop3Codes"], ["BK0001", "BK0002", "BK0003"])
        self.assertEqual(metadata["baseline"]["candidateEqualWeightCodes"], [row["code"] for row in selected])

    def test_snapshot_dates_filter_and_baseline_does_not_backfill_missing_returns(self):
        rows = [board(1, tradeDate="20260911"), board(2, tradeDate="2026-09-10"),
                board(3, tradeDate="bad"), board(4, pctChg=None)]
        selected, metadata = build_discovery_pool(rows, {}, "2026-09-11")
        self.assertEqual({row["code"] for row in selected}, {"BK0001", "BK0004"})
        self.assertEqual(metadata["excludedSnapshotDateCount"], 2)
        self.assertEqual(metadata["baseline"]["dailyTop3Codes"], ["BK0001"])

    def test_provider_and_argument_validation(self):
        with self.assertRaisesRegex(ValueError, "同一行情来源"):
            build_discovery_pool([board(1), board(2, code="THS:885955")], {}, "2026-09-11")
        for keyword in ("histories", "event_sources", "relative_strength"):
            kwargs = {"histories": {}, "event_sources": {}, "relative_strength": {}}
            kwargs[keyword] = {"THS:885955": []}
            with self.subTest(keyword=keyword), self.assertRaisesRegex(ValueError, "同一行情来源"):
                build_discovery_pool([board(1)], trade_date="2026-09-11", **kwargs)
        for bad_limit in (-1, True, 0.5):
            with self.subTest(limit=bad_limit), self.assertRaises(ValueError):
                build_discovery_pool([], {}, "2026-09-11", limit=bad_limit)
        for bad_date in ("unknown", "2026-02-30"):
            with self.assertRaises(ValueError):
                build_discovery_pool([], {}, bad_date)
        with self.assertRaises(ValueError):
            build_discovery_pool([], {}, "2026-09-11", decision_at="unknown")
        self.assertEqual(build_discovery_pool([board(1)], {}, "2026-09-11", limit=0)[0], [])

    def test_results_are_name_blind_and_input_order_independent(self):
        rows = [board(index, pctChg=1, breadth=50, amount=1e8) for index in range(1, 30)]
        selected, _ = build_discovery_pool(rows, {}, "2026-09-11")
        renamed = [{**row, "name": "某热点重组蛋白"} for row in reversed(rows)]
        again, _ = build_discovery_pool(renamed, {}, "2026-09-11")
        self.assertEqual([row["code"] for row in selected], [row["code"] for row in again])

    def test_seed_budget_samples_outside_leaders_and_rotates_reproducibly(self):
        rows = [board(index) for index in range(1, 101)]
        seeds = select_discovery_seeds(rows, 24, "2026-09-11")
        codes = [row["code"] for row in seeds]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(codes), 24)
        self.assertTrue(any(int(code[2:]) > 24 for code in codes))
        renamed = [{**row, "name": "另一个名称"} for row in reversed(rows)]
        self.assertEqual(codes, [row["code"] for row in select_discovery_seeds(renamed, 24, "2026-09-11")])
        self.assertNotEqual(codes, [row["code"] for row in select_discovery_seeds(rows, 24, "2026-09-12")])
        self.assertEqual(select_discovery_seeds(rows, 0), [])
        self.assertEqual(len(select_discovery_seeds(rows[:2], 24)), 2)

    def test_small_read_budget_reaches_spread_before_all_leaders(self):
        rows = [board(index) for index in range(1, 101)]
        seeds = select_discovery_seeds(rows, 48, "20260911")
        # The first two reads already include one name outside the 24 daily leaders.
        self.assertEqual(seeds[0]["code"], "BK0001")
        self.assertGreater(int(seeds[1]["code"][2:]), 24)
        self.assertEqual(seeds[2]["code"], "BK0002")
        self.assertGreater(int(seeds[3]["code"][2:]), 24)
        self.assertEqual(len(select_discovery_seeds(rows, 5, "20260911")), 5)


if __name__ == "__main__":
    unittest.main()
