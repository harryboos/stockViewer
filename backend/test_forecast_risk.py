from __future__ import annotations

import copy
import json
import unittest
from datetime import date, timedelta

from backend.forecast_risk import enrich_risk_context


def observations(count=25, *, scale=1.0, reverse=False):
    rows = []
    day, close = date(2026, 8, 20), 100.0
    while len(rows) < count:
        if day.weekday() < 5:
            close *= 1 + ((len(rows) % 7) - 2) * .004
            rows.append({"date": day.isoformat(), "close": close * scale,
                         "historySource": "测试日线源", "historyUrl": "https://example.com/history"})
        day += timedelta(days=1)
    return list(reversed(rows)) if reverse else rows


def fixture(count=25):
    histories = {"BK1001": observations(count), "BK1002": observations(count, scale=3)}
    benchmarks = {"sh000001": observations(count, scale=2), "sh000688": observations(count, scale=4)}
    candidates = [{"code": "BK1001", "name": "甲", "stocks": [{"code": "600001"}, {"code": "600002"}],
                   "evidence": [{"kind": "news", "url": "https://example.com/event?a=1#paragraph"}]},
                  {"code": "BK1002", "name": "乙", "stocks": [{"code": "600002"}, {"code": "600003"}],
                   "evidence": [{"kind": "news", "url": "https://example.com/event?a=1"}]}]
    return candidates, histories, benchmarks, histories["BK1001"][-1]["date"]


