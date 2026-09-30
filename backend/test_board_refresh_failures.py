from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

from backend.data_sources import MarketDataService
from backend.source_errors import BoardSourceError, source_failure_reason


def board_frame():
    return pd.DataFrame([{"板块代码": "BK1001", "板块名称": "测试概念", "涨跌幅": 1.5,
                          "上涨家数": 10, "下跌家数": 2}])


class BoardRefreshFailureTests(unittest.TestCase):
    def test_empty_or_invalid_primary_response_reaches_secondary_for_each_category(self):
        for kind in ("industry", "concept"):
            for bad in (None, pd.DataFrame(), pd.DataFrame([{"unexpected": 1}]),
                        board_frame().assign(涨跌幅="-")):
                with self.subTest(kind=kind, response=type(bad).__name__):
                    direct, ak = Mock(), Mock()
                    getattr(direct, f"{kind}_name_frame").return_value = bad
                    expected = board_frame()
                    getattr(ak, f"stock_board_{kind}_name_em").return_value = expected
                    service = MarketDataService(eastmoney_client=direct)
                    with patch.object(service, "_akshare", return_value=ak):
                        actual, source = service._board_name_frame(kind)
                    self.assertIs(actual, expected)
                    self.assertIn("AKShare", source)
                    getattr(ak, f"stock_board_{kind}_name_em").assert_called_once()

    def test_valid_primary_does_not_call_akshare(self):
        direct = Mock()
        direct.concept_name_frame.return_value = board_frame()
        service = MarketDataService(eastmoney_client=direct)
        with patch.object(service, "_akshare") as ak:
            frame, source = service._concept_name_frame()
        self.assertEqual(len(frame), 1)
        self.assertIn("备用线路", source)
        ak.assert_not_called()

    def test_each_failed_source_has_a_safe_reason(self):
        direct, ak = Mock(), Mock()
        direct.concept_name_frame.side_effect = RuntimeError(
            "RemoteDisconnected('Remote end closed connection without response') https://user:password@proxy.invalid")
        ak.stock_board_concept_name_em.return_value = pd.DataFrame()
        service = MarketDataService(eastmoney_client=direct)
        with patch.object(service, "_akshare", return_value=ak), self.assertRaises(BoardSourceError) as caught:
            service._concept_name_frame()
        self.assertEqual(str(caught.exception), "东财备用：远端中断连接；AKShare：返回空数据")

    def test_failed_refresh_keeps_cached_date_and_explains_each_category(self):
        service = MarketDataService()
        cached = {"tradeDate": "20260930", "updatedAt": "2026-09-30T16:00:00+08:00", "warnings": []}
        with (patch.object(service, "_cached_json", return_value=cached),
              patch.object(service, "market_snapshot", return_value=[{"tradeDate": "20260930"}]),
              patch.object(service, "_industry_name_frame", side_effect=BoardSourceError("东财备用：远端中断连接；AKShare：请求超时")),
              patch.object(service, "_concept_name_frame", side_effect=BoardSourceError("东财备用：返回空数据；AKShare：返回数据格式异常")),
              patch("backend.data_sources.database.set_meta") as save,
              self.assertLogs("backend.data_sources", level="WARNING")):
            result = service.sector_overview(True)
        self.assertEqual(result["tradeDate"], cached["tradeDate"])
        self.assertEqual(result["updatedAt"], cached["updatedAt"])
        self.assertEqual(result["refreshStatus"], "failed")
        self.assertIn("行业（东财备用：远端中断连接；AKShare：请求超时）", result["refreshError"])
        self.assertIn("概念（东财备用：返回空数据；AKShare：返回数据格式异常）", result["refreshError"])
        save.assert_not_called()

    def test_error_classification_never_echoes_a_remote_url(self):
        cases = ((requests.Timeout("private server"), "请求超时"),
                 (requests.ConnectionError("proxy https://secret@invalid"), "代理连接失败"),
                 (requests.ConnectionError("Name resolution failed for private server"), "域名解析失败"),
                 (ValueError("secret HTML response"), "返回数据格式异常"))
        for error, expected in cases:
            self.assertEqual(source_failure_reason(error), expected)
