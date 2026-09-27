from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend import concept_comparison as comparison, database, forecast_feedback as feedback, forecast_history as history
from backend.forecast_prices import CALENDAR_KEY
from backend.main import app
from backend.test_forecast_feedback import calendar_fixture, instant, outcome, prices, report_fixture


class ObservationWindowReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "windows.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": calendar_fixture()}))

    def archive(self, day="2026-09-12"):
        result = report_fixture()["result"]
        result["comparisonUniverse"] = {"asOf": day + "T17:00:00+08:00", "scope": "测试冻结范围",
            "boards": [{"code": "BK1001", "name": "预测概念"}, {"code": "BK1002", "name": "对照概念"}]}
        with patch.object(database, "now_iso", return_value=day + "T18:00:00+08:00"):
            token = database.start_ai_run("forecast:glm", "glm-5.3", day, "test", True)
            database.finish_ai_run("forecast:glm", day, result, None, token)
        with database.connection() as db:
            return db.execute("SELECT MAX(id) FROM forecast_reports").fetchone()[0]

    @staticmethod
    def freeze(clock, value="2026-10-20T18:00:00"):
        clock.now.return_value = instant(value)
        clock.combine = datetime.combine

    def test_invalid_publication_is_explained_without_blocking_other_archives(self):
        bad, good = self.archive(), self.archive("2026-09-13")
        with database.connection() as db:
            db.execute("UPDATE forecast_reports SET published_at='2026-09-12T18:00:00' WHERE id=?", (bad,))
        with (patch.object(comparison.FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(comparison, "index_history", return_value=prices()),
              patch.object(comparison, "datetime") as clock):
            self.freeze(clock)
            with self.assertRaisesRegex(RuntimeError, "发布时间无效"):
                comparison.refresh_comparisons(Mock())
        with database.connection() as db:
            rows = db.execute("SELECT report_id FROM comparison_outcomes").fetchall()
            self.assertEqual([row[0] for row in rows], [good] * 4)
            self.assertEqual(db.execute("SELECT published_at FROM forecast_reports WHERE id=?", (bad,)).fetchone()[0],
                             "2026-09-12T18:00:00")
        result = comparison.comparison_payload(bad, 15, instant("2026-10-20T18:00:00"))
        self.assertIsNone(result["strongest"])
        self.assertIn("时区", result["message"])

    def test_short_calendar_is_a_failed_refresh_and_does_not_fetch_prices(self):
        report = self.archive()
        dates = calendar_fixture()[:3]
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": dates}))
        with (patch.object(comparison.FeedbackPriceClient, "calendar", return_value=dates),
              patch.object(comparison, "index_history") as fetch,
              patch.object(comparison, "datetime") as clock):
            self.freeze(clock)
            with self.assertRaisesRegex(RuntimeError, "交易日历覆盖不足"):
                comparison.refresh_comparisons(Mock())
        fetch.assert_not_called()
        result = comparison.comparison_payload(report, 15, instant("2026-10-20T18:00:00"))
        self.assertEqual(result["coveredCount"], 0)
        self.assertIn("交易日历覆盖不足", result["message"])

    def test_not_started_window_remains_waiting_instead_of_becoming_a_failed_refresh(self):
        report = self.archive()
        with (patch.object(comparison.FeedbackPriceClient, "calendar", return_value=[]),
              patch.object(comparison, "index_history") as fetch,
              patch.object(comparison, "datetime") as clock):
            self.freeze(clock, "2026-09-12T18:00:00")
            self.assertEqual(comparison.refresh_comparisons(Mock())["checked"], 0)
        fetch.assert_not_called()
        result = comparison.comparison_payload(report, 15, instant("2026-09-12T18:00:00"))
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["message"])

    def test_malformed_cached_calendar_is_a_read_only_explanation_not_an_api_crash(self):
        report = self.archive()
        client = TestClient(app)
        for raw in ("not-json", "[]", '{"dates":null}', '{"dates":["bad-day"]}'):
            with self.subTest(raw=raw):
                database.set_meta(CALENDAR_KEY, raw)
                with patch.object(comparison.FeedbackPriceClient, "calendar", side_effect=AssertionError("GET must not fetch")):
                    response = client.get(f"/api/research/comparison?report_id={report}&days=15")
                self.assertEqual(response.status_code, 200)
                self.assertIn("交易日历", response.json()["message"])
                self.assertEqual(database.get_meta(CALENDAR_KEY), raw)

    def test_invalid_feedback_timestamp_is_excluded_without_losing_valid_samples(self):
        ids = [self.archive(day) for day in ("2026-09-12", "2026-09-13", "2026-09-14")]
        with database.connection() as db:
            db.execute("UPDATE forecast_reports SET published_at='2026-09-12T18:00:00' WHERE id=?", (ids[0],))
            for report_id, updated in zip(ids, ("2026-09-28T18:00:00+08:00", "not-a-timestamp", "2026-09-28T18:00:00+08:00")):
                db.execute("INSERT INTO forecast_feedback VALUES (?,?,?,?,?)",
                           (report_id, "BK1001", 15, json.dumps(outcome()), updated))
        result = history.feedback_context(instant("2026-10-20T18:00:00"))
        self.assertEqual(result["summaries"]["15"]["sampleCount"], 1)
        self.assertEqual(result["summaries"]["15"]["averageReturnPct"], 8)
        self.assertEqual(result["recentCases"][0]["predictedOn"], "2026-09-14")

    def test_invalid_publication_does_not_fabricate_an_entry_or_return(self):
        for stamp in ("2026-09-12T18:00:00", "bad-time", None):
            with self.subTest(stamp=stamp):
                report = {**report_fixture(), "publishedAt": stamp}
                value = feedback.evaluate(report, 15, prices(), {}, calendar_fixture(), instant("2026-10-20T18:00:00"))
                self.assertEqual(value["dataStatus"], "unavailable")
                self.assertIsNone(value["entryDate"])
                self.assertIsNone(value["returnPct"])

    def test_comparison_and_forecast_share_the_same_entry_after_intraday_publication(self):
        report = {**report_fixture(), "publishedAt": "2026-09-14T01:30:00Z"}
        now = instant("2026-09-18T14:00:00")
        expected = comparison.expected_range(report, 15, calendar_fixture(), now)
        result = feedback.evaluate(report, 15, prices(), {}, calendar_fixture(), now)
        self.assertEqual(expected, ("2026-09-15", "2026-09-17"))
        self.assertEqual(expected, (result["entryDate"], result["exitDate"]))
