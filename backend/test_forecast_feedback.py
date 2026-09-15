from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend import database, forecast_feedback as feedback, forecast_history as history
from backend.concept_forecast import PROMPT_VERSION, assemble_result, build_prompt
from backend.forecast_prices import CalendarUnavailableError, FeedbackPriceClient, decode_calendar, normalize_bars
from backend.main import app
from backend.test_concept_forecast import forecast_evidence, forecast_result


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=database.CHINA_TZ)


def report_fixture():
    return {"id": 1, "runDate": "2026-09-12", "publishedAt": "2026-09-12T18:00:00+08:00",
            "result": assemble_result(forecast_result(), forecast_evidence())}


def calendar_fixture():
    start = date(2026, 9, 1)
    # Fixture deliberately includes a closure on Sep 25 and all Oct weekdays.
    return [(start + timedelta(days=i)).isoformat() for i in range(61)
            if (start + timedelta(days=i)).weekday() < 5 and i != 24]


def prices(final=108):
    rows = [{"date": day, "open": 100, "close": 100, "high": 101, "low": 99} for day in calendar_fixture()]
    for row in rows:
        if row["date"] == "2026-09-24":
            row.update(close=final, high=max(final, 100) + 1, low=min(final, 100) - 1)
    return {"rows": rows, "source": "fixture", "url": "https://example.org/data"}


def outcome(value=108, benchmark_missing=False):
    return feedback.evaluate(report_fixture(), 15, prices(value), {
        "sh000001": prices(103), "sh000688": {"rows": []} if benchmark_missing else prices(112)
    }, calendar_fixture(), instant("2026-10-20T18:00:00"))


