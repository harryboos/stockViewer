from __future__ import annotations

import copy
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from backend import concept_data, concept_forecast
from backend.forecast_data import technical_metrics
from backend.forecast_review import REVIEW_INSTRUCTION
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
        self.assertEqual(concept_forecast.PROMPT_VERSION, "forecast-v10-complete-history-deepseek-flash-max")

    def test_scientific_tool_terms_include_real_business_but_do_not_merge_collagen(self):
        _, topics = concept_data.research_terms("重组蛋白")
        self.assertIn("湿实验", topics)
        self.assertIn("AI制药", topics)
        self.assertNotIn("湿实验", concept_data.research_terms("重组胶原蛋白")[1])


if __name__ == "__main__":
    unittest.main()
