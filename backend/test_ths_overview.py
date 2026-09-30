from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backend import concept_data, database, research_data
from backend.data_sources import MarketDataService
from backend.history_sources import ths_concept_history
from backend.ths import ths_client


def frame(kind, day="2026-09-30"):
    result = pd.DataFrame([{"板块代码": "THS:886042" if kind == "concept" else "THS:881155",
        "板块名称": "测试概念" if kind == "concept" else "测试行业", "涨跌幅": 2.5, "成交额": 100_000_000,
        "上涨家数": 8, "下跌家数": 2, "行情时间": datetime.fromisoformat(day + "T15:00:00+08:00").timestamp(),
        "领涨股票": ""}])
    result.attrs = {"coverage": {"kind": "sample", "isComplete": False}, "warnings": ["样本来源"]}
    return result


class THSOverviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "test.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        self.service = MarketDataService(sector_source="ths")

    def test_sample_has_quoted_date_isolated_cache_and_no_eastmoney_calls(self):
        stock = {"code": "600001", "name": "同日股票", "price": 8, "pctChg": 2, "amount": 200_000_000, "tradeDate": "20260930"}
        with (patch.object(ths_client, "board_frame", side_effect=lambda kind, **_: frame(kind)),
              patch.object(ths_client, "strong_stocks", return_value=[stock, {**stock, "tradeDate": "20260929"}]),
              patch.object(self.service, "market_snapshot") as market,
              patch.object(self.service, "_concept_name_frame") as east):
            result = self.service.sector_overview()
        self.assertEqual(result["tradeDate"], "20260930")
        self.assertEqual(result["quoteAsOf"], "2026-09-30T15:00:00+08:00")
        self.assertIn("非全市场", result["scope"])
        self.assertEqual(result["sourceProvider"], "ths")
        board = result["conceptBoards"][0]
        self.assertEqual(len(board["leaders"]), 1)
        self.assertEqual(board["leaders"][0]["role"], "样本强势股")
        for field in ("mainNetInflow", "previousAmount", "amountDelta"):
            self.assertIsNone(board[field])
        self.assertEqual(research_data.latest_universe("ths")["scope"], result["scope"])
        self.assertIsNone(database.get_meta("sector_overview:v4"))
        self.assertIsNotNone(database.get_meta("sector_overview:ths:v4"))
        east.assert_not_called()
        market.assert_not_called()

    def test_failed_refresh_only_preserves_previous_ths_snapshot(self):
        database.set_meta("sector_overview:v4", json.dumps({"conceptBoards": [{"code": "BK1001"}]}))
        with patch.object(ths_client, "board_frame", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "同花顺概念"):
                self.service.sector_overview(True)
        with (patch.object(ths_client, "board_frame", side_effect=lambda kind, **_: frame(kind)),
              patch.object(ths_client, "strong_stocks", return_value=[])):
            saved = self.service.sector_overview(True)
        with patch.object(ths_client, "board_frame", side_effect=RuntimeError("unavailable")):
            result = self.service.sector_overview(True)
        self.assertEqual(result["refreshStatus"], "failed")
        self.assertTrue(result["usingCachedSnapshot"])
        self.assertEqual(result["tradeDate"], saved["tradeDate"])
        self.assertEqual(result["updatedAt"], saved["updatedAt"])
        self.assertEqual(result["conceptBoards"], saved["conceptBoards"])

    def test_old_industry_session_is_not_combined_with_new_concept_quotes(self):
        with (patch.object(ths_client, "board_frame", side_effect=lambda kind, **_: frame(kind, "2026-09-29" if kind == "industry" else "2026-09-30")),
              patch.object(ths_client, "strong_stocks", return_value=[])):
            result = self.service.sector_overview(True)
        self.assertEqual(result["refreshStatus"], "partial")
        self.assertEqual(result["industryBoards"], [])
        self.assertEqual(result["summary"]["industryCount"], 0)

    def test_ths_history_and_members_do_not_request_eastmoney(self):
        client = concept_data.ConceptResearchClient()
        history = {"rows": [{"date": "2026-09-30", "close": 120, "amount": None, "pctChg": 2}],
                   "source": "同花顺概念日线", "url": "https://q.10jqka.com.cn/gn/"}
        with (patch.object(client, "_json") as east,
              patch.object(concept_data, "ths_concept_history", return_value=history) as prices,
              patch.object(ths_client, "strong_stocks", return_value=[{"code": "600001"}]) as stocks):
            rows = client.daily_history("THS:886042", "20260930")
            client.strong_stocks("THS:886042", "20260930")
            self.assertEqual(client.daily_history("THS:886042", "20260930"), rows)
        self.assertIsNone(rows[0]["amount"])
        self.assertEqual(rows[0]["historySource"], history["source"])
        prices.assert_called_once()
        stocks.assert_called_once_with("THS:886042", "20260930")
        east.assert_not_called()

    def test_ths_history_amount_and_returns_are_from_verified_daily_bars(self):
        payload = 'quotebridge_v4_line_bk_886042_01_2026({"data":"20260929,100,110,95,105,100,250000000;20260930,105,115,100,110,90,-"})'
        with patch("backend.history_sources._get_text", return_value=payload):
            result = ths_concept_history("THS:886042", "2026-09-29", "2026-09-30")
        self.assertEqual(result["rows"][0]["amount"], 250_000_000)
        self.assertIsNone(result["rows"][1]["amount"])
        self.assertAlmostEqual(result["rows"][1]["pctChg"], (110 / 105 - 1) * 100)

    def test_history_retention_keeps_two_per_provider_and_code(self):
        for code in ("BK1001", "THS:886042", "THS:886033"):
            for day in ("20260928", "20260929", "20260930", "20260925"):
                database.set_meta(f"concept_history:{code}:{day}:v1", "{}")
            for day in ("20260929", "20260930"):
                self.assertIsNotNone(database.get_meta(f"concept_history:{code}:{day}:v1"))
            for day in ("20260925", "20260928"):
                self.assertIsNone(database.get_meta(f"concept_history:{code}:{day}:v1"))


if __name__ == "__main__":
    unittest.main()
