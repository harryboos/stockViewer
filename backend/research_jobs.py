"""Explicit background jobs and read-only operational status for the whole application."""
from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta
from uuid import uuid4

from . import database
from .research_store import cache_get

logger = logging.getLogger(__name__)
LABELS = {"rules": "规则策略计算", "selections": "策略历史成绩", "rotation": "板块轮动",
          "comparison": "同期最强概念核对", "market": "大盘行情更新", "sectors": "板块行情更新"}
_tasks: dict[str, asyncio.Task] = {}
_starts: dict[str, asyncio.Task] = {}
ai_token: ContextVar[str | None] = ContextVar("research_ai_token", default=None)


def job_status(key: str) -> dict:
    with database.connection() as db:
        row = db.execute("SELECT * FROM research_jobs WHERE job_key=?", (key,)).fetchone()
    if not row:
        return {"key": key, "name": LABELS[key], "status": "idle", "progress": {}, "error": None}
    value = {"key": key, "name": LABELS[key], "status": row["status"], "startedAt": row["started_at"],
             "finishedAt": row["finished_at"], "progress": json.loads(row["progress_json"] or "{}"), "error": row["error"]}
    if value["status"] == "running" and (datetime.now(database.CHINA_TZ) - datetime.fromisoformat(row["started_at"])).total_seconds() > 1200:
        value.update(status="failed", error="上次任务已中断，可重试")
    return value


def _claim(key: str) -> str | None:
    with database._write_lock, database.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM research_jobs WHERE job_key=?", (key,)).fetchone()
        if row and row["status"] == "running" and row["started_at"] > (datetime.now(database.CHINA_TZ) - timedelta(minutes=20)).isoformat():
            return None
        token = uuid4().hex
        db.execute("INSERT OR REPLACE INTO research_jobs VALUES (?,?, 'running', ?,NULL,'{}',NULL)",
                   (key, token, database.now_iso()))
        return token


def _run(key: str, token: str) -> None:
    error = None
    def progress(done: int, total: int, stage: str):
        with database._write_lock, database.connection() as db:
            db.execute("UPDATE research_jobs SET progress_json=? WHERE job_key=? AND token=?",
                       (json.dumps({"done": done, "total": total, "stage": stage}, ensure_ascii=False), key, token))
    try:
        from .data_sources import market_data
        from .strategies import calculate_public_strategies
        from .selection_history import refresh_selections
        from .research_data import refresh_rotation
        from .concept_comparison import refresh_comparisons
        progress(0, 1, LABELS[key])
        if key == "rules":
            calculate_public_strategies(True)
            progress(1, 1, "规则策略已完成")
        elif key == "market":
            market_data.market_overview(True)
            progress(1, 1, "大盘行情已更新")
        elif key == "sectors":
            market_data.sector_overview(True)
            progress(1, 1, "板块行情已更新")
        else:
            {"selections": refresh_selections, "rotation": refresh_rotation, "comparison": refresh_comparisons}[key](progress)
    except Exception as exc:
        logger.warning("research_job_failed key=%s error_type=%s", key, type(exc).__name__)
        error = str(exc) if isinstance(exc, RuntimeError) else "数据核对失败，已有结果已保留，可稍后重试"
    finally:
        with database._write_lock, database.connection() as db:
            db.execute("UPDATE research_jobs SET status=?,finished_at=?,error=? WHERE job_key=? AND token=?",
                       ("failed" if error else "succeeded", database.now_iso(), error, key, token))


async def start_job(key: str) -> dict:
    if key not in LABELS:
        raise ValueError("不支持的任务")
    async def dispatch():
        if key in _tasks and not _tasks[key].done():
            return await asyncio.to_thread(job_status, key)
        token = await asyncio.to_thread(_claim, key)
        if token:
            worker = asyncio.create_task(asyncio.to_thread(_run, key, token))
            _tasks[key] = worker
            worker.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.to_thread(job_status, key)

    if key not in _starts or _starts[key].done():
        _starts[key] = asyncio.create_task(dispatch())
        _starts[key].add_done_callback(lambda done: None if done.cancelled() else done.exception())
    # A disconnected HTTP request cannot cancel the durable-claim/worker handoff.
    return await asyncio.shield(_starts[key])


async def scheduled_research() -> None:
    for key in ("selections", "rotation", "comparison"):
        await start_job(key)


def record_ai_stage(token: str, stage: str, *, call: bool = False) -> None:
    with database._write_lock, database.connection() as db:
        db.execute("UPDATE ai_attempts SET stage=?,calls=calls+? WHERE token=? AND status='running'",
                   (stage, int(call), token))


