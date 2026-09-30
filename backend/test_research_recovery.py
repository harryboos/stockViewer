from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import concept_comparison as comparison, database, forecast_feedback, forecast_history
from backend import research_data, research_pool, research_store
from backend.forecast_prices import CALENDAR_KEY, FeedbackPriceClient
from backend.test_forecast_feedback import calendar_fixture, instant, prices, report_fixture


class BoundedLaneTests(unittest.TestCase):
    def test_empty_batch_returns_without_waiting_for_saturated_capacity(self):
        capacity = threading.BoundedSemaphore(1)
        capacity.acquire()
        with (patch.object(research_pool, "_capacity", capacity),
              patch.object(research_pool.time, "sleep", side_effect=AssertionError("empty batch waited")),
              patch.object(research_pool._pool, "submit") as submit):
            result = research_pool.fetch_batch([], Mock())
        self.assertEqual(result, {})
        submit.assert_not_called()

    def test_background_scans_leave_capacity_for_prediction_feedback(self):
        release, saturated = threading.Event(), threading.Event()
        guard = threading.Lock()
        count = 0

        def slow(item):
            nonlocal count
            with guard:
                count += 1
                if count == 2:
                    saturated.set()
            release.wait(2)
            return item

        with ThreadPoolExecutor(max_workers=3) as pool, ThreadPoolExecutor(max_workers=1) as caller:
            with (patch.object(research_pool, "_pool", pool),
                  patch.object(research_pool, "_capacity", threading.BoundedSemaphore(3)),
                  patch.object(research_pool, "_background_capacity", threading.BoundedSemaphore(2))):
                background = caller.submit(research_pool.fetch_batch, list(range(8)), slow, 1, background=True)
                try:
                    self.assertTrue(saturated.wait(1))
                    foreground = research_pool.fetch_batch(["prediction"], lambda value: value, seconds=.2)
                    self.assertEqual(foreground, {"prediction": "prediction"})
                finally:
                    release.set()
                    background.result(timeout=2)

    def test_busy_batch_waits_for_a_slot_and_never_allocates_a_work_queue(self):
        capacity = threading.BoundedSemaphore(1)
        capacity.acquire()
        with ThreadPoolExecutor(max_workers=1) as pool, ThreadPoolExecutor(max_workers=1) as caller:
            with patch.object(research_pool, "_pool", pool), patch.object(research_pool, "_capacity", capacity):
                work = Mock(side_effect=lambda item: item * 2)
                future = caller.submit(research_pool.fetch_batch, [1], work, .5)
                try:
                    # While capacity is held there must not be a queued fetch.
                    work.assert_not_called()
                finally:
                    capacity.release()
                self.assertEqual(future.result(timeout=1), {1: 2})

    def test_timeouts_and_unstarted_items_have_distinct_reasons(self):
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with (patch.object(research_pool, "_pool", pool),
                  patch.object(research_pool, "_capacity", threading.BoundedSemaphore(1))):
                try:
                    result = research_pool.fetch_batch([1, 2], lambda _: release.wait(2), seconds=.02)
                    self.assertEqual(result, {})
                    self.assertEqual(result.attempted, {1})
                    self.assertEqual(research_pool.history_result(result, 1)["errorKind"], "timeout")
                    self.assertEqual(research_pool.history_result(result, 2)["errorKind"], "not_started")
                finally:
                    release.set()
                    pool.shutdown(wait=True)

    def test_worker_failures_are_visible_without_leaking_provider_details(self):
        def failed(_):
            raise RuntimeError("Authorization: private-key")
        result = research_pool.fetch_batch([1], failed)
        self.assertEqual(result.errors[1], "failed")
        self.assertNotIn("private", research_pool.history_result(result, 1)["error"])


class RecoveryDatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target in (patch.object(database, "DATABASE_PATH", Path(directory.name) / "recovery.sqlite3"),
                       patch.object(research_data, "last_closed_day", return_value="2026-09-29")):
            target.start()
            self.addCleanup(target.stop)
        database.initialize()
        self.now = instant("2026-09-30T10:00:00")
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": calendar_fixture(), "fetchedOn": database.china_date()}))

    def archive(self, count=80, day="2026-09-12"):
        result = report_fixture()["result"]
        start = date.fromisoformat(day) + timedelta(days=1)
        result["window"].update(generatedOn=day, startDate=start.isoformat(), endDate=(start + timedelta(days=14)).isoformat())
        boards = [{"code": f"BK{1000 + index}", "name": f"概念{index}"} for index in range(count)]
        result["concepts"] = [{**result["concepts"][0], **boards[-1]}]
        result["comparisonUniverse"] = {"asOf": f"{day}T18:00:00+08:00", "scope": "测试冻结范围", "boards": boards}
        token = database.start_ai_run("forecast:glm", "glm-5.3", day, "test")
        with patch.object(database, "now_iso", return_value=f"{day}T18:00:00+08:00"):
            database.finish_ai_run("forecast:glm", day, result, None, token)
        with database.connection() as db:
            return db.execute("SELECT MAX(id) FROM forecast_reports").fetchone()[0]

    def comparison_clock(self, clock):
        clock.now.return_value = self.now
        clock.combine, clock.fromisoformat = datetime.combine, datetime.fromisoformat

    def test_overlapping_archives_and_horizons_fetch_each_concept_once_selected_first(self):
        first, second = self.archive(), self.archive(day="2026-09-17")
        legacy = {"status": "tracking", "returnPct": None,
                  "note": "行情请求未完成（等待超时或后台任务繁忙）；缺少行情", "missingDates": ["2026-09-22"]}
        with database.connection() as db:
            db.execute("INSERT INTO comparison_outcomes VALUES (?,?,?,?,?)",
                       (first, "BK1079", 15, json.dumps(legacy), database.now_iso()))
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(comparison, "datetime") as clock,
              patch.object(comparison, "index_history", return_value=prices()) as fetch):
            self.comparison_clock(clock)
            result = comparison.refresh_comparisons(Mock())
        self.assertEqual(fetch.call_count, 80)
        self.assertEqual(fetch.call_args_list[0].args[0], "BK1079")
        self.assertEqual(result["checked"], 320)
        self.assertEqual(result["remaining"], 0)
        for report in (first, second):
            payload = comparison.comparison_payload(report, 15, self.now)
            self.assertEqual(payload["coveredCount"], 80)
            self.assertEqual(payload["waitingCount"], 0)
            self.assertEqual(payload["missingCount"], 0)

    def test_unstarted_batch_does_not_turn_legacy_waiting_rows_into_missing_prices(self):
        report = self.archive(count=3)
        legacy = {"status": "tracking", "returnPct": None,
                  "note": "行情请求未完成（等待超时或后台任务繁忙）；缺少 2026-09-22，等待补齐后核对"}
        with database.connection() as db:
            db.execute("INSERT INTO comparison_outcomes VALUES (?,?,?,?,?)",
                       (report, "BK1002", 15, json.dumps(legacy), database.now_iso()))

        def busy(items, *args, **kwargs):
            result = research_pool.BatchResult()
            result.errors = dict.fromkeys(items, "busy")
            return result

        progress = Mock()
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(comparison, "datetime") as clock,
              patch.object(comparison, "fetch_batch", side_effect=busy)):
            self.comparison_clock(clock)
            with self.assertRaisesRegex(RuntimeError, "已核对 0 项"):
                comparison.refresh_comparisons(progress)
        self.assertEqual(progress.call_args.args[0], 0)
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comparison_outcomes").fetchone()[0], 1)
        payload = comparison.comparison_payload(report, 15, self.now)
        self.assertEqual(payload["missingCount"], 0)
        self.assertEqual(payload["waitingCount"], 3)
        self.assertIsNone(payload["strongest"])

    def test_source_outage_pauses_broad_scan_without_counting_unread_data_missing(self):
        report = self.archive()
        outage = {"rows": [], "error": "行情连接中断，暂不可用"}
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(comparison, "datetime") as clock,
              patch.object(comparison, "index_history", return_value=outage) as fetch):
            self.comparison_clock(clock)
            with self.assertRaisesRegex(RuntimeError, "已暂停本批后续查询"):
                comparison.refresh_comparisons(Mock())
        self.assertGreaterEqual(fetch.call_count, 8)
        self.assertLessEqual(fetch.call_count, 8 + research_pool.BATCH_WORKERS - 1)
        self.assertEqual(fetch.call_args_list[0].args[0], "BK1079")
        payload = comparison.comparison_payload(report, 15, self.now)
        self.assertEqual(payload["coveredCount"], 0)
        self.assertEqual(payload["missingCount"], 0)
        self.assertEqual(payload["waitingCount"], 80)
        self.assertIn("连接中断", payload["message"])
        with database.connection() as db:
            saved = [json.loads(row[0]) for row in db.execute("SELECT result_json FROM comparison_outcomes")]
        self.assertEqual(len(saved), 2 * fetch.call_count)
        self.assertTrue(all(not value.get("missingDates") and "缺少" not in value["note"] for value in saved))

    def test_one_valid_concept_prevents_false_source_outage_pause(self):
        report = self.archive()
        def history(code, *args):
            return prices() if code == "BK1079" else {"rows": [], "error": "行情连接中断"}
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(comparison, "datetime") as clock,
              patch.object(comparison, "index_history", side_effect=history) as fetch):
            self.comparison_clock(clock)
            with self.assertRaises(RuntimeError) as error:
                comparison.refresh_comparisons(Mock())
        self.assertNotIn("已暂停本批", str(error.exception))
        self.assertEqual(fetch.call_count, 80)
        payload = comparison.comparison_payload(report, 15, self.now)
        self.assertEqual(payload["coveredCount"], 1)
        self.assertEqual(payload["missingCount"], 0)
        self.assertEqual(payload["waitingCount"], 79)

    def test_comparison_request_keys_stay_stable_across_close_boundary(self):
        self.archive(count=3)
        now = instant("2026-09-29T15:09:59")
        def fetch(code, start, end):
            # A worker finishes after the boundary; reading its result must use
            # the exact same request key captured before the fetch started.
            clock.now.return_value = instant("2026-09-29T15:10:01")
            return prices()
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(comparison, "datetime") as clock,
              patch.object(comparison, "last_closed_day", return_value="2026-09-28") as closed,
              patch.object(research_data, "last_closed_day", side_effect=AssertionError("clock re-read")),
              patch.object(comparison, "index_history", side_effect=fetch) as history):
            self.comparison_clock(clock)
            clock.now.return_value = now
            result = comparison.refresh_comparisons(Mock())
        closed.assert_called_once_with(now)
        self.assertEqual(history.call_count, 3)
        self.assertTrue(all(call.args[2] == "2026-09-28" for call in history.call_args_list))
        self.assertEqual(result["checked"], 6)
        self.assertEqual(result["remaining"], 0)

    def test_forecast_feedback_deduplicates_windows_and_does_not_count_queued_data_missing(self):
        self.archive(count=3)
        self.archive(count=3, day="2026-09-17")
        seen = []

        def busy(items, *args, **kwargs):
            seen.extend(items)
            result = research_pool.BatchResult()
            result.errors = dict.fromkeys(items, "busy")
            return result

        token = forecast_feedback.claim_refresh()
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(forecast_feedback, "fetch_batch", side_effect=busy)):
            forecast_feedback.refresh_feedback(token)
        self.assertEqual(len(seen), 3)  # One predicted concept + two benchmark symbols.
        state = forecast_history.refresh_status()
        self.assertEqual(state["outcomesMissing"], 0)
        self.assertEqual(state["outcomesWaiting"], 4)
        self.assertEqual(state["reportsChecked"], 0)
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM forecast_feedback").fetchone()[0], 0)

    def test_empty_parallel_histories_share_one_short_lived_failure_without_durable_empty_cache(self):
        moment = [100.0]
        started, release = threading.Event(), threading.Event()

        def failed(*args):
            started.set()
            release.wait(2)
            return {"rows": [], "error": "行情连接中断"}

        with (patch.object(research_data, "clock", SimpleNamespace(monotonic=lambda: moment[0])),
              patch.object(FeedbackPriceClient, "history", side_effect=failed) as fetch,
              ThreadPoolExecutor(max_workers=4) as pool):
            futures = [pool.submit(research_data.index_history, "BK1152", f"2026-09-{day:02d}", "2026-09-29")
                       for day in (18, 21, 22, 23)]
            self.assertTrue(started.wait(1))
            release.set()
            self.assertTrue(all(not future.result()["rows"] for future in futures))
            self.assertEqual(fetch.call_count, 1)
            code, start, end = research_data.history_request("BK1152", "2026-09-18", "2026-09-29")
            self.assertIsNone(research_store.cache_get(f"evaluation:{code}:{start}:{end}"))
            moment[0] += 31
            research_data.index_history(code, start, end)
            self.assertEqual(fetch.call_count, 2)

    def test_incomplete_cached_window_retries_early_but_complete_window_keeps_hour_cache(self):
        code, start, end = research_data.history_request("BK1152", "2026-09-18", "2026-09-29")
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        dates = [(first + timedelta(days=index)).isoformat() for index in range((last - first).days + 1)
                 if (first + timedelta(days=index)).weekday() < 5]
        full = {"rows": [{"date": day, "open": 100, "close": 101, "high": 102, "low": 99} for day in dates]}
        key = f"evaluation:{code}:{start}:{end}"
        research_store.cache_put(key, {"rows": full["rows"][:-1]})
        with database.connection() as db:
            db.execute("UPDATE research_cache SET updated_at=? WHERE cache_key=?",
                       ((datetime.now(database.CHINA_TZ) - timedelta(seconds=120)).isoformat(), key))
        with (patch.object(FeedbackPriceClient, "history", return_value=full) as fetch,
              patch.object(FeedbackPriceClient, "known_calendar", return_value=tuple(dates))):
            self.assertEqual(research_data.index_history(code, start, end), full)
            with database.connection() as db:
                db.execute("UPDATE research_cache SET updated_at=? WHERE cache_key=?",
                           ((datetime.now(database.CHINA_TZ) - timedelta(seconds=1800)).isoformat(), key))
            self.assertEqual(research_data.index_history(code, start, end), full)
            fetch.assert_called_once()
