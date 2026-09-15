from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend import ai, concept_ai, concept_data, concept_forecast as forecast, concept_news, database
from backend.forecast_data import add_forecast_metrics, forecast_window, technical_metrics
from backend.main import app
from backend.test_concept_ai import evidence_fixture
from backend.test_glm_transport import delta, mock_http


def history_fixture(count=21):
    return [{"date": (date(2026, 8, 22) + timedelta(days=i)).strftime("%Y%m%d"),
             "close": 100 + i, "amount": 1_000_000 * (i + 1)} for i in range(count)]


def forecast_evidence():
    evidence = evidence_fixture()
    evidence["window"] = forecast_window("2026-09-12")
    candidate = evidence["candidates"][0]
    candidate["stocks"][0].update(peDynamic=23.5, pb=2.1, marketCap=2_000_000_000)
    add_forecast_metrics(candidate, history_fixture(), evidence["dataAsOf"])
    candidate["evidence"].append({"id": "BK1001:news:1", "kind": "news", "title": "行业订单公告",
                                  "source": "测试公告源", "publishedAt": evidence["dataAsOf"],
                                  "url": "https://example.org/order", "excerpt": "测试订单资料"})
    return evidence


def forecast_result():
    return {"summary": "结合行情和产业订单线索，关注未来半个月的条件性机会。", "concepts": [{
        "code": "BK1001", "thesis": "订单交付预期与当前技术趋势共同支持观察未来半个月。", "conviction": "medium",
        "technical": {"summary": "当前均线结构偏强，需观察能否持续。", "status": "supported", "evidenceIds": ["BK1001:technical"]},
        "fundamental": {"summary": "订单资料支持需求线索，尚需核实盈利转化。", "status": "supported", "evidenceIds": ["BK1001:news:1"]},
        "news": {"summary": "近期订单公告提供现实线索。", "status": "supported", "evidenceIds": ["BK1001:news:1"]},
        "catalysts": [{"title": "订单预期变化", "event": "公告显示订单资料有变化。", "transmission": "订单变化传导至产能利用和盈利预期。",
                       "impact": "需跟踪未来半个月订单落地进展。", "status": "reported", "evidenceIds": ["BK1001:news:1"]}],
        "stocks": [{"code": "600001", "reason": "当前成交活跃，关联产业逻辑尚需继续核验。"}],
        "confirmation": "产业订单得到进一步确认且上涨广度扩大。", "invalidation": "若订单取消或量价趋势转弱则判断失效。",
    }]}


class ForecastEvidenceTests(unittest.TestCase):
    def test_window_includes_weekends_and_crosses_year_boundary(self):
        self.assertEqual(forecast_window("2026-09-12"), {
            "generatedOn": "2026-09-12", "startDate": "2026-09-13", "endDate": "2026-09-27", "calendarDays": 15})
        self.assertEqual(forecast_window("2026-12-25")["endDate"], "2027-01-09")

    def test_technicals_use_only_current_aligned_history_and_complete_amount_windows(self):
        history = history_fixture()
        history[-1]["amount"] = 1  # Partial-session turnover must not contaminate volume comparisons.
        result = technical_metrics(history, "20260911")
        self.assertEqual(result["ma5"], 118)
        self.assertEqual(result["ma20"], 110.5)
        self.assertEqual(result["rsi14"], 100)
        self.assertEqual(result["distanceTo20dHigh"], 0)
        self.assertEqual(result["amountRatio5d"], round(sum(range(16, 21)) / sum(range(11, 16)), 2))
        history.append({"date": "20990101", "close": 10000, "amount": 10000})
        self.assertEqual(technical_metrics(history, "20260911"), result)
        stale = technical_metrics(history, "20260912")
        self.assertIsNone(stale["ma5"])
        self.assertIsNone(stale["lastClose"])
        self.assertEqual(stale["historyAsOf"], "20260911")

    def test_flat_prices_and_missing_turnover_do_not_create_infinite_indicators(self):
        history = [{**row, "close": 100, "amount": None} for row in history_fixture()]
        result = technical_metrics(history, "20260911")
        self.assertEqual(result["rsi14"], 50)
        self.assertIsNone(result["amountRatio5d"])

    def test_forecast_keeps_pullbacks_that_recommendations_filter_out(self):
        board = {**evidence_fixture()["candidates"][0], "pctChg": -2, "kind": "concept"}
        overview = {"tradeDate": "20260911", "updatedAt": database.now_iso(), "conceptBoards": [board], "warnings": []}
        with (patch.object(concept_data.market_data, "sector_overview", return_value=overview),
              patch.object(concept_data.ConceptResearchClient, "daily_history", return_value=[]),
              patch.object(concept_data.ConceptResearchClient, "strong_stocks", return_value=[])):
            self.assertEqual(concept_data.collect_concept_evidence()["candidates"], [])
            candidate = concept_data.collect_concept_evidence(forecast=True)["candidates"][0]
            self.assertEqual(candidate["code"], "BK1001")
            self.assertIsNone(candidate["technicalData"]["ma20"])
            self.assertEqual(candidate["fundamentalData"]["coverage"], "missing")

    def test_forecast_search_looks_for_forward_industry_factors(self):
        query = concept_news.search_query("制糖概念", forecast=True)
        for term in ("未来两周", "业绩", "订单", "政策", "天气", "供需"):
            self.assertIn(term, query)
        self.assertLessEqual(len(query), 70)


