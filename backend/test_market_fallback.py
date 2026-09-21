from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from backend import database
from backend.data_sources import MarketDataService
from backend.eastmoney import EastmoneyClient
from backend.tencent import TencentClient


class TencentSnapshotTests(unittest.TestCase):
    def test_rank_requests_all_a_shares_and_reuses_complete_snapshot(self):
        client = TencentClient()
        first = [{"code": f"sh{600000 + i}"} for i in range(200)]
        second = [{"code": "bj920001"}]
        with (patch.object(client, "_request_json", side_effect=[
                {"data": {"total": 201, "rank_list": first}},
                {"data": {"total": 201, "rank_list": second}}]) as request,
              patch("backend.tencent.time.sleep")):
            rows = client.fetch_rank_rows()
            self.assertEqual(len(rows), 201)
            self.assertIs(client.fetch_rank_rows(), rows)
        self.assertEqual(request.call_count, 2)
        for offset, call in zip((0, 200), request.call_args_list):
            self.assertEqual(call.args[1]["board_code"], "aStock")
            self.assertEqual(call.args[1]["sort_type"], "price")
            self.assertEqual(call.args[1]["offset"], offset)
            self.assertNotIn("post_json", call.kwargs)

    def test_repeated_page_and_incomplete_response_never_become_market_totals(self):
        for payload in (
            {"data": {"total": 2, "rank_list": [{"code": "sh600000"}] * 2}},
            {"data": {"total": 2000, "rank_list": [{"code": "sh600000"}]}},
            {"data": {"rank_list": [{"code": "sh600000"}]}},
        ):
            client = TencentClient()
            with self.subTest(payload=payload), patch.object(client, "_request_json", return_value=payload):
                with self.assertRaises(RuntimeError):
                    client.fetch_rank_rows()
                self.assertEqual(client._rank_cache, [])

    def test_provider_quote_date_and_units_survive_normalization(self):
        client = TencentClient()
        quote_time = datetime(2026, 9, 21, 15, 0, tzinfo=database.CHINA_TZ).timestamp()
        row = {"code": "bj920001", "name": "测试", "zxj": "12", "zd": "2", "zdf": "20",
               "volume": "500", "turnover": "60", "zsz": "8", "ltsz": "3", "pe_ttm": "20"}
        with (patch.object(client, "fetch_rank_rows", return_value=[row]),
              patch.object(client, "_quote_timestamp", return_value=quote_time)):
            frame = client.spot_frame()
        service = MarketDataService()
        with patch.object(service, "latest_trade_date", side_effect=AssertionError("must use actual quote date")):
            normalized = service._normalize_spot(frame, "腾讯证券")[0]
        self.assertEqual(normalized["tsCode"], "920001.BJ")
        self.assertEqual(normalized["tradeDate"], "20260921")
        self.assertEqual(normalized["quoteTime"], "15:00")
        self.assertEqual(normalized["vol"], 50_000)
        self.assertEqual(normalized["amount"], 600_000)
        self.assertEqual(normalized["totalMv"], 800_000_000)
        self.assertEqual(normalized["preClose"], 10)
        self.assertIsNone(normalized["open"])

    def test_quote_timestamp_requires_valid_provider_date(self):
        client = TencentClient()
        fields = [""] * 31
        fields[30] = "20260921161400"
        session = Mock()
        session.get.return_value.text = 'v_sh000001="' + "~".join(fields) + '";'
        with patch.object(client, "_session", return_value=session):
            value = client._quote_timestamp()
            self.assertEqual(datetime.fromtimestamp(value, database.CHINA_TZ).strftime("%Y%m%d"), "20260921")
            session.get.return_value.text = "invalid"
            with self.assertRaisesRegex(RuntimeError, "日期"):
                client._quote_timestamp()

    def test_expired_batch_budget_does_not_send_another_request(self):
        client = TencentClient()
        session = Mock()
        with patch.object(client, "_session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                client._request_json(client.RANK_URL, {}, deadline=0)
        session.get.assert_not_called()
        session.close.assert_called_once()


class MarketFallbackTests(unittest.TestCase):
    def test_eastmoney_disconnect_switches_provider_and_respects_cooldown(self):
        eastmoney, tencent = Mock(), Mock()
        eastmoney.spot_frame.side_effect = RuntimeError("RemoteDisconnected")
        tencent.spot_frame.return_value = pd.DataFrame([{"代码": "600000", "最新价": 10}])
        service = MarketDataService(eastmoney, tencent)
        with patch.object(service, "_akshare") as akshare:
            self.assertIn("腾讯", service._spot_frame()[1])
            self.assertIn("腾讯", service._spot_frame()[1])
            eastmoney.spot_frame.assert_called_once()
            akshare.assert_not_called()
            service._eastmoney_spot_failed_at -= timedelta(days=1)
            eastmoney.spot_frame.side_effect = None
            eastmoney.spot_frame.return_value = tencent.spot_frame.return_value
            self.assertIn("东方财富", service._spot_frame()[1])
            self.assertIsNone(service._eastmoney_spot_failed_at)

    @staticmethod
    def cached():
        return {"tradeDate": "20260921", "updatedAt": "2026-09-21T15:10:00+08:00", "source": "原来源",
                "snapshot": {"turnover": 100}, "warnings": [], "dataStatus": {
                    "fundFlow": {"state": "live", "source": "原来源"}}}

    def test_restart_and_outage_return_dated_cache_without_overwriting_success(self):
        service = MarketDataService()
        cached = self.cached()
        with (patch.object(service, "market_snapshot", side_effect=RuntimeError("offline")),
              patch("backend.research_store.cache_get", return_value=cached),
              patch("backend.research_store.cache_put") as save,
              patch.object(database, "set_meta")):
            result = service.market_overview(True)
            again = service.market_overview()
        self.assertTrue(result["stale"])
        self.assertEqual(result["tradeDate"], cached["tradeDate"])
        self.assertEqual(result["updatedAt"], cached["updatedAt"])
        self.assertEqual(result["snapshot"], cached["snapshot"])
        self.assertEqual(result["dataStatus"]["fundFlow"]["state"], "cached")
        self.assertEqual(again["warnings"], result["warnings"])
        self.assertNotIn("stale", cached)
        self.assertEqual(cached["dataStatus"]["fundFlow"]["state"], "live")
        save.assert_not_called()

    def test_forced_failure_invalidates_fresh_memory_status_and_retries(self):
        service = MarketDataService()
        service._overview_cache = self.cached()
        service._overview_fetched_at = datetime.now(database.CHINA_TZ)
        with (patch.object(service, "market_snapshot", side_effect=RuntimeError("offline")) as request,
              patch.object(database, "set_meta")):
            self.assertTrue(service.market_overview(True)["stale"])
            self.assertTrue(service.market_overview()["stale"])
        self.assertEqual(request.call_count, 2)

    def test_cold_start_failure_has_clear_error_and_does_not_invent_data(self):
        service = MarketDataService()
        with (patch.object(service, "market_snapshot", side_effect=RuntimeError("offline")),
              patch("backend.research_store.cache_get", return_value=None),
              patch.object(database, "set_meta")):
            with self.assertRaisesRegex(RuntimeError, "尚无成功快照"):
                service.market_overview()

    def test_recovery_replaces_stale_snapshot_and_clears_error(self):
        service = MarketDataService()
        service._overview_cache = {**self.cached(), "stale": True}
        rows = [{"symbol": "600001", "name": "样本", "exchange": "SSE", "tradeDate": "20260922",
                 "pctChg": 2, "amount": 1000, "source": "腾讯证券"}]
        status = {"state": "unavailable", "source": None, "updatedAt": None}
        with (patch.object(service, "market_snapshot", return_value=rows),
              patch.object(service, "_index_turnover_history", return_value=([], None)),
              patch.object(service, "_intraday_turnover_comparison", return_value=(None, None, status)),
              patch.object(service, "_fund_flow_history", return_value=([], None, status)),
              patch("backend.research_store.cache_put") as save,
              patch.object(database, "set_meta") as meta):
            result = service.market_overview()
        self.assertFalse(result["stale"])
        self.assertEqual(result["tradeDate"], "20260922")
        self.assertEqual(result["dataStatus"]["quotes"]["state"], "fallback")
        self.assertEqual(result["warnings"], [])
        save.assert_called_once_with("latest-market", result)
        meta.assert_any_call("market_overview_error", "")

    def test_known_provider_outage_skips_repeated_eastmoney_extension_requests(self):
        eastmoney, tencent = EastmoneyClient(), Mock()
        service = MarketDataService(eastmoney, tencent)
        service._eastmoney_spot_failed_at = datetime.now(database.CHINA_TZ)
        tencent.index_intraday_turnover_pair.return_value = {"date": "20260921", "currentTurnover": 100}
        tencent.market_fund_flow_snapshot.return_value = {"date": "20260921", "mainNetInflow": 10}
        with (patch.object(service, "_akshare") as akshare,
              patch.object(eastmoney, "index_intraday_turnover_points") as intraday,
              patch.object(eastmoney, "market_fund_flow_frame") as flow,
              patch.object(database, "get_meta", return_value=None), patch.object(database, "set_meta")):
            self.assertEqual(service._index_turnover_history()[0], [])
            self.assertEqual(service._intraday_turnover_comparison("20260921", [])[2]["state"], "fallback")
            self.assertEqual(service._fund_flow_history("20260921")[2]["state"], "fallback")
        akshare.assert_not_called()
        intraday.assert_not_called()
        flow.assert_not_called()

    def test_background_refresh_does_not_report_cached_data_as_success(self):
        from backend import research_jobs
        with tempfile.TemporaryDirectory() as directory, patch.object(database, "DATABASE_PATH", Path(directory) / "test.db"):
            database.initialize()
            token = research_jobs._claim("market")
            with patch("backend.data_sources.market_data.market_overview", return_value={"stale": True}):
                research_jobs._run("market", token)
            result = research_jobs.job_status("market")
        self.assertEqual(result["status"], "failed")
        self.assertIn("保留最近成功快照", result["error"])
