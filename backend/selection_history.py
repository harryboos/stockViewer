"""Forward-only evaluation of the first saved daily selection, never reconstructed picks."""
from __future__ import annotations

import json
from datetime import datetime, date, time

from . import database
from .forecast_feedback import evaluate
from .forecast_history import BENCHMARKS, summarize
from .forecast_prices import FeedbackPriceClient, normalize_bars
from .research_data import stock_history, index_history, last_closed_day, fetch_batch

HORIZONS = (5, 10, 20)


def recent_entries(limit: int = 5) -> list[dict]:
    """Digest cards need recent names and picks, not every strategy's lifetime statistics."""
    with database.connection() as db:
        reports = db.execute("SELECT * FROM selection_reports ORDER BY published_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": row["id"], "runDate": row["run_date"], "strategyKey": row["strategy_key"],
             "name": row["name"], "publishedAt": row["published_at"], "origin": row["origin"],
             "picks": [{"code": pick["code"], "name": pick["name"]} for pick in json.loads(row["picks_json"])]}
            for row in reports]


def selection_outcome(report: dict, sessions: int, rows: list, indexes: dict,
                      calendar: list[str], now: datetime) -> dict:
    published = datetime.fromisoformat(report["published_at"])
    eligible = [day for day in calendar
                if day >= published.date().isoformat()
                and datetime.combine(date.fromisoformat(day), time(9, 30), database.CHINA_TZ) > published]
    empty = {"status": "pending", "returnPct": None, "maxDrawdownPct": None, "benchmarks": {},
             "entryDate": None, "exitDate": None, "note": "等待发布后交易日行情", "horizonSessions": sessions}
    if len(eligible) < sessions:
        return {**empty, "status": "missing_data", "note": "交易日历覆盖不足，无法核定观察窗口"}
    if calendar[0] > published.date().isoformat():
        return {**empty, "status": "missing_data", "note": "交易日历未覆盖发布时间，无法核定首个交易日"}
    start, target = eligible[0], eligible[sessions - 1]
    span = (date.fromisoformat(target) - date.fromisoformat(start)).days + 1
    synthetic = {"publishedAt": report["published_at"], "result": {"window": {"startDate": start}}}
    converted = []
    for row in rows:
        day = str(row["date"])
        if len(day) == 8:
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        converted.append([day, row.get("open"), row.get("close"), row.get("high"), row.get("low")])
    history = {"rows": normalize_bars(converted, start, target), "source": "AKShare / BaoStock 前复权日线"}
    outcome = evaluate(synthetic, span, history, indexes, calendar, now)
    outcome.pop("path", None)
    return {**outcome, "horizonSessions": sessions, "priceBasis": "前复权价格表现，不计交易成本"}


def refresh_selections(progress) -> dict:
    client = FeedbackPriceClient()
    calendar = client.calendar()
    now = datetime.now(database.CHINA_TZ)
    with database.connection() as db:
        reports = [dict(row) for row in db.execute("SELECT * FROM selection_reports ORDER BY id")]
        saved = {(row["report_id"], row["code"], row["sessions"]): (json.loads(row["result_json"]), row["updated_at"])
                 for row in db.execute("SELECT * FROM selection_outcomes")}
    pending = []
    for report in reports:
        if report["published_at"][:10] > last_closed_day(now):
            continue
        for pick in json.loads(report["picks_json"]):
            old = [saved.get((report["id"], pick["code"], n)) for n in HORIZONS]
            def ready(item):
                return item and item[0]["status"] == "completed" and all(
                    item[0].get("benchmarks", {}).get(code, {}).get("returnPct") is not None for code in BENCHMARKS)
            if all(ready(item) for item in old):
                continue
            if all(item and item[1][:10] == database.china_date() and item[1][11:16] >= "15:10"
                   and item[0].get("dataStatus") == "ready" and all(
                       item[0].get("benchmarks", {}).get(code, {}).get("returnPct") is not None for code in BENCHMARKS)
                   for item in old):
                continue
            pending.append((min((item[1] if item else "") for item in old), report, pick))
    batch = sorted(pending, key=lambda item: (item[0], item[1]["id"]))[:24]
    progress(0, len(batch), "核对选股后的真实日线")
    histories = fetch_batch(list({pick["code"] for _, _, pick in batch}), stock_history)
    end = last_closed_day(now)
    index_requests = list({(code, report["published_at"][:10], end) for _, report, _ in batch for code in BENCHMARKS})
    index_prices = fetch_batch(index_requests, lambda item: index_history(*item))
    for index, (_, report, pick) in enumerate(batch):
        start = report["published_at"][:10]
        indexes = {code: index_prices.get((code, start, end), {"rows": []}) for code in BENCHMARKS}
        for n in HORIZONS:
            value = selection_outcome(report, n, histories.get(pick["code"], []), indexes, calendar, now)
            old = saved.get((report["id"], pick["code"], n))
            if old and old[0].get("returnPct") is not None:
                if value.get("returnPct") is None:
                    value = {**old[0], "dataStatus": "unavailable", "note": "行情暂不可用，保留上次核对日期与价格"}
                elif old[0]["status"] == "completed":
                    original = old[0]
                    if value["entryDate"] == original["entryDate"] and value["exitDate"] == original["exitDate"]:
                        paired = dict(original.get("benchmarks", {}))
                        for code in BENCHMARKS:
                            update = value.get("benchmarks", {}).get(code, {})
                            if paired.get(code, {}).get("returnPct") is None and update.get("returnPct") is not None:
                                paired[code] = {**update, "excessPct": round(original["returnPct"] - update["returnPct"], 4)}
                        value = {**original, "benchmarks": paired}
                    else:
                        value = original
            with database._write_lock, database.connection() as db:
                db.execute("INSERT OR REPLACE INTO selection_outcomes VALUES (?,?,?,?,?)",
                           (report["id"], pick["code"], n, json.dumps(value, ensure_ascii=False), database.now_iso()))
        progress(index + 1, len(batch), "核对选股后的真实日线")
    return {"checked": len(batch), "remaining": max(0, len(pending) - len(batch))}


def history_payload(sessions: int = 10, page: int = 1, strategy: str = "") -> dict:
    with database.connection() as db:
        reports = [dict(row) for row in db.execute("SELECT * FROM selection_reports ORDER BY published_at DESC,id DESC")]
        saved = {(row["report_id"], row["code"]): json.loads(row["result_json"])
                 for row in db.execute("SELECT * FROM selection_outcomes WHERE sessions=?", (sessions,))}
    groups, entries = {}, []
    for report in reports:
        key = report["strategy_key"]
        group = groups.setdefault(key, {"key": key, "name": report["name"], "outcomes": [], "days": 0})
        group["days"] += 1
        picks = []
        for pick in json.loads(report["picks_json"]):
            item = saved.get((report["id"], pick["code"])) or {
                "status": "pending", "returnPct": None, "benchmarks": {}, "note": "等待核对行情"}
            group["outcomes"].append(item)
            picks.append({**pick, "outcome": item})
        if not strategy or strategy == key:
            entries.append({"id": report["id"], "runDate": report["run_date"], "strategyKey": key,
                            "name": report["name"], "publishedAt": report["published_at"], "picks": picks,
                            "origin": report['origin']})
    summaries = [{"key": key, "name": group["name"], "days": group["days"], **summarize(group["outcomes"])}
                 for key, group in groups.items()]
    return {"summaries": summaries, "entries": entries[(page - 1) * 10:page * 10], "total": len(entries),
            "page": page, "pageSize": 10, "sessions": sessions, "asOf": database.now_iso()}
