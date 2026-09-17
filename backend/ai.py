from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import database
from .strategies import candidate_snapshot


Provider = Literal["deepseek", "glm", "qwen"]
PROVIDERS: tuple[Provider, ...] = ("deepseek", "glm", "qwen")
KEY_NAMES: dict[Provider, tuple[str, ...]] = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "glm": ("GLM_API_KEY",),
    "qwen": ("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
}
DEFAULT_MODELS: dict[Provider, str] = {
    "deepseek": "deepseek-chat",
    "glm": "glm-5.3",
    "qwen": "qwen3.8-max",
}
MODEL_KEYS: dict[Provider, str] = {
    "deepseek": "DEEPSEEK_MODEL",
    "glm": "GLM_MODEL",
    "qwen": "QWEN_MODEL",
}
DEFAULT_BASE_URLS: dict[Provider, str] = {
    "deepseek": "https://api.deepseek.com",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
BASE_URL_KEYS: dict[Provider, str] = {
    "deepseek": "DEEPSEEK_BASE_URL",
    "glm": "GLM_BASE_URL",
    "qwen": "QWEN_BASE_URL",
}
PROVIDER_LABELS: dict[Provider, str] = {
    "deepseek": "DeepSeek",
    "glm": "GLM",
    "qwen": "Qwen",
}
PROMPT_VERSION = "5"
TEXT_SAFETY_LIMITS = {
    "title": 50,
    "summary": 5000,
    "logic": 2000,
    "name": 100,
    "reason": 2000,
    "risk": 1000,
}
SHARED_SYSTEM_INSTRUCTION = (
    "你是谨慎的 A 股量化研究助手。只根据用户提供的候选快照做横向研究排序，"
    "输出简体中文 JSON；不得杜撰事实，不构成投资建议。"
)


class AiPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=4)
    risk: str = Field(min_length=2)


class AiResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=TEXT_SAFETY_LIMITS["title"])
    summary: str = Field(min_length=8)
    logic: str = Field(min_length=4)
    picks: list[AiPick] = Field(min_length=3, max_length=3)


RESULT_SCHEMA = AiResult.model_json_schema()
_daily_task: asyncio.Task[dict[str, Any]] | None = None
_scoped_tasks: dict[tuple, asyncio.Task] = {}


