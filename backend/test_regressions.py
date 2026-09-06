from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pandas as pd
from fastapi.testclient import TestClient

from backend import ai, database
from backend.data_sources import MarketDataService
from backend.eastmoney import EastmoneyClient
from backend.main import app
from backend.strategy_factors import build_factor_row, percentile, top_picks
from backend.tencent import TencentClient


class DataRegressionTests(unittest.TestCase):
    def test_empty_first_page_does_not_trigger_thousands_of_requests(self) -> None:
        client = EastmoneyClient()
        with (patch.object(client, "_session", return_value=Mock()),
              patch.object(client, "_request_page", return_value={"total": 6000, "diff": []}) as request):
            with self.assertRaisesRegex(RuntimeError, "没有返回行情"):
                client.fetch_pages("82", {})
        request.assert_called_once()

    def test_eastmoney_comparison_aligns_both_days_and_exchanges(self) -> None:
        points = [
            ("2026-09-04", "10:00", 10), ("2026-09-07", "10:00", 15),
            ("2026-09-07", "10:01", 5),
        ]
        pair = EastmoneyClient.market_intraday_pair_from_points(
            {"1.000001": points, "0.399106": points}, "20260907", "10:01"
        )
        self.assertEqual(pair["comparisonTime"], "10:00")
        self.assertEqual(pair["currentTurnover"], 30)
        self.assertEqual(pair["previousTurnover"], 20)

    def test_tencent_comparison_sorts_and_aligns_both_days(self) -> None:
        client = TencentClient()
        days = [{"date": "20260904", "data": ["1000 0 0 10"]},
                {"date": "20260907", "data": ["1001 0 0 20", "1000 0 0 15"]}]
        with patch.object(client, "_index_days", return_value=days):
            pair = client.index_intraday_turnover_pair("20260907", "10:01")
        self.assertEqual(pair["comparisonTime"], "10:00")
        self.assertEqual(pair["currentTurnover"], 30)
        self.assertEqual(pair["previousTurnover"], 20)

    def test_tencent_rejects_partial_market_totals(self) -> None:
        client = TencentClient()
        with patch.object(client, "_request_json", return_value={"total": 5000, "data": [{"zljlr": 1}]}):
            with self.assertRaisesRegex(RuntimeError, "不完整"):
                client.fetch_rank_rows()

    def test_empty_watchlist_never_fetches_market_data(self) -> None:
        service = MarketDataService()
        with patch.object(service, "market_snapshot") as fetch:
            self.assertEqual(service.refresh_quotes([]), {})
        fetch.assert_not_called()

    def test_catalog_failure_does_not_discard_successful_quotes(self) -> None:
        service = MarketDataService()
        rows = [{"tsCode": "600519.SH"}]
        with (patch.object(service, "market_snapshot", return_value=rows),
              patch.object(service, "sync_catalog", side_effect=RuntimeError("catalog unavailable")),
              patch.object(service, "_baostock_latest_quote") as fallback,
              patch.object(database, "upsert_stock_basics"),
              patch.object(database, "upsert_quotes"),
              patch.object(database, "get_quotes", return_value={"600519.SH": rows[0]})):
            result = service.refresh_quotes(["600519.SH"], force=True)
        self.assertEqual(result["600519.SH"], rows[0])
        fallback.assert_not_called()

    def test_trade_calendar_expires_on_next_china_date(self) -> None:
        service = MarketDataService()
        calendar = pd.DataFrame({"trade_date": [date(2026, 9, 4), date(2026, 9, 7)]})
        akshare = Mock()
        akshare.tool_trade_date_hist_sina.return_value = calendar
        with patch.object(service, "_akshare", return_value=akshare), patch("backend.data_sources.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 4, tzinfo=database.CHINA_TZ)
            self.assertEqual(service.latest_trade_date(), "20260904")
            self.assertEqual(service.latest_trade_date(), "20260904")
            clock.now.return_value = datetime(2026, 9, 7, tzinfo=database.CHINA_TZ)
            self.assertEqual(service.latest_trade_date(), "20260907")
        self.assertEqual(akshare.tool_trade_date_hist_sina.call_count, 2)

    def test_empty_market_response_activates_backoff(self) -> None:
        service = MarketDataService()
        with patch.object(service, "_spot_frame", return_value=(pd.DataFrame(), "source", "transport")) as fetch:
            with self.assertRaises(RuntimeError):
                service.market_snapshot()
            with self.assertRaisesRegex(RuntimeError, "等待自动重试"):
                service.market_snapshot()
        fetch.assert_called_once()

    def test_spot_lots_match_baostock_history_shares(self) -> None:
        service = MarketDataService()
        frame = pd.DataFrame([{"代码": "600519", "名称": "测试", "最新价": 10, "成交量": 1000}])
        with patch.object(service, "latest_trade_date", return_value="20260302"):
            spot = service._normalize_spot(frame, "AKShare")[0]
        history = [{"date": (date(2026, 1, 1) + timedelta(days=index)).strftime("%Y%m%d"),
                    "close": 10, "vol": 100000, "amount": 1000000, "pctChg": 0}
                   for index in range(60)]
        with patch("backend.strategy_factors.market_data.dividend_yield", return_value=None):
            factor = build_factor_row(spot, history)
        self.assertEqual(spot["vol"], 100000)
        self.assertEqual(factor["volumeTrend5To20"], 1)
        self.assertEqual(factor["volumeRatio"], 1)

    def test_history_failure_falls_back_only_for_missing_symbols(self) -> None:
        service = MarketDataService()
        history = [{"date": "20260904", "close": 10}]
        with (patch.object(service, "_baostock_session", return_value=nullcontext(Mock())),
              patch.object(service, "_query_baostock_history", side_effect=[history, []]),
              patch.object(service, "history", return_value=history) as fallback):
            result = service.history_batch(["600519", "000001"])
        self.assertEqual(result, {"600519": history, "000001": history})
        fallback.assert_called_once_with("000001", 420)

    def test_turnover_history_requires_both_exchanges_on_each_date(self) -> None:
        service = MarketDataService()
        akshare = Mock()
        akshare.stock_zh_index_daily_em.side_effect = [
            pd.DataFrame([{"date": "2026-09-04", "amount": 100}, {"date": "2026-09-07", "amount": 120}]),
            pd.DataFrame([{"date": "2026-09-04", "amount": 200}]),
        ]
        with (patch.object(service, "_akshare", return_value=akshare),
              patch.object(service, "_cached_json_any", return_value=None),
              patch.object(database, "set_meta")):
            rows, error = service._index_turnover_history()
        self.assertIsNone(error)
        self.assertEqual(rows, [{"date": "20260904", "turnover": 300}])

    def test_percentiles_handle_ties_and_nonfinite_values(self) -> None:
        rows = [{"value": 2}, {"value": 2}, {"value": 2}]
        for direction in (True, False):
            score = percentile(rows, "value", direction)
            self.assertEqual(score(rows[0]), 50)
            self.assertEqual(score({"value": float("nan")}), 40)
            self.assertEqual(score({"value": float("inf")}), 40)
        self.assertEqual(percentile([{ "value": 1 }, {"value": 2}], "value")({"value": -1}), 0)

    def test_ranking_uses_unrounded_scores_and_stable_ties(self) -> None:
        rows = [{"symbol": code, "name": code, "industry": "test", "score": score}
                for code, score in [("000002", 50.1), ("000001", 50.1), ("000003", 50.2)]]
        ranked = top_picks(rows, lambda row: row["score"], lambda _: "test")
        self.assertEqual([row["code"] for row in ranked], ["000003", "000001", "000002"])


class AiRunRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(temporary.name) / "test.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        env = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        database.initialize()

    async def test_no_configured_models_do_not_require_market_data(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(ai, "candidate_snapshot") as fetch:
            result = await ai.run_daily_ai(True)
        self.assertTrue(all(run["status"] == "not_configured" for run in result["runs"]))
        fetch.assert_not_called()

    async def test_successful_cached_runs_do_not_require_market_data(self) -> None:
        run_date = database.china_date()
        token = database.start_ai_run("deepseek", ai.model_for("deepseek"), run_date, ai.PROMPT_VERSION)
        database.finish_ai_run("deepseek", run_date, {"title": "cached"}, None, token)
        with patch.object(ai, "candidate_snapshot") as fetch:
            result = await ai.run_daily_ai()
        self.assertEqual(result["runs"][0]["status"], "succeeded")
        fetch.assert_not_called()

    async def test_cancelled_run_is_retryable(self) -> None:
        with patch.object(ai, "_call_compatible", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await ai._execute_provider("deepseek", [])
        self.assertEqual(ai.get_daily_ai_runs()["runs"][0]["status"], "failed")

    async def test_force_does_not_duplicate_an_active_model_call(self) -> None:
        started, finish = asyncio.Event(), asyncio.Event()

        async def response(*_args):
            started.set()
            await finish.wait()
            raise RuntimeError("test failure")

        with patch.object(ai, "_call_compatible", AsyncMock(side_effect=response)) as call:
            first = asyncio.create_task(ai._execute_provider("deepseek", [], True))
            await started.wait()
            try:
                second = await ai._execute_provider("deepseek", [], True)
                self.assertEqual(second["status"], "running")
                call.assert_awaited_once()
            finally:
                finish.set()
                await first

    async def test_concurrent_daily_requests_share_candidate_work(self) -> None:
        started, finish = asyncio.Event(), asyncio.Event()

        async def response(_force):
            started.set()
            await finish.wait()
            return {"runs": []}

        with patch.object(ai, "_run_daily_ai", AsyncMock(side_effect=response)) as run:
            first = asyncio.create_task(ai.run_daily_ai(True))
            await started.wait()
            second = asyncio.create_task(ai.run_daily_ai(True))
            await asyncio.sleep(0)
            finish.set()
            self.assertEqual(await first, await second)
        run.assert_awaited_once()


class ApiRegressionTests(unittest.TestCase):
    def test_add_stock_succeeds_even_when_quotes_are_unavailable(self) -> None:
        with (patch("backend.main.database.add_watch_stock") as add,
              patch("backend.main.market_data.refresh_quotes", side_effect=RuntimeError("offline")),
              patch("backend.main.database.set_meta"),
              patch("backend.main._watchlist_payload", return_value={"ok": True, "stocks": []}),
              patch("backend.main.database.initialize"),
              patch("backend.main.SCHEDULER", Mock(enabled=False)),
              TestClient(app) as client):
            response = client.post("/api/watchlist", json={"tsCode": "600519.SH"})
        self.assertEqual(response.status_code, 200)
        add.assert_called_once_with("600519.SH")