class EvaluationTests(unittest.TestCase):
    def test_calendar_supports_linux_racer_without_context_manager(self):
        class LinuxRacer:
            # py-mini-racer 0.6, selected by AKShare on Linux, has no
            # __enter__, __exit__ or close; resources are released by __del__.
            def eval(self, script):
                pass

            def call(self, function, encoded):
                return ["2026-09-14T00:00:00.000Z", "2026-12-31T00:00:00.000Z"]

        session = MagicMock()
        session.__enter__.return_value.get.return_value.text = 'var datelist="encoded-calendar";'
        with (patch("py_mini_racer.MiniRacer", LinuxRacer),
              patch.object(FeedbackPriceClient, "_session", return_value=session),
              patch.object(database, "get_meta", return_value=None), patch.object(database, "set_meta")):
            self.assertEqual(FeedbackPriceClient().calendar(), ["2026-09-14", "2026-12-31"])

    def test_modern_calendar_runtime_is_closed_on_success_and_decode_error(self):
        for failure in (False, True):
            runtime = MagicMock()
            runtime.call.return_value = ["2026-09-14T00:00:00.000Z"]
            if failure:
                runtime.call.side_effect = ValueError("invalid data")
            with patch("py_mini_racer.MiniRacer", return_value=runtime):
                if failure:
                    with self.assertRaises(ValueError):
                        decode_calendar("encoded")
                else:
                    self.assertEqual(decode_calendar("encoded"), ["2026-09-14"])
            runtime.close.assert_called_once()

    def test_calendar_decode_failure_is_identified_and_valid_cache_survives(self):
        session = MagicMock()
        session.__enter__.return_value.get.return_value.text = 'var datelist="encoded";'
        for saved in ('malformed-json', json.dumps({"dates": ["2026-09-14"], "fetchedOn": "2026-09-01"})):
            with (patch.object(FeedbackPriceClient, "_session", return_value=session),
                  patch.object(database, "get_meta", return_value=saved),
                  patch("backend.forecast_prices.decode_calendar", side_effect=TypeError("legacy engine error")),
                  self.assertLogs("backend.forecast_prices", level="ERROR") as logs):
                if saved == 'malformed-json':
                    with self.assertRaisesRegex(CalendarUnavailableError, "交易日历解析失败"):
                        FeedbackPriceClient().calendar()
                else:
                    self.assertEqual(FeedbackPriceClient().calendar(), ["2026-09-14"])
            self.assertIn("TypeError", " ".join(logs.output))

    def test_latest_forecast_waits_for_first_close_while_old_forecast_can_track(self):
        now = instant("2026-09-15T01:00:00")
        old = feedback.evaluate(report_fixture(), 15, prices(), {}, calendar_fixture(), now)
        self.assertEqual(old["entryDate"], "2026-09-14")
        self.assertEqual(old["dataStatus"], "ready")
        latest = report_fixture()
        latest["publishedAt"] = "2026-09-14T10:45:00+08:00"
        latest["result"]["window"]["startDate"] = "2026-09-15"
        current = feedback.evaluate(latest, 15, prices(), {}, calendar_fixture(), now)
        self.assertIsNone(current["returnPct"])
        self.assertEqual(current["dataStatus"], "waiting_for_close")
        self.assertIn("2026-09-15", current["note"])

    def test_calendar_holidays_aligned_prices_and_percentage_point_excess(self):
        item = outcome()
        self.assertEqual(item["status"], "completed")
        self.assertEqual((item["entryDate"], item["exitDate"], item["targetDate"]),
                         ("2026-09-14", "2026-09-24", "2026-09-27"))
        self.assertEqual(item["returnPct"], 8)
        self.assertEqual(item["benchmarks"]["sh000001"]["excessPct"], 5)
        self.assertEqual(item["benchmarks"]["sh000688"]["excessPct"], -4)
        self.assertEqual(item["maxRisePct"], 9)
        self.assertEqual(item["maxFallPct"], -1)

    def test_eight_and_ten_average_nine_and_incomplete_samples_do_not_dilute(self):
        first, second = outcome(), outcome(110, benchmark_missing=True)
        result = history.summarize([first, second, {"status": "tracking", "returnPct": 99}, {"status": "missing_data"}])
        self.assertEqual(result["sampleCount"], 2)
        self.assertEqual(result["averageReturnPct"], 9)
        self.assertEqual(result["positiveRate"], 100)
        self.assertEqual(result["benchmarks"]["sh000001"]["averageExcessPct"], 6)
        self.assertEqual(result["benchmarks"]["sh000688"]["sampleCount"], 1)
        self.assertEqual(result["benchmarks"]["sh000688"]["averageExcessPct"], -4)
        self.assertEqual(result["benchmarks"]["sh000688"]["outperformRate"], 0)
        self.assertIsNone(history.summarize([])["positiveRate"])
        self.assertEqual(history.summarize([outcome(100), outcome(90)])["positiveRate"], 0)

    def test_missing_interior_or_boundary_bars_never_finish_and_no_weekday_fallback(self):
        for missing in ("2026-09-14", "2026-09-17", "2026-09-24"):
            data = prices()
            data["rows"] = [row for row in data["rows"] if row["date"] != missing]
            item = feedback.evaluate(report_fixture(), 15, data, {}, calendar_fixture(), instant("2026-10-20T18:00:00"))
            self.assertEqual(item["status"], "missing_data")
            self.assertIsNone(item["returnPct"])
        item = feedback.evaluate(report_fixture(), 15, prices(), {}, calendar_fixture()[:10], instant("2026-10-20T18:00:00"))
        self.assertIsNone(item["returnPct"])
        self.assertIn("日历", item["note"])

    def test_publication_after_open_cannot_take_an_earlier_entry(self):
        report = report_fixture()
        report["publishedAt"] = "2026-09-14T10:00:00+08:00"
        item = feedback.evaluate(report, 15, prices(), {}, calendar_fixture(), instant("2026-10-20T18:00:00"))
        self.assertEqual(item["entryDate"], "2026-09-15")

    def test_intraday_and_future_bars_excluded_and_30_days_is_extended_tracking(self):
        item = feedback.evaluate(report_fixture(), 15, prices(), {}, calendar_fixture(), instant("2026-09-24T14:00:00"))
        self.assertEqual(item["status"], "tracking")
        self.assertEqual(item["exitDate"], "2026-09-23")
        self.assertEqual(item["returnPct"], 0)
        item = feedback.evaluate(report_fixture(), 30, prices(), {}, calendar_fixture(), instant("2026-09-28T18:00:00"))
        self.assertEqual(item["targetDate"], "2026-10-12")
        self.assertEqual(item["status"], "tracking")
        self.assertEqual(item["exitDate"], "2026-09-28")

    def test_close_drawdown_uses_previous_peak_instead_of_final_loss(self):
        data = prices()
        next(row for row in data["rows"] if row["date"] == "2026-09-16").update(close=120, high=121)
        item = feedback.evaluate(report_fixture(), 15, data, {}, calendar_fixture(), instant("2026-10-20T18:00:00"))
        self.assertEqual(item["maxDrawdownPct"], -16.6667)
        self.assertEqual(item["returnPct"], 8)

    def test_provider_filters_wrong_dates_bad_ohlc_and_nonfinite_values(self):
        rows = [["2026-09-14", "100", "108", "110", "99"], ["2026-09-15", "nan", "2", "3", "1"],
                ["2026-09-16", "100", "108", "101", "99"], ["2099-01-01", "1", "2", "3", "1"]]
        self.assertEqual(len(normalize_bars(rows, "2026-09-01", "2026-10-01")), 1)
        client = FeedbackPriceClient()
        with patch.object(client, "_json", return_value={"data": {"code": "BK9999", "klines": [",".join(rows[0])]}}):
            self.assertEqual(client.history("BK1001", "2026-09-01", "2026-10-01")["rows"], [])
        with patch.object(client, "_json", return_value={"data": {"code": "BK1001", "klines": [",".join(rows[0])]}}):
            self.assertEqual(client.history("BK1001", "2026-09-01", "2026-10-01")["url"],
                             "https://quote.eastmoney.com/bk/90.BK1001.html")
        with self.assertRaises(ValueError):
            client.history("untrusted/code", "2026-09-01", "2026-10-01")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "feedback.sqlite3")
        target.start(); self.addCleanup(target.stop)
        database.initialize()

    def save(self, text="original", day="2026-09-12", published="2026-09-12T18:00:00+08:00"):
        report = report_fixture()["result"]
        report["summary"] = text
        token = database.start_ai_run("forecast:glm", "glm-5.3", day, PROMPT_VERSION, True)
        with patch.object(database, "now_iso", return_value=published):
            database.finish_ai_run("forecast:glm", day, report, None, token)
        return token

    def put(self, report_id, days, value, updated="2026-09-28T18:00:00+08:00"):
        with database.connection() as db:
            db.execute("INSERT OR REPLACE INTO forecast_feedback VALUES (?, 'BK1001', ?, ?, ?)",
                       (report_id, days, json.dumps(value), updated))

    def test_versions_are_immutable_idempotent_and_only_first_counts(self):
        token = self.save()
        database.finish_ai_run("forecast:glm", "2026-09-12", {"summary": "late overwrite"}, None, token)
        self.save("second")
        self.put(1, 15, outcome())
        self.put(2, 15, outcome(150))
        data = history.history_payload(as_of=instant("2026-10-20T18:00:00"))
        self.assertEqual(data["totalReports"], 2)
        self.assertEqual(data["summaries"]["15"]["averageReturnPct"], 8)
        self.assertEqual([row["includedInStats"] for row in data["reports"]], [False, True])
        self.assertEqual(history.get_report(1)["result"]["summary"], "original")
        self.assertEqual(history.get_report(2)["result"]["summary"], "second")
        data = history.history_payload(page=2, page_size=1)
        self.assertEqual(data["reports"][0]["id"], 1)
        self.assertIn("entryPrice", data["reports"][0]["concepts"][0]["outcomes"]["30"])

    def test_failed_or_stale_worker_never_creates_forecast_archive(self):
        token = database.start_ai_run("forecast:glm", "glm-5.3", "2026-09-12", PROMPT_VERSION)
        database.finish_ai_run("forecast:glm", "2026-09-12", report_fixture()["result"], None, "old-token")
        self.assertEqual(history.history_payload()["totalReports"], 0)
        database.finish_ai_run("forecast:glm", "2026-09-12", None, "failed", token)
        self.assertEqual(history.history_payload()["totalReports"], 0)

    def test_failed_calendar_refresh_preserves_phase_and_explains_empty_rows(self):
        self.save()
        token = feedback.claim_refresh()
        message = "交易日历解析失败，请更新服务后重试"
        with patch.object(FeedbackPriceClient, "calendar", side_effect=CalendarUnavailableError(message)):
            feedback.refresh_feedback(token)
        self.assertEqual(history.refresh_status()["error"], message)
        data = history.history_payload(as_of=instant("2026-09-15T01:00:00"))
        item = data["reports"][0]["concepts"][0]["outcomes"]["15"]
        self.assertEqual(item["dataStatus"], "unavailable")
        self.assertEqual(item["note"], message)

    def test_real_legacy_reports_migrate_once_before_daily_cache_cleanup(self):
        report = report_fixture()["result"]
        with database.connection() as db:
            db.execute("""INSERT INTO ai_runs (run_date,provider,model,status,result_json,started_at,finished_at)
                       VALUES ('2026-01-01','forecast:glm','glm-5.3','succeeded',?,'2026-01-01T18:00:00+08:00','2026-01-01T18:01:00+08:00')""",
                       (json.dumps(report),))
        database.initialize()
        database.maintain_storage(force=True)
        database.initialize()
        self.assertEqual(history.history_payload()["totalReports"], 1)
        self.assertEqual(history.get_report(1)["result"], report)
        self.assertIsNone(database.read_ai_run("forecast:glm", "2026-01-01"))

    def test_empty_forecast_is_preserved_as_abstention_with_no_fake_zero_return(self):
        token = database.start_ai_run("forecast:glm", "glm-5.3", "2026-09-12", PROMPT_VERSION)
        report = report_fixture()["result"]; report["concepts"] = []
        database.finish_ai_run("forecast:glm", "2026-09-12", report, None, token)
        data = history.history_payload()
        self.assertEqual(data["abstentionDays"], 1)
        self.assertEqual(data["summaries"]["15"]["sampleCount"], 0)
        self.assertIsNone(data["summaries"]["15"]["averageReturnPct"])

    def test_feedback_context_excludes_future_unknown_and_regenerated_outcomes(self):
        self.save()
        self.save("rerun")
        self.put(1, 15, outcome())
        self.put(2, 15, outcome(160))
        self.put(1, 30, {**outcome(), "targetDate": "2026-10-12"}, updated="2026-10-13T18:00:00+08:00")
        context = history.feedback_context(instant("2026-09-29T18:00:00"))
        self.assertEqual(context["summaries"]["15"]["averageReturnPct"], 8)
        self.assertEqual(context["summaries"]["30"]["sampleCount"], 0)
        self.assertEqual(len(context["recentCases"]), 1)
        self.assertEqual(context["recentCases"][0]["thesis"], report_fixture()["result"]["concepts"][0]["thesis"])
        self.assertEqual(history.feedback_context(instant("2026-09-28T10:00:00"))["recentCases"], [])
        self.assertIn('"recentCases"', build_prompt({**forecast_evidence(), "historicalFeedback": context}))

    def test_expired_tracking_is_not_automatically_treated_as_final(self):
        self.save()
        self.put(1, 15, {**outcome(), "status": "tracking"})
        data = history.history_payload(as_of=instant("2026-10-20T18:00:00"))
        self.assertEqual(data["reports"][0]["concepts"][0]["outcomes"]["15"]["status"], "missing_data")
        self.assertEqual(data["summaries"]["15"]["sampleCount"], 0)

    def test_refresh_keeps_completed_prices_and_fills_missing_benchmark_without_ai(self):
        self.save()
        self.put(1, 15, outcome(108, benchmark_missing=True))
        def fake_prices(code, *args):
            return prices(103 if code == "sh000001" else 112 if code == "sh000688" else 110)
        token = feedback.claim_refresh()
        self.assertIsNone(feedback.claim_refresh())
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(FeedbackPriceClient, "history", side_effect=fake_prices),
              patch.object(feedback, "datetime") as clock):
            clock.now.return_value = instant("2026-10-20T18:00:00")
            clock.combine = datetime.combine; clock.fromisoformat = datetime.fromisoformat
            feedback.refresh_feedback(token)
        data = history.history_payload(as_of=instant("2026-10-20T18:00:00"))
        item = data["reports"][0]["concepts"][0]["outcomes"]["15"]
        self.assertEqual(item["returnPct"], 8)
        self.assertEqual(item["benchmarks"]["sh000688"]["excessPct"], -4)
        self.assertEqual(data["refresh"]["status"], "succeeded")

    def test_read_endpoints_work_without_model_key_and_post_requires_secret(self):
        self.save()
        with (patch.dict(os.environ, {"DAILY_RUN_SECRET": "secret"}, clear=True),
              patch("backend.main.start_feedback_refresh", new_callable=AsyncMock, return_value={"status": "running"}) as update):
            client = TestClient(app)
            self.assertEqual(client.get("/api/forecast/history").json()["totalReports"], 1)
            self.assertEqual(client.get("/api/forecast/history/report?id=1").json()["result"]["summary"], "original")
            self.assertEqual(client.get("/api/forecast/history/report?id=999").status_code, 404)
            self.assertEqual(client.get("/api/forecast/history?page=0").status_code, 422)
            update.assert_not_awaited()
            self.assertEqual(client.post("/api/forecast/history").status_code, 401)
            self.assertEqual(client.post("/api/forecast/history", headers={"x-daily-run-secret": "secret"}).status_code, 200)
            update.assert_awaited_once()

    def test_source_outage_does_not_erase_audited_results_and_missing_reports_rotate(self):
        self.save()
        self.save("next day", day="2026-09-13", published="2026-09-13T18:00:00+08:00")
        original = outcome(108, benchmark_missing=True)
        self.put(1, 15, original)
        token = feedback.claim_refresh()
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(FeedbackPriceClient, "history", return_value={"rows": []}),
              patch.object(feedback, "reports_to_refresh", return_value=[history.get_report(1)])):
            feedback.refresh_feedback(token)
        with database.connection() as db:
            saved = json.loads(db.execute("SELECT result_json FROM forecast_feedback WHERE report_id = 1 AND horizon_days = 15").fetchone()[0])
        self.assertEqual(saved, original)
        self.assertEqual(history.reports_to_refresh(limit=1)[0]["id"], 2)

    def test_complete_tracking_day_is_not_fetched_repeatedly_but_missing_benchmark_retries(self):
        self.save()
        tracked = {**outcome(), "status": "tracking"}
        for days in (15, 30):
            self.put(1, days, tracked)
        with database.connection() as db:
            db.execute("UPDATE forecast_reports SET checked_at = ?", (database.china_date() + "T16:30:00+08:00",))
        self.assertEqual(history.reports_to_refresh(), [])
        tracked["benchmarks"]["sh000688"]["returnPct"] = None
        self.put(1, 30, tracked)
        self.assertEqual(len(history.reports_to_refresh()), 1)

    def test_outage_retains_dated_tracking_prices_without_finalizing_and_remains_retryable(self):
        self.save()
        original = {**outcome(), "status": "tracking", "dataStatus": "ready"}
        self.put(1, 15, original)
        token = feedback.claim_refresh()
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(FeedbackPriceClient, "history", return_value={"rows": []}),
              patch.object(feedback, "datetime") as clock):
            clock.now.return_value = instant("2026-10-20T18:00:00")
            clock.combine = datetime.combine; clock.fromisoformat = datetime.fromisoformat
            feedback.refresh_feedback(token)
        data = history.history_payload(as_of=instant("2026-10-20T18:00:00"))
        saved = data["reports"][0]["concepts"][0]["outcomes"]["15"]
        self.assertEqual(saved["returnPct"], original["returnPct"])
        self.assertEqual(saved["exitDate"], original["exitDate"])
        self.assertEqual(saved["status"], "missing_data")
        self.assertEqual(saved["dataStatus"], "unavailable")
        self.assertIn("保留截至", saved["note"])
        self.assertEqual(data["summaries"]["15"]["sampleCount"], 0)
        self.assertEqual(len(history.reports_to_refresh()), 1)


if __name__ == "__main__":
    unittest.main()
