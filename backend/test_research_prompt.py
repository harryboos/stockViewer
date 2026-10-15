from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from backend import concept_ai, concept_forecast, concept_news, database
from backend.research_prompt import prepare_research_evidence
from backend.test_concept_forecast import forecast_evidence, forecast_result
from backend.test_concept_ai import evidence_fixture, result_fixture


def evidence_at_decision():
    evidence = forecast_evidence()
    evidence.update(decisionAt="2026-09-11T18:00:00+08:00", quoteAsOf="2026-09-11T14:00:00+08:00")
    return evidence


class ResearchEvidenceTests(unittest.TestCase):
    def test_news_after_quote_before_decision_is_usable_but_not_a_prior_price_cause(self):
        evidence = evidence_at_decision()
        evidence["candidates"][0]["evidence"][-1]["publishedAt"] = "2026-09-11T08:00:00Z"
        original = copy.deepcopy(evidence)
        prepared = prepare_research_evidence(evidence)
        source = prepared["candidates"][0]["evidence"][-1]
        self.assertEqual(source["temporalRelation"], "after_quote")
        self.assertEqual(source["publishedAtPrecision"], "second")
        self.assertEqual(prepare_research_evidence(prepared), prepared)
        self.assertEqual(evidence, original)

    def test_date_only_news_never_invents_midnight_or_intraday_order(self):
        for stamp, marker in (("2026-09-11", None), ("2026-09-11T00:00:00+08:00", "date")):
            evidence = evidence_at_decision()
            source = evidence["candidates"][0]["evidence"][-1]
            source.update(publishedAt=stamp, publishedAtPrecision=marker)
            news = prepare_research_evidence(evidence)["candidates"][0]["evidence"][-1]
            self.assertEqual(news["publishedAtPrecision"], "date")
            self.assertEqual(news["temporalRelation"], "unknown")
        evidence["candidates"][0]["evidence"][-1]["publishedAt"] = "2026-09-10"
        self.assertEqual(prepare_research_evidence(evidence)["candidates"][0]["evidence"][-1]["temporalRelation"], "before_quote")

    def test_data_refresh_time_cannot_substitute_for_unknown_quote_time(self):
        evidence = evidence_at_decision()
        del evidence["quoteAsOf"]
        news = prepare_research_evidence(evidence)["candidates"][0]["evidence"][-1]
        self.assertEqual(news["temporalRelation"], "unknown")

    def test_unusable_news_is_not_in_prompt_and_cannot_be_cited(self):
        for change in ({"publishedAt": "2026-09-11T19:00:00+08:00"},
                       {"publishedAt": "2026-09-12", "publishedAtPrecision": "date"},
                       {"publishedAt": "not a date"}, {"publishedAt": None},
                       {"id": "BK9999:news:1"}, {"url": "javascript:alert(1)"},
                       {"url": "https://user:password@example.org/"}):
            with self.subTest(change=change):
                evidence = evidence_at_decision()
                source = evidence["candidates"][0]["evidence"][-1]
                source.update(change, excerpt="这一篇不应进入模型输入")
                self.assertNotIn("这一篇不应进入模型输入", concept_forecast.build_prompt(evidence))
                with self.assertRaises(ValueError):
                    concept_forecast.assemble_result(forecast_result(), evidence)

    def test_reserved_data_id_cannot_be_mislabeled_as_news(self):
        evidence = evidence_at_decision()
        technical = next(source for source in evidence["candidates"][0]["evidence"] if source["id"].endswith(":technical"))
        technical["kind"] = "news"
        with self.assertRaises(ValueError):
            concept_forecast.assemble_result(forecast_result(), evidence)

    def test_duplicate_reference_ids_are_ambiguous_and_both_excluded(self):
        evidence = evidence_at_decision()
        references = evidence["candidates"][0]["evidence"]
        references.append({**references[-1], "excerpt": "另一个内容"})
        prepared = prepare_research_evidence(evidence)
        self.assertFalse(any(source["kind"] == "news" for source in prepared["candidates"][0]["evidence"]))
        self.assertTrue(prepared["candidates"][0]["warnings"])
        with self.assertRaises(ValueError):
            concept_forecast.assemble_result(forecast_result(), evidence)

    def test_search_preserves_upstream_date_precision(self):
        today = database.china_date()
        payload = {"search_result": [{"title": "产品获批公告", "content": "真实事件资料", "link": "https://example.org/news", "publish_date": today}]}
        news = concept_news.normalize_search_results(payload, "BK1001")
        self.assertEqual(news[0]["publishedAtPrecision"], "date")


