"""Realized 15/30-calendar-day returns, using only closed, aligned sessions."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from . import database
from .forecast_history import BENCHMARKS, HORIZONS, REFRESH_KEY, audited_timestamp, reports_to_refresh, refresh_status
from .forecast_prices import CalendarUnavailableError, FeedbackPriceClient
from .research_pool import HISTORY_WAITING_KINDS, fetch_batch, history_result

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
_start: asyncio.Task | None = None
_refresh_lock = threading.Lock()


def bounds(report: dict, days: int) -> tuple[date, date]:
    start = date.fromisoformat(report["result"]["window"]["startDate"])
    return start, start + timedelta(days=days - 1)


def pending_outcome(report: dict, days: int, now: datetime) -> dict:
    start, target = bounds(report, days)
    mature = now >= datetime.combine(target, time(15, 10), database.CHINA_TZ)
    last_closed_day = now.date() if now.time() >= time(15, 10) else now.date() - timedelta(days=1)
    waiting = last_closed_day < start
    return {"horizonDays": days, "targetDate": target.isoformat(),
            "status": "missing_data" if mature else "pending" if now.date() < start else "tracking",
            "dataStatus": "waiting_for_close" if waiting else "awaiting_update",
            "note": (f"观察期从 {start.isoformat()} 开始，等待首个交易日的完整收盘行情" if waiting
                     else "已到期，等待完整收盘行情核对" if mature else "等待核对收盘行情，可点击更新实际表现"),
            "returnPct": None, "entryDate": None, "exitDate": None, "entryPrice": None, "exitPrice": None,
            "maxDrawdownPct": None, "maxRisePct": None, "maxFallPct": None, "benchmarks": {}, "path": []}


def _return(start: float, end: float) -> float:
    return round((end / start - 1) * 100, 4)


def observation_window(report: dict, days: int, calendar: list[str], now: datetime) -> tuple[dict, list[str]]:
    """Share close/publication/calendar rules between predicted and comparison concepts."""
    outcome = pending_outcome(report, days, now)
    start, target = bounds(report, days)
    last_closed_day = now.date() if now.time() >= time(15, 10) else now.date() - timedelta(days=1)
    through = min(target, last_closed_day)
    if through < start:
        return outcome, []
    # Calendar coverage is required; weekdays alone would misclassify holidays.
    if not calendar or calendar[0] > start.isoformat() or calendar[-1] < through.isoformat():
        return {**outcome, "dataStatus": "unavailable", "note": "交易日历覆盖不足，等待核对"}, []
    published = audited_timestamp(report.get("publishedAt"))
    if published is None:
        return {**outcome, "dataStatus": "unavailable", "note": "历史发布时间无效或缺少时区，无法可靠计算"}, []
    # Calendars are sorted by the provider. Only inspect this observation window,
    # rather than scan decades of sessions for every concept and both horizons.
    window = calendar[bisect_left(calendar, start.isoformat()):bisect_right(calendar, through.isoformat())]
    sessions = [day for day in window
                if datetime.combine(date.fromisoformat(day), time(9, 30), database.CHINA_TZ) > published]
    if not sessions:
        return {**outcome, "dataStatus": "waiting_for_close", "note": "预测发布后尚无可用的完整交易日"}, []
    return outcome, sessions


def evaluate(report: dict, days: int, history: dict, benchmarks: dict,
             calendar: list[str], now: datetime) -> dict:
    outcome, sessions = observation_window(report, days, calendar, now)
    if not sessions:
        return outcome
    rows = {row["date"]: row for row in history.get("rows", [])}
    missing = [day for day in sessions if day not in rows]
    if missing:
        reason = history.get("error") or "概念日线缺失或不连续"
        dates = "、".join(missing[:4]) + (f" 等 {len(missing)} 个交易日" if len(missing) > 4 else "")
        return {**outcome, "dataStatus": "unavailable", "missingDates": missing,
                "note": f"{reason}；缺少 {dates}，等待补齐后核对"}
    entry, exit_day = sessions[0], sessions[-1]
    opening, closing = rows[entry]["open"], rows[exit_day]["close"]
    change = _return(opening, closing)
    peak, drawdown = opening, 0.0
    path = []
    for day in sessions:
        row = rows[day]
        peak = max(peak, row["close"])
        drawdown = min(drawdown, _return(peak, row["close"]))
        path.append({"date": day, "open": row["open"], "close": row["close"],
                     "high": row["high"], "low": row["low"], "returnPct": _return(opening, row["close"])})
    paired = {}
    for code, name in BENCHMARKS.items():
        data = benchmarks.get(code, {})
        index_rows = {row["date"]: row for row in data.get("rows", [])}
        ready = entry in index_rows and exit_day in index_rows
        benchmark_return = _return(index_rows[entry]["open"], index_rows[exit_day]["close"]) if ready else None
        paired[code] = {"name": name, "returnPct": benchmark_return,
                        "excessPct": round(change - benchmark_return, 4) if ready else None,
                        "entryPrice": index_rows[entry]["open"] if ready else None,
                        "exitPrice": index_rows[exit_day]["close"] if ready else None,
                        "source": data.get("source"), "url": data.get("url")}
    return {**outcome, "dataStatus": "ready", "status": "completed" if outcome["status"] == "missing_data" else "tracking",
            "note": "已按完整交易日核对" if outcome["status"] == "missing_data" else "截至最近完整收盘，阶段表现不计入统计",
            "entryDate": entry, "exitDate": exit_day, "entryPrice": opening, "exitPrice": closing,
            "returnPct": change, "maxDrawdownPct": drawdown,
            "maxRisePct": _return(opening, max(rows[day]["high"] for day in sessions)),
            "maxFallPct": _return(opening, min(rows[day]["low"] for day in sessions)),
            "benchmarks": paired, "source": history.get("source"), "url": history.get("url"), "path": path}


def claim_refresh() -> str | None:
    with database._write_lock, database.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_meta WHERE key = ?", (REFRESH_KEY,)).fetchone()
        now = datetime.now(database.CHINA_TZ)
        saved = json.loads(row["value"]) if row else {}
        age = (now - datetime.fromisoformat(saved["startedAt"])).total_seconds() if saved.get("startedAt") else 9999
        if age < 60 or (saved.get("status") == "running" and age < 900):
            return None
        token = uuid4().hex
        state = {"status": "running", "token": token, "startedAt": now.isoformat(), "finishedAt": None, "error": None}
        db.execute("INSERT OR REPLACE INTO app_meta VALUES (?, ?, ?)", (REFRESH_KEY, json.dumps(state), now.isoformat()))
    return token


def refresh_feedback(token: str) -> None:
    # A process-local guard also prevents overlap if a stale lease is reclaimed.
    if not _refresh_lock.acquire(blocking=False):
        return
    error, checked, ready, unavailable, waiting = None, 0, 0, 0, 0
    try:
        reports = reports_to_refresh()
        if not reports:
            return
        client = FeedbackPriceClient()
        calendar = client.calendar()
        now = datetime.now(database.CHINA_TZ)
        from .research_data import history_request, index_history, last_closed_day
        closed = last_closed_day(now)
        requests = set()
        for report in reports:
            start, end = bounds(report, 30)
            for code in [*BENCHMARKS, *(c["code"] for c in report["result"]["concepts"])]:
                requests.add(history_request(code, start.isoformat(), min(end, now.date()).isoformat(), closed=closed))
        # Share successful same-symbol reads with comparison/rotation workers.
        prices = fetch_batch(sorted(requests), lambda item: index_history(*item))
        attempted = getattr(prices, "attempted", set(prices))
        if len(prices) < len(requests):
            error = "部分行情请求失败或等待超时，已核对可用数据并保留此前结果，可稍后重试"
        for report in reports:
            start, end = bounds(report, 30)
            span = (start.isoformat(), min(end, now.date()).isoformat())
            indexes = {code: history_result(prices, history_request(code, *span, closed=closed)) for code in BENCHMARKS}
            results = []
            for concept in report["result"]["concepts"]:
                request = history_request(concept["code"], *span, closed=closed)
                if request not in attempted:
                    waiting += len(HORIZONS)
                    continue
                for days in HORIZONS:
                    history = history_result(prices, request)
                    value = evaluate(report, days, history, indexes, calendar, now)
                    if history.get("errorKind"):
                        value["errorKind"] = history["errorKind"]
                    if history.get("errorKind") in HISTORY_WAITING_KINDS:
                        waiting += 1
                        value["note"] = history.get("error") or "尚未取得行情查询结果"
                        value.pop("missingDates", None)
                    elif value.get("dataStatus") == "unavailable":
                        unavailable += 1
                    elif value.get("returnPct") is not None:
                        ready += 1
                        if any(item.get("returnPct") is None for item in value["benchmarks"].values()):
                            if any(data.get("errorKind") in HISTORY_WAITING_KINDS for data in indexes.values()):
                                waiting += 1
                            else:
                                unavailable += 1
                    results.append((concept["code"], days, value))
            if not results:
                continue
            with database._write_lock, database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                state = json.loads(db.execute("SELECT value FROM app_meta WHERE key = ?", (REFRESH_KEY,)).fetchone()["value"])
                if state.get("token") != token:
                    return
                for code, days, value in results:
                    old = db.execute("SELECT result_json FROM forecast_feedback WHERE report_id = ? AND concept_code = ? AND horizon_days = ?",
                                     (report["id"], code, days)).fetchone()
                    saved = json.loads(old["result_json"]) if old else None
                    # A temporary outage must not erase an audited outcome or a
                    # previously available same-date benchmark observation.
                    if saved and saved["status"] == "completed":
                        if value["status"] != "completed":
                            continue
                        if value["entryDate"] != saved["entryDate"] or value["exitDate"] != saved["exitDate"]:
                            continue
                        revised = dict(saved["benchmarks"])
                        for benchmark in BENCHMARKS:
                            current = value["benchmarks"][benchmark]
                            if (revised.get(benchmark, {}).get("returnPct") is None
                                    and current["returnPct"] is not None):
                                revised[benchmark] = {**current, "excessPct": round(saved["returnPct"] - current["returnPct"], 4)}
                        if revised == saved["benchmarks"]:
                            continue
                        value = {**saved, "benchmarks": revised}
                    elif saved and saved.get("returnPct") is not None and value.get("returnPct") is None:
                        # Keep a dated tracking observation during an outage, but
                        # never promote a partial window to a final outcome.
                        value = {**saved, "status": value["status"], "dataStatus": "unavailable",
                                 "note": f"{value['note']}；保留截至 {saved['exitDate']} 的上次行情"}
                    value["checkedAt"] = database.now_iso()
                    db.execute("INSERT OR REPLACE INTO forecast_feedback VALUES (?, ?, ?, ?, ?)",
                               (report["id"], code, days, json.dumps(value, ensure_ascii=False), value["checkedAt"]))
                if any(value.get("errorKind") not in HISTORY_WAITING_KINDS for _, _, value in results):
                    db.execute("UPDATE forecast_reports SET checked_at = ? WHERE id = ?", (database.now_iso(), report["id"]))
                    checked += 1
        if unavailable:
            error = f"本次核对 {ready} 项概念收益，仍有 {unavailable} 项概念或基准日线不完整，另有 {waiting} 项等待读取；已保留此前结果，可稍后重试"
        elif waiting:
            error = f"本次核对 {ready} 项概念收益，仍有 {waiting} 项因来源不可用、排队或等待超时尚未核对；不计作日线缺失，可稍后重试"
    except CalendarUnavailableError as exc:
        error = str(exc)
    except Exception:
        logger.exception("历史预测反馈更新失败")
        error = "行情或交易日历暂不可用，已保留此前结果，可稍后重试"
    finally:
        try:
            with database._write_lock, database.connection() as db:
                row = db.execute("SELECT value FROM app_meta WHERE key = ?", (REFRESH_KEY,)).fetchone()
                state = json.loads(row["value"]) if row else {}
                if state.get("token") == token:
                    state.update(status="failed" if error else "succeeded", finishedAt=database.now_iso(),
                                 error=error, reportsChecked=checked, outcomesReady=ready, outcomesMissing=unavailable,
                                 outcomesWaiting=waiting)
                    db.execute("UPDATE app_meta SET value = ?, updated_at = ? WHERE key = ?",
                               (json.dumps(state, ensure_ascii=False), database.now_iso(), REFRESH_KEY))
        finally:
            _refresh_lock.release()


async def start_feedback_refresh() -> dict:
    global _start

    def finished(task):
        _tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error("forecast_feedback_worker_failed error_type=%s", type(task.exception()).__name__)

    async def dispatch():
        if _refresh_lock.locked() or any(not task.done() for task in _tasks):
            return await asyncio.to_thread(refresh_status)
        if await asyncio.to_thread(reports_to_refresh, limit=1):
            token = await asyncio.to_thread(claim_refresh)
            if token:
                task = asyncio.create_task(asyncio.to_thread(refresh_feedback, token))
                _tasks.add(task)
                task.add_done_callback(finished)
        return await asyncio.to_thread(refresh_status)

    if _start is None or _start.done():
        _start = asyncio.create_task(dispatch())
        _start.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    # Slow SQLite reads cannot block every request; disconnects cannot orphan a claimed job.
    return await asyncio.shield(_start)
