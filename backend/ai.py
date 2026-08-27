from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import database
from .strategies import candidate_snapshot


Provider = Literal["deepseek", "gemini", "openai"]
PROVIDERS: tuple[Provider, ...] = ("deepseek", "gemini", "openai")
KEY_NAMES: dict[Provider, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
}
DEFAULT_MODELS: dict[Provider, str] = {
    "deepseek": "deepseek-chat",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-5.4-mini",
}
MODEL_KEYS: dict[Provider, str] = {
    "deepseek": "DEEPSEEK_MODEL",
    "gemini": "GEMINI_MODEL",
    "openai": "OPENAI_MODEL",
}
PROMPT_VERSION = "2"
SHARED_SYSTEM_INSTRUCTION = (
    "你是谨慎的 A 股量化研究助手。只根据用户提供的候选快照做横向研究排序，"
    "输出简体中文 JSON；不得杜撰事实，不构成投资建议。"
)


class AiPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=20)
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=4, max_length=160)
    risk: str = Field(min_length=2, max_length=100)


class AiResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=20)
    summary: str = Field(min_length=8, max_length=220)
    logic: str = Field(min_length=4, max_length=100)
    picks: list[AiPick] = Field(min_length=3, max_length=3)


RESULT_SCHEMA = AiResult.model_json_schema()


def read_secret(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def model_for(provider: Provider) -> str:
    return read_secret(MODEL_KEYS[provider]) or DEFAULT_MODELS[provider]


def provider_status() -> dict[str, bool]:
    return {provider: bool(read_secret(KEY_NAMES[provider])) for provider in PROVIDERS}


def _empty_run(provider: Provider, status: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model_for(provider),
        "status": status,
        "result": None,
        "error": None,
        "finishedAt": None,
    }


def _prompt_cache_key(provider: Provider, run_date: str) -> str:
    return f"ai_prompt:{provider}:{run_date}:v{PROMPT_VERSION}"


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
        f"候选快照：{json.dumps(snapshot, ensure_ascii=False)}"
    )


async def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    timeout = httpx.Timeout(90.0, connect=20.0)
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


def _openai_text(payload: dict[str, Any]) -> str | None:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    return None


async def _call_openai(prompt: str, model: str, key: str) -> dict[str, Any]:
    payload = await _post_json(
        "https://api.openai.com/v1/responses",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": model,
            "store": False,
            "input": [
                {"role": "developer", "content": SHARED_SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            "text": {"format": {"type": "json_schema", "name": "a_share_picks", "strict": True, "schema": RESULT_SCHEMA}},
        },
    )
    text = _openai_text(payload)
    if not text:
        raise RuntimeError("OpenAI 未返回可解析结果")
    return json.loads(text)


async def _call_deepseek(prompt: str, model: str, key: str) -> dict[str, Any]:
    payload = await _post_json(
        "https://api.deepseek.com/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SHARED_SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        },
    )
    try:
        return json.loads(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("DeepSeek 未返回可解析结果") from error


async def _call_gemini(prompt: str, model: str, key: str) -> dict[str, Any]:
    payload = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {
            "systemInstruction": {"parts": [{"text": SHARED_SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": RESULT_SCHEMA,
                "temperature": 0.2,
            },
        },
    )
    try:
        text = "".join(part.get("text", "") for part in payload["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Gemini 未返回可解析结果") from error


async def _execute_provider(provider: Provider, candidates: list[dict[str, Any]], force: bool = False) -> dict[str, Any]:
    run_date = database.china_date()
    model = model_for(provider)
    key = read_secret(KEY_NAMES[provider])
    if not key:
        return _empty_run(provider, "not_configured")
    existing = database.read_ai_run(provider, run_date)
    prompt_is_current = database.get_meta(_prompt_cache_key(provider, run_date)) == PROMPT_VERSION
    if existing and prompt_is_current and not force:
        return existing

    database.start_ai_run(provider, model, run_date)
    database.set_meta(_prompt_cache_key(provider, run_date), PROMPT_VERSION)
    try:
        prompt = _build_prompt(candidates)
        if provider == "openai":
            raw = await _call_openai(prompt, model, key)
        elif provider == "deepseek":
            raw = await _call_deepseek(prompt, model, key)
        else:
            raw = await _call_gemini(prompt, model, key)
        result = AiResult.model_validate(raw)
        allowed = {stock["symbol"]: stock["name"] for stock in candidates}
        codes = [pick.code for pick in result.picks]
        if len(set(codes)) != 3:
            raise RuntimeError("模型返回了重复股票")
        for pick in result.picks:
            if pick.code not in allowed:
                raise RuntimeError(f"模型返回了候选池外代码 {pick.code}")
            pick.name = allowed[pick.code]
        saved = result.model_dump()
        database.finish_ai_run(provider, run_date, saved, None)
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")
    except (RuntimeError, ValidationError, json.JSONDecodeError) as error:
        database.finish_ai_run(provider, run_date, None, str(error))
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")
    except Exception as error:
        database.finish_ai_run(provider, run_date, None, f"未预期错误：{error}")
        return database.read_ai_run(provider, run_date) or _empty_run(provider, "failed")


def get_daily_ai_runs() -> dict[str, Any]:
    run_date = database.china_date()
    runs: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        if not read_secret(KEY_NAMES[provider]):
            runs.append(_empty_run(provider, "not_configured"))
            continue
        existing = database.read_ai_run(provider, run_date)
        prompt_is_current = database.get_meta(_prompt_cache_key(provider, run_date)) == PROMPT_VERSION
        runs.append(existing if existing and prompt_is_current else _empty_run(provider, "pending"))
    return {"runDate": run_date, "runs": runs}


async def run_daily_ai(force: bool = False) -> dict[str, Any]:
    try:
        candidates = await asyncio.to_thread(candidate_snapshot, force)
    except Exception as error:
        raise RuntimeError(f"真实行情不足，无法执行今日 AI 选股：{error}") from error
    if len(candidates) < 3:
        raise RuntimeError("真实行情不足，无法执行今日 AI 选股")
    runs = await asyncio.gather(*(_execute_provider(provider, candidates, force) for provider in PROVIDERS))
    return {"runDate": database.china_date(), "runs": list(runs)}
