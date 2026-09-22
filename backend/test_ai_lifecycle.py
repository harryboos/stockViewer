from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend import ai, concept_ai, concept_forecast, database, glm_transport, research_jobs
from backend.test_concept_ai import evidence_fixture
from backend.test_glm_transport import Chunks, delta, mock_http


class AiLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target in (patch.object(database, "DATABASE_PATH", Path(directory.name) / "ai.sqlite3"),
                       patch.dict(os.environ, {"GLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": "test-key"}, clear=True)):
            target.start()
            self.addCleanup(target.stop)
        database.initialize()

    async def test_ai_database_work_does_not_run_on_the_event_loop(self):
        loop_thread = threading.get_ident()
        calls = []

        def threaded(name, function):
            def invoke(*args, **kwargs):
                calls.append((name, threading.get_ident()))
                return function(*args, **kwargs)
            return invoke

        evidence = {**evidence_fixture(), "candidates": []}
        with ExitStack() as stack:
            for name in ("start_ai_run", "finish_ai_run", "read_ai_run"):
                stack.enter_context(patch.object(database, name, side_effect=threaded(name, getattr(database, name))))
            stack.enter_context(patch.object(research_jobs, "record_ai_stage", side_effect=threaded("stage", research_jobs.record_ai_stage)))
            stack.enter_context(patch.object(ai, "_call_compatible", new_callable=AsyncMock, side_effect=RuntimeError("测试无模型调用")))
            stack.enter_context(patch.object(concept_ai, "collect_concept_evidence", return_value=evidence))
            stack.enter_context(patch.object(concept_forecast, "collect_evidence", new_callable=AsyncMock, return_value=evidence))
            stack.enter_context(patch.object(concept_forecast, "feedback_context", return_value={}))
            await ai._execute_provider("deepseek", [])
            await concept_ai.start_concept_run()
            await concept_forecast.start_forecast_run()
            while concept_ai._tasks or concept_forecast._tasks:
                await asyncio.gather(*list(concept_ai._tasks | concept_forecast._tasks))
        self.assertTrue({"start_ai_run", "finish_ai_run", "read_ai_run", "stage"}.issubset({name for name, _ in calls}))
        self.assertTrue(all(thread != loop_thread for _, thread in calls), calls)
        self.assertEqual(concept_ai.get_concept_run()["status"], "succeeded")
        self.assertEqual(concept_forecast.get_forecast_run()["status"], "succeeded")

    async def test_cancelled_daily_claim_is_finalized_without_a_model_request(self):
        claimed = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        start = database.start_ai_run

        def delayed_claim(*args):
            token = start(*args)
            loop.call_soon_threadsafe(claimed.set)
            release.wait(timeout=2)
            return token

        with (patch.object(database, "start_ai_run", side_effect=delayed_claim),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock) as model):
            task = asyncio.create_task(ai._execute_provider("deepseek", []))
            try:
                await asyncio.wait_for(claimed.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        model.assert_not_awaited()
        run = database.read_ai_run("deepseek", database.china_date())
        self.assertEqual(run["status"], "failed")
        self.assertIn("中断", run["error"])

    async def test_disconnected_concept_request_still_launches_claimed_worker(self):
        claimed = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        start = database.start_ai_run

        def delayed_claim(*args):
            token = start(*args)
            loop.call_soon_threadsafe(claimed.set)
            release.wait(timeout=2)
            return token

        with (patch.object(database, "start_ai_run", side_effect=delayed_claim),
              patch.object(concept_ai, "collect_concept_evidence", return_value={**evidence_fixture(), "candidates": []}),
              patch.object(ai, "_call_compatible", new_callable=AsyncMock) as model):
            task = asyncio.create_task(concept_ai.start_concept_run())
            try:
                await asyncio.wait_for(claimed.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                while concept_ai._tasks:
                    await asyncio.gather(*list(concept_ai._tasks))
        self.assertEqual(concept_ai.get_concept_run()["status"], "succeeded")
        model.assert_not_awaited()
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_attempts").fetchone()[0], 1)

    async def test_worker_context_is_restored_and_unexpected_details_are_not_persisted(self):
        outer = research_jobs.ai_token.set("outer-token")
        try:
            with patch.object(ai, "_call_compatible", new_callable=AsyncMock,
                              side_effect=Exception("Authorization: Bearer private-key; private prompt")):
                await ai._execute_provider("deepseek", [])
            self.assertEqual(research_jobs.ai_token.get(), "outer-token")
            error = database.read_ai_run("deepseek", database.china_date())["error"]
            self.assertNotIn("private", error)
            for module, namespace in ((concept_ai, "concept:glm"), (concept_forecast, "forecast:glm")):
                token = database.start_ai_run(namespace, module.MODEL, database.china_date(), module.PROMPT_VERSION)
                empty = {**evidence_fixture(), "candidates": []}
                with (patch.object(concept_ai, "collect_concept_evidence", return_value=empty),
                      patch.object(concept_forecast, "collect_evidence", new_callable=AsyncMock, return_value=empty),
                      patch.object(concept_forecast, "feedback_context", return_value={})):
                    await module._execute(database.china_date(), "test-key", token)
                self.assertEqual(research_jobs.ai_token.get(), "outer-token")
        finally:
            research_jobs.ai_token.reset(outer)

    def test_replacing_an_expired_lease_closes_its_attempt_and_rejects_late_results(self):
        day = database.china_date()
        old = database.start_ai_run("deepseek", "model", day, "v1")
        expired = (datetime.now(database.CHINA_TZ) - timedelta(minutes=10)).isoformat()
        with database.connection() as db:
            db.execute("UPDATE ai_runs SET started_at=? WHERE run_token=?", (expired, old))
        new = database.start_ai_run("deepseek", "model", day, "v1")
        database.finish_ai_run("deepseek", day, {"title": "过期结果"}, None, old)
        with database.connection() as db:
            attempts = {row["token"]: dict(row) for row in db.execute("SELECT * FROM ai_attempts")}
        self.assertEqual(attempts[old]["status"], "failed")
        self.assertIsNotNone(attempts[old]["finished_at"])
        self.assertEqual(attempts[new]["status"], "running")
        self.assertIsNone(database.read_ai_run("deepseek", day)["result"])


class BoundedStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_newline_free_response_is_rejected_before_the_whole_body_is_read(self):
        consumed = []

        class UnboundedLine(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index in range(100):
                    consumed.append(index)
                    yield b"x" * 16

        with (mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=UnboundedLine())),
              patch.object(glm_transport, "MAX_EVENT_CHARS", 64)):
            with self.assertRaisesRegex(RuntimeError, "安全长度"):
                await ai._call_compatible("glm", "test", "glm-5.3", "test-key", resilient_stream=True)
        self.assertLess(len(consumed), 10)

    async def test_crlf_split_between_chunks_keeps_events_intact(self):
        raw = delta('{"summary":"完整报告"}', "stop").replace(b"\n", b"\r\n")
        stream = Chunks([raw[index:index + 1] for index in range(len(raw))])
        with mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)):
            self.assertEqual(await ai._call_compatible("glm", "test", "glm-5.3", "test-key", resilient_stream=True),
                             {"summary": "完整报告"})
        self.assertTrue(stream.closed)
