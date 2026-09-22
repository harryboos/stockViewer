from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import requests

from backend import database
from backend.concept_data import ConceptResearchClient
from backend.data_sources import MarketDataService
from backend.eastmoney import EastmoneyClient
from backend.forecast_prices import FeedbackPriceClient
from backend.history_sources import TushareHistoryClient
from backend.tencent import TencentClient


def history(days: list[str], source: str) -> dict:
    return {"rows": [{"date": day, "open": 100, "close": 102, "high": 103, "low": 99}
                     for day in days], "source": source}


class HistoryCoverageReviewTests(unittest.TestCase):
    def setUp(self):
        calendar = patch("backend.forecast_prices._known_calendar", ())
        calendar.start()
        self.addCleanup(calendar.stop)

    def test_latest_bar_does_not_hide_a_missing_first_or_middle_session(self):
        complete = history(["2026-09-17", "2026-09-18", "2026-09-21"], "east")
        for dates in (["2026-09-18", "2026-09-21"], ["2026-09-17", "2026-09-21"]):
            with (self.subTest(dates=dates), patch.dict(os.environ, {"TUSHARE_TOKEN": "test-only"}),
                  patch.object(TushareHistoryClient, "history", return_value=history(dates, "tushare")),
                  patch.object(FeedbackPriceClient, "eastmoney_history", return_value=complete) as backup):
                actual = FeedbackPriceClient().history("BK1152", "2026-09-17", "2026-09-21")
            self.assertEqual(actual, complete)
            backup.assert_called_once()

    def test_weekend_end_does_not_trigger_unneeded_fallback(self):
        complete = history(["2026-09-17", "2026-09-18"], "tushare")
        with (patch.dict(os.environ, {"TUSHARE_TOKEN": "test-only"}),
              patch.object(TushareHistoryClient, "history", return_value=complete),
              patch.object(FeedbackPriceClient, "eastmoney_history") as backup):
            actual = FeedbackPriceClient().history("BK1152", "2026-09-17", "2026-09-20")
        self.assertEqual(actual, complete)
        backup.assert_not_called()

    def test_partial_sources_are_never_spliced_into_a_complete_series(self):
        first = history(["2026-09-17", "2026-09-18"], "tushare")
        newer = history(["2026-09-18", "2026-09-21"], "east")
        with (patch.dict(os.environ, {"TUSHARE_TOKEN": "test-only"}),
              patch.object(TushareHistoryClient, "history", return_value=first),
              patch.object(FeedbackPriceClient, "eastmoney_history", return_value=newer)):
            actual = FeedbackPriceClient().history("BK1152", "2026-09-17", "2026-09-21")
        self.assertEqual(actual, newer)
        self.assertEqual(len(actual["rows"]), 2)

    def test_loaded_exchange_calendar_avoids_holiday_fallback_reads(self):
        complete = history(["2026-09-30", "2026-10-08"], "tushare")
        with (patch.dict(os.environ, {"TUSHARE_TOKEN": "test-only"}),
              patch.object(FeedbackPriceClient, "_calendar", return_value=["2026-09-30", "2026-10-08"]),
              patch.object(TushareHistoryClient, "history", return_value=complete),
              patch.object(FeedbackPriceClient, "eastmoney_history") as backup,
              patch.object(database, "get_meta", side_effect=AssertionError("history must not read calendar DB"))):
            FeedbackPriceClient().calendar()
            actual = FeedbackPriceClient().history("BK1152", "2026-09-30", "2026-10-08")
        self.assertEqual(actual, complete)
        backup.assert_not_called()

    def test_malformed_tencent_envelope_reaches_sina_fallback(self):
        complete = history(["2026-09-21"], "sina")
        with (patch.object(FeedbackPriceClient, "_json", return_value={"data": [1]}),
              patch("backend.forecast_prices.sina_index_history", return_value=complete),
              patch.object(FeedbackPriceClient, "eastmoney_history") as backup):
            self.assertEqual(FeedbackPriceClient().history("sh000001", "2026-09-21", "2026-09-21"), complete)
        backup.assert_not_called()

    def test_research_bars_reject_invalid_ohlc_dates_and_out_of_window_data(self):
        klines = [
            "2026-09-18,100,102,103,99,10,500000,1,2",
            "2026-09-21,102,108,109,101,10,-1,1,5",
            "2026-09-19,100,102,101,99,10,500000,1,2",  # impossible high
            "2026-09-20,100,102,103,101,10,500000,1,2",  # impossible low
            "2026-07-01,100,102,103,99,10,500000,1,2",  # outside requested window
            "2026-08-32,100,102,103,99,10,500000,1,2",  # invalid calendar date
            "2026-09-22,100,102,103,99,10,500000,1,2",  # future to requested day
            "2026-09-18,100,nan,103,99,10,999999,1,3",  # invalid duplicate cannot replace valid extras
        ]
        client = ConceptResearchClient()
        with (patch.dict(os.environ, {"TUSHARE_TOKEN": ""}),
              patch.object(database, "get_meta", return_value=None), patch.object(database, "set_meta"),
              patch.object(client, "_json", return_value={"data": {"code": "BK1152", "klines": klines}})):
            rows = client.daily_history("BK1152", "20260921")
        self.assertEqual([row["date"] for row in rows], ["20260918", "20260921"])
        self.assertEqual(rows[0]["amount"], 500000)
        self.assertIsNone(rows[-1]["amount"])


