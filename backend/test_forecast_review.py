from __future__ import annotations

import copy
import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from backend import concept_data, concept_forecast
from backend.forecast_data import technical_metrics
from backend.forecast_review import FORWARD_SELECTION_INSTRUCTION, REVIEW_INSTRUCTION, prompt_candidates
from backend.test_concept_forecast import forecast_evidence, forecast_result


def bars(closes):
    return [{"date": (date(2026, 8, 20) + timedelta(days=i)).strftime("%Y%m%d"),
             "close": close, "amount": 100_000_000} for i, close in enumerate(closes)]


class ForecastReviewTests(unittest.TestCase):
    def test_reversal_features_can_improve_while_recent_five_day_return_is_negative(self):
        history = bars([100] * 15 + [98, 96, 94, 92, 90, 89, 90, 91, 92, 93])
        last = history[-1]["date"]
        result = technical_metrics(history, last)
        self.assertLess((93 / 94 - 1) * 100, 0)
        self.assertGreater(result["change3d"], 0)
        self.assertGreater(result["momentumChange5d"], 0)
        self.assertLess(result["distanceToMa20"], 0)
        self.assertEqual(result["upDays5d"], 4)
        self.assertAlmostEqual(result["drawdown5d"], round((89 / 90 - 1) * 100, 2))
        # Appending later prices must never alter what the prior decision sees.
        later = history + [{"date": "20990101", "close": 200, "amount": 1_000_000_000}]
        self.assertEqual(technical_metrics(later, last), result)

    def test_short_or_stale_history_never_invents_rotation_features(self):
        short = bars([100, 101, 100])
        result = technical_metrics(short, short[-1]["date"])
        for name in ("change3d", "change20d", "momentumChange5d", "distanceToMa20", "drawdown5d"):
            self.assertIsNone(result[name])
        stale = technical_metrics(bars([100] * 30), "20261010")
        self.assertTrue(all(value is None for name, value in stale.items() if name != "historyAsOf"))

    def test_candidate_audit_keeps_rejected_inputs_without_mutating_evidence(self):
        evidence = forecast_evidence()
        alternative = copy.deepcopy(evidence["candidates"][0])
        alternative.update(code="BK1002", name="另一个方向")
        for source in alternative["evidence"]:
            source["id"] = source["id"].replace("BK1001:", "BK1002:")
        evidence["candidates"].append(alternative)
        evidence["decisionAt"] = "2026-09-12T12:00:00+08:00"
        original = copy.deepcopy(evidence)
        result = concept_forecast.assemble_result(forecast_result(), evidence)
        audit = result["candidateAudit"]
        self.assertTrue(audit["complete"])
        self.assertEqual(audit["decisionAt"], evidence["decisionAt"])
        self.assertEqual([item["selected"] for item in audit["candidates"]], [True, False])
        self.assertEqual(audit["candidates"][1]["technicalData"], alternative["technicalData"])
        self.assertTrue(audit["candidates"][1]["news"][0]["url"])
        self.assertNotIn("excerpt", audit["candidates"][1]["news"][0])
        self.assertEqual(evidence, original)
        self.assertEqual(audit["modelCandidateOrder"], [row["code"] for row in concept_forecast.prompt_evidence(evidence)["candidates"]])
        self.assertEqual(audit["presentationPolicy"], "snapshot-code-hash-v1")

    def test_forecast_uses_new_recall_pool_while_recommendation_keeps_original_pool(self):
        evidence = forecast_evidence()
        a = {**evidence["candidates"][0], "kind": "concept"}
        b = {**a, "code": "BK1002", "name": "广度改善方向", "pctChg": -0.1,
             "candidateSignals": [{"metric": "breadth", "rank": 1, "value": 80, "selectedBy": True}]}
        overview = {"tradeDate": evidence["tradeDate"], "updatedAt": evidence["dataAsOf"],
                    "conceptBoards": [a], "researchConcepts": [a], "forecastConcepts": [b],
                    "forecastCandidateSelection": {"method": "snapshot-rank-round-robin-v1", "availableCount": 20}, "warnings": []}
        with (patch.object(concept_data, "_concept_overview", return_value=(overview, None)),
              patch("backend.forecast_pipeline.cached_histories", return_value={}),
              patch("backend.forecast_prices.FeedbackPriceClient.calendar", return_value=[]),
              patch.object(concept_data.ConceptResearchClient, "daily_history", return_value=[]),
              patch.object(concept_data.ConceptResearchClient, "strong_stocks", return_value=[])):
            forecast = concept_data.collect_concept_evidence(forecast=True)
            recommended = concept_data.collect_concept_evidence()
            del overview["forecastConcepts"]
            legacy = concept_data.collect_concept_evidence(forecast=True)
        self.assertEqual(forecast["candidates"], [])
        self.assertEqual([row["code"] for row in forecast["comparisonUniverse"]["boards"]], ["BK1002"])
        self.assertIn("多路", forecast["scope"])
        self.assertEqual([row["code"] for row in recommended["candidates"]], ["BK1001"])
        self.assertEqual([row["code"] for row in legacy["comparisonUniverse"]["boards"]], ["BK1001"])
        self.assertEqual(legacy["candidateSelection"]["historyQuality"]["eligibleCount"], 0)

    def test_prompt_learns_conditions_not_a_named_hindsight_winner(self):
        prompt = concept_forecast.build_prompt(forecast_evidence())
        self.assertIn(REVIEW_INSTRUCTION, prompt)
        for term in ("初期修复", "首次公开时间", "outside_pool", "unknown", "同一产业链", "decisionAt", "missedLeaderCases"):
            self.assertIn(term, prompt)
        self.assertNotIn("重组蛋白", REVIEW_INSTRUCTION)
        self.assertNotIn("BK1063", REVIEW_INSTRUCTION)
        self.assertEqual(concept_forecast.MODEL, "deepseek-flash")
        self.assertEqual(concept_forecast.PROMPT_VERSION, "forecast-v11-forward-edge-deepseek-flash-max")

    def test_prompt_removes_rank_anchors_but_preserves_metrics_and_audit(self):
        evidence = forecast_evidence()
        template = evidence["candidates"][0]
        evidence["candidates"] = []
        for index in range(1, 13):
            candidate = copy.deepcopy(template)
            candidate.update(code=f"BK{index:04d}", pctChg=20 - index, name=f"方向{index}",
                             candidateSignals=[{"metric": "pctChg", "value": 20 - index,
                                                "rank": index, "selectedBy": True}])
            for source in candidate["evidence"]:
                source["id"] = source["id"].replace("BK1001:", f"{candidate['code']}:")
            evidence["candidates"].append(candidate)
        evidence["candidateSelection"] = {"baseline": {"dailyTop3Codes": ["BK0001", "BK0002", "BK0003"]},
                                           "exploration": {"codes": ["BK0012"], "selectedCount": 1}}
        original = copy.deepcopy(evidence)
        compact = concept_forecast.prompt_evidence(evidence)
        codes = [row["code"] for row in compact["candidates"]]
        self.assertCountEqual(codes, [row["code"] for row in evidence["candidates"]])
        self.assertNotEqual(codes, [row["code"] for row in evidence["candidates"]])
        self.assertNotIn("baseline", compact["candidateSelection"])
        self.assertEqual(compact["candidateSelection"]["exploration"], {"selectedCount": 1})
        for row in compact["candidates"]:
            self.assertNotIn("candidateSignals", row)
            self.assertEqual(row["pctChg"], 20 - int(row["code"][2:]))
            self.assertEqual(row["technicalData"], template["technicalData"])
            self.assertEqual(row["evidence"][-1]["excerpt"], template["evidence"][-1]["excerpt"])
        evidence["candidates"].reverse()
        for row in evidence["candidates"]:
            row.update(pctChg=-row["pctChg"], name="重命名")
            row["candidateSignals"][0].update(rank=1, selectedBy=False)
        self.assertEqual(codes, [row["code"] for row in prompt_candidates(evidence)])
        self.assertEqual(compact, concept_forecast.prompt_evidence(original))
        # The actual call uses this presentation too, not only a standalone helper.
        payload = json.loads(concept_forecast.build_prompt(original).split("以下仅为数据材料：", 1)[1])
        self.assertEqual(codes, [row["code"] for row in payload["candidates"]])
        self.assertTrue(original["candidates"][0]["candidateSignals"][0]["selectedBy"])

    def test_forward_selection_contract_allows_strength_but_requires_remaining_edge(self):
        prompt = concept_forecast.build_prompt(forecast_evidence())
        self.assertIn(FORWARD_SELECTION_INSTRUCTION, prompt)
        self.assertLess(prompt.index(FORWARD_SELECTION_INSTRUCTION), prompt.index("technical分析"))
        for clause in ("剩余增量；窗口内验证机会；已兑现或失败风险；相对另一个候选的取舍",
                       "趋势延续可以入选", "低位修复也需证据", "无法判断", "不能只把conviction降为low",
                       "只有一个候选", "不虚构对手", "不能把长期逻辑硬塞进15天"):
            self.assertIn(clause, prompt)
        self.assertNotIn("重组蛋白", FORWARD_SELECTION_INSTRUCTION)
        self.assertNotIn("必须选择冷门", FORWARD_SELECTION_INSTRUCTION)

    def test_scientific_tool_terms_include_real_business_but_do_not_merge_collagen(self):
        _, topics = concept_data.research_terms("重组蛋白")
        self.assertIn("湿实验", topics)
        self.assertIn("AI制药", topics)
        self.assertNotIn("湿实验", concept_data.research_terms("重组胶原蛋白")[1])


if __name__ == "__main__":
    unittest.main()
