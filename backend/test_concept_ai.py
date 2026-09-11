from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from fastapi.testclient import TestClient

from backend import ai, concept_ai, concept_data, concept_news, database
from backend.main import app


def evidence_fixture() -> dict:
    return {"tradeDate": "20260911", "dataAsOf": "2026-09-11T14:00:00+08:00", "scope": "测试候选范围",
            "warnings": [], "candidates": [{
                "code": "BK1001", "name": "测试概念", "tradeDate": "20260911", "pctChg": 2.5,
                "change5d": 7.2, "change10d": None, "amount": 120_000_000, "mainNetInflow": None,
                "breadth": 75.0, "upCount": 3, "downCount": 1, "warnings": [],
                "strengthStatus": "recent_strength", "historyAsOf": "20260911",
                "stocks": [{"code": "600001", "name": "真实名称", "price": 12.5, "pctChg": 5.0,
                            "amount": 100_000_000, "turnoverRate": None, "tradeDate": "20260911"}],
                "evidence": [{"id": "BK1001:market", "kind": "data", "title": "行情", "source": "行情源",
                              "publishedAt": "2026-09-11T14:00:00+08:00", "excerpt": "数据",
                              "url": "https://quote.eastmoney.com/bk/90.BK1001.html"}],
            }]}


def result_fixture() -> dict:
    return {"summary": "根据近期动量与当日广度，候选概念保持相对强势。", "concepts": [{
        "code": "BK1001", "reason": "近期涨幅与当日广度共同支持相对强度。", "risk": "量价持续性仍需跟踪。",
        "stocks": [{"code": "600001", "reason": "当日涨幅靠前且成交活跃。"}],
        "catalysts": [{"title": "现实催化尚待核实", "event": "尚未找到已证实的现实事件催化。",
                       "transmission": "需核对产业订单变化是否改善收入和盈利预期。", "impact": "关注后续公告能否验证需求增长。",
                       "status": "hypothesis", "evidenceIds": []}],
        "drivers": [{"kind": "data", "title": "广度与动量", "explanation": "多数成份股上涨，近期趋势保持正收益。",
                     "evidenceIds": ["BK1001:market"]},
                    {"kind": "hypothesis", "title": "催化待验证", "explanation": "没有足够资讯判断具体事件催化。", "evidenceIds": []}],
    }]}


