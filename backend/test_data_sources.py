from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pandas as pd

from backend.data_sources import MarketDataService
from backend.eastmoney import EastmoneyClient
from backend.strategy_factors import volume_breakout_picks, wilder_rsi
from backend.tencent import TencentClient


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

    def test_sector_history_route_returns_recent_turnover(self) -> None:
        client = EastmoneyClient()
        response = _FakeResponse({
            "data": {
                "trends": [
                    "2026-08-27 09:30,1,2,3,0,100,400000000,2",
                    "2026-08-27 15:00,1,2,3,0,100,800000000,2",
                    "2026-08-28 09:30,1,2,3,0,100,600000000,2",
                    "2026-08-28 15:00,1,2,3,0,100,1200000000,2",
                ],
            },
        })
        session = Mock()
        session.get.return_value = response
        with patch.object(client, "_session", return_value=session):
            frame = client.sector_history_frame("BK1036")

        self.assertEqual(list(frame["日期"]), ["2026-08-27", "2026-08-28"])
        self.assertEqual(frame.iloc[0]["成交额"], 1_200_000_000)
        self.assertEqual(frame.iloc[-1]["成交额"], 1_800_000_000)
        self.assertEqual(session.get.call_args.kwargs["params"]["ndays"], "2")
        session.close.assert_called_once()

    def test_market_intraday_turnover_pair_compares_same_minute(self) -> None:
        client = EastmoneyClient()
        sh_rows = [
            "2026-08-27 09:30,1,2,3,0,100,400000000,2",
            "2026-08-27 10:00,1,2,3,0,100,600000000,2",
            "2026-08-27 15:00,1,2,3,0,100,2000000000,2",
            "2026-08-28 09:30,1,2,3,0,100,500000000,2",
            "2026-08-28 10:00,1,2,3,0,100,700000000,2",
        ]
        sz_rows = [
            "2026-08-27 09:30,1,2,3,0,100,300000000,2",
            "2026-08-27 10:00,1,2,3,0,100,500000000,2",
            "2026-08-27 15:00,1,2,3,0,100,1800000000,2",
            "2026-08-28 09:30,1,2,3,0,100,450000000,2",
            "2026-08-28 10:00,1,2,3,0,100,650000000,2",
        ]
        with patch.object(client, "_trend_rows", side_effect=[sh_rows, sz_rows]):
            result = client.market_intraday_turnover_pair("20260828", "10:00")

        self.assertEqual(result["previousDate"], "20260827")
        self.assertEqual(result["comparisonTime"], "10:00")
        self.assertEqual(result["previousTurnover"], 1_800_000_000)
        self.assertEqual(result["currentTurnover"], 2_300_000_000)

    def test_tencent_intraday_pair_uses_cumulative_amount_at_same_minute(self) -> None:
        client = TencentClient()
        sh_days = [
            {"date": "20260827", "data": ["0930 1 10 400", "1000 1 20 1000", "1500 1 30 5000"]},
            {"date": "20260828", "data": ["0930 1 10 500", "1000 1 20 1300"]},
        ]
        sz_days = [
            {"date": "20260827", "data": ["0930 1 10 300", "1000 1 20 800", "1500 1 30 4000"]},
            {"date": "20260828", "data": ["0930 1 10 450", "1000 1 20 1100"]},
        ]
        with patch.object(client, "_index_days", side_effect=[sh_days, sz_days]):
            result = client.index_intraday_turnover_pair("20260828", "10:00")

        self.assertEqual(result["previousDate"], "20260827")
        self.assertEqual(result["comparisonTime"], "10:00")
        self.assertEqual(result["previousTurnover"], 1800)
        self.assertEqual(result["currentTurnover"], 2400)

    def test_tencent_market_fund_flow_aggregates_stock_net_values(self) -> None:
        client = TencentClient()
        rows = [
            {"turnover": "1000", "zljlr": "120.5"},
            {"turnover": "500", "zljlr": "-20.5"},
            {"turnover": "-", "zljlr": "-"},
        ]
        with patch.object(client, "fetch_rank_rows", return_value=rows):
            result = client.market_fund_flow_snapshot("20260828")

        self.assertEqual(result["mainNetInflow"], 1_000_000)
        self.assertAlmostEqual(result["mainNetInflowRatio"], 100 / 1500 * 100)
        self.assertEqual(result["source"], "腾讯证券逐股资金汇总")

    def test_fund_flow_falls_back_to_tencent_and_persists_snapshot(self) -> None:
        eastmoney = Mock()
        eastmoney.market_fund_flow_frame.side_effect = RuntimeError("blocked")
        tencent = Mock()
        tencent.market_fund_flow_snapshot.return_value = {
            "date": "20260828",
            "shClose": None,
            "shPctChg": None,
            "szClose": None,
            "szPctChg": None,
            "mainNetInflow": 1_000_000,
            "mainNetInflowRatio": 1.2,
            "superLargeNetInflow": None,
            "largeNetInflow": None,
            "source": "腾讯证券逐股资金汇总",
        }
        service = MarketDataService(eastmoney_client=eastmoney, tencent_client=tencent)
        with (
            patch("backend.data_sources.database.get_meta", return_value=None),
            patch("backend.data_sources.database.set_meta") as set_meta,
            patch("backend.data_sources.database.now_iso", return_value="2026-08-28T10:00:00+08:00"),
        ):
            rows, warning, status = service._fund_flow_history("20260828")

        self.assertEqual(rows[-1]["mainNetInflow"], 1_000_000)
        self.assertIn("腾讯证券", warning or "")
        self.assertEqual(status["state"], "fallback")
        self.assertEqual(status["source"], "腾讯证券逐股资金汇总")
        set_meta.assert_called_once()

    def test_intraday_comparison_falls_back_to_tencent(self) -> None:
        eastmoney = EastmoneyClient()
        tencent = Mock()
        tencent.index_intraday_turnover_pair.return_value = {
            "date": "20260828",
            "previousDate": "20260827",
            "comparisonTime": "10:00",
            "currentTurnover": 2_400,
            "previousTurnover": 1_800,
        }
        service = MarketDataService(eastmoney_client=eastmoney, tencent_client=tencent)
        with (
            patch.object(eastmoney, "index_intraday_turnover_points", side_effect=RuntimeError("blocked")),
            patch("backend.data_sources.database.get_meta", return_value=None),
            patch("backend.data_sources.database.set_meta"),
            patch("backend.data_sources.database.now_iso", return_value="2026-08-28T10:00:00+08:00"),
        ):
            result, warning, status = service._intraday_turnover_comparison(
                "20260828",
                [{"tradeDate": "20260828", "quoteTime": "10:00"}],
            )

        self.assertEqual(result["previousTurnover"], 1_800)
        self.assertIn("腾讯证券", warning or "")
        self.assertEqual(status["state"], "fallback")

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
        self.assertEqual(rows[0]["quoteTime"], "15:37")
        self.assertEqual(rows[0]["source"], "东方财富备用线路")

    def test_market_comparison_time_respects_lunch_and_close(self) -> None:
        service = MarketDataService()
        self.assertEqual(
            service._market_comparison_time(
                "20260828",
                [{"tradeDate": "20260828", "quoteTime": "12:10"}],
            ),
            "11:30",
        )
        self.assertEqual(
            service._market_comparison_time(
                "20260828",
                [{"tradeDate": "20260828", "quoteTime": "15:20"}],
            ),
            "15:00",
        )

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
            "largeNetInflow": 3_000_000_000, "source": "东方财富大盘资金流",
        }]
        live_status = {
            "state": "live", "source": "测试源",
            "updatedAt": "2026-08-28T10:00:00+08:00", "message": None,
        }
        with (
            patch.object(service, "market_snapshot", return_value=snapshot),
            patch.object(service, "_index_turnover_history", return_value=([{"date": "20260827", "turnover": 10_000_000_000}], None)),
            patch.object(
                service,
                "_intraday_turnover_comparison",
                return_value=({
                    "previousDate": "20260827",
                    "comparisonTime": "10:00",
                    "currentTurnover": 9_000_000_000,
                    "previousTurnover": 8_000_000_000,
                }, None, live_status),
            ),
            patch.object(service, "_fund_flow_history", return_value=(flow, None, live_status)),
            patch("backend.data_sources.database.set_meta"),
            patch("backend.data_sources.database.now_iso", return_value="2026-08-28T15:10:00+08:00"),
        ):
            result = service.market_overview()

        self.assertEqual(result["snapshot"]["turnover"], 11_000_000_000)
        self.assertEqual(result["snapshot"]["turnoverDelta"], 1_000_000_000)
        self.assertEqual(result["snapshot"]["turnoverDeltaPct"], 12.5)
        self.assertEqual(result["snapshot"]["turnoverComparisonDate"], "20260827")
        self.assertEqual(result["snapshot"]["turnoverComparisonTime"], "10:00")
        self.assertEqual(result["turnoverHistory"][-1]["turnover"], 10_000_000_000)
        self.assertEqual(result["snapshot"]["advancers"], 2)
        self.assertEqual(result["snapshot"]["decliners"], 1)
        self.assertEqual(result["snapshot"]["flat"], 1)
        self.assertEqual(result["snapshot"]["limitUp"], 1)
        self.assertEqual(result["snapshot"]["limitDown"], 1)
        self.assertEqual(result["latestFlow"]["mainNetInflow"], 8_000_000_000)
        self.assertEqual(result["dataStatus"]["fundFlow"]["state"], "live")

    def test_sector_overview_merges_strength_fund_flow_and_leaders(self) -> None:
        service = MarketDataService()
        industry = pd.DataFrame([
            {
                "板块名称": "半导体", "板块代码": "BK1036", "涨跌幅": 3.5,
                "成交额": 4_000_000_000,
                "换手率": 2.1, "总市值": 8_000_000_000_000,
                "上涨家数": 80, "下跌家数": 20,
                "领涨股票": "龙头科技", "领涨股票-涨跌幅": 12.0,
            },
            {
                "板块名称": "银行", "板块代码": "BK0475", "涨跌幅": -0.2,
                "成交额": 2_000_000_000,
                "换手率": 0.4, "总市值": 10_000_000_000_000,
                "上涨家数": 10, "下跌家数": 30,
                "领涨股票": "稳健银行", "领涨股票-涨跌幅": 1.1,
            },
        ])
        concept = pd.DataFrame([
            {
                "板块名称": "AI算力", "板块代码": "BK2001", "涨跌幅": 4.2,
                "成交额": 6_000_000_000,
                "换手率": 4.1, "总市值": 4_000_000_000_000,
                "上涨家数": 45, "下跌家数": 5,
                "领涨股票": "算力股份", "领涨股票-涨跌幅": 10.0,
            },
            {
                "板块名称": "昨日连板", "板块代码": "BK9999", "涨跌幅": 8.0,
                "成交额": 1_000_000_000,
                "换手率": 9.0, "总市值": 1_000_000,
                "上涨家数": 9, "下跌家数": 1,
                "领涨股票": "样本", "领涨股票-涨跌幅": 10.0,
            },
            {
                "板块名称": "富时罗素", "板块代码": "BK0867", "涨跌幅": 0.5,
                "成交额": 99_000_000_000,
                "换手率": 1.0, "总市值": 20_000_000_000_000,
                "上涨家数": 60, "下跌家数": 40,
                "领涨股票": "宽基样本", "领涨股票-涨跌幅": 2.0,
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
            patch.object(
                service,
                "_sector_amount_pair",
                side_effect=lambda kind, code, _date: {
                    "BK1036": (4_000_000_000, 3_000_000_000, "历史源"),
                    "BK0475": (2_000_000_000, 2_500_000_000, "历史源"),
                    "BK2001": (6_000_000_000, 4_000_000_000, "历史源"),
                }[code],
            ),
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
        self.assertEqual(result["industryBoards"][0]["amount"], 4_000_000_000)
        self.assertEqual(result["industryBoards"][0]["previousAmount"], 3_000_000_000)
        self.assertEqual(result["industryBoards"][0]["amountDelta"], 1_000_000_000)
        self.assertEqual(result["industryBoards"][1]["amountDelta"], -500_000_000)
        self.assertEqual(result["conceptBoards"][0]["amountDelta"], 2_000_000_000)
        self.assertEqual(len(result["conceptBoards"][0]["leaders"]), 1)
        self.assertEqual(
            [board["name"] for board in result["turnoverBoards"]],
            ["AI算力", "半导体", "银行"],
        )
        self.assertEqual(result["turnoverBoards"][0]["amount"], 6_000_000_000)
        self.assertEqual(result["turnoverBoards"][0]["amountDelta"], 2_000_000_000)


if __name__ == "__main__":
    unittest.main()