class ForecastValidationTests(unittest.TestCase):
    def test_prompt_compacts_duplicate_metrics_without_losing_news_or_mutating_evidence(self):
        evidence = forecast_evidence()
        original = copy.deepcopy(evidence)
        compact = forecast.prompt_evidence(evidence)
        candidate = compact["candidates"][0]
        self.assertEqual(candidate["technicalData"], evidence["candidates"][0]["technicalData"])
        self.assertEqual(candidate["evidence"][-1], evidence["candidates"][0]["evidence"][-1])
        self.assertIn("technicalData", candidate["evidence"][1]["excerpt"])
        self.assertEqual(evidence, original)

    def test_prices_window_and_sources_are_joined_from_server_evidence(self):
        evidence = forecast_evidence()
        result = forecast.assemble_result(forecast_result(), evidence)
        row = result["concepts"][0]
        self.assertEqual(row["stocks"][0]["price"], 12.5)
        self.assertEqual(row["stocks"][0]["name"], "真实名称")
        self.assertEqual(row["technicalData"]["ma20"], 110.5)
        self.assertEqual(row["fundamentalData"]["valuations"][0]["peDynamic"], 23.5)
        self.assertEqual(result["window"], evidence["window"])
        self.assertEqual(row["news"]["sources"][0]["url"], "https://example.org/order")

    def test_cross_concept_sources_wrong_kind_and_fabricated_stocks_are_rejected(self):
        for key, invalid_id in (("technical", "BK1001:news:1"), ("fundamental", "BK1001:market"),
                                ("news", "BK1001:technical"), ("news", "BK9999:news:1")):
            raw = forecast_result()
            raw["concepts"][0][key]["evidenceIds"] = [invalid_id]
            with self.subTest(key=key, invalid_id=invalid_id), self.assertRaises(ValueError):
                forecast.assemble_result(raw, forecast_evidence())
        for invalid in ("600999", "600001"):
            raw = forecast_result()
            raw["concepts"][0]["stocks"].append({"code": invalid, "reason": "无效或重复候选股票。"})
            with self.assertRaises(ValueError):
                forecast.assemble_result(raw, forecast_evidence())
        raw = forecast_result()
        raw["concepts"].append(copy.deepcopy(raw["concepts"][0]))
        with self.assertRaises(ValueError):
            forecast.assemble_result(raw, forecast_evidence())

    def test_missing_evidence_caps_conviction_and_allows_no_forecast(self):
        evidence = forecast_evidence()
        evidence["candidates"][0]["technicalData"]["ma20"] = None
        result = forecast.assemble_result(forecast_result(), evidence)
        self.assertEqual(result["concepts"][0]["conviction"], "low")
        raw = forecast_result()
        raw["concepts"][0]["news"].update(status="hypothesis", evidenceIds=[])
        self.assertEqual(forecast.assemble_result(raw, forecast_evidence())["concepts"][0]["conviction"], "low")
        raw["concepts"][0]["news"]["evidenceIds"] = ["BK1001:news:1"]
        with self.assertRaises(ValueError):
            forecast.assemble_result(raw, forecast_evidence())
        self.assertEqual(forecast.assemble_result({"summary": "资料不足，暂时没有足够有依据的预测方向。", "concepts": []}, evidence)["concepts"], [])


class ForecastRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target in (patch.object(database, "DATABASE_PATH", Path(temporary.name) / "forecast.sqlite3"),
                       patch.object(database, "china_date", return_value="2026-09-11"),
                       patch.dict(os.environ, {"GLM_API_KEY": "test-key", "GLM_MODEL": "wrong-model"}, clear=True),
                       patch.object(forecast, "enrich_world_news", new_callable=AsyncMock, side_effect=lambda evidence, key, **kw: evidence)):
            target.start()
            self.addCleanup(target.stop)
        database.initialize()

    async def test_concurrent_generate_uses_glm_max_once_and_separate_daily_cache(self):
        day = database.china_date()
        token = database.start_ai_run("concept:glm", "glm-5.3", day, concept_ai.PROMPT_VERSION)
        database.finish_ai_run("concept:glm", day, {"summary": "原推荐缓存"}, None, token)
        with (patch.object(forecast, "collect_concept_evidence", return_value=forecast_evidence()) as collect,
              patch.object(ai, "_call_compatible", new_callable=AsyncMock, return_value=forecast_result()) as model):
            await asyncio.gather(*(forecast.start_forecast_run(True) for _ in range(4)))
            await asyncio.gather(*list(forecast._tasks))
            self.assertEqual(forecast.get_forecast_run()["status"], "succeeded")
            await forecast.start_forecast_run()
            self.assertEqual(model.await_count, 1)
            self.assertEqual(model.await_args.args[0], "glm")
            self.assertEqual(model.await_args.args[2], "glm-5.3")
            self.assertEqual(model.await_args.kwargs["reasoning_effort"], "max")
            self.assertTrue(model.await_args.kwargs["resilient_stream"])
            collect.assert_called_once_with(forecast=True)
            forecast.enrich_world_news.assert_awaited_once()
            self.assertTrue(forecast.enrich_world_news.await_args.kwargs["forecast"])
            await forecast.start_forecast_run(True)
            await asyncio.gather(*list(forecast._tasks))
            self.assertEqual(model.await_count, 2)
        self.assertEqual(concept_ai.get_concept_run()["result"]["summary"], "原推荐缓存")
        with patch.object(database, "china_date", return_value="2099-01-01"):
            self.assertEqual(forecast.get_forecast_run()["status"], "idle")

    async def test_slow_data_collection_is_shared_and_fails_before_model_call(self):
        release = threading.Event()

        def collect(**kwargs):
            release.wait(timeout=2)
            return forecast_evidence()

        with (patch.object(forecast, "DATA_TIMEOUT_SECONDS", 0.02),
              patch.object(forecast, "collect_concept_evidence", side_effect=collect) as source,
              patch.object(ai, "_call_compatible", new_callable=AsyncMock) as model):
            try:
                for _ in range(2):
                    await forecast.start_forecast_run()
                    await asyncio.gather(*list(forecast._tasks))
                    self.assertIn("概念行情收集超时", forecast.get_forecast_run()["error"])
                source.assert_called_once()
                model.assert_not_awaited()
            finally:
                release.set()
                await asyncio.wrap_future(forecast._evidence_future)

    async def test_feedback_outage_preserves_prediction_but_reports_missing_feedback(self):
        with (patch.object(forecast, "collect_concept_evidence", return_value=forecast_evidence()),
              patch.object(forecast, "feedback_context", side_effect=RuntimeError("locked")),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock, return_value=forecast_result()) as model):
            await forecast.start_forecast_run()
            await asyncio.gather(*list(forecast._tasks))
        result = forecast.get_forecast_run()
        self.assertEqual(result["status"], "succeeded")
        self.assertIn('"available":false', model.await_args.args[1])
        self.assertIn("历史反馈暂不可用", " ".join(result["result"]["warnings"]))

    async def test_overload_recovers_through_real_transport_and_archives_once(self):
        from backend import glm_transport

        responses = [httpx.Response(429, json={"error": {"code": "1305"}}),
                     httpx.Response(200, headers={"content-type": "text/event-stream"},
                                    content=delta(json.dumps(forecast_result(), ensure_ascii=False), "stop"))]
        with (patch.object(forecast, "collect_concept_evidence", return_value=forecast_evidence()),
              mock_http(lambda request: responses.pop(0)),
              patch.object(glm_transport.asyncio, "sleep", new_callable=AsyncMock) as sleep):
            await forecast.start_forecast_run()
            await asyncio.gather(*list(forecast._tasks))
            self.assertEqual(forecast.get_forecast_run()["status"], "succeeded")
            self.assertEqual(forecast.get_forecast_run()["result"]["concepts"][0]["stocks"][0]["price"], 12.5)
            sleep.assert_awaited_once()
        self.assertEqual(responses, [])
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM forecast_reports").fetchone()[0], 1)

    async def test_late_shared_data_cannot_be_used_after_date_changes(self):
        evidence = forecast_evidence()
        evidence["dataAsOf"] = "2026-09-10T14:00:00+08:00"
        with patch.object(forecast, "collect_concept_evidence", return_value=evidence):
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                await forecast.collect_evidence()

    async def test_browser_disconnect_during_database_claim_does_not_orphan_run(self):
        release = threading.Event()
        claimed = asyncio.Event()
        loop = asyncio.get_running_loop()
        start = database.start_ai_run

        def delayed_claim(*args, **kwargs):
            token = start(*args, **kwargs)
            loop.call_soon_threadsafe(claimed.set)
            release.wait(timeout=2)
            return token

        with (patch.object(database, "start_ai_run", side_effect=delayed_claim),
              patch.object(forecast, "collect_concept_evidence", return_value=forecast_evidence()),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock, return_value=forecast_result()) as model):
            request = asyncio.create_task(forecast.start_forecast_run())
            try:
                await asyncio.wait_for(claimed.wait(), timeout=1)
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
            finally:
                release.set()
                while forecast._tasks:
                    await asyncio.gather(*list(forecast._tasks))
            self.assertEqual(forecast.get_forecast_run()["status"], "succeeded")
            model.assert_awaited_once()

    async def test_read_only_api_missing_key_and_secret_enforcement(self):
        client = TestClient(app)
        with (patch.dict(os.environ, {"DAILY_RUN_SECRET": "test-secret"}),
              patch("backend.main.start_forecast_run", new_callable=AsyncMock, return_value={"status": "running"}) as start):
            self.assertEqual(client.get("/api/forecast/concepts").json()["status"], "idle")
            start.assert_not_awaited()
            self.assertEqual(client.post("/api/forecast/concepts").status_code, 401)
            response = client.post("/api/forecast/concepts?force=true", headers={"x-daily-run-secret": "test-secret"})
            self.assertEqual(response.json()["status"], "running")
            start.assert_awaited_once_with(True)
        with patch.dict(os.environ, {}, clear=True), patch.object(ai, "_call_compatible", new_callable=AsyncMock) as model:
            self.assertEqual((await forecast.start_forecast_run())["status"], "not_configured")
            model.assert_not_awaited()

    async def test_timeout_can_retry_and_old_worker_cannot_overwrite_new_result(self):
        async def slow_model(*args, **kwargs):
            await asyncio.sleep(1)
        with (patch.object(forecast, "RUN_TIMEOUT_SECONDS", 0.02),
              patch.object(forecast, "collect_concept_evidence", return_value=forecast_evidence()),
              patch.object(ai, "_call_compatible", side_effect=slow_model)):
            await forecast.start_forecast_run()
            await asyncio.gather(*list(forecast._tasks))
        self.assertEqual(forecast.get_forecast_run()["status"], "failed")
        self.assertIn("超时", forecast.get_forecast_run()["error"])
        day = database.china_date()
        token = database.start_ai_run(forecast.RUN_NAMESPACE, "glm-5.3", day, forecast.PROMPT_VERSION)
        five_minutes_ago = (datetime.now(database.CHINA_TZ) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute("UPDATE ai_runs SET started_at = ? WHERE provider = ?", (five_minutes_ago, forecast.RUN_NAMESPACE))
        self.assertEqual(forecast.get_forecast_run()["status"], "running")
        self.assertIsNone(database.start_ai_run(forecast.RUN_NAMESPACE, "glm-5.3", day, forecast.PROMPT_VERSION, True))
        expired = (datetime.now(database.CHINA_TZ) - timedelta(minutes=12)).isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute("UPDATE ai_runs SET started_at = ? WHERE provider = ?", (expired, forecast.RUN_NAMESPACE))
        new_token = database.start_ai_run(forecast.RUN_NAMESPACE, "glm-5.3", day, forecast.PROMPT_VERSION)
        database.finish_ai_run(forecast.RUN_NAMESPACE, day, {"summary": "新结果"}, None, new_token)
        database.finish_ai_run(forecast.RUN_NAMESPACE, day, {"summary": "已过期结果"}, None, token)
        self.assertEqual(forecast.get_forecast_run()["result"]["summary"], "新结果")
