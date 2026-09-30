from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from backend import concept_ai, concept_comparison, concept_forecast, database, forecast_feedback
from backend import forecast_history, research_data, research_store
from backend.forecast_prices import CALENDAR_KEY, FeedbackPriceClient
from backend.forecast_data import add_forecast_metrics
from backend.test_concept_ai import evidence_fixture, result_fixture
from backend.test_concept_forecast import forecast_evidence, forecast_result, history_fixture
from backend.test_forecast_feedback import calendar_fixture, instant, prices, report_fixture


def ths_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False).replace("BK1001", "THS:886042"))


class THSModelTests(unittest.TestCase):
    def test_forecast_feature_sources_use_actual_index_provider_and_valid_links(self):
        candidate = ths_copy(evidence_fixture())["candidates"][0]
        candidate["evidence"][0].update(source="同花顺公开概念行情", url="https://q.10jqka.com.cn/gn/")
        history = [{**row, "historySource": "同花顺概念指数日线（独立口径）", "historyUrl": "https://q.10jqka.com.cn/gn/"}
                   for row in history_fixture()]
        add_forecast_metrics(candidate, history, "2026-09-11T15:30:00+08:00")
        references = candidate["evidence"][-2:]
        for reference in references:
            self.assertIn("同花顺", reference["source"])
            self.assertNotIn("东方财富", reference["source"])
            self.assertEqual(reference["url"], "https://q.10jqka.com.cn/gn/")
        self.assertIn("历史收盘价", references[0]["source"])
        self.assertIn("估值快照", references[1]["source"])

    def test_bk_features_keep_same_index_history_transport_provenance(self):
        candidate = evidence_fixture()["candidates"][0]
        history = [{**row, "historySource": "Tushare东财日线", "historyUrl": "https://tushare.pro"} for row in history_fixture()]
        add_forecast_metrics(candidate, history, "2026-09-11T15:30:00+08:00")
        technical, fundamental = candidate["evidence"][-2:]
        self.assertIn("Tushare", technical["source"])
        self.assertEqual(technical["url"], "https://tushare.pro")
        self.assertEqual(fundamental["url"], "https://quote.eastmoney.com/bk/90.BK1001.html")

    def test_recommendation_and_forecast_preserve_ths_identity_and_evidence(self):
        result = concept_ai.assemble_result(ths_copy(result_fixture()), ths_copy(evidence_fixture()))
        self.assertEqual(result["concepts"][0]["code"], "THS:886042")
        self.assertEqual(result["concepts"][0]["drivers"][0]["sources"][0]["id"], "THS:886042:market")
        forecast = concept_forecast.assemble_result(ths_copy(forecast_result()), ths_copy(forecast_evidence()))
        self.assertEqual(forecast["conceptProvider"], "ths")
        self.assertEqual(forecast["concepts"][0]["technical"]["sources"][0]["id"], "THS:886042:technical")

    def test_unknown_or_malformed_concept_namespace_is_rejected(self):
        for code in ("886042", "THS:986042", "THS:886042/", "OTHER:886042"):
            with self.subTest(code=code):
                raw = forecast_result()
                raw["concepts"][0]["code"] = code
                with self.assertRaises(ValidationError):
                    concept_forecast.ForecastResult.model_validate(raw)
                with self.assertRaises(ValueError):
                    FeedbackPriceClient().history(code, "2026-09-18", "2026-09-21")

    def test_forecast_cannot_mix_different_index_populations(self):
        evidence = forecast_evidence()
        evidence["candidates"] += ths_copy(evidence)["candidates"]
        with self.assertRaisesRegex(ValueError, "同一行情来源"):
            concept_forecast.assemble_result(forecast_result(), evidence)

    def test_ths_history_outage_never_falls_back_to_eastmoney(self):
        with (patch("backend.forecast_prices.ths_concept_history", side_effect=RuntimeError("同花顺暂不可用")),
              patch.object(FeedbackPriceClient, "eastmoney_history") as east):
            result = FeedbackPriceClient().history("THS:886042", "2026-09-18", "2026-09-21")
        self.assertEqual(result["errorKind"], "unavailable")
        self.assertIn("同花顺", result["error"])
        east.assert_not_called()


class THSArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "ths.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": calendar_fixture(), "fetchedOn": database.china_date()}))

    def archive(self, code="THS:886042", *, frozen=None, day="2026-09-12"):
        result = report_fixture()["result"]
        result["concepts"][0]["code"] = code
        result.pop("conceptProvider", None)  # Derive legacy identity from immutable codes.
        if frozen is not None:
            result["comparisonUniverse"] = {"asOf": day + "T18:00:00+08:00", "scope": "冻结概念范围", "boards": frozen}
        token = database.start_ai_run("forecast:glm", "glm-5.3", day, "test", True)
        with patch.object(database, "now_iso", return_value=day + "T18:00:00+08:00"):
            database.finish_ai_run("forecast:glm", day, result, None, token)
        with database.connection() as db:
            report_id = db.execute("SELECT MAX(id) FROM forecast_reports").fetchone()[0]
        return forecast_history.get_report(report_id)

    def test_each_provider_keeps_its_latest_population_even_with_same_timestamp(self):
        bk = [{"code": "BK1001", "name": "原概念"}]
        ths = [{"code": "THS:886042", "name": "同花顺概念"}]
        research_store.save_universe(bk, "2026-09-18T18:00:00+08:00")
        research_store.save_universe(ths, "2026-09-18T18:00:00+08:00")
        self.assertEqual(research_data.latest_universe("eastmoney")["boards"], bk)
        self.assertEqual(research_data.latest_universe("ths")["boards"], ths)
        self.assertEqual(research_data.latest_universe()["provider"], "ths")
        research_store.save_universe(ths, "2026-09-21T18:00:00+08:00")
        database.initialize()
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM concept_universes").fetchone()[0], 2)

    def test_legacy_population_migrates_and_remains_after_new_ths_snapshot(self):
        with database.connection() as db:
            db.execute("DROP TABLE concept_universes")
            db.execute("CREATE TABLE concept_universes (as_of TEXT PRIMARY KEY, boards_json TEXT NOT NULL)")
            db.execute("INSERT INTO concept_universes VALUES (?,?)",
                       ("2026-09-18T18:00:00+08:00", '[{"code":"BK1001","name":"原概念"}]'))
        database.initialize()
        research_store.save_universe([{"code": "THS:886042", "name": "新来源"}], "2026-09-21T18:00:00+08:00")
        self.assertEqual(research_data.latest_universe("eastmoney")["boards"][0]["code"], "BK1001")

    def test_old_bk_report_never_uses_current_ths_universe(self):
        report = self.archive("BK1001")
        research_store.save_universe([{"code": "THS:886042", "name": "同名但不同指数"}], database.now_iso())
        self.assertIsNone(concept_comparison.universe_for(report, create=True))
        research_store.save_universe([{"code": "BK1001", "name": "原指数"}], database.now_iso())
        universe = concept_comparison.universe_for(report, create=True)
        self.assertEqual(universe["provider"], "eastmoney")
        self.assertEqual(universe["boards"][0]["code"], "BK1001")

    def test_public_sample_scope_is_retained_for_a_backfilled_ths_universe(self):
        report = self.archive()
        scope = "同花顺公开榜单活跃概念样本（非全市场）"
        research_store.save_universe([{"code": "THS:886042", "name": "样本概念"}], database.now_iso(), scope)
        self.assertEqual(research_data.latest_universe("ths")["scope"], scope)
        self.assertIn(scope, concept_comparison.universe_for(report, create=True)["scope"])

    def test_frozen_comparison_excludes_other_provider_even_if_name_matches(self):
        report = self.archive(frozen=[{"code": "THS:886042", "name": "同名概念"},
                                      {"code": "BK1001", "name": "同名概念"}])
        universe = concept_comparison.universe_for(report)
        self.assertEqual(universe["boards"], [{"code": "THS:886042", "name": "同名概念"}])

    def test_rotation_fetches_configured_provider_and_retains_sample_scope(self):
        day = "2026-10-20"
        scope = "同花顺公开活跃样本（非全市场）"
        research_store.save_universe([{"code": "BK1001", "name": "旧来源"}], day + "T16:00:00+08:00")

        def overview(_):
            research_store.save_universe([{"code": "THS:886042", "name": "新来源"}], day + "T18:00:00+08:00", scope)

        with (patch.object(research_data.market_data, "_sector_source", "ths"),
              patch.object(research_data.market_data, "sector_overview", side_effect=overview) as fetch,
              patch.object(database, "china_date", return_value=day),
              patch.object(research_data, "last_closed_day", return_value=day),
              patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(research_data, "index_history", return_value=prices()) as history):
            result = research_data.refresh_rotation(Mock())
        fetch.assert_called_once()
        self.assertEqual(history.call_args.args[0], "THS:886042")
        self.assertEqual(result["sourceProvider"], "ths")
        self.assertEqual(result["scope"], scope)
        self.assertEqual(research_data.rotation_payload()["scope"], scope)

    def test_legacy_rotation_is_not_relabelled_with_current_other_provider_snapshot(self):
        research_store.cache_put("rotation", {"asOf": "2026-09-30", "totalCount": 1, "items": [{
            "code": "BK1001", "name": "旧概念", "asOf": "2026-09-30", "path": [],
            "returns": dict.fromkeys(("5", "10", "20"), 1), "previousReturns": dict.fromkeys(("5", "10", "20"), 0)}]})
        research_store.cache_put("rotation-snapshot", {"asOf": "2026-10-01T16:00:00+08:00", "sourceProvider": "ths",
            "boards": [{"code": "THS:886042", "breadth": 99}]})
        result = research_data.rotation_payload()
        self.assertEqual(result["sourceProvider"], "eastmoney")
        self.assertIn("东方财富", result["scope"])
        self.assertIsNone(result["snapshotAsOf"])
        self.assertIsNone(result["items"][0]["snapshot"])

    def test_feedback_uses_ths_prices_and_retains_source_and_benchmarks(self):
        report = self.archive()
        ths_prices = {**prices(), "source": "同花顺概念指数日线（独立口径）"}
        with (patch("backend.forecast_prices.ths_concept_history", return_value=ths_prices) as ths,
              patch.object(FeedbackPriceClient, "eastmoney_history") as east):
            value = forecast_feedback.evaluate(report, 15,
                FeedbackPriceClient().history("THS:886042", "2026-09-14", "2026-09-24"),
                {"sh000001": prices(103), "sh000688": prices(112)},
                calendar_fixture(), instant("2026-10-20T18:00:00"))
        self.assertEqual(value["returnPct"], 8)
        self.assertEqual(value["benchmarks"]["sh000001"]["excessPct"], 5)
        self.assertIn("同花顺", value["source"])
        self.assertEqual(forecast_history.history_payload()["reports"][0]["concepts"][0]["code"], "THS:886042")
        ths.assert_called_once()
        east.assert_not_called()

    def test_bk_outage_does_not_stop_a_ths_comparison_batch(self):
        bk_boards = [{"code": f"BK{1000 + index}", "name": f"旧概念{index}"} for index in range(30)]
        self.archive("BK1001", frozen=bk_boards)
        ths_boards = [{"code": "THS:886042", "name": "同花顺概念"}]
        report = self.archive(frozen=ths_boards, day="2026-09-13")
        calls = []

        def read(code, *args):
            calls.append(code)
            return prices() if code.startswith("THS:") else {"rows": [], "errorKind": "unavailable", "error": "旧源不可用"}

        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(concept_comparison, "index_history", side_effect=read),
              patch.object(concept_comparison, "datetime") as clock):
            clock.now.return_value = instant("2026-10-20T18:00:00")
            clock.combine, clock.fromisoformat = datetime.combine, datetime.fromisoformat
            with self.assertRaisesRegex(RuntimeError, "尚未取得查询结果"):
                concept_comparison.refresh_comparisons(Mock())
        self.assertEqual(calls[0], "THS:886042")
        payload = concept_comparison.comparison_payload(report["id"], 15, instant("2026-10-20T18:00:00"))
        self.assertEqual(payload["strongest"]["code"], "THS:886042")
        self.assertEqual(payload["coveredCount"], 1)


if __name__ == "__main__":
    unittest.main()
