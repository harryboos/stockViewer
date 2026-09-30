from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, call, patch

import pandas as pd

from backend import concept_data, data_sources, database
from backend.data_sources import MarketDataService
from backend.eastmoney import CONCEPT_FIELD_MAP
from backend.forecast_prices import FeedbackPriceClient
from backend.test_concept_ai import evidence_fixture


CALENDAR = ["2026-09-21", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08"]


def now(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=database.CHINA_TZ)


class ForecastCompletedSessionTests(unittest.TestCase):
    def test_forecast_uses_latest_completed_session_until_daily_close_is_available(self):
        with patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR):
            for instant in ("2026-09-30T09:30:00", "2026-09-30T13:00:00", "2026-09-30T15:09:59"):
                with self.subTest(instant=instant):
                    note = concept_data.concept_snapshot_warning("20260929", "2026-09-29T16:00:00+08:00",
                                forecast=True, now=now(instant), quote_as_of="2026-09-29T15:00:00+08:00")
                    self.assertIn("不含当日盘中变化", note)
                    self.assertIn("2026-09-29", note)
            with self.assertRaisesRegex(RuntimeError, "2026-09-30"):
                concept_data.concept_snapshot_warning("20260929", "2026-09-29T16:00:00+08:00",
                                                     forecast=True, now=now("2026-09-30T15:10:00"))

    def test_daily_recommendations_do_not_take_the_forecast_close_fallback(self):
        with patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR):
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                concept_data.concept_snapshot_warning("20260929", "2026-09-29T16:00:00+08:00",
                                                     now=now("2026-09-30T10:00:00"))

    def test_old_session_and_intraday_quotes_cannot_masquerade_as_closing_snapshot(self):
        with patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR):
            for session, fetched, quoted in (
                ("20260928", "2026-09-29T16:00:00+08:00", "2026-09-28T15:00:00+08:00"),
                ("20260929", "2026-09-29T14:00:00+08:00", None),
                ("20260929", "2026-09-29T16:00:00+08:00", "2026-09-29T14:00:00+08:00"),
            ):
                with self.subTest(session=session, fetched=fetched, quoted=quoted), self.assertRaisesRegex(RuntimeError, "旧缓存"):
                    concept_data.concept_snapshot_warning(session, fetched, quote_as_of=quoted,
                                                         forecast=True, now=now("2026-09-30T10:00:00"))

    def test_after_close_requires_same_day_closing_data_not_old_intraday_cache(self):
        for fetched, quoted in (("2026-09-30T14:00:00+08:00", None),
                                ("2026-09-30T16:00:00+08:00", "2026-09-30T14:00:00+08:00")):
            with self.subTest(fetched=fetched, quoted=quoted), self.assertRaisesRegex(RuntimeError, "当日已收盘"):
                concept_data.concept_snapshot_warning("20260930", fetched, quote_as_of=quoted,
                                                     forecast=True, now=now("2026-09-30T16:00:00"))


class ForecastSnapshotRecoveryTests(unittest.TestCase):
    def setUp(self):
        for target in (patch.object(database, "now_iso", return_value="2026-09-30T16:00:00+08:00"),
                       patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR)):
            target.start()
            self.addCleanup(target.stop)
        board = {**evidence_fixture()["candidates"][0], "kind": "concept"}
        self.old = {"tradeDate": "20260921", "updatedAt": "2026-09-21T16:00:00+08:00",
                    "conceptBoards": [board], "warnings": []}
        self.fresh = {**self.old, "tradeDate": "20260930", "updatedAt": database.now_iso(),
                      "quoteAsOf": "2026-09-30T15:39:30+08:00"}

    def test_stale_memory_snapshot_automatically_refreshes_once_before_prediction(self):
        with (patch.object(concept_data.market_data, "sector_overview", side_effect=[self.old, self.fresh]) as refresh,
              patch.object(concept_data.ConceptResearchClient, "daily_history", return_value=[]),
              patch.object(concept_data.ConceptResearchClient, "strong_stocks", return_value=[])):
            result = concept_data.collect_concept_evidence(forecast=True)
        self.assertEqual(refresh.call_args_list, [call(False), call(True)])
        self.assertEqual(result["tradeDate"], "20260930")
        self.assertEqual(result["quoteAsOf"], self.fresh["quoteAsOf"])
        self.assertEqual(result["candidates"][0]["evidence"][0]["publishedAt"], self.fresh["quoteAsOf"])

    def test_already_failed_refresh_is_not_repeated_or_relabeled_as_current(self):
        stale = {**self.old, "refreshStatus": "failed", "refreshError": "行业与概念板块行情源均未返回有效数据"}
        original = copy.deepcopy(stale)
        with (patch.object(concept_data.market_data, "sector_overview", return_value=stale) as refresh,
              patch.object(concept_data.ConceptResearchClient, "daily_history") as prices):
            with self.assertRaisesRegex(RuntimeError, "2026-09-21.*2026-09-30.*行情源"):
                concept_data.collect_concept_evidence(forecast=True)
        refresh.assert_called_once_with(False)
        prices.assert_not_called()
        self.assertEqual(stale, original)

    def test_a_failed_forced_refresh_stops_without_retry_loop(self):
        with patch.object(concept_data.market_data, "sector_overview", return_value=self.old) as refresh:
            with self.assertRaisesRegex(RuntimeError, "已自动尝试更新"):
                concept_data._concept_overview(True)
        self.assertEqual(refresh.call_args_list, [call(False), call(True)])


