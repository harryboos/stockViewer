"""Hindsight winners, explicitly separate from ex-ante prediction accuracy."""
from __future__ import annotations

import json
from datetime import date, datetime, time

from . import database
from .forecast_feedback import bounds, evaluate
from .forecast_history import get_report
from .forecast_prices import FeedbackPriceClient
from .research_data import fetch_batch, index_history, last_closed_day, latest_universe


def universe_for(report: dict, *, create: bool = False) -> dict | None:
    frozen = report["result"].get("comparisonUniverse")
    if frozen and frozen.get("boards"):
        return frozen
    with database.connection() as db:
        row = db.execute("SELECT * FROM comparison_universes WHERE report_id=?", (report["id"],)).fetchone()
    if row:
        return {"asOf": row["as_of"], "scope": row["scope"], "boards": json.loads(row["boards_json"])}
    if not create:
        return None
    available = latest_universe()
    if not available:
        return None
    scope = "当前概念列表回溯；旧预测未保存当时概念范围，存在范围差异"
    with database._write_lock, database.connection() as db:
        db.execute("INSERT OR IGNORE INTO comparison_universes VALUES (?,?,?,?)",
                   (report["id"], available["asOf"], scope, json.dumps(available["boards"], ensure_ascii=False)))
    return universe_for(report)


def expected_range(report: dict, days: int, calendar: list[str], now: datetime) -> tuple[str, str] | None:
    start, target = bounds(report, days)
    end = min(target.isoformat(), last_closed_day(now))
    if not calendar or calendar[0] > start.isoformat() or calendar[-1] < end:
        return None
    published = datetime.fromisoformat(report["publishedAt"])
    dates = [day for day in calendar if start.isoformat() <= day <= end
             and datetime.combine(date.fromisoformat(day), time(9, 30), database.CHINA_TZ) > published]
    return (dates[0], dates[-1]) if dates else None


def refresh_comparisons(progress) -> dict:
    calendar = FeedbackPriceClient().calendar()
    now = datetime.now(database.CHINA_TZ)
    with database.connection() as db:
        ids = [row[0] for row in db.execute("SELECT id FROM forecast_reports ORDER BY id")]
        saved = {(row["report_id"], row["code"], row["horizon_days"]): (json.loads(row["result_json"]), row["updated_at"])
                 for row in db.execute("SELECT * FROM comparison_outcomes")}
    pending = []
    unavailable = 0
    for report_id in ids:
        report = get_report(report_id)
        universe = universe_for(report, create=True)
        if universe is None:
            unavailable += 1
            continue
        for days in (15, 30):
            span = expected_range(report, days, calendar, now)
            if span is None:
                continue
            mature = now >= datetime.combine(bounds(report, days)[1], time(15, 10), database.CHINA_TZ)
            for position, board in enumerate(universe["boards"]):
                old = saved.get((report_id, board["code"], days))
                value = old[0] if old else {}
                if (value.get("returnPct") is not None and (value.get("entryDate"), value.get("exitDate")) == span
                        and (value.get("status") == "completed" or not mature)):
                    continue
                pending.append((old[1] if old else "", report, board, days, span, position))
    # Give each archive/horizon coverage before exhausting one large pool; selected concepts come first.
    def priority(item):
        checked, report, board, days, _, position = item
        selected = any(row['code'] == board['code'] for row in report['result']['concepts'])
        return (checked, not selected, position, -report['id'], days)
    batch = sorted(pending, key=priority)[:64]
    progress(0, len(batch), "核对同期概念涨幅（每批最多 64 项）")
    requests = list(dict.fromkeys((board["code"], *span) for _, _, board, _, span, _ in batch))
    prices = fetch_batch(requests, lambda item: index_history(*item))
    ready, missing = 0, 0
    for index, (_, report, board, days, span, _) in enumerate(batch):
        history = prices.get((board["code"], *span), {
            "rows": [], "error": "行情请求未完成（等待超时或后台任务繁忙）"})
        value = evaluate(report, days, history, {}, calendar, now)
        if value.get("returnPct") is None:
            missing += 1
        else:
            ready += 1
        # Only retain an auditable compact outcome; selected predictions already retain their paths.
        value = {key: value[key] for key in ("status", "dataStatus", "note", "missingDates", "entryDate", "exitDate", "returnPct", "entryPrice", "exitPrice", "source", "url") if key in value}
        with database._write_lock, database.connection() as db:
            stored = db.execute("SELECT result_json FROM comparison_outcomes WHERE report_id=? AND code=? AND horizon_days=?",
                                (report["id"], board["code"], days)).fetchone()
            old = (json.loads(stored[0]),) if stored else None
            if old and old[0].get("status") == "completed" and old[0].get("returnPct") is not None:
                continue  # Preserve the first audited final prices, including across worker restarts.
            if old and old[0].get("returnPct") is not None and value.get("returnPct") is None:
                value = old[0]  # Its original dates remain visible; it cannot enter another span's ranking.
            db.execute("INSERT OR REPLACE INTO comparison_outcomes VALUES (?,?,?,?,?)",
                       (report["id"], board["code"], days, json.dumps(value, ensure_ascii=False), database.now_iso()))
        progress(index + 1, len(batch), f"已核对 {ready} 项，缺少完整日线 {missing} 项")
    if unavailable and not batch:
        raise RuntimeError("旧预测缺少概念范围，请先更新板块行情，再核对同期最强概念")
    if missing:
        raise RuntimeError(f"本批已核对 {ready}/{len(batch)} 项，{missing} 项概念日线缺失或不连续；已保留可用结果，请检查行情连接后重试")
    return {"checked": len(batch), "remaining": max(0, len(pending) - len(batch)), "missingUniverse": unavailable}


