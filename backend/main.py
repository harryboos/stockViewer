from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import database
from .ai import get_daily_ai_runs, provider_status, run_daily_ai
from .config import SCHEDULER
from .concept_ai import get_concept_run, start_concept_run
from .concept_forecast import get_forecast_run, start_forecast_run
from .forecast_feedback import start_feedback_refresh
from .forecast_history import get_report, history_payload
from .data_sources import market_data
from .strategies import calculate_public_strategies
from . import research_jobs, research_data, research_store, selection_history, concept_comparison

logger = logging.getLogger(__name__)


class WatchlistInput(BaseModel):
    tsCode: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")


class WatchNoteInput(WatchlistInput):
    groupName: str = Field(default="未分组", max_length=40)
    reason: str = Field(default="", max_length=1000)
    note: str = Field(default="", max_length=4000)


def _watchlist_payload(**extra: object) -> dict:
    return {
        **extra,
        "stocks": database.get_watchlist_rows(),
        "dataSource": market_data.status(),
    }


def _authorize_daily(supplied: str | None) -> None:
    expected = os.getenv("DAILY_RUN_SECRET", "").strip()
    if expected and not secrets.compare_digest((supplied or "").encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="运行密钥不正确")


async def run_daily_bundle() -> None:
    try:
        await asyncio.to_thread(calculate_public_strategies, False)
    except Exception:
        logger.exception("每日规则策略运行失败")
    if os.getenv("ENABLE_SCHEDULED_AI", "").lower() == "true":
        try:
            await run_daily_ai(False)
        except Exception:
            logger.exception("每日 AI 选股运行失败")