class ForecastPromptIterationTests(unittest.TestCase):
    def test_hypothesis_background_citations_are_removed_without_upgrading_or_mutating_inputs(self):
        evidence = evidence_at_decision()
        raw = forecast_result()
        for field in ("technical", "fundamental", "news"):
            raw["concepts"][0][field]["status"] = "hypothesis"
        raw["concepts"][0]["catalysts"][0]["status"] = "hypothesis"
        original = copy.deepcopy((raw, evidence))
        report = concept_forecast.assemble_result(raw, evidence)
        result = report["concepts"][0]
        for field in ("technical", "fundamental", "news"):
            self.assertEqual(result[field]["status"], "hypothesis")
            self.assertEqual(result[field]["sources"], [])
        self.assertEqual(result["catalysts"][0]["sources"], [])
        self.assertEqual(result["catalysts"][0]["status"], "hypothesis")
        self.assertEqual(result["conviction"], "low")
        corrections = [warning for warning in result["warnings"] if "背景引用已移除" in warning]
        self.assertEqual(len(corrections), 2)  # One notice per kind of correction.
        self.assertEqual((raw, evidence), original)
        self.assertFalse(any("背景引用已移除" in warning
                             for warning in report["candidateAudit"]["candidates"][0]["warnings"]))

    def test_hypotheses_cannot_hide_fabricated_cross_concept_or_wrong_kind_sources(self):
        for invalid_id in ("BK9999:news:1", "BK1001:news:invented", "BK1001:market", "BK1001:fundamentals"):
            with self.subTest(invalid_id=invalid_id):
                raw = forecast_result()
                raw["concepts"][0]["news"].update(status="hypothesis", evidenceIds=["BK1001:news:1", invalid_id])
                with self.assertRaises(ValueError):
                    concept_forecast.assemble_result(raw, evidence_at_decision())

    def test_recommendation_hypotheses_also_keep_their_uncertainty_without_citations(self):
        raw = result_fixture()
        raw["concepts"][0]["drivers"][1]["evidenceIds"] = ["BK1001:market"]
        result = concept_ai.assemble_result(raw, evidence_fixture())["concepts"][0]
        self.assertEqual(result["drivers"][1]["kind"], "hypothesis")
        self.assertEqual(result["drivers"][1]["sources"], [])
        self.assertTrue(any("背景引用已移除" in warning for warning in result["warnings"]))

    def test_valuation_alone_cannot_certify_operating_fundamentals(self):
        raw = forecast_result()
        raw["concepts"][0]["fundamental"]["evidenceIds"] = ["BK1001:fundamentals"]
        with self.assertRaises(ValueError):
            concept_forecast.assemble_result(raw, evidence_at_decision())

    def test_real_operating_evidence_does_not_require_pe_coverage(self):
        evidence = evidence_at_decision()
        evidence["candidates"][0]["fundamentalData"].update(coverage="missing", sampleSize=0, valuations=[])
        result = concept_forecast.assemble_result(forecast_result(), evidence)
        self.assertEqual(result["concepts"][0]["conviction"], "medium")

    def test_no_catalyst_is_allowed_and_caps_conviction(self):
        raw = forecast_result()
        raw["concepts"][0]["catalysts"] = []
        result = concept_forecast.assemble_result(raw, evidence_at_decision())
        self.assertEqual(result["concepts"][0]["catalysts"], [])
        self.assertEqual(result["concepts"][0]["conviction"], "low")

    def test_ambiguous_candidates_rejected_before_building_model_prompt(self):
        for other_code in ("BK1001", "THS:885955"):
            evidence = evidence_at_decision()
            other = copy.deepcopy(evidence["candidates"][0])
            other["code"] = other_code
            evidence["candidates"].append(other)
            with self.assertRaises(ValueError):
                concept_forecast.build_prompt(evidence)

    def test_time_cutoff_is_saved_and_schema_can_be_exported_without_data(self):
        evidence = evidence_at_decision()
        result = concept_forecast.assemble_result(forecast_result(), evidence)
        self.assertEqual(result["decisionAt"], evidence["decisionAt"])
        self.assertEqual(result["candidateAudit"]["decisionAt"], evidence["decisionAt"])
        with patch.object(database, "now_iso", return_value=evidence["decisionAt"]):
            self.assertIn("输出JSON结构", concept_forecast.build_prompt({}))


if __name__ == "__main__":
    unittest.main()
