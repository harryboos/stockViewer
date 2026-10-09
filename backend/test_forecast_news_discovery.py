from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.concept_news import discover_world_news


def article(title: str, index: int, excerpt: str = "公开产业消息") -> dict:
    return {"title": title, "excerpt": excerpt, "url": f"https://example.org/news/{index}",
            "publishedAt": "2026-09-13T00:00:00+08:00", "publishedAtPrecision": "date", "kind": "news"}


class ForecastNewsDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ascii_abbreviation_does_not_match_english_word_fragments(self):
        news = [article("Microsoft introduces micro-scale platform", 1),
                article("macro research", 2), article("CROWD company report", 3),
                article("科研 CRO订单出现变化", 4)]
        with patch("backend.concept_news.search_world_news", new=AsyncMock(return_value=news)):
            sources, metadata = await discover_world_news([{"code": "BK0899", "name": "CRO"}], "test")
        self.assertEqual([row["url"] for row in sources["BK0899"]], ["https://example.org/news/4"])
        self.assertEqual(metadata["matchedCount"], 1)
        self.assertEqual(sources["BK0899"][0]["publishedAtPrecision"], "date")

    async def test_ascii_case_punctuation_and_adjacent_chinese_are_valid(self):
        news = [article("国内cro订单变化", 1), article("产业报告：CRO、PCB", 2),
                article("端侧升级", 3, "PCB_A CRO2026 microprocessor")]
        with patch("backend.concept_news.search_world_news", new=AsyncMock(return_value=news)):
            sources, _ = await discover_world_news([{"code": "BK0899", "name": "CRO"},
                                                    {"code": "BK0877", "name": "PCB"}], "test")
        self.assertEqual({row["url"] for row in sources["BK0899"]},
                         {"https://example.org/news/1", "https://example.org/news/2"})
        self.assertEqual([row["url"] for row in sources["BK0877"]], ["https://example.org/news/2"])

    async def test_chinese_names_keep_literal_matching_without_fixed_preferences(self):
        news = [article("重组蛋白研发动态", 1), article("新型材料供需变化", 2)]
        with patch("backend.concept_news.search_world_news", new=AsyncMock(return_value=news)):
            sources, _ = await discover_world_news([{"code": "BK1063", "name": "重组蛋白概念"},
                                                    {"code": "BK9000", "name": "新型材料"}], "test")
        self.assertEqual(set(sources), {"BK1063", "BK9000"})

    async def test_no_key_skips_search_and_partial_search_keeps_valid_matches(self):
        with patch("backend.concept_news.search_world_news", new=AsyncMock()) as search:
            sources, status = await discover_world_news([{"code": "BK0899", "name": "CRO"}], "")
            search.assert_not_called()
        self.assertFalse(sources)
        self.assertEqual(status["status"], "unavailable")
        with patch("backend.concept_news.search_world_news", new=AsyncMock(side_effect=[
                RuntimeError("unavailable"), [article("CRO需求观察", 1)]])):
            sources, status = await discover_world_news([{"code": "BK0899", "name": "CRO"}], "test")
        self.assertIn("BK0899", sources)
        self.assertEqual(status["status"], "partial")


if __name__ == "__main__":
    unittest.main()
