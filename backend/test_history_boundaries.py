from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from backend import database, forecast_feedback, forecast_history, research_store, selection_history
from backend.forecast_prices import CALENDAR_KEY, FeedbackPriceClient
from backend.test_forecast_feedback import calendar_fixture, instant, prices, report_fixture


class HistoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "boundaries.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": calendar_fixture()}))
        self.report = report_fixture()
        token = database.start_ai_run("forecast:glm", "glm-5.3", "2026-09-12", "test")
        with patch.object(database, "now_iso", return_value=self.report["publishedAt"]):
            database.finish_ai_run("forecast:glm", "2026-09-12", self.report["result"], None, token)
        with database.connection() as db:
            research_store.archive_selection(db, "2026-09-12", "rule:test", "测试策略",
                                             self.report["publishedAt"], [{"code": "600519", "name": "测试股票"}])

    @staticmethod
    def freeze(clock, moment):
        clock.now.return_value = instant(moment)
        clock.combine, clock.fromisoformat = datetime.combine, datetime.fromisoformat

    def save_forecasts(self, as_of):
        with database.connection() as db:
            for days in (15, 30):
                value = forecast_feedback.evaluate(self.report, days, prices(),
                    {code: prices() for code in forecast_history.BENCHMARKS}, calendar_fixture(), instant(as_of))
                db.execute("INSERT OR REPLACE INTO forecast_feedback VALUES (?,?,?,?,?)",
                           (1, "BK1001", days, json.dumps(value), "2026-09-24T15:11:00+08:00"))
            db.execute("UPDATE forecast_reports SET checked_at='2026-09-24T15:11:00+08:00'")

    def test_feedback_finishing_after_close_does_not_hide_previous_close_values(self):
        self.save_forecasts("2026-09-24T15:09:00")
        with (patch.object(forecast_history, "datetime") as clock,
              patch.object(database, "china_date", return_value="2026-09-24")):
            self.freeze(clock, "2026-09-24T15:11:00")
            self.assertEqual(len(forecast_history.reports_to_refresh()), 1)
            self.save_forecasts("2026-09-24T15:11:00")
            self.assertEqual(forecast_history.reports_to_refresh(), [])

    def test_up_to_date_intraday_results_do_not_fetch_same_close_repeatedly(self):
        self.save_forecasts("2026-09-24T10:00:00")
        with database.connection() as db:
            db.execute("UPDATE forecast_reports SET checked_at='2026-09-24T10:01:00+08:00'")
        with patch.object(forecast_history, "datetime") as clock:
            self.freeze(clock, "2026-09-24T11:00:00")
            self.assertEqual(forecast_history.reports_to_refresh(), [])

    def test_tracking_result_must_finalize_when_target_passes_even_without_new_session(self):
        self.save_forecasts("2026-09-26T18:00:00")
        with patch.object(forecast_history, "datetime") as clock:
            self.freeze(clock, "2026-09-27T18:00:00")
            self.assertEqual(len(forecast_history.reports_to_refresh()), 1)

    def test_unknown_calendar_cannot_mark_tracking_data_current(self):
        self.save_forecasts("2026-09-24T15:11:00")
        for raw in ("[]", "{\"dates\":null}", "bad-json"):
            database.set_meta(CALENDAR_KEY, raw)
            with patch.object(forecast_history, "datetime") as clock:
                self.freeze(clock, "2026-09-24T16:00:00")
                self.assertEqual(len(forecast_history.reports_to_refresh()), 1)

    def test_selection_finishing_after_close_still_fetches_new_close(self):
        with database.connection() as db:
            for sessions in selection_history.HORIZONS:
                value = selection_history.selection_outcome({"published_at": self.report["publishedAt"]}, sessions,
                    prices()["rows"], {code: prices() for code in forecast_history.BENCHMARKS},
                    calendar_fixture(), instant("2026-09-24T15:09:00"))
                db.execute("INSERT INTO selection_outcomes VALUES (?,?,?,?,?)",
                           (1, "600519", sessions, json.dumps(value), "2026-09-24T15:11:00+08:00"))
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", return_value=prices()["rows"]) as fetch,
              patch.object(selection_history, "index_history", return_value=prices()),
              patch.object(selection_history, "datetime") as clock,
              patch.object(database, "china_date", return_value="2026-09-24")):
            self.freeze(clock, "2026-09-24T15:11:00")
            selection_history.refresh_selections(Mock())
        fetch.assert_called_once()
        with database.connection() as db:
            saved = json.loads(db.execute("SELECT result_json FROM selection_outcomes WHERE sessions=10").fetchone()[0])
        self.assertEqual(saved["exitDate"], "2026-09-24")

    def test_selection_worker_failure_is_waiting_not_missing_bars(self):
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", side_effect=RuntimeError("source disconnected")),
              patch.object(selection_history, "index_history", return_value=prices()),
              patch.object(selection_history, "datetime") as clock):
            self.freeze(clock, "2026-10-20T18:00:00")
            with self.assertRaisesRegex(RuntimeError, "尚未取得查询结果"):
                selection_history.refresh_selections(Mock())
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM selection_outcomes").fetchone()[0], 0)

    def test_concurrently_completed_selection_prices_cannot_be_overwritten(self):
        original = selection_history.selection_outcome({"published_at": self.report["publishedAt"]}, 5,
            prices()["rows"], {code: prices() for code in forecast_history.BENCHMARKS},
            calendar_fixture(), instant("2026-10-20T18:00:00"))
        def fetch(_):
            # Another process commits its audit while this worker is fetching.
            with database.connection() as db:
                db.execute("INSERT INTO selection_outcomes VALUES (?,?,?,?,?)",
                           (1, "600519", 5, json.dumps(original), database.now_iso()))
            data = prices()["rows"]
            next(row for row in data if row["date"] == "2026-09-18")["close"] = 101
            return data
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", side_effect=fetch),
              patch.object(selection_history, "index_history", return_value=prices()),
              patch.object(selection_history, "datetime") as clock):
            self.freeze(clock, "2026-10-20T18:00:00")
            selection_history.refresh_selections(Mock())
        with database.connection() as db:
            saved = json.loads(db.execute("SELECT result_json FROM selection_outcomes WHERE sessions=5").fetchone()[0])
        self.assertEqual(saved, original)

    def test_selection_benchmark_outage_keeps_stock_return_without_claiming_missing_bars(self):
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(selection_history, "stock_history", return_value=prices()["rows"]),
              patch.object(selection_history, "index_history", return_value={"rows": [], "error": "连接中断"}),
              patch.object(selection_history, "datetime") as clock):
            self.freeze(clock, "2026-10-20T18:00:00")
            with self.assertRaisesRegex(RuntimeError, "3 项基准行情尚未取得查询结果"):
                selection_history.refresh_selections(Mock())
        with database.connection() as db:
            saved = json.loads(db.execute("SELECT result_json FROM selection_outcomes WHERE sessions=5").fetchone()[0])
        self.assertEqual(saved["status"], "completed")
        self.assertIsNotNone(saved["returnPct"])
        self.assertIsNone(saved["benchmarks"]["sh000001"]["returnPct"])