class ConceptEvidenceTests(unittest.TestCase):
    def test_closed_history_cache_survives_outage_but_intraday_cache_does_not_become_a_close(self) -> None:
        client = concept_data.ConceptResearchClient()
        rows = [{"date": "20260910", "close": 100}]
        saved = {"closed": True, "fetchedAt": "2000-01-01T12:00:00+08:00", "rows": rows}
        with (patch.object(database, "get_meta", return_value=json.dumps(saved)),
              patch.object(database, "china_date", return_value="2026-09-11"),
              patch.object(client, "_json", side_effect=RuntimeError("unavailable")) as request):
            self.assertEqual(client.daily_history("BK1001", "20260910"), rows)
            request.assert_not_called()
        saved["closed"] = False
        with (patch.object(database, "get_meta", return_value=json.dumps(saved)),
              patch.object(database, "china_date", return_value="2026-09-11"),
              patch.object(client, "_json", side_effect=RuntimeError("unavailable")) as request):
            with self.assertRaises(RuntimeError):
                client.daily_history("BK1001", "20260910")
            request.assert_called_once()

    def test_returns_use_trading_bars_and_never_synthesize_stale_history(self) -> None:
        dates = ["20260828", "20260831", "20260901", "20260902", "20260903", "20260904",
                 "20260907", "20260908", "20260909", "20260910", "20260911"]
        bars = [{"date": day, "close": 100 + index} for index, day in enumerate(dates)]
        result = concept_data.recent_metrics(list(reversed(bars)) + [bars[-1]], "20260911")
        self.assertEqual(result["change5d"], 4.76)
        self.assertEqual(result["change10d"], 10.0)
        self.assertEqual(result["historySessions"], 11)
        self.assertIsNone(concept_data.recent_metrics(bars[:-1], "20260911")["change5d"])
        self.assertIsNone(concept_data.recent_metrics(bars[-5:], "20260911")["change5d"])

    def test_stocks_require_positive_liquid_non_st_same_day_quotes(self) -> None:
        client = concept_data.ConceptResearchClient()
        stamp = datetime(2026, 9, 11, 14, tzinfo=database.CHINA_TZ).timestamp()
        good = {"f12": "600001", "f14": "正常股", "f2": 12, "f3": 5, "f6": 100_000_000, "f124": stamp}
        cases = [good, {**good, "f12": "600002", "f124": stamp - 86400},
                 {**good, "f12": "600003", "f14": "*ST测试"}, {**good, "f12": "600004", "f3": -2},
                 {**good, "f12": "600005", "f6": 99_000_000}, {**good, "f12": "600006", "f124": None}]
        with patch.object(client, "_json", return_value={"data": {"diff": cases}}):
            self.assertEqual([row["code"] for row in client.strong_stocks("BK1001", "20260911")], ["600001"])

    def test_news_filters_irrelevant_old_and_future_articles_and_strips_markup(self) -> None:
        now = datetime.now(database.CHINA_TZ)
        good = {"code": "123", "title": "<em>机器人</em>产业新进展", "content": "&amp; 新产品", "date": now.isoformat()}
        rows = [good, {**good, "code": "124", "date": (now - timedelta(days=31)).isoformat()},
                {**good, "code": "125", "date": (now + timedelta(days=1)).isoformat()},
                {**good, "code": "126", "title": "无关新闻"}, {**good, "code": "malicious/path"}]
        client = concept_data.ConceptResearchClient()
        with patch.object(client, "_json", return_value={"result": {"cmsArticleWebOld": rows}}):
            news = client.news("机器人概念", "BK1001")
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["title"], "机器人产业新进展")
        self.assertEqual(news[0]["url"], "https://finance.eastmoney.com/a/123.html")

    def test_old_overview_is_rejected_before_model_or_additional_requests(self) -> None:
        with patch.object(concept_data.market_data, "sector_overview", return_value={
            "tradeDate": "20260910", "updatedAt": "2000-01-01T14:00:00+08:00",
        }):
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                concept_data.collect_concept_evidence()

    def test_history_outage_keeps_today_active_concepts_and_real_stocks(self) -> None:
        board = {**evidence_fixture()["candidates"][0], "kind": "concept"}
        overview = {"tradeDate": "20260911", "updatedAt": database.now_iso(), "conceptBoards": [board]}
        with (patch.object(concept_data.market_data, "sector_overview", return_value=overview),
              patch.object(concept_data.ConceptResearchClient, "daily_history", side_effect=RuntimeError("历史接口不可用")),
              patch.object(concept_data.ConceptResearchClient, "strong_stocks", return_value=board["stocks"])):
            evidence = concept_data.collect_concept_evidence()
        self.assertEqual(len(evidence["candidates"]), 1)
        candidate = evidence["candidates"][0]
        self.assertEqual(candidate["strengthStatus"], "today_active")
        self.assertIsNone(candidate["change5d"])
        self.assertEqual(candidate["stocks"], board["stocks"])
        self.assertIn("历史行情接口暂不可用", candidate["warnings"])

    def test_negative_five_day_return_does_not_hide_positive_ten_day_trend(self) -> None:
        board = {**evidence_fixture()["candidates"][0], "pctChg": -1}
        bars = [{"date": f"202609{i + 1:02}", "close": close} for i, close in enumerate([100, 103, 106, 109, 111, 112, 111, 110, 110, 110, 110])]
        with (patch.object(concept_data.market_data, "sector_overview", return_value={
            "tradeDate": "20260911", "updatedAt": database.now_iso(), "conceptBoards": [board],
        }), patch.object(concept_data.ConceptResearchClient, "daily_history", return_value=bars),
              patch.object(concept_data.ConceptResearchClient, "strong_stocks", return_value=[])):
            evidence = concept_data.collect_concept_evidence()
        self.assertEqual(evidence["candidates"][0]["strengthStatus"], "recent_strength")
        self.assertLess(evidence["candidates"][0]["change5d"], 0)
        self.assertEqual(evidence["candidates"][0]["change10d"], 10)
        raw = result_fixture()
        raw["concepts"][0]["stocks"] = []
        self.assertEqual(concept_ai.assemble_result(raw, evidence)["concepts"][0]["stocks"], [])

    def test_reported_catalysts_require_news_not_market_or_invented_evidence(self) -> None:
        for ids in ([], ["BK1001:market"], ["BK1001:invented"]):
            raw = result_fixture()
            raw["concepts"][0]["catalysts"][0].update(status="reported", evidenceIds=ids)
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, "现实催化"):
                concept_ai.assemble_result(raw, evidence_fixture())
        evidence = evidence_fixture()
        evidence["candidates"][0]["evidence"].append({"id": "BK1001:news:1", "kind": "news", "title": "产业订单公告"})
        raw["concepts"][0]["catalysts"][0].update(evidenceIds=["BK1001:news:1"])
        self.assertEqual(concept_ai.assemble_result(raw, evidence)["concepts"][0]["catalysts"][0]["sources"][0]["title"], "产业订单公告")

    def test_assembly_uses_authoritative_prices_names_and_sources(self) -> None:
        result = concept_ai.assemble_result(result_fixture(), evidence_fixture())
        concept = result["concepts"][0]
        self.assertEqual(concept["name"], "测试概念")
        self.assertEqual(concept["stocks"][0]["name"], "真实名称")
        self.assertEqual(concept["stocks"][0]["price"], 12.5)
        self.assertIsNone(concept["mainNetInflow"])
        self.assertEqual(concept["drivers"][0]["sources"][0]["id"], "BK1001:market")

    def test_hallucinated_membership_duplicates_and_false_sources_are_rejected(self) -> None:
        mutations = [
            lambda c: c.update(code="BK9999"),
            lambda c: c["stocks"][0].update(code="600999"),
            lambda c: c["stocks"].append(copy.deepcopy(c["stocks"][0])),
            lambda c: c["drivers"][0].update(evidenceIds=["BK9999:market"]),
            lambda c: c["drivers"].append({**c["drivers"][0], "kind": "news"}),
            lambda c: c["drivers"][1].update(evidenceIds=["BK1001:market"]),
        ]
        for mutation in mutations:
            raw = result_fixture()
            mutation(raw["concepts"][0])
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                concept_ai.assemble_result(raw, evidence_fixture())
        raw = result_fixture()
        raw["concepts"].append(copy.deepcopy(raw["concepts"][0]))
        with self.assertRaises(ValueError):
            concept_ai.assemble_result(raw, evidence_fixture())


class ConceptRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target in [patch.object(database, "DATABASE_PATH", Path(temporary.name) / "test.sqlite3"),
                       patch.dict(os.environ, {
                           "GLM_API_KEY": "glm-test-key", "DEEPSEEK_API_KEY": "deepseek-test-key",
                           "QWEN_API_KEY": "qwen-test-key", "GLM_MODEL": "another-model",
                           "CONCEPT_AI_PROVIDER": "deepseek",
                       }, clear=True)]:
            target.start()
            self.addCleanup(target.stop)
        database.initialize()
        news = patch.object(concept_ai, "enrich_world_news", new_callable=AsyncMock, side_effect=lambda evidence, key: evidence)
        news.start()
        self.addCleanup(news.stop)

    async def test_read_only_and_missing_configuration_never_generate(self) -> None:
        with patch.object(ai, "_call_compatible", new_callable=AsyncMock) as call:
            self.assertEqual(concept_ai.get_concept_run()["status"], "idle")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual((await concept_ai.start_concept_run())["status"], "not_configured")
            with patch.dict(os.environ, {"GLM_API_KEY": ""}):
                self.assertEqual(concept_ai.get_concept_run()["status"], "not_configured")
                self.assertEqual((await concept_ai.start_concept_run())["status"], "not_configured")
            call.assert_not_awaited()

    async def test_concurrent_forced_runs_call_once_and_keep_stock_cache_separate(self) -> None:
        date = database.china_date()
        token = database.start_ai_run("deepseek", ai.model_for("deepseek"), date, ai.PROMPT_VERSION)
        database.finish_ai_run("deepseek", date, {"title": "选股缓存"}, None, token)
        with (patch.object(concept_ai, "collect_concept_evidence", return_value=evidence_fixture()),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock, return_value=result_fixture()) as call):
            runs = await asyncio.gather(*(concept_ai.start_concept_run(True) for _ in range(4)))
            self.assertTrue(all(run["status"] == "running" for run in runs))
            await asyncio.gather(*list(concept_ai._tasks))
            self.assertEqual(concept_ai.get_concept_run()["status"], "succeeded")
            await concept_ai.start_concept_run()
            self.assertEqual(call.await_count, 1)
            self.assertEqual(call.await_args.args[0], "glm")
            self.assertEqual(call.await_args.args[2:4], ("glm-5.3", "glm-test-key"))
            self.assertEqual(call.await_args.kwargs["reasoning_effort"], "max")
            self.assertEqual(call.await_args.kwargs["timeout_seconds"], concept_ai.MODEL_TIMEOUT_SECONDS)
            self.assertEqual(concept_ai.get_concept_run()["model"], "glm-5.3")
            await concept_ai.start_concept_run(True)
            await asyncio.gather(*list(concept_ai._tasks))
            self.assertEqual(call.await_count, 2)
        self.assertEqual(database.read_ai_run("deepseek", date)["result"], {"title": "选股缓存"})

    def test_cached_results_from_other_models_are_not_reused(self) -> None:
        date = database.china_date()
        for provider, model in [("concept:deepseek", "deepseek-chat"), ("concept:glm", "another-model")]:
            token = database.start_ai_run(provider, model, date, concept_ai.PROMPT_VERSION)
            database.finish_ai_run(provider, date, {"summary": "旧模型结果"}, None, token)
        run = concept_ai.get_concept_run()
        self.assertEqual(run["provider"], "glm")
        self.assertEqual(run["model"], "glm-5.3")
        self.assertEqual(run["status"], "idle")
        self.assertIsNone(run["result"])

    def test_previous_reasoning_cache_is_invalidated_and_max_run_keeps_its_lease(self) -> None:
        date = database.china_date()
        token = database.start_ai_run("concept:glm", "glm-5.3", date, "concept-v2-real-world")
        database.finish_ai_run("concept:glm", date, {"summary": "之前的低推理档位结果"}, None, token)
        self.assertEqual(concept_ai.get_concept_run()["status"], "idle")
        token = database.start_ai_run("concept:glm", "glm-5.3", date, concept_ai.PROMPT_VERSION)
        self.assertIsNotNone(token)
        started = (datetime.now(database.CHINA_TZ) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute("UPDATE ai_runs SET started_at = ? WHERE provider = 'concept:glm'", (started,))
        self.assertEqual(concept_ai.get_concept_run()["status"], "running")
        self.assertIsNone(database.start_ai_run("concept:glm", "glm-5.3", date, concept_ai.PROMPT_VERSION, True))
        self.assertLess(concept_ai.MODEL_TIMEOUT_SECONDS, concept_ai.RUN_TIMEOUT_SECONDS)
        self.assertLess(concept_ai.RUN_TIMEOUT_SECONDS, database.CONCEPT_AI_RUN_LEASE_SECONDS)
        expired = (datetime.now(database.CHINA_TZ) - timedelta(minutes=12)).isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute("UPDATE ai_runs SET started_at = ? WHERE provider = 'concept:glm'", (expired,))
        self.assertEqual(concept_ai.get_concept_run()["status"], "failed")
        self.assertIsNotNone(database.start_ai_run("concept:glm", "glm-5.3", date, concept_ai.PROMPT_VERSION))

    async def test_weak_market_produces_empty_recommendations_without_a_paid_call(self) -> None:
        evidence = {**evidence_fixture(), "candidates": []}
        with (patch.object(concept_ai, "collect_concept_evidence", return_value=evidence),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock) as model):
            await concept_ai.start_concept_run()
            await asyncio.gather(*list(concept_ai._tasks))
        model.assert_not_awaited()
        self.assertEqual(concept_ai.get_concept_run()["status"], "succeeded")
        self.assertEqual(concept_ai.get_concept_run()["result"]["concepts"], [])

    async def test_failed_evidence_skips_model_and_can_retry(self) -> None:
        with (patch.object(concept_ai, "collect_concept_evidence", side_effect=RuntimeError("暂无有效行情")),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock) as call):
            await concept_ai.start_concept_run()
            await asyncio.gather(*list(concept_ai._tasks))
            self.assertEqual(concept_ai.get_concept_run()["status"], "failed")
            self.assertIsNone(concept_ai.get_concept_run()["result"])
            call.assert_not_awaited()
        with (patch.object(concept_ai, "collect_concept_evidence", return_value=evidence_fixture()),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock, return_value=result_fixture())):
            await concept_ai.start_concept_run()
            await asyncio.gather(*list(concept_ai._tasks))
            self.assertEqual(concept_ai.get_concept_run()["status"], "succeeded")

    async def test_timeout_is_persisted_before_lease_expiry_and_next_day_has_no_old_result(self) -> None:
        async def slow_model(*args, **kwargs):
            await asyncio.sleep(1)
        with (patch.object(concept_ai, "RUN_TIMEOUT_SECONDS", 0.02),
              patch.object(concept_ai, "collect_concept_evidence", return_value=evidence_fixture()),
              patch.object(ai, "_call_compatible", side_effect=slow_model)):
            await concept_ai.start_concept_run()
            await asyncio.gather(*list(concept_ai._tasks))
        self.assertEqual(concept_ai.get_concept_run()["status"], "failed")
        self.assertIn("超时", concept_ai.get_concept_run()["error"])
        with patch.object(database, "china_date", return_value="2099-01-01"):
            self.assertEqual(concept_ai.get_concept_run()["status"], "idle")
            self.assertIsNone(concept_ai.get_concept_run()["result"])

    def test_api_get_is_read_only_and_post_requires_configured_secret(self) -> None:
        client = TestClient(app)
        with (patch.dict(os.environ, {"DAILY_RUN_SECRET": "test-secret"}),
              patch("backend.main.start_concept_run", new_callable=AsyncMock, return_value={"status": "running"}) as start):
            response = client.get("/api/market/concepts/ai")
            self.assertEqual(response.status_code, 200)
            start.assert_not_awaited()
            self.assertEqual(client.post("/api/market/concepts/ai").status_code, 401)
            response = client.post("/api/market/concepts/ai?force=true", headers={"x-daily-run-secret": "test-secret"})
            self.assertEqual(response.json()["status"], "running")
            start.assert_awaited_once_with(True)