def operations_payload() -> dict:
    from .ai import get_daily_ai_runs
    from .concept_ai import get_concept_run
    from .concept_forecast import get_forecast_run
    from .data_sources import market_data
    from .config import SCHEDULER
    now = datetime.now(database.CHINA_TZ)
    day = database.china_date()
    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    with database.connection() as db:
        quote = db.execute("SELECT MAX(fetched_at) AS fetched,MAX(trade_date) AS day,COUNT(*) AS count FROM quote_snapshots").fetchone()
        attempts = [dict(row) for row in db.execute("SELECT * FROM ai_attempts WHERE started_at>=? AND started_at<? ORDER BY started_at DESC", (day, next_day))]
    for attempt in attempts:
        if attempt['status'] == 'running' and attempt['started_at'] <= database._ai_lease_cutoff(attempt['provider']):
            attempt.update(status='failed', stage='已中断或超时')
    sources = []
    def source(key: str, name: str, updated: str | None, error: str | None, trade_date: str | None):
        age = (now - datetime.fromisoformat(updated)).total_seconds() if updated else None
        state = "unavailable" if not updated else "degraded" if error else "fresh" if age is not None and age <= 900 else "cached"
        sources.append({"key": key, "name": name, "updatedAt": updated, "tradeDate": trade_date, "state": state, "error": error})
    source("quotes", "个股行情", quote["fetched"], market_data.status().get("error"), quote["day"])
    for key, name in (("latest-market", "大盘观察"), ("latest-sectors", "板块概念"), ("rotation", "板块轮动")):
        cached = cache_get(key) or {}
        source(key, name, cached.get("updatedAt"), "；".join(cached.get("warnings", [])) or None,
               cached.get("tradeDate") or cached.get("asOf"))
    runs = get_daily_ai_runs(include_result=False)["runs"] + [
        {**get_concept_run(include_result=False), "provider": "concept:glm"},
        {**get_forecast_run(include_result=False), "provider": "forecast:glm"}]
    stage_by_provider = {}
    for attempt in attempts:
        stage_by_provider.setdefault(attempt["provider"], attempt["stage"])
    next_rules = now.replace(hour=SCHEDULER.hour, minute=SCHEDULER.minute, second=0, microsecond=0)
    while next_rules <= now or next_rules.weekday() >= 5:
        next_rules += timedelta(days=1)
    return {"sources": sources, "jobs": [job_status(key) for key in LABELS],
            "aiRuns": [{**{key: value for key, value in run.items() if key not in ('result', 'previousResult')},
                        "stage": stage_by_provider.get(run["provider"])} for run in runs],
            "aiUsage": {"runsToday": len(attempts), "requestsToday": sum(row["calls"] for row in attempts),
                        "failedToday": sum(row["status"] == "failed" for row in attempts),
                        "note": "仅统计启用记录后的生成任务与分析请求次数，不含新闻检索；非账单金额"},
            "nextRulesAt": next_rules.isoformat() if SCHEDULER.enabled else None,
            "researchSchedule": "每日 16:05—22:50，每 15 分钟分批核对；不调用 AI", "asOf": database.now_iso()}


def daily_digest() -> dict:
    from .forecast_history import history_payload
    from .selection_history import recent_entries
    watches = database.get_watchlist_rows()
    market = cache_get("latest-market")
    sectors = cache_get("latest-sectors")
    forecasts = history_payload(page_size=1)
    latest_quote_day = max((stock["quote"]["tradeDate"] for stock in watches if stock.get("quote")), default=None)
    movers = sorted((stock for stock in watches if stock.get("quote") and stock["quote"]["tradeDate"] == latest_quote_day
                     and stock["quote"].get("pctChg") is not None), key=lambda stock: abs(stock["quote"]["pctChg"]), reverse=True)[:5]
    return {"asOf": database.now_iso(), "quoteDate": latest_quote_day, "watchMovers": movers,
            "market": {key: market[key] for key in ("tradeDate", "updatedAt", "snapshot", "warnings")} if market else None,
            "strongBoards": (sectors or {}).get("conceptBoards", [])[:3], "sectorAsOf": (sectors or {}).get("updatedAt"),
            "sectorTradeDate": (sectors or {}).get("tradeDate"),
            "recentSelections": recent_entries(), "forecastSummaries": forecasts["summaries"],
            "note": "依据已取得的数据自动汇总，不额外调用 AI；涨跌表现不等于预测依据已被证实"}
