from __future__ import annotations

import copy
import math
import unittest

from backend.forecast_candidates import daily_change_baseline, forecast_candidate_metadata, select_forecast_boards


def board(index: int, **changes) -> dict:
    return {"kind": "concept", "code": f"BK{index:04d}", "name": f"方向{index}",
            "pctChg": 20 - index, "breadth": 50, "amount": 100_000_000, **changes}


class ForecastCandidatesTests(unittest.TestCase):
    def test_breadth_and_turnover_recall_outside_top_nine_with_same_budget(self):
        rows = [board(index) for index in range(1, 21)]
        rows[14]["breadth"] = 98
        rows[15]["amount"] = 1_000_000_000
        original = copy.deepcopy(rows)
        selected = select_forecast_boards(rows)
        self.assertEqual(len(selected), 12)
        self.assertEqual([row["code"] for row in selected[:3]], ["BK0001", "BK0015", "BK0016"])
        routes = [next(signal["metric"] for signal in row["candidateSignals"] if signal["selectedBy"])
                  for row in selected]
        self.assertEqual(routes.count("pctChg"), 4)
        self.assertEqual(routes.count("breadth"), 4)
        self.assertEqual(routes.count("amount"), 4)
        self.assertEqual(rows, original)
        self.assertEqual(forecast_candidate_metadata(rows, selected), {
            "method": "snapshot-rank-round-robin-v1", "availableCount": 20, "selectedCount": 12, "limit": 12})

    def test_overlapping_rankings_deduplicate_and_fill_budget(self):
        rows = [board(index, breadth=100-index, amount=1_000_000_000-index) for index in range(1, 21)]
        selected = select_forecast_boards([*rows, copy.deepcopy(rows[0])])
        self.assertEqual(len(selected), 12)
        self.assertEqual(len({row["code"] for row in selected}), 12)
        self.assertEqual(forecast_candidate_metadata([*rows, rows[0]], selected)["availableCount"], 20)

    def test_missing_values_do_not_rank_as_zero_ahead_of_negative_return(self):
        rows = [board(1, pctChg=-2, breadth=None, amount=None),
                board(2, pctChg=None, breadth=80, amount=None),
                board(3, pctChg=math.nan, breadth=101, amount=-1),
                board(4, pctChg=math.inf, breadth=True, amount="--")]
        selected = select_forecast_boards(rows)
        self.assertEqual([row["code"] for row in selected], ["BK0001", "BK0002"])
        self.assertEqual(selected[0]["candidateSignals"], [
            {"metric": "pctChg", "rank": 1, "value": -2, "selectedBy": True}])
        self.assertEqual(selected[1]["candidateSignals"], [
            {"metric": "breadth", "rank": 1, "value": 80, "selectedBy": True}])

    def test_missing_routes_yield_budget_to_remaining_route(self):
        rows = [board(index, breadth=None, amount=None) for index in range(1, 21)]
        selected = select_forecast_boards(rows)
        self.assertEqual(len(selected), 12)
        self.assertEqual([row["code"] for row in selected], [f"BK{index:04d}" for index in range(1, 13)])

    def test_validations_empty_short_pool_and_custom_limit(self):
        rows = [board(1), board(2), board(3, code="unknown"), board(4, kind="industry"), board(5, name="")]
        self.assertEqual(len(select_forecast_boards(rows)), 2)
        self.assertEqual(select_forecast_boards(rows, 0), [])
        self.assertEqual(len(select_forecast_boards(rows, 1)), 1)
        self.assertEqual(select_forecast_boards([]), [])
        for limit in (-1, 1.5, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                select_forecast_boards(rows, limit)

    def test_provider_mixing_is_rejected_but_ths_scope_is_not_expanded(self):
        with self.assertRaisesRegex(ValueError, "同一行情来源"):
            select_forecast_boards([board(1), board(2, code="THS:886042")])
        rows = [board(index, code=f"THS:88{index:04d}") for index in range(1, 25)]
        selected = select_forecast_boards(rows)
        self.assertEqual(len(selected), 12)
        self.assertTrue({row["code"] for row in selected} <= {row["code"] for row in rows})
        self.assertEqual(forecast_candidate_metadata(rows, selected)["availableCount"], 24)

    def test_name_changes_do_not_change_recall_and_ties_are_deterministic(self):
        rows = [board(index, pctChg=1, breadth=80, amount=2e8) for index in range(1, 21)]
        expected = [row["code"] for row in select_forecast_boards(rows)]
        renamed = [{**row, "name": f"替换名称{index}"} for index, row in enumerate(reversed(rows))]
        self.assertEqual([row["code"] for row in select_forecast_boards(renamed)], expected)

    def test_daily_baseline_is_three_codes_without_missing_return_imputation(self):
        rows = [board(1, pctChg=-1), board(2, pctChg=None), board(3, pctChg=-2), board(4, pctChg=-3)]
        self.assertEqual(daily_change_baseline(rows), ["BK0001", "BK0003", "BK0004"])
        self.assertEqual(daily_change_baseline(rows, 1), ["BK0001"])
        self.assertEqual(daily_change_baseline([], 0), [])
        with self.assertRaises(ValueError):
            daily_change_baseline(rows, True)


if __name__ == "__main__":
    unittest.main()
