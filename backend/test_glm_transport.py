from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx

from backend import ai, glm_transport as transport


def event(value) -> bytes:
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n".encode()


def delta(content=None, finish=None, **fields) -> bytes:
    return event({"choices": [{"index": 0, "delta": {"content": content, **fields}, "finish_reason": finish}]})


def success() -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=delta('{"summary":"完整结果"}', "stop") + b"data: [DONE]\n\n")


@contextmanager
def mock_http(handler):
    client_class = httpx.AsyncClient
    with patch.object(transport.httpx, "AsyncClient", side_effect=lambda **kw: client_class(
            **kw, transport=httpx.MockTransport(handler))):
        yield


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


class SlowStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(1)
        yield delta('{}', 'stop')


class GlmTransportTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, timeout=480):
        return await ai._call_compatible("glm", "研究材料", "glm-5.3", "secret-test-key",
                                         reasoning_effort="max", timeout_seconds=timeout, resilient_stream=True)

    async def test_stream_collects_split_utf8_ignores_reasoning_and_keeps_max(self):
        requests = []
        raw = (b": heartbeat\n\n" + delta(reasoning_content="不应返回或存储的思考") +
               event({"choices": [], "usage": {}}) + delta('{"summary":') + delta('"完整结果"}', "stop"))
        stream = Chunks([raw[i:i + 7] for i in range(0, len(raw), 7)])

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=stream)

        with mock_http(handle):
            self.assertEqual(await self.call(), {"summary": "完整结果"})
        self.assertTrue(stream.closed)
        self.assertEqual(requests[0]["model"], "glm-5.3")
        self.assertEqual(requests[0]["thinking"], {"type": "enabled"})
        self.assertEqual(requests[0]["reasoning_effort"], "max")
        self.assertTrue(requests[0]["stream"])

    async def test_rate_limit_and_html_gateway_outage_recover_with_backoff(self):
        responses = [httpx.Response(429, json={"error": {"code": "1302"}}, headers={"Retry-After": "12"}),
                     httpx.Response(503, text="gateway unavailable"), success()]
        with (mock_http(lambda request: responses.pop(0)),
              patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep,
              patch.object(transport.random, "uniform", return_value=0)):
            self.assertEqual(await self.call(), {"summary": "完整结果"})
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [12, 10])
        self.assertEqual(responses, [])

    async def test_temporary_business_errors_retry_but_exhaust_at_five_attempts(self):
        for code in ("1305", "1234", ""):
            calls = []

            def handle(request):
                calls.append(request)
                return httpx.Response(503 if code == "1234" else 429, json={"error": {"code": code}})

            with (self.subTest(code=code), mock_http(handle),
                  patch.object(transport.asyncio, "sleep", new_callable=AsyncMock)):
                with self.assertRaisesRegex(RuntimeError, "HTTP"):
                    await self.call()
            self.assertEqual(len(calls), 5)

    async def test_permanent_429_and_auth_errors_do_not_retry_or_echo_provider_text(self):
        cases = [(429, code) for code in ("1113", "1308", "1309", "1310", "1311", "1313", "1314", "1315", "1316", "1321")]
        cases += [(401, "1000"), (403, "1220"), (400, "1211"), (400, "1261")]
        for status, code in cases:
            calls = []

            def handle(request):
                calls.append(request)
                return httpx.Response(status, json={"error": {"code": code, "message": "secret-test-key 研究材料"}})

            with self.subTest(status=status, code=code), mock_http(handle):
                with self.assertRaises(RuntimeError) as error:
                    await self.call()
            self.assertEqual(len(calls), 1)
            self.assertIn(code, str(error.exception))
            self.assertNotIn("secret-test-key", str(error.exception))
            self.assertNotIn("研究材料", str(error.exception))

    async def test_retry_after_larger_than_budget_is_not_shortened(self):
        with (mock_http(lambda request: httpx.Response(429, json={}, headers={"retry-after": "900"})),
              patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep):
            with self.assertRaisesRegex(RuntimeError, "繁忙"):
                await self.call()
            sleep.assert_not_awaited()

    def test_retry_after_supports_dates_and_ignores_invalid_values(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=50), usegmt=True)
        self.assertTrue(48 <= transport.retry_after_seconds(future) <= 50)
        for value in (None, "NaN", "inf", "invalid"):
            self.assertIsNone(transport.retry_after_seconds(value))
        self.assertEqual(transport.retry_after_seconds("-1"), 0)

    async def test_connection_setup_failure_retries(self):
        calls = []

        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectTimeout("connection timed out")
            return success()

        with mock_http(handle), patch.object(transport.asyncio, "sleep", new_callable=AsyncMock):
            self.assertEqual(await self.call(), {"summary": "完整结果"})
        self.assertEqual(len(calls), 2)

    async def test_partial_output_disconnect_does_not_replay_or_return_partial_json(self):
        for prefix in (delta('{"summary":"'), delta(reasoning_content="already computing")):
            stream = Chunks([prefix, httpx.ReadError("disconnected")])
            with (mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)),
                  patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep):
                with self.assertRaisesRegex(RuntimeError, "连接中断"):
                    await self.call()
                sleep.assert_not_awaited()
            self.assertTrue(stream.closed)

    async def test_stream_errors_and_truncated_results_are_never_accepted_or_retried(self):
        cases = [delta('{}') + b"data: [DONE]\n\n", delta('{}'), delta('{}', "length"),
                 delta('{}', "network_error"), delta('{}', "sensitive"), b"data: not-json\n\n",
                 delta(reasoning_content="started") + event({"error": {"code": "1305"}})]
        for raw in cases:
            with (self.subTest(raw=raw), mock_http(lambda request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=raw)),
                  patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep):
                with self.assertRaises(RuntimeError):
                    await self.call()
                sleep.assert_not_awaited()

    async def test_non_stream_gateway_response_still_requires_complete_object(self):
        with mock_http(lambda request: httpx.Response(200, json={
                "choices": [{"message": {"content": '{"summary":"complete"}'}, "finish_reason": "stop"}]})):
            self.assertEqual(await self.call(), {"summary": "complete"})
        for body in ([], {}, {"choices": [{"message": {"content": '{}'}, "finish_reason": "length"}]},
                     {"choices": [{"message": {"content": '[]'}, "finish_reason": "stop"}]}):
            with self.subTest(body=body), mock_http(lambda request: httpx.Response(200, json=body)):
                with self.assertRaises(RuntimeError):
                    await self.call()

    async def test_response_size_is_bounded(self):
        with (mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=delta('x' * 101))),
              patch.object(transport, "MAX_CONTENT_CHARS", 100)):
            with self.assertRaisesRegex(RuntimeError, "安全长度"):
                await self.call()

    async def test_slow_stream_obeys_total_deadline(self):
        with mock_http(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowStream())):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                await self.call(timeout=0.02)

    async def test_cancellation_is_not_retried(self):
        started = asyncio.Event()

        def handle(request):
            started.set()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowStream())

        with mock_http(handle):
            task = asyncio.create_task(self.call())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