async def maintain_storage() -> None:
    try:
        await asyncio.to_thread(database.maintain_storage)
    except Exception:
        logger.exception("数据库定期清理失败")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    database.initialize()
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(maintain_storage, trigger="cron", hour=4, minute=0,
                      id="storage-maintenance", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(start_feedback_refresh, trigger="cron", hour="16-22", minute=20,
                      id="forecast-feedback", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(research_jobs.scheduled_research, trigger="cron", hour="16-22", minute="5,20,35,50",
                      id="research-feedback", replace_existing=True, max_instances=1, coalesce=True)
    if SCHEDULER.enabled:
        scheduler.add_job(
            run_daily_bundle,
            trigger="cron",
            day_of_week="mon-fri",
            hour=SCHEDULER.hour,
            minute=SCHEDULER.minute,
            id="daily-strategies",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    await start_feedback_refresh()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="观星 A股本地数据服务",
    description="AKShare + BaoStock 免费行情、SQLite 持久化与每日策略。",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Daily-Run-Secret"],
)


@app.get("/api/system")
def system_status() -> dict:
    with database.connection() as db:
        has_quotes = bool(db.execute("SELECT 1 FROM quote_snapshots LIMIT 1").fetchone())
    return {
        "ok": True,
        "database": True,
        "providers": {
            "marketData": has_quotes,
            **provider_status(),
        },
        "dataSource": market_data.status(),
        "scheduler": {
            "enabled": SCHEDULER.enabled,
            "time": f"{SCHEDULER.hour:02d}:{SCHEDULER.minute:02d}",
            "timezone": "Asia/Shanghai",
        },
    }


@app.get("/api/watchlist")
def get_watchlist(refresh: bool = Query(False), cached_only: bool = Query(False)) -> dict:
    if cached_only:
        return _watchlist_payload(updatedAt=database.now_iso())
    codes = database.list_watch_codes()
    try:
        market_data.refresh_quotes(codes, refresh)
    except Exception as error:
        if not database.get_quotes(codes):
            raise HTTPException(status_code=503, detail=str(error)) from error
    return _watchlist_payload(updatedAt=database.now_iso())


@app.post("/api/watchlist")
def add_watchlist(payload: WatchlistInput) -> dict:
    try:
        inserted = database.add_watch_stock(payload.tsCode)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        market_data.refresh_quotes([payload.tsCode], False)
        if inserted:
            research_store.capture_reference(payload.tsCode)
    except Exception as error:
        # Adding a stock has already committed; unavailable quotes do not undo it.
        database.set_meta("market_data_error", str(error))
    return _watchlist_payload(ok=True)


@app.delete("/api/watchlist")
def delete_watchlist(tsCode: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$")) -> dict:
    database.remove_watch_stock(tsCode)
    return _watchlist_payload(ok=True)


@app.patch("/api/watchlist")
def update_watch_note(payload: WatchNoteInput) -> dict:
    try:
        research_store.update_watch(payload.tsCode, payload.groupName, payload.reason, payload.note)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _watchlist_payload(ok=True)


@app.get("/api/stocks/search")
def stock_search(q: str = Query("", max_length=30)) -> dict:
    normalized = q.strip()
    if not normalized:
        return {"stocks": []}
    try:
        market_data.sync_catalog(False)
    except Exception:
        pass
    return {"stocks": database.search_stocks(normalized)}


@app.get("/api/market/overview")
async def market_overview(force: bool = Query(False)) -> dict:
    try:
        return await asyncio.to_thread(market_data.market_overview, force)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/market/sectors")
async def sector_overview(force: bool = Query(False)) -> dict:
    try:
        return await asyncio.to_thread(market_data.sector_overview, force)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/strategies/public")
async def public_strategies(force: bool = Query(False)) -> dict:
    try:
        return await asyncio.to_thread(calculate_public_strategies, force)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/strategies/ai")
def ai_strategies() -> dict:
    return get_daily_ai_runs()


@app.get("/api/market/concepts/ai")
def concept_recommendations() -> dict:
    return get_concept_run()


@app.post("/api/market/concepts/ai")
async def generate_concept_recommendations(
    force: bool = Query(False),
    x_daily_run_secret: str | None = Header(None),
) -> dict:
    _authorize_daily(x_daily_run_secret)
    return await start_concept_run(force)


@app.get("/api/forecast/concepts")
def concept_forecast() -> dict:
    return get_forecast_run()


@app.post("/api/forecast/concepts")
async def generate_concept_forecast(
    force: bool = Query(False),
    x_daily_run_secret: str | None = Header(None),
) -> dict:
    _authorize_daily(x_daily_run_secret)
    return await start_forecast_run(force)


@app.get("/api/forecast/history")
def forecast_history(page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=30)) -> dict:
    return history_payload(page, page_size)


@app.get("/api/forecast/history/report")
def forecast_history_report(id: int = Query(..., ge=1)) -> dict:
    report = get_report(id)
    if report is None:
        raise HTTPException(status_code=404, detail="历史预测不存在")
    return report


@app.post("/api/forecast/history")
async def update_forecast_feedback(x_daily_run_secret: str | None = Header(None)) -> dict:
    _authorize_daily(x_daily_run_secret)
    return await start_feedback_refresh()


@app.post("/api/strategies/ai")
async def execute_ai_strategies(
    force: bool = Query(False),
    provider: Literal["glm", "deepseek", "qwen"] | None = Query(None),
    failed_only: bool = Query(False),
    x_daily_run_secret: str | None = Header(None),
) -> dict:
    _authorize_daily(x_daily_run_secret)
    try:
        return await run_daily_ai(force, provider, failed_only)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/daily")
async def execute_daily(x_daily_run_secret: str | None = Header(None)) -> dict:
    _authorize_daily(x_daily_run_secret)
    public_result = await asyncio.to_thread(calculate_public_strategies, False)
    ai_result = await run_daily_ai(False)
    return {"public": public_result, "ai": ai_result}


@app.get("/api/research/stock")
def stock_research(code: str = Query(..., pattern=r"^\d{6}$"),
                   part: Literal["profile", "history", "news"] = "profile") -> dict:
    try:
        return {"profile": research_data.stock_profile, "history": research_data.stock_series,
                "news": research_data.stock_news}[part](code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="个股资料暂不可用，已加载的其他资料仍可查看") from exc


@app.get("/api/research/operations")
def operations() -> dict:
    return research_jobs.operations_payload()


@app.post("/api/research/jobs")
async def research_job(key: Literal["rules", "selections", "rotation", "comparison", "market", "sectors"],
                       x_daily_run_secret: str | None = Header(None)) -> dict:
    _authorize_daily(x_daily_run_secret)
    return await research_jobs.start_job(key)


@app.get("/api/research/jobs")
def research_job_status(key: Literal["rules", "selections", "rotation", "comparison", "market", "sectors"]) -> dict:
    return research_jobs.job_status(key)


@app.get("/api/research/rotation")
def rotation() -> dict:
    return research_data.rotation_payload()


@app.get("/api/research/selections")
def selections(sessions: Literal['5', '10', '20'] = '10', page: int = Query(1, ge=1),
               strategy: str = Query("", max_length=80)) -> dict:
    return selection_history.history_payload(int(sessions), page, strategy)


@app.get("/api/research/comparison")
def comparison(report_id: int = Query(..., ge=1), days: Literal['15', '30'] = '15') -> dict:
    try:
        return concept_comparison.comparison_payload(report_id, int(days))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/research/digest")
def digest() -> dict:
    return research_jobs.daily_digest()
