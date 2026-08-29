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

    def test_sector_fund_flow_route_uses_industry_and_concept_filters(self) -> None:
        client = EastmoneyClient()
        payload = [{
            "f14": "半导体", "f3": 2.5, "f62": 1_200_000_000,
            "f184": 3.2, "f204": "龙头科技", "f205": "688001",
        }]
        with patch.object(client, "fetch_pages", return_value=payload) as fetch:
            industry = client.sector_fund_flow_frame("industry")
            industry_params = fetch.call_args.args[1]
            concept = client.sector_fund_flow_frame("concept")
            concept_params = fetch.call_args.args[1]

        self.assertEqual(industry_params["fs"], "m:90 t:2")
        self.assertEqual(concept_params["fs"], "m:90 t:3")
        self.assertEqual(industry.iloc[0]["主力净流入最大股代码"], "688001")
        self.assertEqual(concept.iloc[0]["名称"], "半导体")

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

    def test_market_overview_calculates_breadth_turnover_and_daily_delta(self) -> None:
        service = MarketDataService()
        snapshot = [
            {"symbol": "600001", "name": "沪市样本", "exchange": "SSE", "tradeDate": "20260828", "pctChg": 10.01, "amount": 5_000_000_000, "source": "测试行情"},
            {"symbol": "300001", "name": "创业样本", "exchange": "SZSE", "tradeDate": "20260828", "pctChg": 12.0, "amount": 3_000_000_000, "source": "测试行情"},
            {"symbol": "000001", "name": "深市样本", "exchange": "SZSE", "tradeDate": "20260828", "pctChg": -10.02, "amount": 2_000_000_000, "source": "测试行情"},
            {"symbol": "430001", "name": "北交样本", "exchange": "BSE", "tradeDate": "20260828", "pctChg": 0.0, "amount": 1_000_000_000, "source": "测试行情"},
        ]
        flow = [{
            "date": "20260828", "shClose": 3500.0, "shPctChg": 0.5,
            "szClose": 11000.0, "szPctChg": -0.2, "mainNetInflow": 8_000_000_000,
            "mainNetInflowRatio": 0.7, "superLargeNetInflow": 5_000_000_000,
            "largeNetInflow": 3_000_000_000,
        }]
        with (
            patch.object(service, "market_snapshot", return_value=snapshot),
            patch.object(service, "_index_turnover_history", return_value=([{"date": "20260827", "turnover": 10_000_000_000}], None)),
            patch.object(service, "_fund_flow_history", return_value=(flow, None)),
            patch("backend.data_sources.database.set_meta"),
            patch("backend.data_sources.database.now_iso", return_value="2026-08-28T15:10:00+08:00"),
        ):
            result = service.market_overview()

        self.assertEqual(result["snapshot"]["turnover"], 11_000_000_000)
        self.assertEqual(result["snapshot"]["turnoverDelta"], 0)
        self.assertEqual(result["turnoverHistory"][-1]["turnover"], 10_000_000_000)
        self.assertEqual(result["snapshot"]["advancers"], 2)
        self.assertEqual(result["snapshot"]["decliners"], 1)
        self.assertEqual(result["snapshot"]["flat"], 1)
        self.assertEqual(result["snapshot"]["limitUp"], 1)
        self.assertEqual(result["snapshot"]["limitDown"], 1)
        self.assertEqual(result["latestFlow"]["mainNetInflow"], 8_000_000_000)

    def test_sector_overview_merges_strength_fund_flow_and_leaders(self) -> None:
        service = MarketDataService()
        industry = pd.DataFrame([
            {
                "板块名称": "半导体", "板块代码": "BK1036", "涨跌幅": 3.5,
                "换手率": 2.1, "总市值": 8_000_000_000_000,
                "上涨家数": 80, "下跌家数": 20,
                "领涨股票": "龙头科技", "领涨股票-涨跌幅": 12.0,
            },
            {
                "板块名称": "银行", "板块代码": "BK0475", "涨跌幅": -0.2,
                "换手率": 0.4, "总市值": 10_000_000_000_000,
                "上涨家数": 10, "下跌家数": 30,
                "领涨股票": "稳健银行", "领涨股票-涨跌幅": 1.1,
            },
        ])
        concept = pd.DataFrame([
            {
                "板块名称": "AI算力", "板块代码": "BK2001", "涨跌幅": 4.2,
                "换手率": 4.1, "总市值": 4_000_000_000_000,
                "上涨家数": 45, "下跌家数": 5,
                "领涨股票": "算力股份", "领涨股票-涨跌幅": 10.0,
            },
            {
                "板块名称": "昨日连板", "板块代码": "BK9999", "涨跌幅": 8.0,
                "换手率": 9.0, "总市值": 1_000_000,
                "上涨家数": 9, "下跌家数": 1,
                "领涨股票": "样本", "领涨股票-涨跌幅": 10.0,
            },
        ])
        industry_flow = pd.DataFrame([{
            "名称": "半导体", "主力净流入-净额": 2_000_000_000,
            "主力净流入-净占比": 4.5, "主力净流入最大股": "资金科技",
            "主力净流入最大股代码": "600002",
        }])
        concept_flow = pd.DataFrame([{
            "名称": "AI算力", "主力净流入-净额": 3_000_000_000,
            "主力净流入-净占比": 5.5, "主力净流入最大股": "算力股份",
            "主力净流入最大股代码": "300001",
        }])
        snapshot = [
            {"symbol": "688001", "name": "龙头科技", "tradeDate": "20260828", "close": 88.0, "pctChg": 12.0, "amount": 5_000_000_000},
            {"symbol": "600002", "name": "资金科技", "tradeDate": "20260828", "close": 20.0, "pctChg": 6.0, "amount": 8_000_000_000},
            {"symbol": "300001", "name": "算力股份", "tradeDate": "20260828", "close": 30.0, "pctChg": 10.0, "amount": 7_000_000_000},
        ]
        with (
            patch.object(service, "market_snapshot", return_value=snapshot),
            patch.object(service, "_industry_name_frame", return_value=(industry, "行业源")),
            patch.object(service, "_concept_name_frame", return_value=(concept, "概念源")),
            patch.object(service, "_sector_fund_flow_frame", side_effect=[(industry_flow, "资金源"), (concept_flow, "资金源")]),
            patch("backend.data_sources.database.get_meta", return_value=None),
            patch("backend.data_sources.database.set_meta"),
            patch("backend.data_sources.database.now_iso", return_value="2026-08-28T15:10:00+08:00"),
        ):
            result = service.sector_overview()

        self.assertEqual(result["summary"]["industryCount"], 2)
        self.assertEqual(result["summary"]["conceptCount"], 1)
        self.assertEqual(result["summary"]["topBoard"]["name"], "AI算力")
        self.assertEqual(result["summary"]["topFundBoard"]["mainNetInflow"], 3_000_000_000)
        self.assertEqual(result["industryBoards"][0]["leaders"][0]["code"], "688001")
        self.assertEqual(result["industryBoards"][0]["leaders"][1]["role"], "资金龙头")
        self.assertEqual(len(result["conceptBoards"][0]["leaders"]), 1)


if __name__ == "__main__":
    unittest.main()