class SnapshotTransportReviewTests(unittest.TestCase):
    def test_duplicate_pages_do_not_become_a_complete_snapshot(self):
        client, session = EastmoneyClient(), Mock()
        with (patch.object(client, "_session", return_value=session),
              patch.object(client, "_request_page", side_effect=[
                  {"total": 4, "diff": [{"f12": "600001"}, {"f12": "600002"}]},
                  {"total": 4, "diff": [{"f12": "600001"}, {"f12": "600002"}]},
              ]), patch("backend.eastmoney.time.sleep")):
            with self.assertRaisesRegex(RuntimeError, "重复分页"):
                client.fetch_pages("82", {"fields": "f12,f2"})
        session.close.assert_called_once()

    def test_changed_total_and_surplus_rows_are_rejected(self):
        for last in ({"total": 5, "diff": [{"f12": "600003"}, {"f12": "600004"}]},
                     {"total": 3, "diff": [{"f12": "600003"}, {"f12": "600004"}]}):
            client = EastmoneyClient()
            with (self.subTest(last=last), patch.object(client, "_session", return_value=Mock()),
                  patch.object(client, "_request_page", side_effect=[
                      {"total": 3, "diff": [{"f12": "600001"}, {"f12": "600002"}]}, last]),
                  patch("backend.eastmoney.time.sleep")):
                with self.assertRaises(RuntimeError):
                    client.fetch_pages("82", {"fields": "f12"})

    def test_snapshot_deadline_stops_following_pages(self):
        client = EastmoneyClient()
        with (patch.object(client, "_session", return_value=Mock()),
              patch.object(client, "_request_page", return_value={"total": 2, "diff": [{"f12": "600001"}]}) as fetch,
              patch("backend.eastmoney.time.monotonic", side_effect=[0, 0, 61])):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                client.fetch_pages("82", {"fields": "f12"})
        fetch.assert_called_once()

    def test_history_deadline_stops_other_hosts_after_timeout(self):
        client, session = EastmoneyClient(), Mock()
        session.get.side_effect = requests.Timeout("secret proxy credentials")
        with (patch.object(client, "_session", return_value=session),
              patch("backend.eastmoney.requests.utils.get_environ_proxies", return_value={}),
              patch("backend.eastmoney.time.monotonic", side_effect=[0, 0, 26])):
            with self.assertRaisesRegex(RuntimeError, "超时") as error:
                client._trend_rows("90.BK1152")
        self.assertNotIn("secret", str(error.exception))
        session.get.assert_called_once()
        session.close.assert_called_once()

    def test_history_rejects_wrong_index_and_continues_to_another_host(self):
        client, session = EastmoneyClient(), Mock()
        session.get.return_value.json.side_effect = [
            {"data": {"code": "BK1136", "trends": ["wrong"]}},
            {"data": {"code": "BK1152", "trends": ["correct"]}},
        ]
        with (patch.object(client, "_session", return_value=session),
              patch("backend.eastmoney.requests.utils.get_environ_proxies", return_value={})):
            self.assertEqual(client._trend_rows("90.BK1152"), ["correct"])
        self.assertEqual(session.get.call_count, 2)

    def test_tencent_ttm_is_not_mislabeled_as_dynamic_pe(self):
        client = TencentClient()
        timestamp = datetime(2026, 9, 21, 15, tzinfo=database.CHINA_TZ).timestamp()
        with (patch.object(client, "fetch_rank_rows", return_value=[
                {"code": "sh600001", "name": "测试", "zxj": "10", "pe_ttm": "18"}]),
              patch.object(client, "_quote_timestamp", return_value=timestamp)):
            frame = client.spot_frame()
        self.assertEqual(frame.iloc[0]["市盈率-TTM"], "18")
        normalized = MarketDataService()._normalize_spot(frame, "腾讯证券")
        self.assertEqual(normalized[0]["peTtm"], 18)

    def test_dynamic_pe_is_not_used_as_ttm_in_stock_valuation(self):
        import pandas as pd
        frame = pd.DataFrame([{"代码": "600001", "名称": "测试", "最新价": 10, "市盈率-动态": 18,
                               "行情时间": datetime(2026, 9, 21, 15, tzinfo=database.CHINA_TZ).timestamp()}])
        normalized = MarketDataService()._normalize_spot(frame, "东方财富")
        self.assertIsNone(normalized[0]["peTtm"])
