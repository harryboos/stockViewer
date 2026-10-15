from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import ai, glm_transport as transport, research_jobs
from backend.test_glm_transport import Chunks, delta, event, mock_http, success


class DeepSeekTransportTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, timeout=480):
        return await ai._call_compatible("deepseek", "研究材料", "deepseek-flash", "test-deepseek-key",
                                         reasoning_effort="max", timeout_seconds=timeout, resilient_stream=True)

    async def test_flash_max_request_and_long_reasoning_are_not_retained(self):
        requests = []
        raw = b": keep-alive\n\n" + b"".join(delta(reasoning_content="private reasoning" * 100) for _ in range(80))
        raw += event({"choices": [], "usage": {"completion_tokens": 9000}}) + delta('{"summary":"完成"}', "stop")
        stream = Chunks([raw[index:index + 13] for index in range(0, len(raw), 13)])

        def handle(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        with (mock_http(handle), patch.object(transport, "MAX_DEEPSEEK_CONTENT_CHARS", 50),
              patch.dict(os.environ, {"DEEPSEEK_BASE_URL": "https://api.deepseek.com"}),
              self.assertLogs("backend.glm_transport", "INFO") as logs):
            result = await self.call()
        self.assertEqual(result, {"summary": "完成"})
        self.assertTrue(stream.closed)
        request = json.loads(requests[0].content)
        self.assertEqual(str(requests[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-deepseek-key")
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertEqual(request["thinking"], {"type": "enabled"})
        self.assertEqual(request["reasoning_effort"], "max")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertTrue(request["stream"])
        for name in ("temperature", "max_tokens", "max_completion_tokens"):
            self.assertNotIn(name, request)
        rendered = " ".join(logs.output)
        self.assertIn("forecast_deepseek_stage", rendered)
        self.assertNotIn("GLM", rendered)
        self.assertNotIn("private reasoning", rendered)
        self.assertNotIn("test-deepseek-key", rendered)

    async def test_symbolic_rate_limit_and_server_rejections_retry_safely(self):
        responses = [httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "private"}},
                                    headers={"retry-after": "12"}),
                     httpx.Response(500, json={"error": {"code": "server_error"}}),
                     httpx.Response(503, text="private proxy failure"), success()]
        with (mock_http(lambda request: responses.pop(0)),
              patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep,
              patch.object(transport.random, "uniform", return_value=0),
              self.assertLogs("backend.glm_transport", "WARNING") as logs):
            self.assertEqual(await self.call(), {"summary": "完整结果"})
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [12, 10, 20])
        self.assertTrue(all("forecast_deepseek_retry" in line for line in logs.output))
        self.assertNotIn("private", " ".join(logs.output))
        self.assertFalse(responses)

    async def test_auth_balance_and_bad_parameters_are_not_retried(self):
        for status in (400, 401, 402, 403, 422):
            calls = []

            def handle(request):
                calls.append(request)
                return httpx.Response(status, json={"error": {"code": "private upstream detail", "message": "test-deepseek-key"}})

            with self.subTest(status=status), mock_http(handle):
                with self.assertRaisesRegex(RuntimeError, "DeepSeek") as failure:
                    await self.call()
            self.assertEqual(len(calls), 1)
            self.assertNotIn("private", str(failure.exception))
            self.assertNotIn("test-deepseek-key", str(failure.exception))

    async def test_keepalive_queue_does_not_consume_generation_budget(self):
        class QueuedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b": keep-alive\n\n"
                await asyncio.sleep(0.06)
                yield b": keep-alive\n\n"
                yield delta(reasoning_content="private first token")
                await asyncio.sleep(0.025)
                yield delta('{"summary":"排队后完成"}', "stop")

        with (mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=QueuedStream())),
              patch.object(transport, "QUEUE_TIMEOUT_SECONDS", 0.2)):
            self.assertEqual(await self.call(timeout=0.05), {"summary": "排队后完成"})

    async def test_keepalive_only_queue_has_its_own_bounded_timeout(self):
        class QueuedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b": keep-alive\n\n"
                await asyncio.sleep(1)
                yield delta('{}', "stop")

        with (mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=QueuedStream())),
              patch.object(transport, "QUEUE_TIMEOUT_SECONDS", 0.02),
              patch.object(transport.asyncio, "sleep", wraps=asyncio.sleep) as sleep):
            with self.assertRaisesRegex(RuntimeError, "DeepSeek 高峰接入等待超时"):
                await self.call(timeout=1)
        self.assertEqual(sleep.call_count, 1)  # The stream's wait, no retry delay.

    async def test_accepted_connect_errors_never_replay_even_before_first_token(self):
        for prefix in (b": keep-alive\n\n", delta(reasoning_content="private"), delta('{"summary":"')):
            calls = []
            stream = Chunks([prefix, httpx.ConnectError("private connection detail")])

            def handle(request):
                calls.append(request)
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

            with (self.subTest(prefix=prefix), mock_http(handle),
                  patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep):
                with self.assertRaisesRegex(RuntimeError, "DeepSeek 连接中断"):
                    await self.call()
                sleep.assert_not_awaited()
            self.assertEqual(len(calls), 1)
            self.assertTrue(stream.closed)

    async def test_incomplete_finish_and_stream_error_are_not_accepted_or_replayed(self):
        for raw in [delta('{}', reason) for reason in ("length", "aborted", "insufficient_system_resource", "content_filter")
                    ] + [delta('{}') + b"data: [DONE]\n\n", event({"error": {"code": "server_error"}})]:
            with (self.subTest(raw=raw), mock_http(lambda request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=raw)),
                  patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep):
                with self.assertRaisesRegex(RuntimeError, "DeepSeek"):
                    await self.call()
                sleep.assert_not_awaited()

    async def test_json_gateway_discards_reasoning_before_leaving_transport(self):
        body = {"choices": [{"message": {"content": '{"summary":"完成"}', "reasoning_content": "private reasoning"},
                             "finish_reason": "stop"}]}
        with mock_http(lambda request: httpx.Response(200, json=body)):
            result = await transport.post_research("https://api.deepseek.com/chat/completions", {}, {},
                                                   timeout_seconds=1, provider="deepseek")
        self.assertNotIn("reasoning_content", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    async def test_progress_names_deepseek_without_recording_thoughts(self):
        raw = delta(reasoning_content="private") + delta(reasoning_content="private again") + delta('{}', "stop")
        token = research_jobs.ai_token.set("test-token")
        try:
            with (mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)),
                  patch.object(research_jobs, "record_ai_stage") as record):
                await self.call()
            labels = [call.args[1] for call in record.call_args_list]
            self.assertEqual(labels, ["等待 DeepSeek 接入 · 第 1 次请求", "DeepSeek 已接入，等待推理开始",
                                      "DeepSeek MAX 深度推理中", "正在整理预测报告"])
            self.assertEqual(sum(call.kwargs.get("call", False) for call in record.call_args_list), 1)
        finally:
            research_jobs.ai_token.reset(token)

    async def test_daily_default_is_flash_without_changing_daily_thinking_policy(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ai.model_for("deepseek"), "deepseek-flash")
        with patch.object(ai, "_post_json", new_callable=AsyncMock, return_value={
                "choices": [{"message": {"content": '{}'}, "finish_reason": "stop"}]}) as post:
            await ai._call_compatible("deepseek", "快照", "deepseek-flash", "test-key")
        request = post.call_args.args[2]
        self.assertEqual(request["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", request)