class WorldNewsTests(unittest.IsolatedAsyncioTestCase):
    def test_sugar_query_expands_to_weather_supply_and_commodity_vocabulary(self) -> None:
        query = concept_news.search_query("制糖概念")
        for term in ("白糖", "天气", "厄尔尼诺", "产量", "供需"):
            self.assertIn(term, query)
        prompt = concept_ai.build_prompt(evidence_fixture())
        self.assertIn("这是机制示例，不是本次已发生的事实", prompt)
        self.assertIn("盘后新变量", prompt)
        self.assertLessEqual(len(concept_news.search_query("长概念名称" * 20)), 70)

    async def test_search_request_matches_provider_contract(self) -> None:
        requests = []
        def reply(request):
            requests.append(request)
            return httpx.Response(200, json={"search_result": []})
        client_type = httpx.AsyncClient
        with patch.object(concept_news.httpx, "AsyncClient", side_effect=lambda **kwargs: client_type(
            transport=httpx.MockTransport(reply), **kwargs,
        )):
            self.assertEqual(await concept_news.search_world_news("制糖", "BK1001", "test-key"), [])
        request = requests[0]
        body = json.loads(request.content)
        self.assertTrue(str(request.url).endswith("/web_search"))
        self.assertFalse(body["search_intent"])
        self.assertEqual(body["search_recency_filter"], "oneMonth")
        self.assertLessEqual(len(body["search_query"]), 70)

    async def test_glm_research_uses_supported_enabled_thinking_mode(self) -> None:
        with patch.object(ai, "_post_json", new_callable=AsyncMock, return_value={
            "choices": [{"message": {"content": "{}"}}],
        }) as post:
            await ai._call_compatible("glm", "test", "glm-5.3", "test-key", reasoning_effort="max", timeout_seconds=480)
        self.assertEqual(post.await_args.args[2]["thinking"], {"type": "enabled"})
        self.assertEqual(post.await_args.args[2]["reasoning_effort"], "max")
        self.assertEqual(post.await_args.kwargs["timeout_seconds"], 480)

    def test_web_results_reject_unsafe_links_and_undated_old_future_articles(self) -> None:
        now = datetime.now(database.CHINA_TZ)
        good = {"title": "产区天气与供给", "content": "原始报道摘要", "link": "https://example.org/weather",
                "publish_date": now.isoformat(), "media": "新闻来源"}
        rows = [good, good, {**good, "link": "javascript:alert(1)"},
                {**good, "link": "https://example.org/old", "publish_date": (now - timedelta(days=31)).isoformat()},
                {**good, "link": "https://example.org/future", "publish_date": (now + timedelta(days=1)).isoformat()},
                {**good, "link": "https://example.org/undated", "publish_date": ""}]
        sources = concept_news.normalize_search_results({"search_result": rows}, "BK1001")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["url"], good["link"])
        self.assertEqual(sources[0]["excerpt"], good["content"])

    async def test_search_unavailability_uses_financial_news_fallback(self) -> None:
        evidence = evidence_fixture()
        source = {"id": "BK1001:news:123", "kind": "news", "title": "真实资讯"}
        with (patch.object(concept_news, "search_world_news", side_effect=httpx.ConnectError("unavailable")),
              patch.object(concept_news.ConceptResearchClient, "news", return_value=[source])):
            result = await concept_news.enrich_world_news(evidence, "test-key")
        self.assertIn(source, result["candidates"][0]["evidence"])
        self.assertTrue(any("备用来源" in note for note in result["candidates"][0]["warnings"]))

    async def test_missing_all_sources_is_explicit_and_never_fabricates_news(self) -> None:
        with (patch.object(concept_news, "search_world_news", return_value=[]),
              patch.object(concept_news.ConceptResearchClient, "news", return_value=[])):
            result = await concept_news.enrich_world_news(evidence_fixture(), "test-key")
        self.assertEqual(len(result["candidates"][0]["evidence"]), 1)
        self.assertTrue(any("来源不足" in note for note in result["candidates"][0]["warnings"]))
