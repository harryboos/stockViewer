from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend import ai, database, main, research_data, research_jobs, research_store
from backend import concept_comparison as comparison, selection_history as selections
from backend.forecast_history import get_report
from backend.forecast_prices import CALENDAR_KEY
from backend.test_forecast_feedback import calendar_fixture, instant, prices, report_fixture


class ResearchDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = patch.object(database, 'DATABASE_PATH', Path(temporary.name) / 'research.sqlite3')
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        database.set_meta(CALENDAR_KEY, json.dumps({'dates': calendar_fixture(), 'fetchedOn': database.china_date()}))

    def archive(self, *, legacy=False):
        report = report_fixture()['result']
        if not legacy:
            report['comparisonUniverse'] = {'asOf': '2026-09-12T17:00:00+08:00', 'scope': '预测时冻结的概念范围',
                'boards': [{'code': 'BK1001', 'name': '预测概念'}, {'code': 'BK1002', 'name': '同期赢家'},
                           {'code': 'BK1003', 'name': '尚缺行情'}]}
        with patch.object(database, 'now_iso', return_value='2026-09-12T18:00:00+08:00'):
            token = database.start_ai_run('forecast:glm', 'glm-5.3', '2026-09-12', 'v1', True)
            database.finish_ai_run('forecast:glm', '2026-09-12', report, None, token)
        with database.connection() as db:
            return db.execute('SELECT MAX(id) FROM forecast_reports').fetchone()[0]

    def save_comparison(self, report, code, change, entry='2026-09-14', end='2026-09-24', days=15):
        result = {'status': 'completed', 'returnPct': change, 'entryDate': entry, 'exitDate': end,
                  'entryPrice': 100, 'exitPrice': 100 + change, 'source': 'test', 'url': 'https://example.org'}
        with database.connection() as db:
            db.execute('INSERT OR REPLACE INTO comparison_outcomes VALUES (?,?,?,?,?)',
                       (report, code, days, json.dumps(result), database.now_iso()))

    def test_watch_notes_and_original_reference_survive_restart_and_duplicate_add(self):
        code = '600900.SH'
        self.assertTrue(database.add_watch_stock(code))
        database.upsert_quotes([{'tsCode': code, 'tradeDate': '20260917', 'close': 20,
                                'source': 'test', 'fetchedAt': database.now_iso()}])
        research_store.capture_reference(code)
        research_store.update_watch(code, ' 长期观察 ', ' 估值观察 ', '关注订单')
        self.assertFalse(database.add_watch_stock(code))
        database.initialize()
        row = next(item for item in database.get_watchlist_rows() if item['tsCode'] == code)
        self.assertEqual((row['groupName'], row['reason'], row['note']), ('长期观察', '估值观察', '关注订单'))
        self.assertEqual(row['referencePrice'], 20)
        with patch.object(main.market_data, 'refresh_quotes'), patch.object(research_store, 'capture_reference') as capture:
            main.add_watchlist(main.WatchlistInput(tsCode=code))
        capture.assert_not_called()

    def test_old_watchlist_never_gets_a_fabricated_reference(self):
        with database.connection() as db:
            db.execute("UPDATE watchlist SET added_at='2025-01-01T10:00:00+08:00'")
        database.upsert_quotes([{'tsCode': '600519.SH', 'tradeDate': '20260917', 'close': 100,
                                'source': 'test', 'fetchedAt': database.now_iso()}])
        research_store.capture_reference('600519.SH')
        self.assertIsNone(database.get_watchlist_rows()[0]['referencePrice'])

    def test_first_daily_selection_is_immutable_and_failed_rerun_keeps_success(self):
        day = database.china_date()
        first = {'picks': [{'code': '600519', 'name': '贵州茅台', 'reason': '最初理由'}]}
        token = database.start_ai_run('glm', 'glm-5.3', day, 'v1')
        database.finish_ai_run('glm', day, first, None, token)
        retry = database.start_ai_run('glm', 'glm-5.3', day, 'v1', True)
        database.finish_ai_run('glm', day, None, '暂时失败', retry)
        self.assertEqual(database.read_ai_run('glm', day)['previousResult'], first)
        retry = database.start_ai_run('glm', 'glm-5.3', day, 'v1', True)
        database.finish_ai_run('glm', day, {'picks': []}, None, retry)
        database.initialize()
        payload = selections.history_payload()
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['entries'][0]['picks'][0]['reason'], '最初理由')

    def test_rules_keep_first_success_across_force_and_version_changes(self):
        day = database.china_date()
        result = [{'runDate': day, 'id': 'momentum', 'name': '动量', 'picks': [{'code': '600519'}]}]
        database.save_strategy_run(day + ':v6', '20260917', result, 'fixture')
        result[0]['picks'] = []
        database.save_strategy_run(day + ':v7', '20260917', result, 'fixture')
        self.assertEqual(selections.history_payload()['entries'][0]['picks'][0]['code'], '600519')

    def test_frozen_universe_and_exact_dates_exclude_lookahead_and_misaligned_winners(self):
        report = self.archive()
        self.save_comparison(report, 'BK1001', 8)
        self.save_comparison(report, 'BK1002', 12)
        self.save_comparison(report, 'BK1003', 99, end='2026-09-23')
        self.save_comparison(report, 'BK9999', 300)
        research_store.save_universe([{'code': 'BK9999', 'name': '后来新概念'}], database.now_iso())
        result = comparison.comparison_payload(report, 15, instant('2026-10-20T18:00:00'))
        self.assertEqual(result['strongest']['code'], 'BK1002')
        self.assertEqual((result['coveredCount'], result['totalCount'], result['fullCoverage']), (2, 3, False))
        self.assertEqual(result['selected'][0]['gapPct'], -4)
        self.assertEqual(result['selected'][0]['rank'], 2)
        self.assertEqual(result['averageSelectedReturn'], 8)
        self.assertEqual((result['entryDate'], result['exitDate']), ('2026-09-14', '2026-09-24'))

    def test_all_negative_is_still_ranked_and_missing_does_not_become_zero(self):
        report = self.archive()
        self.save_comparison(report, 'BK1001', -8)
        self.save_comparison(report, 'BK1002', -2)
        result = comparison.comparison_payload(report, 15, instant('2026-10-20T18:00:00'))
        self.assertEqual(result['strongest']['returnPct'], -2)
        self.assertEqual(result['averageSelectedReturn'], -8)
        self.assertEqual(result['coveredCount'], 2)

    def test_legacy_pool_is_explicitly_labelled_and_frozen_after_first_backfill(self):
        report = get_report(self.archive(legacy=True))
        self.assertIsNone(comparison.universe_for(report))
        research_store.save_universe([{'code': 'BK1001', 'name': '原概念'}], database.now_iso())
        first = comparison.universe_for(report, create=True)
        research_store.save_universe([{'code': 'BK9999', 'name': '新概念'}], database.now_iso())
        self.assertEqual(comparison.universe_for(report, create=True), first)
        self.assertIn('范围差异', first['scope'])

    def test_same_windows_are_separate_for_fifteen_and_thirty_days(self):
        report = self.archive()
        self.save_comparison(report, 'BK1002', 12)
        self.save_comparison(report, 'BK1001', 25, end='2026-10-12', days=30)
        now = instant('2026-10-20T18:00:00')
        self.assertEqual(comparison.comparison_payload(report, 15, now)['strongest']['code'], 'BK1002')
        monthly = comparison.comparison_payload(report, 30, now)
        self.assertEqual(monthly['strongest']['code'], 'BK1001')
        self.assertEqual(monthly['exitDate'], '2026-10-12')

    def test_batch_refresh_preserves_completed_prices_on_provider_revision(self):
        report = self.archive()
        self.save_comparison(report, 'BK1001', 8)
        with patch.object(comparison.FeedbackPriceClient, 'calendar', return_value=calendar_fixture()), \
             patch.object(comparison, 'index_history', return_value=prices(150)), \
             patch.object(comparison, 'datetime') as clock:
            clock.now.return_value = instant('2026-10-20T18:00:00')
            clock.combine = datetime.combine
            clock.fromisoformat = datetime.fromisoformat
            comparison.refresh_comparisons(Mock())
        with database.connection() as db:
            value = json.loads(db.execute('SELECT result_json FROM comparison_outcomes WHERE report_id=? AND code=? AND horizon_days=15',
                                         (report, 'BK1001')).fetchone()[0])
        self.assertEqual(value['returnPct'], 8)

    def test_shared_history_is_fetched_once_for_concurrent_readers(self):
        started, release = threading.Event(), threading.Event()
        def fetch():
            started.set()
            self.assertTrue(release.wait(2))
            return {'rows': [1]}
        fetcher = Mock(side_effect=fetch)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(research_data.shared_read, 'same-history', fetcher) for _ in range(4)]
            self.assertTrue(started.wait(2))
            release.set()
            self.assertEqual([future.result() for future in futures], [{'rows': [1]}] * 4)
        fetcher.assert_called_once()

    def test_empty_market_response_does_not_poison_history_cache(self):
        fetch = Mock(side_effect=[{'rows': []}, {'rows': [1]}])
        research_data.shared_read('retry-history', fetch)
        self.assertEqual(research_data.shared_read('retry-history', fetch), {'rows': [1]})
        self.assertEqual(fetch.call_count, 2)

    def test_read_only_routes_accept_period_queries_and_never_generate_ai(self):
        report = self.archive()
        client = TestClient(main.app)  # No startup scheduling or network in these API contract tests.
        with patch.object(main, 'run_daily_ai') as run, patch.object(research_data, 'index_history') as prices_call, \
             patch.object(research_data.market_data, 'market_snapshot') as snapshot:
            for path in ['/api/research/rotation', '/api/research/selections?sessions=5',
                         f'/api/research/comparison?report_id={report}&days=15', '/api/research/digest',
                         '/api/research/jobs?key=comparison', '/api/research/stock?code=600519&part=profile']:
                response = client.get(path)
                self.assertEqual(response.status_code, 200, (path, response.text))
            self.assertEqual(client.get('/api/research/selections?sessions=7').status_code, 422)
            self.assertEqual(client.get(f'/api/research/comparison?report_id={report}&days=20').status_code, 422)
        run.assert_not_called()
        prices_call.assert_not_called()
        snapshot.assert_not_called()

    def test_watch_note_api_validation_and_removed_stock(self):
        client = TestClient(main.app)
        self.assertEqual(client.patch('/api/watchlist', json={'tsCode': '600519.SH', 'note': 'a' * 4001}).status_code, 422)
        self.assertEqual(client.patch('/api/watchlist', json={'tsCode': '600519.SH', 'note': '关注订单'}).status_code, 200)
        database.remove_watch_stock('600519.SH')
        self.assertEqual(client.patch('/api/watchlist', json={'tsCode': '600519.SH', 'note': '已移除'}).status_code, 404)

    def test_initial_watchlist_snapshot_does_not_wait_for_market_network(self):
        with patch.object(main.market_data, 'refresh_quotes') as fetch:
            response = TestClient(main.app).get('/api/watchlist?cached_only=true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['stocks']), len(database.DEFAULT_WATCHLIST))
        fetch.assert_not_called()

    def test_legacy_selections_are_labelled_without_overwriting_new_first_success(self):
        day = database.china_date()
        new = [{'runDate': day, 'id': 'momentum', 'name': '动量', 'picks': []}]
        database.save_strategy_run(day + ':v6', day.replace('-', ''), new, 'test')
        with database.connection() as db:
            db.execute('INSERT INTO strategy_runs VALUES (?,?,?,?,?)',
                       ('2026-08-28:v6', '20260828', json.dumps([{**new[0], 'runDate': '2026-08-28'}]),
                        'test', '2026-08-28T18:00:00+08:00'))
        database.initialize()
        entries = selections.history_payload()['entries']
        self.assertEqual(entries[0]['origin'], 'first_success')
        self.assertEqual(entries[1]['origin'], 'retained_cache')

    def test_rotation_ranks_use_only_same_date_and_same_population(self):
        def row(code, change, before, day='2026-09-16'):
            return {'code': code, 'name': code, 'asOf': day, 'path': [],
                    'returns': dict.fromkeys(('5', '10', '20'), change),
                    'previousReturns': dict.fromkeys(('5', '10', '20'), before)}
        research_store.cache_put('rotation', {'asOf': '2026-09-16', 'totalCount': 3,
            'items': [row('BK1', 10, 2), row('BK2', 5, 12), row('BK3', 99, 99, '2026-09-15')]})
        result = research_data.rotation_payload()
        self.assertEqual(result['coveredCount'], 2)
        first = next(item for item in result['items'] if item['code'] == 'BK1')
        self.assertEqual(first['ranks']['5'], 1)
        self.assertEqual(first['rankChanges']['5'], 1)

    def test_retention_preserves_archives_and_last_success_but_bounds_caches_and_logs(self):
        report = self.archive()
        old = (datetime.now(database.CHINA_TZ) - timedelta(days=40)).isoformat()
        with database.connection() as db:
            db.execute('INSERT INTO research_cache VALUES (?,?,?)', ('stock-news:600519', '{}', old))
            db.execute('INSERT INTO research_cache VALUES (?,?,?)', ('latest-market', '{"saved":true}', old))
            db.execute("INSERT INTO ai_attempts(token,provider,started_at,status) VALUES ('old','glm',?,'failed')", (old,))
        database.maintain_storage(force=True)
        self.assertIsNone(research_store.cache_get('stock-news:600519'))
        self.assertEqual(research_store.cache_get('latest-market'), {'saved': True})
        self.assertIsNotNone(get_report(report))
        with database.connection() as db:
            self.assertIsNone(db.execute("SELECT * FROM ai_attempts WHERE token='old'").fetchone())


class SelectionEvaluationTests(unittest.TestCase):
    def report(self, published='2026-09-12T18:00:00+08:00'):
        return {'published_at': published}

    def test_horizon_counts_trading_sessions_and_skips_intraday_publication(self):
        data = prices()['rows']
        for row in data:
            if row['date'] == '2026-09-21':
                row.update(close=110, high=111)
        result = selections.selection_outcome(self.report('2026-09-14T10:45:00+08:00'), 5, data, {},
                                             calendar_fixture(), instant('2026-10-20T18:00:00'))
        self.assertEqual((result['entryDate'], result['exitDate']), ('2026-09-15', '2026-09-21'))
        self.assertEqual(result['returnPct'], 10)
        self.assertEqual(result['status'], 'completed')

    def test_missing_stock_session_does_not_become_complete_or_zero(self):
        rows = [row for row in prices()['rows'] if row['date'] != '2026-09-15']
        result = selections.selection_outcome(self.report(), 5, rows, {}, calendar_fixture(), instant('2026-10-20T18:00:00'))
        self.assertEqual(result['status'], 'missing_data')
        self.assertIsNone(result['returnPct'])

    def test_incomplete_period_is_tracking_and_benchmarks_use_exact_endpoints(self):
        result = selections.selection_outcome(self.report(), 10, prices()['rows'], {'sh000001': prices(103)},
                                             calendar_fixture(), instant('2026-09-24T18:00:00'))
        self.assertEqual(result['status'], 'tracking')
        self.assertEqual(result['returnPct'], 8)
        self.assertEqual(result['benchmarks']['sh000001']['excessPct'], 5)
        self.assertIsNone(result['benchmarks']['sh000688']['returnPct'])

    def test_short_calendar_cannot_invent_start_session(self):
        result = selections.selection_outcome(self.report(), 5, prices()['rows'], {}, calendar_fixture()[15:],
                                             instant('2026-10-20T18:00:00'))
        self.assertEqual(result['status'], 'missing_data')
        self.assertIsNone(result['returnPct'])


class ResearchJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_http_request_still_hands_off_once_after_durable_claim(self):
        claimed, release = threading.Event(), threading.Event()
        def claim(key):
            claimed.set()
            release.wait(2)
            return 'token'
        research_jobs._tasks.clear()
        research_jobs._starts.clear()
        with patch.object(research_jobs, '_claim', side_effect=claim) as claim_call, \
             patch.object(research_jobs, '_run') as run, \
             patch.object(research_jobs, 'job_status', return_value={'status': 'running'}):
            first = asyncio.create_task(research_jobs.start_job('rotation'))
            self.assertTrue(await asyncio.to_thread(claimed.wait, 2))
            second = asyncio.create_task(research_jobs.start_job('rotation'))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertEqual(await second, {'status': 'running'})
            await research_jobs._tasks['rotation']
        claim_call.assert_called_once()
        run.assert_called_once_with('rotation', 'token')

    async def test_failed_only_retries_only_failed_provider(self):
        current = {'runs': [{'provider': 'glm', 'status': 'succeeded'}, {'provider': 'qwen', 'status': 'failed'},
                            {'provider': 'deepseek', 'status': 'pending'}]}
        with patch.object(ai, 'get_daily_ai_runs', return_value=current), \
             patch.object(ai, 'candidate_snapshot', return_value=[{}, {}, {}]), \
             patch.object(ai, '_execute_provider', new_callable=AsyncMock) as execute:
            await ai._run_daily_ai(False, failed_only=True)
        self.assertEqual(execute.await_count, 1)
        self.assertEqual(execute.call_args.args[0], 'qwen')

    async def test_explicit_provider_is_not_swallowed_by_another_running_provider(self):
        ai._scoped_tasks.clear()
        started = asyncio.Event()
        release = asyncio.Event()
        async def work(force, provider, failed_only):
            if provider == 'glm':
                started.set()
                await release.wait()
            return {'provider': provider}
        with patch.object(ai, '_run_daily_ai', side_effect=work):
            first = asyncio.create_task(ai.run_daily_ai(provider='glm'))
            await started.wait()
            self.assertEqual(await ai.run_daily_ai(provider='qwen'), {'provider': 'qwen'})
            release.set()
            await first

    async def test_default_daily_scheduler_never_starts_paid_ai(self):
        with patch.dict('os.environ', {'ENABLE_SCHEDULED_AI': 'false'}), \
             patch.object(main, 'calculate_public_strategies') as calculate, \
             patch.object(main, 'run_daily_ai', new_callable=AsyncMock) as run:
            await main.run_daily_bundle()
        calculate.assert_called_once_with(False)
        run.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