class SectorQuoteTimestampTests(unittest.TestCase):
    def setUp(self):
        self.service = MarketDataService()
        self.instant = now("2026-09-30T16:00:00")
        self.snapshot = [{"symbol": "600001", "name": "测试股票", "tradeDate": "20260929", "close": 12.3, "pctChg": 3}]
        self.patches = [patch.object(self.service, "market_snapshot", return_value=self.snapshot),
                        patch.object(self.service, "_cached_json", return_value=None),
                        patch.object(database, "set_meta"),
                        patch.object(database, "now_iso", return_value=self.instant.isoformat()),
                        patch.object(data_sources, "datetime", wraps=datetime),
                        patch("backend.research_store.cache_put"), patch("backend.research_store.save_universe")]
        self.mocks = [target.start() for target in self.patches]
        self.mocks[4].now.return_value = self.instant
        for target in self.patches:
            self.addCleanup(target.stop)

    def test_board_source_date_overrides_old_stock_snapshot_and_force_propagates(self):
        quote = now("2026-09-30T15:39:30")
        board = {"板块代码": "BK1001", "板块名称": "测试概念", "涨跌幅": 3, "上涨家数": 2,
                 "下跌家数": 1, "领涨股票": "测试股票", "领涨股票-涨跌幅": 4, "行情时间": quote.timestamp()}
        with (patch.object(self.service, "_industry_name_frame", return_value=(pd.DataFrame(), "行业源")),
              patch.object(self.service, "_concept_name_frame", return_value=(pd.DataFrame([board]), "概念源")),
              patch.object(self.service, "_sector_fund_flow_frame", return_value=(pd.DataFrame(), "资金源")),
              patch.object(self.service, "_enrich_board_turnover", return_value=([], 0))):
            result = self.service.sector_overview(True)
        self.mocks[0].assert_called_once_with(force=True)
        self.assertEqual(CONCEPT_FIELD_MAP["行情时间"], "f124")
        self.assertEqual(result["tradeDate"], "20260930")
        self.assertEqual(result["quoteAsOf"], quote.isoformat())
        self.assertIsNone(result["conceptBoards"][0]["leaders"][0]["price"])
        self.assertEqual(result["conceptBoards"][0]["leaders"][0]["pctChg"], 4)
        self.assertEqual(result["refreshStatus"], "partial")
        self.assertIn("行业", result["refreshError"])

    def test_source_outage_preserves_dates_and_records_failed_attempt_separately(self):
        cached = {"tradeDate": "20260921", "updatedAt": "2026-09-21T16:00:00+08:00", "warnings": ["原有提示"]}
        self.mocks[1].return_value = cached
        with (patch.object(self.service, "_industry_name_frame", side_effect=RuntimeError("industry offline")),
              patch.object(self.service, "_concept_name_frame", side_effect=RuntimeError("concept offline")),
              self.assertLogs("backend.data_sources", level="WARNING") as logs):
            result = self.service.sector_overview(True)
        self.assertEqual(result["updatedAt"], cached["updatedAt"])
        self.assertEqual(result["tradeDate"], cached["tradeDate"])
        self.assertEqual(result["refreshStatus"], "failed")
        self.assertEqual(result["refreshAttemptedAt"], self.instant.isoformat())
        self.assertIn("industry offline", logs.output[0])
        self.assertIn("原有提示", result["warnings"])
        self.mocks[2].assert_not_called()
        self.assertNotIn("refreshStatus", cached)


class SectorRefreshJobStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = patch.object(database, "DATABASE_PATH", Path(directory.name) / "test.db")
        path.start()
        self.addCleanup(path.stop)
        database.initialize()

    def test_failed_or_partial_fetch_never_reports_board_refresh_success(self):
        from backend import research_jobs
        for state, message in (("failed", "行业与概念板块行情源均未返回有效数据，已保留最近成功快照"),
                               ("partial", "概念板块行情源暂未返回有效数据")):
            token = research_jobs._claim("sectors")
            with self.subTest(state=state), patch("backend.data_sources.market_data.sector_overview", return_value={
                "tradeDate": "20260921", "refreshStatus": state, "refreshError": message,
            }) as refresh:
                research_jobs._run("sectors", token)
            status = research_jobs.job_status("sectors")
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"], message)
            self.assertEqual(status["progress"]["done"], 0)
            self.assertNotEqual(status["progress"]["stage"], "板块行情已更新")
            refresh.assert_called_once_with(True)

    def test_successful_retry_clears_previous_failure_and_reports_completion(self):
        from backend import research_jobs
        for response in ({"refreshStatus": "failed"}, {"refreshStatus": "succeeded", "tradeDate": "20260930"}):
            token = research_jobs._claim("sectors")
            with patch("backend.data_sources.market_data.sector_overview", return_value=response):
                research_jobs._run("sectors", token)
        status = research_jobs.job_status("sectors")
        self.assertEqual(status["status"], "succeeded")
        self.assertIsNone(status["error"])
        self.assertEqual(status["progress"]["done"], 1)
        self.assertEqual(status["progress"]["stage"], "板块行情已更新")