def comparison_payload(report_id: int, days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(database.CHINA_TZ)
    report = get_report(report_id)
    if not report:
        raise ValueError("预测档案不存在")
    universe = universe_for(report)
    base = {"reportId": report_id, "days": days, "strongest": None, "leaders": [], "selected": [],
            "coveredCount": 0, "totalCount": len(universe["boards"]) if universe else 0,
            "scope": universe["scope"] if universe else "等待建立比较范围", "universeAsOf": universe["asOf"] if universe else None,
            "status": "pending", "entryDate": None, "exitDate": None, "averageSelectedReturn": None,
            "missingCount": 0, "staleCount": 0, "lastCheckedAt": None, "message": None}
    if not universe:
        return base
    from .forecast_prices import CALENDAR_KEY
    calendar = json.loads(database.get_meta(CALENDAR_KEY) or "{}").get("dates", [])
    span = expected_range(report, days, calendar, now)
    if not span:
        return base
    with database.connection() as db:
        rows = db.execute("SELECT * FROM comparison_outcomes WHERE report_id=? AND horizon_days=?", (report_id, days)).fetchall()
    pool = {board["code"]: board["name"] for board in universe["boards"]}
    ranked = []
    missing, stale, last_checked, reason = 0, 0, None, None
    for row in rows:
        if row["code"] not in pool:
            continue
        value = json.loads(row["result_json"])
        last_checked = max(last_checked or "", row["updated_at"])
        if value.get("returnPct") is None:
            missing += 1
            reason = reason or value.get("note")
        elif (value["entryDate"], value["exitDate"]) != span:
            stale += 1
        else:
            ranked.append({**value, "code": row["code"], "name": pool[row["code"]], "checkedAt": row["updated_at"]})
    ranked.sort(key=lambda value: (-value["returnPct"], value["code"]))
    strongest = ranked[0] if ranked else None
    selected_codes = {concept["code"] for concept in report["result"]["concepts"]}
    selected = [{"code": value["code"], "name": value["name"], "rank": index + 1, "returnPct": value["returnPct"],
                 "gapPct": round(value["returnPct"] - strongest["returnPct"], 4)}
                for index, value in enumerate(ranked) if value["code"] in selected_codes]
    mature = now >= datetime.combine(bounds(report, days)[1], time(15, 10), database.CHINA_TZ)
    message = None
    if missing:
        message = f"最近核对有 {missing} 个概念缺少完整日线。{reason or '历史行情来源暂不可用，请检查数据与任务中的核对错误。'}"
    elif stale:
        message = f"有 {stale} 个概念仅有较早区间的核对结果，等待补齐至 {span[1]} 后参与本期排名。"
    return {**base, "status": "completed" if mature else "tracking", "entryDate": span[0], "exitDate": span[1],
            "strongest": strongest, "leaders": ranked[:5], "selected": selected, "coveredCount": len(ranked),
            "missingCount": missing, "staleCount": stale, "lastCheckedAt": last_checked, "message": message,
            "selectedCount": len(selected_codes), "fullCoverage": len(ranked) == len(pool),
            "averageSelectedReturn": round(sum(item["returnPct"] for item in selected) / len(selected), 4) if selected else None}
