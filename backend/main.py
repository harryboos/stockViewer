from __future__ import annotations

import asyncio
import importlib.util
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import database
from .ai import get_daily_ai_runs, provider_status, run_daily_ai
from .config import SCHEDULER
from .data_sources import market_data
from .strategies import calculate_public_strategies


class WatchlistInput(BaseModel):
    tsCode: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")


def _watchlist_payload(**extra: object) -> dict:
    return {
        **extra,
        "stocks": database.get_watchlist_rows(),
        "dataSource": market_data.status(),
    }


def _authorize_daily(supplied: str | None) -> None:
    expected = os.getenv("DAILY_RUN_SECRET", "").strip()
    if expected and supplied != expected:
        raise HTTPException(status_code=401, detail="运行密钥不正确")


async def run_daily_bundle() -> None:
    try:
        await asyncio.to_thread(calculate_public_strategies, False)
    except Exception as error:
        database.set_meta("daily_strategy_error", str(error))
    try:
        await run_daily_ai(False)
    except Exception as error:
        database.set_meta("daily_ai_error", str(error))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    database.initialize()
    scheduler: AsyncIOScheduler | None = None
    if SCHEDULER.enabled:
        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
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
    yield
    if scheduler:
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
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Daily-Run-Secret"],
)


@app.get("/api/system")
async def system_status() -> dict:
    packages_ready = bool(importlib.util.find_spec("akshare")) and bool(importlib.util.find_spec("baostock"))
    return {
        "ok": True,
        "database": True,
        "providers": {
            "marketData": packages_ready,
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
async def get_watchlist(refresh: bool = Query(False)) -> dict:
    codes = database.list_watch_codes()
    try:
        await asyncio.to_thread(market_data.refresh_quotes, codes, refresh)
    except Exception as error:
        if not database.get_quotes(codes):
            raise HTTPException(status_code=503, detail=str(error)) from error
    return _watchlist_payload(updatedAt=database.now_iso())


@app.post("/api/watchlist")
async def add_watchlist(payload: WatchlistInput) -> dict:
    try:
        await asyncio.to_thread(database.add_watch_stock, payload.tsCode)
        await asyncio.to_thread(market_data.refresh_quotes, database.list_watch_codes(), False)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _watchlist_payload(ok=True)


@app.delete("/api/watchlist")
async def delete_watchlist(tsCode: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$")) -> dict:
    await asyncio.to_thread(database.remove_watch_stock, tsCode)
    return _watchlist_payload(ok=True)


@app.get("/api/stocks/search")
async def stock_search(q: str = Query("", max_length=30)) -> dict:
    normalized = q.strip()
    if not normalized:
        return {"stocks": []}
    try:
        await asyncio.to_thread(market_data.sync_catalog, False)
    except Exception:
        pass
    return {"stocks": database.search_stocks(normalized)}


@app.get("/api/strategies/public")
async def public_strategies(force: bool = Query(False)) -> dict:
    try:
        return await asyncio.to_thread(calculate_public_strategies, force)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/strategies/ai")
async def ai_strategies() -> dict:
    return get_daily_ai_runs()


@app.post("/api/strategies/ai")
async def execute_ai_strategies(
    force: bool = Query(False),
    x_daily_run_secret: str | None = Header(None),
) -> dict:
    _authorize_daily(x_daily_run_secret)
    try:
        return await run_daily_ai(force)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/daily")
async def execute_daily(x_daily_run_secret: str | None = Header(None)) -> dict:
    _authorize_daily(x_daily_run_secret)
    public_result = await asyncio.to_thread(calculate_public_strategies, False)
    ai_result = await run_daily_ai(False)
    return {"public": public_result, "ai": ai_result}
