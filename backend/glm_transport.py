"""Bounded streaming and overload recovery for long GLM research requests."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
MAX_CONTENT_CHARS = 256_000
MAX_EVENT_CHARS = 64_000
TRANSIENT_CODES = {"1200", "1230", "1234", "1302", "1305"}
ERROR_MESSAGES = {
    "1113": "GLM 账户余额不足，请检查账户余额",
    "1211": "GLM 模型不可用，请检查接口地址与模型权限",
    "1261": "预测材料超过 GLM 上下文限制",
    "1301": "GLM 未通过内容审核，本次未生成预测",
    "1302": "GLM 请求达到速率限制，请稍后重试",
    "1305": "GLM 高峰期服务繁忙，请稍后重试",
    "1308": "GLM 使用额度已达上限，请等待额度恢复",
    "1309": "GLM 套餐已到期，请检查账户套餐",
    "1310": "GLM 周或月使用额度已达上限，请等待额度恢复",
    "1311": "GLM 套餐尚未开放该模型权限",
    "1313": "GLM 账户使用受限，请检查账户状态",
    "1314": "GLM 企业套餐已失效，请联系账户管理员",
    "1315": "GLM 密钥不适用于当前接口，请检查密钥类型",
    **{str(code): "GLM 使用额度或消费额度已达上限，请检查账户额度" for code in range(1316, 1322)},
}


class RequestFailure(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def response_failure(response: httpx.Response, body: Any) -> RequestFailure:
    detail = body.get("error") if isinstance(body, dict) else None
    raw_code = str(detail.get("code", "")) if isinstance(detail, dict) else ""
    # Never expose provider response text: it may echo prompts or credentials.
    code = raw_code if raw_code.isascii() and raw_code.isdigit() and len(raw_code) <= 6 else ""
    status = response.status_code
    message = ERROR_MESSAGES.get(code)
    if message is None:
        message = ("GLM 密钥验证失败，请检查密钥" if status == 401 else
                   "GLM 接口或模型无访问权限，请检查账户权限" if status == 403 else
                   "GLM 高峰期服务繁忙，请稍后重试" if status == 429 or status >= 500 else
                   "GLM 请求被拒绝，请检查接口配置")
    retryable = code in TRANSIENT_CODES or (not raw_code and status in (429, 500, 502, 503, 504))
    suffix = f"HTTP {status}" + (f"，错误码 {code}" if code else "")
    return RequestFailure(f"{message}（{suffix}）", retryable=retryable,
                          retry_after=retry_after_seconds(response.headers.get("retry-after")))


def check_finish(reason: Any) -> None:
    if reason == "stop":
        return
    message = {
        "length": "GLM 输出达到长度上限，预测未完整生成",
        "model_context_window_exceeded": "预测材料超过 GLM 上下文限制",
        "sensitive": "GLM 未通过内容审核，本次未生成预测",
        "network_error": "GLM 推理过程中断，请稍后重新生成",
    }.get(reason if isinstance(reason, str) else "", "GLM 响应未完整结束，请稍后重新生成")
    raise RequestFailure(message)


async def read_json(response: httpx.Response) -> Any:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > MAX_CONTENT_CHARS * 4:
            raise RequestFailure("GLM 响应超出安全长度，本次未生成预测")
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError) as error:
        raise RequestFailure("GLM 接口返回了无法解析的数据") from error


async def read_stream(response: httpx.Response) -> dict[str, Any]:
    parts: list[str] = []
    content_size = 0
    event: list[str] = []
    event_size = 0
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            text = line[5:].lstrip(" ")
            event.append(text)
            event_size += len(text)
            if event_size > MAX_EVENT_CHARS:
                raise RequestFailure("GLM 响应片段超出安全长度")
        elif line == "" and event:
            text = "\n".join(event)
            event, event_size = [], 0
            if text == "[DONE]":
                break  # A stream must also contain an explicit successful finish.
            try:
                chunk = json.loads(text)
                if not isinstance(chunk, dict):
                    raise ValueError("invalid chunk")
                if "error" in chunk:
                    failure = response_failure(response, chunk)
                    failure.retryable = False  # A 200 stream has already been accepted.
                    raise failure
                for choice in chunk.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    # Reasoning chunks keep the connection active; never retain or log them.
                    if content is not None:
                        if not isinstance(content, str):
                            raise ValueError("invalid content")
                        content_size += len(content)
                        if content_size > MAX_CONTENT_CHARS:
                            raise RequestFailure("GLM 响应超出安全长度，本次未生成预测")
                        parts.append(content)
                    finish = choice.get("finish_reason")
                    if finish is not None:
                        check_finish(finish)
                        return {"choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish}]}
            except (ValueError, TypeError, AttributeError) as error:
                raise RequestFailure("GLM 流式响应格式异常，本次未生成预测") from error
    raise RequestFailure("GLM 连接提前结束，预测未完整生成，请稍后重试")


async def post_research(url: str, headers: dict[str, str], payload: dict[str, Any], *,
                        timeout_seconds: float) -> dict[str, Any]:
    """Retry explicit overload rejection or connection setup failure, never partial output."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        # This deadline includes all requests AND backoff, so retries cannot extend the job lease.
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=20.0)) as client:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        from .research_jobs import ai_token, record_ai_stage
                        if ai_token.get():
                            await asyncio.to_thread(record_ai_stage, ai_token.get(), f"GLM 深度分析 · 第 {attempt} 次请求", call=True)
                        async with client.stream("POST", url, headers=headers, json={**payload, "stream": True}) as response:
                            if response.status_code >= 400:
                                try:
                                    body = await read_json(response)
                                except RequestFailure:
                                    body = None  # Gateways may send an HTML 502/503 response.
                                raise response_failure(response, body)
                            if "text/event-stream" in response.headers.get("content-type", "").lower():
                                return await read_stream(response)
                            # Some compatible gateways ignore stream=True and return normal JSON.
                            body = await read_json(response)
                            if isinstance(body, dict) and "error" in body:
                                raise response_failure(response, body)
                            try:
                                check_finish(body["choices"][0].get("finish_reason"))
                            except (KeyError, IndexError, TypeError, AttributeError) as error:
                                raise RequestFailure("GLM 未返回完整预测结果") from error
                            return body
                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
                        failure = RequestFailure("GLM 连接建立失败，请稍后重试", retryable=True)
                        failure.__cause__ = error
                    except RequestFailure as error:
                        failure = error
                    except httpx.TimeoutException as error:
                        raise RequestFailure("等待 GLM 响应超时，请稍后重新生成") from error
                    except httpx.HTTPError as error:
                        raise RequestFailure("GLM 连接中断，预测未完整生成，请稍后重新生成") from error
                    if not failure.retryable or attempt == MAX_ATTEMPTS:
                        raise failure
                    delay = max(5 * 2 ** (attempt - 1) + random.uniform(0, 1), failure.retry_after or 0)
                    remaining = deadline - asyncio.get_running_loop().time()
                    if delay + 30 >= remaining:
                        raise failure  # Honor Retry-After; never retry earlier to fit our budget.
                    logger.warning("forecast_glm_retry attempt=%s/%s delay=%.1fs reason=%s",
                                   attempt, MAX_ATTEMPTS, delay, failure)
                    if ai_token.get():
                        await asyncio.to_thread(record_ai_stage, ai_token.get(), f"高峰等待 {delay:.0f} 秒后重试")
                    await asyncio.sleep(delay)
    except TimeoutError as error:
        raise RequestFailure("GLM 深度分析及高峰重试等待超时，请稍后重新生成") from error
    raise AssertionError("unreachable")
