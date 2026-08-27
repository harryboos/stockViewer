from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pandas as pd

from backend.data_sources import MarketDataService
from backend.eastmoney import EastmoneyClient
from backend.strategy_factors import volume_breakout_picks, wilder_rsi


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, pages: dict[int, dict]) -> None:
        self.pages = pages
        self.headers: dict[str, str] = {}
        self.trust_env = False
        self.requests: list[int] = []
        self.closed = False

    def get(self, _url: str, params: dict, timeout: tuple[int, int]) -> _FakeResponse:
        self.requests.append(int(params["pn"]))
        return _FakeResponse(self.pages[int(params["pn"])])

    def close(self) -> None:
        self.closed = True


class MarketDataServiceTests(unittest.TestCase):
    def test_delay_route_paginates_and_reuses_successful_session(self) -> None:
        pages = {
            1: {"data": {"total": 3, "diff": [{"f12": "000001"}, {"f12": "000002"}]}},
            2: {"data": {"total": 3, "diff": [{"f12": "000003"}]}},
        }
        session = _FakeSession(pages)
        client = EastmoneyClient()
        with (
            patch.object(client, "_hosts", return_value=["82.push2delay.eastmoney.com"]),
            patch.object(client, "_session", return_value=session),
            patch("backend.eastmoney.time.sleep"),
        ):
            rows = client.fetch_pages("82", {"fields": "f12"})

        self.assertEqual([row["f12"] for row in rows], ["000001", "000002", "000003"])
        self.assertEqual(session.requests, [1, 2])
        self.assertTrue(session.closed)

    def test_spot_frame_prefers_delay_route_without_calling_akshare(self) -> None:
        client = Mock()
        service = MarketDataService(eastmoney_client=client)
        frame = pd.DataFrame([{"代码": "600519", "名称": "贵州茅台", "最新价": 1500.0}])
        akshare = Mock()
        client.spot_frame.return_value = frame
        with (
            patch("backend.data_sources.MARKET", Mock(eastmoney_delay_enabled=True)),
            patch.object(service, "_akshare", return_value=akshare),
        ):
            result, source, transport = service._spot_frame()

        self.assertIs(result, frame)
        self.assertIn("备用线路", source)
        self.assertIn("绕过", transport)
        akshare.stock_zh_a_spot_em.assert_not_called()

    def test_spot_frame_uses_akshare_if_delay_route_fails(self) -> None:
        client = Mock()
        client.spot_frame.side_effect = RuntimeError("blocked")
        service = MarketDataService(eastmoney_client=client)
        frame = pd.DataFrame([{"代码": "600519", "名称": "贵州茅台", "最新价": 1500.0}])
        akshare = Mock()
        akshare.stock_zh_a_spot_em.return_value = frame
        with (
            patch("backend.data_sources.MARKET", Mock(eastmoney_delay_enabled=True)),
            patch.object(service, "_akshare", return_value=akshare),
        ):
            result, source, transport = service._spot_frame()

        self.assertIs(result, frame)
        self.assertEqual(source, "AKShare · 东方财富")
        self.assertEqual(transport, "AKShare 标准线路")

    def test_normalized_spot_keeps_actual_source_and_filters_invalid_rows(self) -> None:
        service = MarketDataService()
        frame = pd.DataFrame(
            [
                {
                    "代码": "600519", "名称": "贵州茅台", "最新价": 1500.0,
                    "涨跌幅": 1.2, "行情时间": 1787816238,
                },
                {"代码": "000001", "名称": "平安银行", "最新价": "-"},
            ]
        )
        with (
            patch.object(service, "latest_trade_date", return_value="20260828"),
            patch("backend.data_sources.database.now_iso", return_value="2026-08-28T10:00:00+08:00"),
        ):
            rows = service._normalize_spot(frame, "东方财富备用线路")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tsCode"], "600519.SH")
        self.assertEqual(rows[0]["tradeDate"], "20260827")
        self.assertEqual(rows[0]["source"], "东方财富备用线路")

    def test_wilder_rsi_handles_monotonic_prices(self) -> None:
        self.assertEqual(wilder_rsi([float(value) for value in range(1, 20)]), 100.0)

    def test_volume_breakout_applies_all_strict_filters(self) -> None:
        eligible = {
            "symbol": "000001", "name": "平安银行", "industry": "银行",
            "previousAmount": 1_100_000_000, "change10d": 31.0,
            "averageAmount20": 1_300_000_000, "volumeRatio": 1.2,
            "volumeRatioMode": "盘中量比", "todayPctChg": 2.0,
            "currentPrice": 12.5, "isSt": False, "isStarMarket": False,
        }
        rejected = {**eligible, "symbol": "688001", "name": "科创样本", "isStarMarket": True}
        picks = volume_breakout_picks([eligible, rejected])

        self.assertEqual([pick["code"] for pick in picks], ["000001"])

    def test_history_batch_reuses_one_baostock_session(self) -> None:
        service = MarketDataService()
        baostock = Mock()
        query = Mock(error_code="0")
        query.next.return_value = False
        baostock.query_history_k_data_plus.return_value = query
        with patch.object(service, "_baostock_session", return_value=nullcontext(baostock)) as session:
            histories = service.history_batch(["600519", "000001"])

        self.assertEqual(set(histories), {"600519", "000001"})
        self.assertEqual(baostock.query_history_k_data_plus.call_count, 2)
        session.assert_called_once()


if __name__ == "__main__":
    unittest.main()