class ForecastRiskTests(unittest.TestCase):
    def test_relative_strength_uses_identical_price_dates_and_percentage_points(self):
        candidates, histories, benchmarks, cutoff = fixture()
        benchmarks["sh000001"][-1]["close"] *= 0.95
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        periods = result["relativeStrengthByCode"]["BK1001"]["benchmarks"][0]["periods"]
        for n in (5, 10):
            p = periods[f"{n}d"]
            a = histories["BK1001"]
            b = benchmarks["sh000001"]
            concept_return = (a[-1]["close"] / a[-n - 1]["close"] - 1) * 100
            benchmark_return = (b[-1]["close"] / b[-n - 1]["close"] - 1) * 100
            self.assertEqual(p["status"], "available")
            self.assertEqual(p["startDate"], a[-n - 1]["date"])
            self.assertEqual(p["endDate"], cutoff)
            self.assertEqual(p["priceCount"], n + 1)
            self.assertAlmostEqual(p["excessPctPoints"], concept_return - benchmark_return, places=3)

    def test_mixed_date_formats_and_out_of_order_rows_produce_same_results(self):
        args = fixture()
        expected = enrich_risk_context(*args)
        mixed = copy.deepcopy(args)
        for rows in [*mixed[1].values(), *mixed[2].values()]:
            rows.reverse()
            for row in rows[::2]:
                row["date"] = row["date"].replace("-", "")
        actual = enrich_risk_context(*mixed[:-1], mixed[-1].replace("-", ""))
        self.assertEqual(actual, expected)
        pair = actual["riskContext"]["pairs"][0]
        self.assertEqual(pair["correlation20d"]["value"], 1.0)
        self.assertEqual(pair["correlation20d"]["returnCount"], 20)

    def test_missing_internal_concept_day_prevents_skip_day_returns_and_correlation(self):
        candidates, histories, benchmarks, cutoff = fixture()
        del histories["BK1001"][-3]
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        relative = result["relativeStrengthByCode"]["BK1001"]["benchmarks"][0]["periods"]
        self.assertIsNone(relative["5d"]["excessPctPoints"])
        pair = result["riskContext"]["pairs"][0]["correlation20d"]
        self.assertIsNone(pair["value"])
        self.assertIn("缺失", pair["reason"])

    def test_both_concepts_missing_same_day_is_detected_by_benchmark_observations(self):
        candidates, histories, benchmarks, cutoff = fixture()
        for rows in histories.values():
            del rows[-4]
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        self.assertIsNone(result["riskContext"]["pairs"][0]["correlation20d"]["value"])

    def test_missing_benchmark_day_detected_even_when_other_benchmark_also_missing(self):
        candidates, histories, benchmarks, cutoff = fixture()
        for rows in benchmarks.values():
            del rows[-2]
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        for benchmark in result["relativeStrengthByCode"]["BK1001"]["benchmarks"]:
            self.assertIsNone(benchmark["periods"]["5d"]["excessPctPoints"])

    def test_missing_data_before_five_day_window_does_not_invalidate_it(self):
        candidates, histories, benchmarks, cutoff = fixture()
        del histories["BK1001"][-8]
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        periods = result["relativeStrengthByCode"]["BK1001"]["benchmarks"][0]["periods"]
        self.assertEqual(periods["5d"]["status"], "available")
        self.assertEqual(periods["10d"]["status"], "unavailable")

    def test_short_correlation_requires_ten_returns_and_marks_small_sample(self):
        result = enrich_risk_context(*fixture(11))["riskContext"]["pairs"][0]["correlation20d"]
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["smallSample"])
        self.assertEqual(result["returnCount"], 10)
        self.assertEqual(result["value"], 1.0)
        result = enrich_risk_context(*fixture(10))["riskContext"]["pairs"][0]["correlation20d"]
        self.assertIsNone(result["value"])

    def test_flat_and_constant_growth_returns_have_no_defined_correlation(self):
        for growth in (1, 1.01):
            candidates, histories, benchmarks, cutoff = fixture()
            for n, row in enumerate(histories["BK1001"]):
                row["close"] = 100 * growth ** n
            result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
            correlation = result["riskContext"]["pairs"][0]["correlation20d"]
            self.assertIsNone(correlation["value"])
            self.assertIn("无定义", correlation["reason"])

    def test_latest_dates_must_match_but_does_not_claim_latest_bar_is_full_day(self):
        candidates, histories, benchmarks, cutoff = fixture()
        del histories["BK1001"][-1]
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        period = result["relativeStrengthByCode"]["BK1001"]["benchmarks"][0]["periods"]["5d"]
        self.assertIsNone(period["excessPctPoints"])
        self.assertIsNone(result["riskContext"]["pairs"][0]["correlation20d"]["value"])
        all_old = fixture()
        result = enrich_risk_context(*all_old[:-1], "20261010")
        relative = result["relativeStrengthByCode"]["BK1001"]
        self.assertEqual(relative["historyAsOf"], all_old[-1])
        self.assertEqual(relative["benchmarks"][0]["periods"]["5d"]["endDate"], all_old[-1])
        self.assertIn("不保证该日已收盘", result["riskContext"]["limits"][0])

    def test_conflicting_duplicate_invalidates_the_day_independent_of_order(self):
        candidates, histories, benchmarks, cutoff = fixture()
        duplicate = {**histories["BK1001"][-2], "close": 999}
        histories["BK1001"].append(duplicate)
        first = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        histories["BK1001"].reverse()
        self.assertEqual(first, enrich_risk_context(candidates, histories, benchmarks, cutoff))
        period = first["relativeStrengthByCode"]["BK1001"]["benchmarks"][0]["periods"]["5d"]
        self.assertIsNone(period["excessPctPoints"])
        self.assertEqual(first["relativeStrengthByCode"]["BK1001"]["coverage"]["invalidPriceDates"], [duplicate["date"]])

    def test_future_invalid_and_nonfinite_data_never_leak_or_break_json(self):
        candidates, histories, benchmarks, cutoff = fixture()
        original = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        histories["BK1001"].extend([{"date": "20990101", "close": 9999999},
                                    {"date": "no date", "close": float("nan")}])
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        self.assertEqual(result["riskContext"], original["riskContext"])
        json.dumps(result, allow_nan=False)
        histories["BK1001"][-4]["close"] = float("inf")
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        self.assertIsNone(result["relativeStrengthByCode"]["BK1001"]["benchmarks"][0]["periods"]["5d"]["excessPctPoints"])
        json.dumps(result, allow_nan=False)

    def test_sample_overlap_is_not_full_constituent_overlap_and_missing_is_unknown(self):
        candidates, histories, benchmarks, cutoff = fixture()
        candidates[0]["stocks"].append({"code": "600002"})
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        overlap = result["riskContext"]["pairs"][0]["strongStockSampleOverlap"]
        self.assertEqual(overlap["leftSampleSize"], 2)
        self.assertEqual(overlap["sharedCount"], 1)
        self.assertEqual(overlap["jaccardPct"], 33.33)
        self.assertEqual(overlap["overlapOfSmallerSamplePct"], 50)
        self.assertIn("不代表全部成份股", overlap["note"])
        candidates[1]["stocks"] = None
        overlap = enrich_risk_context(candidates, histories, benchmarks, cutoff)["riskContext"]["pairs"][0]["strongStockSampleOverlap"]
        self.assertIsNone(overlap["sharedCount"])
        self.assertIsNone(overlap["jaccardPct"])

    def test_shared_urls_are_only_source_overlap_not_event_deduplication(self):
        candidates, histories, benchmarks, cutoff = fixture()
        for candidate in candidates:
            candidate["evidence"].extend([{"kind": "data", "url": "https://example.com/data"},
                                          {"kind": "news", "url": "file:///tmp/local"},
                                          {"kind": "news", "url": "https://user:pass@example.com/private"}])
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        news = result["riskContext"]["pairs"][0]["sharedNewsSources"]
        self.assertEqual(news["count"], 1)
        self.assertEqual(news["urls"], ["https://example.com/event?a=1"])
        self.assertIn("不等于同一事件", news["note"])
        candidates[1]["evidence"] = None
        news = enrich_risk_context(candidates, histories, benchmarks, cutoff)["riskContext"]["pairs"][0]["sharedNewsSources"]
        self.assertEqual(news["count"], 0)

    def test_empty_histories_remain_unknown_and_function_does_not_mutate_inputs(self):
        args = fixture()
        original = copy.deepcopy(args)
        enrich_risk_context(*args)
        self.assertEqual(args, original)
        result = enrich_risk_context(args[0], {}, {}, args[-1])
        self.assertEqual(result["riskContext"]["coverage"]["availableCorrelationPairCount"], 0)
        self.assertEqual(result["riskContext"]["coverage"]["benchmarkObservedDateCount"], 0)
        self.assertIsNone(result["relativeStrengthByCode"]["BK1001"]["historyAsOf"])
        self.assertIsNone(result["relativeStrengthByCode"]["BK1001"]["benchmarks"][0]["periods"]["5d"]["excessPctPoints"])
        json.dumps(result, allow_nan=False)

    def test_invalid_cutoff_fails_explicitly(self):
        with self.assertRaises(ValueError):
            enrich_risk_context([], {}, {}, "2026-99-99")

    def test_broad_recall_can_skip_pair_analysis_without_losing_relative_metrics(self):
        args = fixture()
        full = enrich_risk_context(*args)
        relative_only = enrich_risk_context(*args, include_pairs=False)
        self.assertEqual(full["relativeStrengthByCode"], relative_only["relativeStrengthByCode"])
        self.assertEqual(relative_only["riskContext"]["pairs"], [])
        self.assertFalse(relative_only["riskContext"]["coverage"]["pairAnalysisIncluded"])

    def test_malformed_stock_codes_do_not_create_fake_shared_exposure(self):
        candidates, histories, benchmarks, cutoff = fixture()
        for candidate in candidates:
            candidate["stocks"] = [{"code": "missing"}, {"code": True}, {"code": float("nan")}]
        result = enrich_risk_context(candidates, histories, benchmarks, cutoff)
        overlap = result["riskContext"]["pairs"][0]["strongStockSampleOverlap"]
        self.assertEqual(overlap["leftSampleSize"], 0)
        self.assertIsNone(overlap["jaccardPct"])


if __name__ == "__main__":
    unittest.main()
