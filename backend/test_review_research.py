from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from backend import database, research_data, research_store, selection_history
from backend.strategy_factors import _recent_bars, build_factor_row
from backend.test_forecast_feedback import calendar_fixture, instant, prices


class ResearchReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(temporary.name) / "review.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        with database.connection() as db:
            research_store.archive_selection(db, "2026-09-12", "rule:test", "测试策略",
                                             "2026-09-12T18:00:00+08:00", [{"code": "600519", "name": "测试股票"}])
            self.report_id = db.execute("SELECT id FROM selection_reports").fetchone()[0]

    def save_outcome(self, value, sessions=5):
        with database.connection() as db:
            db.execute("INSERT OR REPLACE INTO selection_outcomes VALUES (?,?,?,?,?)",
                       (self.report_id, "600519", sessions, json.dumps(value), "2026-09-16T18:00:00+08:00"))

    def outcome(self, sessions=5):
        with database.connection() as db:
            row = db.execute("SELECT result_json FROM selection_outcomes WHERE sessions=?", (sessions,)).fetchone()
        return json.loads(row[0])

    @staticmethod
    def clock(mock, day="2026-10-20T18:00:00"):
        mock.now.return_value = instant(day)
        mock.combine, mock.fromisoformat = datetime.combine, datetime.fromisoformat

    def test_selection_outage_is_failed_after_preserving_all_outcome_records(self):
        with (patch.object(selection_history.FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", return_value=[]),
              patch.object(selection_history, "index_history", return_value={"rows": []}),
              patch.object(selection_history, "datetime") as clock):
            self.clock(clock)
            with self.assertRaisesRegex(RuntimeError, "3 项选股观察"):
                selection_history.refresh_selections(Mock())
        for sessions in selection_history.HORIZONS:
            self.assertEqual(self.outcome(sessions)["status"], "missing_data")
            self.assertIsNone(self.outcome(sessions)["returnPct"])

    def test_only_benchmark_missing_still_keeps_stock_result_but_reports_partial_failure(self):
        with (patch.object(selection_history.FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", return_value=prices()["rows"]),
              patch.object(selection_history, "index_history", return_value={"rows": []}),
              patch.object(selection_history, "datetime") as clock):
            self.clock(clock)
            with self.assertRaisesRegex(RuntimeError, "基准日线"):
                selection_history.refresh_selections(Mock())
        self.assertEqual(self.outcome()["status"], "completed")
        self.assertIsNotNone(self.outcome()["returnPct"])
        self.assertIsNone(self.outcome()["benchmarks"]["sh000001"]["returnPct"])

    def test_elapsed_tracking_returns_are_preserved_but_become_missing_final_data(self):
        old = {"status": "tracking", "dataStatus": "ready", "returnPct": 4, "targetDate": "2026-09-18",
               "entryDate": "2026-09-14", "exitDate": "2026-09-16", "benchmarks": {}}
        self.save_outcome(old)
        with patch.object(selection_history, "datetime") as clock:
            self.clock(clock)
            payload = selection_history.history_payload(5)
        item = payload["entries"][0]["picks"][0]["outcome"]
        self.assertEqual(item["status"], "missing_data")
        self.assertEqual((item["returnPct"], item["exitDate"]), (4, "2026-09-16"))
        self.assertEqual(payload["summaries"][0]["sampleCount"], 0)
        self.assertEqual(payload["summaries"][0]["missingCount"], 1)
        self.assertEqual(self.outcome(), old)  # GET presentation cannot rewrite the archive.
        with (patch.object(selection_history.FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", return_value=[]),
              patch.object(selection_history, "index_history", return_value={"rows": []}),
              patch.object(selection_history, "datetime") as clock):
            self.clock(clock)
            with self.assertRaises(RuntimeError):
                selection_history.refresh_selections(Mock())
        self.assertEqual(self.outcome()["status"], "missing_data")
        self.assertEqual(self.outcome()["returnPct"], 4)

    def test_completed_prices_remain_audited_when_only_missing_benchmarks_retry(self):
        old = {"status": "completed", "dataStatus": "ready", "returnPct": 8, "targetDate": "2026-09-18",
               "entryDate": "2026-09-14", "exitDate": "2026-09-18", "benchmarks": {}}
        self.save_outcome(old)
        with (patch.object(selection_history.FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", return_value=[]),
              patch.object(selection_history, "index_history", return_value={"rows": []}),
              patch.object(selection_history, "datetime") as clock):
            self.clock(clock)
            with self.assertRaises(RuntimeError):
                selection_history.refresh_selections(Mock())
        self.assertEqual(self.outcome(), old)

    def test_existing_rotation_rows_cannot_hide_a_failed_pending_batch(self):
        closed = [day for day in calendar_fixture() if day <= "2026-10-09"]
        first = {"code": "BK1001", "name": "已核对", "asOf": closed[-1], "returns": {"5": 2, "10": 3, "20": 4},
                 "previousReturns": {"5": 1, "10": 2, "20": 3}, "path": []}
        research_store.cache_put("rotation", {"asOf": closed[-1], "items": [first]})
        universe = {"asOf": database.china_date() + "T00:00:00+08:00",
                    "boards": [{"code": "BK1001", "name": "已核对"}, {"code": "BK1002", "name": "待补齐"}]}
        with (patch.object(research_data, "latest_universe", return_value=universe),
              patch.object(research_data.FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(research_data, "last_closed_day", return_value=closed[-1]),
              patch.object(research_data, "index_history", return_value={"rows": []})):
            with self.assertRaisesRegex(RuntimeError, "本批已核对 0 项"):
                research_data.refresh_rotation(Mock())
        self.assertEqual(research_store.cache_get("rotation")["items"], [first])


class FactorDateReviewTests(unittest.TestCase):
    def test_future_cache_rows_cannot_enter_an_older_snapshot_factor(self):
        rows = [{"date": (date(2026, 6, 1) + timedelta(days=i)).strftime("%Y%m%d"),
                 "close": 100, "pctChg": 0, "amount": 100000000, "vol": 1000000} for i in range(110)]
        rows[-1]["date"], rows[-1]["close"] = "20260918", 1000
        spot = {"tradeDate": "20260917", "close": 100, "name": "测试", "tsCode": "600519.SH", "symbol": "600519"}
        with patch("backend.strategy_factors.market_data.dividend_yield", return_value=None):
            result = build_factor_row(spot, rows)
        self.assertEqual(result["latestClose"], 100)
        self.assertEqual(result["change5d"], 0)
        self.assertEqual(rows[-1]["close"], 1000)

    def test_iso_compact_duplicates_and_out_of_order_rows_count_as_one_session(self):
        bars = _recent_bars({"tradeDate": "2026-09-18", "close": 108}, [
            {"date": "20260918", "close": 102},
            {"date": "2026-09-17", "close": 100}, {"date": "2026-09-18", "close": 103}])
        self.assertEqual(bars, [{"date": "20260917", "close": 100}, {"date": "20260918", "close": 108}])

    def test_future_rows_are_filtered_even_when_snapshot_price_is_missing(self):
        self.assertEqual(_recent_bars({"tradeDate": "20260918", "close": None}, [
            {"date": "20260918", "close": 100}, {"date": "20260921", "close": 200}]),
            [{"date": "20260918", "close": 100}])

    def test_newer_adjusted_history_cannot_be_spliced_with_old_raw_snapshot(self):
        rows = [{"date": day, "close": 50} for day in ("20260917", "20260918", "20260921")]
        bars = _recent_bars({"tradeDate": "20260918", "close": 100}, rows)
        self.assertEqual([row["close"] for row in bars], [50, 50])
        self.assertEqual([row["date"] for row in bars], ["20260917", "20260918"])
        # Missing adjusted snapshot-day prices cannot be invented from an old raw quote.
        self.assertEqual(_recent_bars({"tradeDate": "20260918", "close": 100}, [rows[0], rows[2]]), [])
