from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from backend import ai, database, research_jobs


@contextmanager
def mock_http(handler):
    client_class = httpx.AsyncClient
    with patch.object(ai.httpx, "AsyncClient", side_effect=lambda **kw: client_class(
            **kw, transport=httpx.MockTransport(handler))):
        yield


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def completion(content: dict, reason: str | None = "stop") -> dict:
    choice = {"message": {"content": json.dumps(content, ensure_ascii=False)}}
    if reason is not None:
        choice["finish_reason"] = reason
    return {"choices": [choice]}


class ModelResponseReviewTests(unittest.IsolatedAsyncioTestCase):
    async def call(self):
        return await ai._call_compatible("glm", "研究材料", "glm-5.3", "test-key", reasoning_effort="max")

    async def test_normal_json_is_read_incrementally_and_keeps_max_payload(self):
        raw = json.dumps(completion({"summary": "完整中文报告"}), ensure_ascii=False).encode()
        stream = TrackedStream([raw[index:index + 1] for index in range(len(raw))])
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, stream=stream)

        with mock_http(handle):
            self.assertEqual(await self.call(), {"summary": "完整中文报告"})
        self.assertTrue(stream.closed)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], "glm-5.3")
        self.assertEqual(requests[0]["reasoning_effort"], "max")
        self.assertNotIn("stream", requests[0])

    async def test_oversized_json_stops_reading_and_closes_without_retry(self):
        stream = TrackedStream([b"x" * 16] * 100)
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(200, stream=stream)

        with mock_http(handle), patch.object(ai, "MAX_RESPONSE_BYTES", 64):
            with self.assertRaisesRegex(RuntimeError, "安全长度"):
                await self.call()
        self.assertEqual(stream.reads, 5)
        self.assertTrue(stream.closed)
        self.assertEqual(len(calls), 1)

    async def test_limit_applies_after_decompression(self):
        stream = TrackedStream([gzip.compress(b"x" * 10000)])
        with (mock_http(lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)),
              patch.object(ai, "MAX_RESPONSE_BYTES", 128)):
            with self.assertRaisesRegex(RuntimeError, "安全长度"):
                await self.call()
        self.assertTrue(stream.closed)

    async def test_http_failures_do_not_read_or_echo_provider_error_bodies(self):
        stream = TrackedStream([b"private-key, private prompt"] * 100)
        with mock_http(lambda _: httpx.Response(503, stream=stream)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503") as failure:
                await self.call()
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_explicit_incomplete_finish_is_rejected_even_with_valid_json(self):
        for reason in ("length", "content_filter", "network_error", "tool_calls", "private upstream detail"):
            with self.subTest(reason=reason), mock_http(lambda _: httpx.Response(
                    200, json=completion({"summary": "看起来已完成的报告"}, reason))):
                with self.assertRaisesRegex(RuntimeError, "未完整|未返回完整") as failure:
                    await self.call()
                self.assertNotIn("private", str(failure.exception))

    async def test_legacy_missing_finish_is_preserved_but_malformed_choices_are_rejected(self):
        with mock_http(lambda _: httpx.Response(200, json=completion({"summary": "兼容返回"}, None))):
            self.assertEqual(await self.call(), {"summary": "兼容返回"})
        for payload in ({"choices": [None]}, {"choices": ["not a choice"]}, []):
            with self.subTest(payload=payload), mock_http(lambda _: httpx.Response(200, json=payload)):
                with self.assertRaises(RuntimeError):
                    await self.call()


class AiRunStatusReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target in (patch.object(database, "DATABASE_PATH", Path(temporary.name) / "ai.sqlite3"),
                       patch.dict(os.environ, {"GLM_API_KEY": "test-key"}, clear=True)):
            target.start()
            self.addCleanup(target.stop)
        database.initialize()

    async def test_incomplete_model_output_is_failed_and_never_archived(self):
        report = {"title": "股票研究", "summary": "依据候选快照研究市场表现。", "logic": "比较行情和成交数据。",
                  "picks": [{"code": f"60000{index}", "name": f"测试{index}", "score": 70,
                             "reason": "相对成交活跃。", "risk": "走势存在不确定性。"} for index in range(1, 4)]}
        candidates = [{"symbol": pick["code"], "name": pick["name"]} for pick in report["picks"]]
        with mock_http(lambda _: httpx.Response(200, json=completion(report, "length"))):
            result = await ai._execute_provider("glm", candidates)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM selection_reports").fetchone()[0], 0)

    def test_status_uses_current_token_and_does_not_revive_invalidated_model_stage(self):
        day = database.china_date()
        old = database.start_ai_run("glm", ai.model_for("glm"), day, ai.PROMPT_VERSION)
        database.finish_ai_run("glm", day, {"title": "旧报告"}, None, old)
        with patch.dict(os.environ, {"GLM_MODEL": "new-configured-model"}):
            run = next(item for item in research_jobs.operations_payload()["aiRuns"] if item["provider"] == "glm")
            self.assertEqual(run["status"], "pending")
            self.assertIsNone(run["stage"])
            new = database.start_ai_run("glm", ai.model_for("glm"), day, ai.PROMPT_VERSION)
            research_jobs.record_ai_stage(new, "新模型分析中")
            run = next(item for item in research_jobs.operations_payload()["aiRuns"] if item["provider"] == "glm")
            self.assertEqual(run["status"], "running")
            self.assertEqual(run["stage"], "新模型分析中")
            self.assertEqual(research_jobs.operations_payload()["aiUsage"]["runsToday"], 2)
