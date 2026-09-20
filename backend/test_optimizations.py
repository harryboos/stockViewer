from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from backend import database, forecast_feedback as feedback, forecast_history as history
from backend import research_data, research_jobs, research_pool, research_store, selection_history
from backend.test_forecast_feedback import instant, outcome, report_fixture
from backend.test_forecast_feedback import calendar_fixture, prices


class ReadOptimizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = patch.object(database, 'DATABASE_PATH', Path(temporary.name) / 'test.sqlite3')
        target.start()
        self.addCleanup(target.stop)
        database.initialize()

    def test_history_pagination_reads_only_primary_and_visible_versions(self):
        for version in range(20):
            with patch.object(database, 'now_iso', return_value='2026-09-12T18:00:00+08:00'):
                token = database.start_ai_run('forecast:glm', 'glm-5.3', '2026-09-12', 'test', True)
                database.finish_ai_run('forecast:glm', '2026-09-12', report_fixture()['result'], None, token)
            value = outcome(108 if version == 0 else 120)
            with database.connection() as db:
                db.execute('INSERT INTO forecast_feedback VALUES (?,?,?,?,?)',
                           (version + 1, 'BK1001', 15, json.dumps(value), database.now_iso()))
        with patch.object(history, '_report', wraps=history._report) as decode:
            data = history.history_payload(page=2, page_size=5, as_of=instant('2026-10-20T18:00:00'))
        self.assertEqual([row['id'] for row in data['reports']], [15, 14, 13, 12, 11])
        self.assertEqual(decode.call_count, 6)  # Five visible versions plus the first report for statistics.
        self.assertEqual(data['totalReports'], 20)
        self.assertEqual(data['forecastDays'], 1)
        self.assertEqual(data['summaries']['15']['averageReturnPct'], 8)
        self.assertTrue(all(not row['includedInStats'] for row in data['reports']))
        self.assertNotIn('path', data['reports'][0]['concepts'][0]['outcomes']['15'])

    def test_digest_never_builds_unused_selection_statistics_or_rotation_rankings(self):
        with database.connection() as db:
            for index in range(12):
                research_store.archive_selection(db, '2026-09-12', f'rule:{index}', str(index),
                                                 '2026-09-12T18:00:00+08:00', [{'code': '600519', 'name': '股票'}])
        with (patch.object(selection_history, 'history_payload', side_effect=AssertionError('unnecessary full history')),
              patch.object(research_data, 'rotation_payload', side_effect=AssertionError('unused rankings'))):
            data = research_jobs.daily_digest()
        self.assertEqual([row['name'] for row in data['recentSelections']], ['11', '10', '9', '8', '7'])

    def test_status_only_reads_do_not_decode_reports(self):
        token = database.start_ai_run('glm', 'glm-5.3', database.china_date(), 'test')
        database.finish_ai_run('glm', database.china_date(), {'summary': 'large report'}, None, token)
        with patch.object(database, 'decode_result', side_effect=AssertionError('unnecessary report decoding')):
            status = database.read_ai_run('glm', database.china_date(), include_result=False)
        self.assertEqual(status['status'], 'succeeded')
        self.assertIsNone(status['result'])
        self.assertNotIn('resultJson', status)
        self.assertEqual(database.read_ai_run('glm', database.china_date())['result']['summary'], 'large report')

    def test_news_cache_does_not_require_reading_profile_or_archives(self):
        with (patch.object(research_data, 'stock_profile', side_effect=AssertionError('unnecessary profile')),
              patch.object(research_data.ConceptResearchClient, 'news', return_value=[{'title': 'test'}]) as news):
            first = research_data.stock_news('600519')
            with patch.object(research_data, 'stock_basic', side_effect=AssertionError('cache hit')):
                self.assertEqual(research_data.stock_news('600519'), first)
        news.assert_called_once()

    def test_one_benchmark_outage_does_not_discard_healthy_concept_feedback(self):
        with patch.object(database, 'now_iso', return_value='2026-09-12T18:00:00+08:00'):
            token = database.start_ai_run('forecast:glm', 'glm-5.3', '2026-09-12', 'test')
            database.finish_ai_run('forecast:glm', '2026-09-12', report_fixture()['result'], None, token)
        def fetch(code, start, end):
            if code == 'sh000001':
                raise RuntimeError('one source failed')
            return prices(112 if code == 'sh000688' else 108)
        token = feedback.claim_refresh()
        with (patch.object(feedback, 'reports_to_refresh', return_value=[report_fixture()]),
              patch.object(feedback.FeedbackPriceClient, 'calendar', return_value=calendar_fixture()),
              patch.object(feedback.FeedbackPriceClient, 'history', side_effect=fetch),
              patch.object(feedback, 'datetime') as clock):
            clock.now.return_value = instant('2026-10-20T18:00:00')
            clock.combine = datetime.combine
            clock.fromisoformat = datetime.fromisoformat
            feedback.refresh_feedback(token)
        with database.connection() as db:
            value = json.loads(db.execute('SELECT result_json FROM forecast_feedback WHERE horizon_days=15').fetchone()[0])
        self.assertEqual(value['returnPct'], 8)
        self.assertIsNone(value['benchmarks']['sh000001']['returnPct'])
        self.assertEqual(value['benchmarks']['sh000688']['returnPct'], 12)
        self.assertEqual(history.refresh_status()['status'], 'failed')


class WorkerPoolTests(unittest.TestCase):
    def test_repeated_timeouts_cannot_create_unbounded_workers_or_queues(self):
        release = threading.Event()
        fetch = Mock(side_effect=lambda item: release.wait(2))
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(research_pool, '_pool', pool), patch.object(research_pool, '_capacity', threading.BoundedSemaphore(2)):
                try:
                    for _ in range(5):
                        self.assertEqual(research_pool.fetch_batch(list(range(20)), fetch, seconds=0.02), {})
                    self.assertEqual(fetch.call_count, 2)
                finally:
                    release.set()
                    pool.shutdown(wait=True)

    def test_one_failure_preserves_healthy_items_and_duplicates_fetch_once(self):
        def fetch(item):
            if item == 2:
                raise RuntimeError('source unavailable')
            return item * 2
        work = Mock(side_effect=fetch)
        self.assertEqual(research_pool.fetch_batch([1, 2, 1, 3], work), {1: 2, 3: 6})
        self.assertEqual(work.call_count, 3)

    def test_feedback_releases_guard_even_if_final_status_write_fails(self):
        lock = threading.Lock()
        with (patch.object(feedback, '_refresh_lock', lock), patch.object(feedback, 'reports_to_refresh', return_value=[]),
              patch.object(database, 'connection', side_effect=RuntimeError('database unavailable'))):
            with self.assertRaises(RuntimeError):
                feedback.refresh_feedback('token')
        self.assertFalse(lock.locked())


class FeedbackDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_during_slow_claim_still_dispatches_once(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def claim():
            started.set()
            release.wait(2)
            return 'token'
        with (patch.object(feedback, '_start', None), patch.object(feedback, '_tasks', set()),
              patch.object(feedback, 'reports_to_refresh', return_value=[{}]),
              patch.object(feedback, 'claim_refresh', side_effect=claim),
              patch.object(feedback, 'refresh_status', return_value={'status': 'running'}),
              patch.object(feedback, 'refresh_feedback', side_effect=lambda token: finished.set()) as worker):
            caller = asyncio.create_task(feedback.start_feedback_refresh())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                release.set()
                await feedback._start
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
                await asyncio.gather(*list(feedback._tasks))
                worker.assert_called_once_with('token')
            finally:
                release.set()
