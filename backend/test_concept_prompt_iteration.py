from __future__ import annotations

import copy
import unittest
from unittest.mock import AsyncMock, patch

from backend import ai, concept_ai, database
from backend.test_concept_ai import evidence_fixture, result_fixture


class ConceptPromptIterationTests(unittest.TestCase):
    def test_model_can_abstain_even_with_populated_candidates(self) -> None:
        raw = {"summary": "近期收益与当日广度信号冲突，等待行情与催化进一步确认。", "concepts": []}
        result = concept_ai.assemble_result(raw, evidence_fixture())
        self.assertEqual(result["concepts"], [])
        self.assertEqual(result["conceptProvider"], "eastmoney")
        self.assertEqual(result["summary"], raw["summary"])

    def test_lack_of_news_does_not_force_an_invented_catalyst(self) -> None:
        raw = result_fixture()
        raw["concepts"][0]["catalysts"] = []
        result = concept_ai.assemble_result(raw, evidence_fixture())
        self.assertEqual(result["concepts"][0]["catalysts"], [])

    def test_mixed_or_duplicate_candidates_fail_before_prompt_and_assembly(self) -> None:
        for code in ("THS:885955", "BK1001", "invalid"):
            evidence = evidence_fixture()
            duplicate = copy.deepcopy(evidence["candidates"][0])
            duplicate["code"] = code
            evidence["candidates"].append(duplicate)
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    concept_ai.build_prompt(evidence)
                with self.assertRaises(ValueError):
                    concept_ai.assemble_result(result_fixture(), evidence)

    def test_same_stock_in_related_concepts_preserves_each_own_evidence(self) -> None:
        evidence, raw = evidence_fixture(), result_fixture()
        candidate = copy.deepcopy(evidence["candidates"][0])
        candidate["code"] = "BK1002"
        candidate["evidence"][0]["id"] = "BK1002:market"
        evidence["candidates"].append(candidate)
        choice = copy.deepcopy(raw["concepts"][0])
        choice["code"] = "BK1002"
        choice["drivers"][0]["evidenceIds"] = ["BK1002:market"]
        raw["concepts"].append(choice)
        result = concept_ai.assemble_result(raw, evidence)
        self.assertEqual([row["stocks"][0]["code"] for row in result["concepts"]], ["600001", "600001"])
        self.assertEqual(result["concepts"][1]["drivers"][0]["sources"][0]["id"], "BK1002:market")
        choice["drivers"][0]["evidenceIds"] = ["BK1001:market"]
        with self.assertRaisesRegex(ValueError, "匹配来源"):
            concept_ai.assemble_result(raw, evidence)

    def test_current_observation_contract_covers_weak_trend_and_intraday_attribution(self) -> None:
        evidence = evidence_fixture()
        evidence["candidates"][0].update(change5d=-2.0, change10d=3.0, mainNetInflow=None)
        prompt = concept_ai.build_prompt(evidence)
        self.assertIn("选0至3个", prompt)
        self.assertIn("至少一项已验证为正", prompt)
        self.assertIn("同一交易日的盘后或行情快照之后", prompt)
        self.assertIn("首次公开时间未知", prompt)
        self.assertIn("不等于分散", prompt)
        self.assertIn('"mainNetInflow":null', prompt)
        self.assertIn("不是未来半个月预测", concept_ai.SYSTEM_INSTRUCTION)
        self.assertNotIn("厄尔尼诺", prompt)

    def test_evaluation_universe_is_not_sent_as_researched_candidates(self) -> None:
        evidence = evidence_fixture()
        evidence["comparisonUniverse"] = {"boards": [{"code": "BK9000", "name": "仅用于事后对照"}]}
        prompt = concept_ai.build_prompt(evidence)
        self.assertNotIn("BK9000", prompt)
        self.assertIn('"code":"BK1001"', prompt)
        self.assertEqual(evidence["comparisonUniverse"]["boards"][0]["code"], "BK9000")

    def test_future_news_cannot_be_cited_while_post_quote_news_keeps_its_timing(self) -> None:
        evidence = evidence_fixture()
        evidence.update(decisionAt="2026-09-11T12:00:00+08:00", quoteAsOf="2026-09-11T10:00:00+08:00")
        evidence["candidates"][0]["evidence"][0]["publishedAt"] = evidence["quoteAsOf"]
        source = {"kind": "news", "title": "订单披露", "excerpt": "公司披露新增订单。",
                  "url": "https://example.com/order", "source": "公告"}
        evidence["candidates"][0]["evidence"].extend([
            {**source, "id": "BK1001:news:later", "title": "截止后才发布的消息",
             "publishedAt": "2026-09-11T13:00:00+08:00"},
            {**source, "id": "BK1001:news:current", "publishedAt": "2026-09-11T11:00:00+08:00"},
        ])
        prompt = concept_ai.build_prompt(evidence)
        self.assertNotIn("截止后才发布的消息", prompt)
        self.assertIn('"temporalRelation":"after_quote"', prompt)
        raw = result_fixture()
        catalyst = raw["concepts"][0]["catalysts"][0]
        catalyst.update(status="reported", evidenceIds=["BK1001:news:later"])
        with self.assertRaisesRegex(ValueError, "现实催化"):
            concept_ai.assemble_result(raw, evidence)
        catalyst["evidenceIds"] = ["BK1001:news:current"]
        result = concept_ai.assemble_result(raw, evidence)
        news = result["concepts"][0]["catalysts"][0]["sources"][0]
        self.assertEqual(news["temporalRelation"], "after_quote")
        self.assertEqual(news["publishedAt"], "2026-09-11T11:00:00+08:00")


class ConceptPromptExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_provider_rejected_without_paid_news_or_model_call(self) -> None:
        evidence = evidence_fixture()
        evidence["candidates"].append({**evidence["candidates"][0], "code": "THS:885955"})
        with (patch.object(concept_ai, "collect_concept_evidence", return_value=evidence),
              patch.object(concept_ai, "enrich_world_news", new_callable=AsyncMock) as news,
              patch.object(ai, "_call_compatible", new_callable=AsyncMock) as model,
              patch.object(database, "finish_ai_run") as finish):
            await concept_ai._execute("2026-10-10", "test-key", "test-token")
        news.assert_not_awaited()
        model.assert_not_awaited()
        self.assertIn("同一行情来源", finish.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