def read_secret(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def provider_key(provider: Provider) -> str | None:
    for name in KEY_NAMES[provider]:
        value = read_secret(name)
        if value:
            return value
    return None


def model_for(provider: Provider) -> str:
    return read_secret(MODEL_KEYS[provider]) or DEFAULT_MODELS[provider]


def base_url_for(provider: Provider) -> str:
    return (read_secret(BASE_URL_KEYS[provider]) or DEFAULT_BASE_URLS[provider]).rstrip("/")


def provider_status() -> dict[str, bool]:
    return {provider: bool(provider_key(provider)) for provider in PROVIDERS}


def _empty_run(provider: Provider, status: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model_for(provider),
        "status": status,
        "result": None,
        "error": None,
        "finishedAt": None,
    }


def _current_run(provider: Provider, run_date: str) -> dict[str, Any] | None:
    existing = database.read_ai_run(provider, run_date)
    if existing and (existing["status"] == "running" or (
        existing.get("promptVersion") == PROMPT_VERSION and existing.get("model") == model_for(provider)
    )):
        return existing
    return None


def _build_prompt(candidates: list[dict[str, Any]]) -> str:
    snapshot = [
        {
            "code": stock["symbol"],
            "name": stock["name"],
            "industry": stock.get("industry"),
            "market": stock.get("market"),
            "tradeDate": stock.get("quote", {}).get("tradeDate"),
            "close": stock.get("quote", {}).get("close"),
            "pctChg": stock.get("quote", {}).get("pctChg"),
            "amount": stock.get("quote", {}).get("amount"),
        }
        for stock in candidates
    ]
    return (
        "任务：从候选股票中严格选择 3 只并排序。\n"
        "统一规则：不得杜撰代码、价格、财务数据或新闻；仅根据输入快照比较；"
        "不预测确定收益；三只股票不得重复且必须来自候选池。\n"
        "输出要求：title、summary、logic 使用简体中文；picks 恰好包含 3 项；"
        "每项必须包含 code、name、0-100 的整数 score、引用输入数据或明确说明数据不足的 reason，以及具体 risk。\n"
        "表达建议：title 尽量控制在 30 字内，其余说明优先使用清晰短句；"
        "完整表达需要更多文字时可以超过建议长度，不会仅因文字较长判定失败。\n"
        f"候选快照：{json.dumps(snapshot, ensure_ascii=False)}"
    )


def _clamp_text(value: Any, max_length: int) -> Any:
    if not isinstance(value, str):
        return value
    return value.strip()[:max_length]


def _normalize_result_text(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep a valid model response usable when a provider ignores text limits."""
    normalized = dict(raw)
    for field in ("title", "summary", "logic"):
        if field in normalized:
            normalized[field] = _clamp_text(normalized[field], TEXT_SAFETY_LIMITS[field])

    picks = normalized.get("picks")
    if isinstance(picks, list):
        normalized_picks: list[Any] = []
        for item in picks:
            if not isinstance(item, dict):
                normalized_picks.append(item)
                continue
            pick = dict(item)
            for field in ("name", "reason", "risk"):
                if field in pick:
                    pick[field] = _clamp_text(pick[field], TEXT_SAFETY_LIMITS[field])
            normalized_picks.append(pick)
        normalized["picks"] = normalized_picks
    return normalized


async def _post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], *, timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    timeout = httpx.Timeout(timeout_seconds, connect=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as error:
        raise RuntimeError("模型接口连接失败") from error
    if response.status_code >= 400:
        raise RuntimeError(f"模型接口请求失败（HTTP {response.status_code}）")
    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError("模型接口返回了无法解析的数据") from error


async def _call_compatible(
    provider: Provider, prompt: str, model: str, key: str,
    *, system_instruction: str = SHARED_SYSTEM_INSTRUCTION,
    reasoning_effort: Literal["low", "high", "max"] | None = None,
    timeout_seconds: float | None = None,
    resilient_stream: bool = False,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    if provider == "qwen":
        request["enable_thinking"] = False
    if provider == "glm" and reasoning_effort:
        request.update(thinking={"type": "enabled"}, reasoning_effort=reasoning_effort)
    transport = _post_json
    from .research_jobs import ai_token, record_ai_stage
    if not resilient_stream and ai_token.get():
        await asyncio.to_thread(record_ai_stage, ai_token.get(), "模型分析中", call=True)
    if resilient_stream:
        if provider != "glm":
            raise ValueError("长时研究流式请求仅用于 GLM")
        from .glm_transport import post_research
        transport = post_research
    payload = await transport(
        f"{base_url_for(provider)}/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        request,
        **({"timeout_seconds": timeout_seconds or 90.0} if resilient_stream or timeout_seconds is not None else {}),
    )
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise TypeError("empty content")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise TypeError("expected JSON object")
        return result
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{PROVIDER_LABELS[provider]} 未返回可解析结果") from error


async def _execute_provider(
    provider: Provider, candidates: list[dict[str, Any]], force: bool = False, run_date: str | None = None,
) -> dict[str, Any]:
    run_date = run_date or database.china_date()
    model = model_for(provider)
    key = provider_key(provider)
    if not key:
        return _empty_run(provider, "not_configured")
    token = database.start_ai_run(provider, model, run_date, PROMPT_VERSION, force)
    if token is None:
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "pending")
    from .research_jobs import ai_token
    context_token = ai_token.set(token)
    try:
        prompt = _build_prompt(candidates)
        raw = await asyncio.wait_for(_call_compatible(provider, prompt, model, key), timeout=100)
        result = AiResult.model_validate(_normalize_result_text(raw))
        allowed = {stock["symbol"]: stock["name"] for stock in candidates}
        codes = [pick.code for pick in result.picks]
        if len(set(codes)) != 3:
            raise RuntimeError("模型返回了重复股票")
        for pick in result.picks:
            if pick.code not in allowed:
                raise RuntimeError(f"模型返回了候选池外代码 {pick.code}")
            pick.name = allowed[pick.code]
        saved = result.model_dump()
        database.finish_ai_run(provider, run_date, saved, None, token)
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")
    except ValidationError as error:
        message = f"模型返回格式不符合约定（{len(error.errors())} 处），请点击重试"
        database.finish_ai_run(provider, run_date, None, message, token)
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")
    except (RuntimeError, json.JSONDecodeError) as error:
        database.finish_ai_run(provider, run_date, None, str(error), token)
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")
    except asyncio.CancelledError:
        database.finish_ai_run(provider, run_date, None, "模型运行已中断，请重试", token)
        raise
    except TimeoutError:
        database.finish_ai_run(provider, run_date, None, "模型接口响应超时，请重试", token)
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")
    except Exception as error:
        database.finish_ai_run(provider, run_date, None, f"未预期错误：{error}", token)
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")
    finally:
        ai_token.reset(context_token)


def get_daily_ai_runs(run_date: str | None = None) -> dict[str, Any]:
    run_date = run_date or database.china_date()
    runs: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        if not provider_key(provider):
            runs.append(_empty_run(provider, "not_configured"))
            continue
        existing = _current_run(provider, run_date)
        runs.append(existing or _empty_run(provider, "pending"))
    return {"runDate": run_date, "runs": runs}


async def run_daily_ai(force: bool = False, provider: Provider | None = None, failed_only: bool = False) -> dict[str, Any]:
    global _daily_task
    if provider is not None or failed_only:
        scope = (provider, failed_only)
        task = _scoped_tasks.get(scope)
        if task is None or task.done():
            task = asyncio.create_task(_run_daily_ai(force, provider, failed_only))
            _scoped_tasks[scope] = task
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)
    if _daily_task is None or _daily_task.done():
        work = _run_daily_ai(force) if provider is None and not failed_only else _run_daily_ai(force, provider, failed_only)
        _daily_task = asyncio.create_task(work)
        # Retrieve abandoned errors when a disconnected caller stops awaiting.
        _daily_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
    return await asyncio.shield(_daily_task)


async def _run_daily_ai(force: bool, provider: Provider | None = None, failed_only: bool = False) -> dict[str, Any]:
    run_date = database.china_date()
    current = get_daily_ai_runs(run_date)
    needed = [run for run in current["runs"] if run["status"] in {"pending", "failed"}
              or (force and run["status"] == "succeeded")]
    needed = [run for run in needed if (provider is None or run["provider"] == provider)
              and (not failed_only or run["status"] == "failed")]
    if not needed:
        return current
    try:
        candidates = await asyncio.to_thread(candidate_snapshot, force)
    except Exception as error:
        raise RuntimeError(f"真实行情不足，无法执行今日 AI 选股：{error}") from error
    if len(candidates) < 3:
        raise RuntimeError("真实行情不足，无法执行今日 AI 选股")
    await asyncio.gather(*(_execute_provider(run["provider"], candidates, force, run_date) for run in needed))
    return get_daily_ai_runs(run_date)
