from __future__ import annotations

import copy
import json
import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import requests

from backend import concept_data, database, forecast_feedback
from backend.forecast_prices import FeedbackPriceClient
from backend.history_sources import TushareHistoryClient, sina_index_history, ths_concept_history
from backend.probe_sources import probe
from backend.test_forecast_feedback import calendar_fixture, instant, report_fixture


def tushare_payload():
    # Deliberately reordered fields to catch positional parsing mistakes.
    return {"code": 0, "data": {"fields": ["low", "ts_code", "trade_date", "high", "close", "open", "amount", "pct_change"],
            "items": [[99, "BK1152.DC", "20260918", 103, 102, 100, 123000000, 2],
                      [101, "BK1152.DC", "20260921", 109, 108, 102, 250000000, 5.8824]]}}


def response_session(payload):
    session = MagicMock()
    session.__enter__.return_value = session
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = payload
    return session


class AlternativeHistoryTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TUSHARE_TOKEN": "test-only-token"})
        env.start()
        self.addCleanup(env.stop)
        TushareHistoryClient._credential = ""
        TushareHistoryClient._retry_at = TushareHistoryClient._next_at = 0

    def test_tushare_preserves_index_identity_dates_ohlc_and_yuan_units(self):
        session = response_session(tushare_payload())
        with patch("backend.history_sources.EastmoneyClient._session", return_value=session):
            data = TushareHistoryClient().history("BK1152", "2026-09-18", "2026-09-21")
        self.assertEqual(data["rows"][0], {"date": "2026-09-18", "open": 100, "close": 102,
                                          "high": 103, "low": 99, "amount": 123000000, "pctChg": 2})
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://api.tushare.pro")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["params"]["ts_code"], "BK1152.DC")
        self.assertEqual(kwargs["timeout"], (3, 8))
        # Existing feedback receives the new source unchanged and computes the same window.
        report = report_fixture()
        report["publishedAt"] = "2026-09-17T00:38:00+08:00"
        report["result"]["window"]["startDate"] = "2026-09-18"
        outcome = forecast_feedback.evaluate(report, 15, data, {}, calendar_fixture(), instant("2026-09-22T08:00:00"))
        self.assertEqual(outcome["returnPct"], 8)
        self.assertEqual(outcome["source"], data["source"])
        self.assertEqual(outcome["status"], "tracking")

    def test_wrong_concept_never_silently_substitutes(self):
        payload = tushare_payload()
        payload["data"]["items"][0][1] = "BK1136.DC"
        with patch("backend.history_sources.EastmoneyClient._session", return_value=response_session(payload)):
            with self.assertRaisesRegex(RuntimeError, "代码不匹配"):
                TushareHistoryClient().history("BK1152", "2026-09-18", "2026-09-21")

    def test_missing_dates_and_invalid_prices_are_not_fabricated(self):
        payload = tushare_payload()
        payload["data"]["items"][0][4] = float("nan")
        with patch("backend.history_sources.EastmoneyClient._session", return_value=response_session(payload)):
            result = TushareHistoryClient().history("BK1152", "2026-09-18", "2026-09-21")
        self.assertEqual([r["date"] for r in result["rows"]], ["2026-09-21"])

    def test_permission_failure_is_sanitized_and_cooled_down_across_symbols(self):
        session = response_session({"code": 2002, "msg": "权限不足 test-only-token"})
        with patch("backend.history_sources.EastmoneyClient._session", return_value=session):
            for code in ("BK1152", "BK1136"):
                with self.assertRaisesRegex(RuntimeError, "权限不足") as error:
                    TushareHistoryClient().history(code, "2026-09-18", "2026-09-21")
                self.assertNotIn("test-only-token", str(error.exception))
        self.assertEqual(session.post.call_count, 1)

    def test_token_rotation_releases_permission_cooldown(self):
        session = response_session({"code": 40101, "msg": "token error"})
        with patch("backend.history_sources.EastmoneyClient._session", return_value=session):
            with self.assertRaises(RuntimeError):
                TushareHistoryClient().history("BK1152", "2026-09-18", "2026-09-21")
            session.post.return_value.json.return_value = tushare_payload()
            with patch.dict(os.environ, {"TUSHARE_TOKEN": "rotated-test-token"}):
                self.assertEqual(len(TushareHistoryClient().history("BK1152", "2026-09-18", "2026-09-21")["rows"]), 2)

    def test_network_errors_do_not_expose_credentials(self):
        session = response_session({})
        session.post.side_effect = requests.ConnectionError("test-only-token")
        with patch("backend.history_sources.EastmoneyClient._session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "连接失败") as error:
                TushareHistoryClient().history("BK1152", "2026-09-18", "2026-09-21")
        self.assertNotIn("test-only-token", str(error.exception))

    def test_no_token_no_request_and_probe_reports_not_configured(self):
        with patch.dict(os.environ, {"TUSHARE_TOKEN": ""}), patch("backend.history_sources.EastmoneyClient._session") as session:
            self.assertEqual(probe("tushare", "BK1152", "2026-09-18", "2026-09-21")["status"], "not_configured")
            with self.assertRaisesRegex(RuntimeError, "未配置"):
                TushareHistoryClient().history("BK1152", "2026-09-18", "2026-09-21")
            session.assert_not_called()

    def test_probe_distinguishes_transport_failure_from_valid_empty_response(self):
        with patch.object(FeedbackPriceClient, "history", return_value={"rows": [], "error": "连接中断"}):
            self.assertEqual(probe("auto", "BK1152", "2026-09-18", "2026-09-21")["status"], "unavailable")
        with patch.object(FeedbackPriceClient, "history", return_value={"rows": []}):
            self.assertEqual(probe("auto", "BK1152", "2026-09-18", "2026-09-21")["status"], "empty")

    def test_malformed_schema_and_truncated_pages_are_rejected(self):
        bad = [None, {"code": 0, "data": []}, {"code": 0, "data": {"fields": [], "items": []}},
               {"code": 0, "data": {"fields": [[]], "items": []}}]
        truncated = tushare_payload()
        truncated["data"]["items"] *= 1000
        for payload in [*bad, truncated]:
            with patch("backend.history_sources.EastmoneyClient._session", return_value=response_session(payload)):
                with self.assertRaises(RuntimeError):
                    TushareHistoryClient._read("test-only-token", "BK1152", "2026-09-18", "2026-09-21")

    def test_sina_is_a_real_benchmark_fallback_after_tencent_failure(self):
        payload = [{"day": "2026-09-18", "open": "1634.635", "close": "1652.631", "high": "1671.013", "low": "1630.104"},
                   {"day": "2026-09-21", "open": "1669.202", "close": "1657.485", "high": "1678.590", "low": "1643.169"},
                   {"day": "2026-09-22", "open": "1", "close": "1", "high": "1", "low": "1"}]
        with (patch("backend.history_sources._get_text", return_value=json.dumps(payload)),
              patch.object(FeedbackPriceClient, "tencent_history", side_effect=RuntimeError("failed")),
              patch.object(FeedbackPriceClient, "eastmoney_history") as east):
            data = FeedbackPriceClient().history("sh000688", "2026-09-18", "2026-09-21")
        self.assertEqual(data["source"], "新浪财经指数日线")
        self.assertEqual(len(data["rows"]), 2)
        east.assert_not_called()

    def test_tushare_failure_falls_back_without_affecting_free_mode(self):
        east = {"rows": [{"date": "2026-09-21", "open": 100, "close": 102, "high": 103, "low": 99}], "source": "east"}
        with (patch.object(TushareHistoryClient, "history", side_effect=RuntimeError("权限不足")) as alternative,
              patch.object(FeedbackPriceClient, "eastmoney_history", return_value=east)):
            self.assertEqual(FeedbackPriceClient().history("BK1152", "2026-09-18", "2026-09-21"), east)
            with patch.dict(os.environ, {"TUSHARE_TOKEN": ""}):
                self.assertEqual(FeedbackPriceClient().history("BK1152", "2026-09-18", "2026-09-21"), east)
            self.assertEqual(alternative.call_count, 1)

    def test_stale_source_does_not_block_fresher_single_provider_series(self):
        first = {"rows": [{"date": "2026-09-18", "open": 100, "close": 102, "high": 103, "low": 99}], "source": "tushare"}
        second = copy.deepcopy(first)
        second["rows"].append({"date": "2026-09-21", "open": 102, "close": 108, "high": 109, "low": 101})
        second["source"] = "east"
        with (patch.object(TushareHistoryClient, "history", return_value=first),
              patch.object(FeedbackPriceClient, "eastmoney_history", return_value=second)):
            self.assertEqual(FeedbackPriceClient().history("BK1152", "2026-09-18", "2026-09-21"), second)

    def test_ths_ohlc_order_and_namespace_are_explicit(self):
        raw = 'quotebridge_v4_line_bk_886042_01_2026({"data":"20260918,100,110,99,108,10000,200000"})'
        with patch("backend.history_sources._get_text", return_value=raw):
            result = ths_concept_history("THS:886042", "2026-09-18", "2026-09-21")
            self.assertEqual(result["rows"][0]["close"], 108)
            self.assertEqual(result["rows"][0]["high"], 110)
            with self.assertRaises(ValueError):
                ths_concept_history("THS:885756", "2026-09-18", "2026-09-21")
        with self.assertRaises(ValueError):
            ths_concept_history("BK1152", "2026-09-18", "2026-09-21")
        with self.assertRaises(ValueError):
            FeedbackPriceClient().history("THS:886042", "2026-09-18", "2026-09-21")
        with self.assertRaises(ValueError):
            sina_index_history("BK1152", "2026-09-18", "2026-09-21")

    def test_closed_research_history_uses_same_concept_backup_and_retains_provenance(self):
        rows = [{"date": "2026-09-21", "open": 100, "close": 102, "high": 103, "low": 99, "amount": 123000000}]
        with (patch.object(database, "get_meta", return_value=None), patch.object(database, "set_meta") as save,
              patch.object(TushareHistoryClient, "history", return_value={"rows": rows, "source": "Tushare", "url": "https://tushare.pro"}),
              patch.object(concept_data.ConceptResearchClient, "_json") as east,
              patch.object(concept_data, "datetime") as clock):
            clock.strptime = datetime.strptime
            clock.now.return_value = instant("2026-09-22T08:00:00")
            data = concept_data.ConceptResearchClient().daily_history("BK1152", "20260921")
        self.assertEqual(data[0]["historySource"], "Tushare")
        self.assertEqual(data[0]["amount"], 123000000)
        save.assert_called_once()
        east.assert_not_called()
