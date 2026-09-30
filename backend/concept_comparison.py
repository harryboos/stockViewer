"""Hindsight winners, explicitly separate from ex-ante prediction accuracy."""
from __future__ import annotations

import json
from datetime import date, datetime, time

from . import database
from .concept_identity import CONCEPT_PROVIDERS, concept_provider, single_provider
from .forecast_feedback import bounds, evaluate, observation_window
from .forecast_history import get_report
from .forecast_prices import FeedbackPriceClient
from .research_data import fetch_batch, history_request, index_history, last_closed_day, latest_universe
from .research_pool import BatchResult, HISTORY_WAITING_KINDS, history_result


def waiting_for_query(value: dict) -> bool:
    return (value.get("errorKind") in HISTORY_WAITING_KINDS
            or "行情请求未完成（等待超时或后台任务繁忙）" in (value.get("note") or ""))


def source_unavailable_batch(values: dict) -> bool:
    finished = set(values) | set(getattr(values, "errors", {}))
    return len(finished) >= 8 and all(history_result(values, item).get("errorKind") in {"unavailable", "failed"}
                                      for item in finished)


def universe_for(report: dict, *, create: bool = False) -> dict | None:
    concepts = report["result"].get("concepts", [])
    provider = single_provider(concepts)
    frozen = report["result"].get("comparisonUniverse")
    if not concepts:
        # Empty older forecasts predate THS support. New reports record their
        # source explicitly even when the model chooses to wait.
        provider = (report["result"].get("conceptProvider")
                    or single_provider((frozen or {}).get("boards", [])) or "eastmoney")
    if provider not in CONCEPT_PROVIDERS:
        return None

    def matching(universe):
        boards = [board for board in universe.get("boards", []) if concept_provider(board.get("code")) == provider]
        return {**universe, "provider": provider, "boards": boards} if boards else None

    if frozen and frozen.get("boards"):
        return matching(frozen)
    with database.connection() as db:
        row = db.execute("SELECT * FROM comparison_universes WHERE report_id=?", (report["id"],)).fetchone()
    if row:
        return matching({"asOf": row["as_of"], "scope": row["scope"], "boards": json.loads(row["boards_json"])})
    if not create:
        return None
    available = latest_universe(provider)
    if not available:
        return None
    scope = f"{available.get('scope') or CONCEPT_PROVIDERS[provider] + '概念列表'}回溯；旧预测未保存当时概念范围，存在范围差异"
    with database._write_lock, database.connection() as db:
        db.execute("INSERT OR IGNORE INTO comparison_universes VALUES (?,?,?,?)",
                   (report["id"], available["asOf"], scope, json.dumps(available["boards"], ensure_ascii=False)))
    return universe_for(report)


def expected_range(report: dict, days: int, calendar: list[str], now: datetime) -> tuple[str, str] | None:
    _, dates = observation_window(report, days, calendar, now)
    return (dates[0], dates[-1]) if dates else None


def refresh_comparisons(progress) -> dict:
    calendar = FeedbackPriceClient().calendar()
    now = datetime.now(database.CHINA_TZ)
    closed = last_closed_day(now)
    with database.connection() as db:
        ids = [row[0] for row in db.execute("SELECT id FROM forecast_reports ORDER BY id")]
        saved = {(row["report_id"], row["code"], row["horizon_days"]): (json.loads(row["result_json"]), row["updated_at"])
                 for row in db.execute("SELECT * FROM comparison_outcomes")}
    pending = []
    unavailable, invalid_windows = 0, 0
    for report_id in ids:
        report = get_report(report_id)
        universe = universe_for(report, create=True)
        if universe is None:
            unavailable += 1
            continue
        for days in (15, 30):
            window, dates = observation_window(report, days, calendar, now)
            if not dates:
                invalid_windows += int(window["dataStatus"] == "unavailable")
                continue
            span = (dates[0], dates[-1])
            mature = now >= datetime.combine(bounds(report, days)[1], time(15, 10), database.CHINA_TZ)
            for position, board in enumerate(universe["boards"]):
                old = saved.get((report_id, board["code"], days))
                value = old[0] if old else {}
                if value.get("status") == "completed" and value.get("returnPct") is not None:
                    continue
                if (value.get("returnPct") is not None and (value.get("entryDate"), value.get("exitDate")) == span
                        and (value.get("status") == "completed" or not mature)):
                    continue
                pending.append((old[1] if old else "", report, board, days, span, position, waiting_for_query(value)))
    # Selected predictions and previously deferred reads must not sit behind
    # hundreds of never-checked broad-pool concepts on every retry.
    def priority(item):
        checked, report, board, days, _, position, deferred = item
        selected = any(row['code'] == board['code'] for row in report['result']['concepts'])
        return (not selected, not deferred, checked, position, -report['id'], days)
    batch = sorted(pending, key=priority)
    requests = list(dict.fromkeys(history_request(board["code"], *span, closed=closed) for _, _, board, _, span, _, _ in batch))
    progress(0, len(requests), "读取同期概念日线（相同概念共用一次查询）")
    prices = BatchResult()
    finished_requests = 0
    # A discontinued or unavailable BK transport must not trip the breaker for
    # new THS forecasts. Each provider gets an independent bounded batch.
    for provider in ("ths", "eastmoney"):
        group = [item for item in requests if concept_provider(item[0]) == provider]
        if not group:
            continue
        values = fetch_batch(group, lambda item: index_history(*item), background=True,
                             progress=lambda done, total: progress(finished_requests + done, len(requests), "读取同期概念日线"),
                             stop_when=source_unavailable_batch)
        prices.update(values)
        prices.attempted.update(getattr(values, "attempted", set(values)))
        prices.errors.update(getattr(values, "errors", {}))
        prices.paused = prices.paused or getattr(values, "paused", False)
        finished_requests += len(values) + len(getattr(values, "errors", {}))
    attempted = getattr(prices, "attempted", set(prices))
    ready, missing, waiting, checked = 0, 0, 0, 0
    updates = []
    for _, report, board, days, span, _, _ in batch:
        request = history_request(board["code"], *span, closed=closed)
        if request not in attempted:
            waiting += 1
            continue  # No source read occurred; never fabricate a missing-price record.
        history = history_result(prices, request)
        value = evaluate(report, days, history, {}, calendar, now)
        if history.get("errorKind"):
            value["errorKind"] = history["errorKind"]
        if waiting_for_query(value):
            waiting += 1
            value["note"] = history.get("error") or "尚未取得行情查询结果"
            value.pop("missingDates", None)
        elif value.get("returnPct") is None:
            missing += 1
            checked += 1
        else:
            ready += 1
            checked += 1
        # Only retain an auditable compact outcome; selected predictions already retain their paths.
        value = {key: value[key] for key in ("status", "dataStatus", "note", "missingDates", "entryDate", "exitDate", "returnPct", "entryPrice", "exitPrice", "source", "url", "errorKind") if key in value}
        updates.append((report["id"], board["code"], days, value))
    with database._write_lock, database.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for report_id, code, days, value in updates:
            stored = db.execute("SELECT result_json FROM comparison_outcomes WHERE report_id=? AND code=? AND horizon_days=?",
                                (report_id, code, days)).fetchone()
            old = (json.loads(stored[0]),) if stored else None
            if old and old[0].get("status") == "completed" and old[0].get("returnPct") is not None:
                continue  # Preserve the first audited final prices, including across worker restarts.
            if old and old[0].get("returnPct") is not None and value.get("returnPct") is None:
                value = old[0]  # Its original dates remain visible; it cannot enter another span's ranking.
            db.execute("INSERT OR REPLACE INTO comparison_outcomes VALUES (?,?,?,?,?)",
                       (report_id, code, days, json.dumps(value, ensure_ascii=False), database.now_iso()))
    progress(checked, len(batch), f"已核对 {checked} 项，可用 {ready} 项，日线不完整 {missing} 项，等待读取 {waiting} 项")
    if unavailable and not batch:
        raise RuntimeError("旧预测缺少概念范围，请先更新板块行情，再核对同期最强概念")
    if missing:
        raise RuntimeError(f"本批已核对 {ready}/{checked} 项，{missing} 项概念日线缺失或不连续；另有 {waiting} 项等待读取，已保留可用结果，请检查行情连接后重试")
    if waiting:
        reason = ("行情来源连续不可用，已暂停本批后续查询" if getattr(prices, "paused", False) else
                  "行情来源不可用、等待超时或后台占用")
        raise RuntimeError(f"本批已核对 {checked} 项，仍有 {waiting} 项尚未取得查询结果；{reason}，不计作日线缺失，可继续核对")
    if invalid_windows:
        raise RuntimeError(f"已保留可用结果，仍有 {invalid_windows} 个观察窗口因交易日历覆盖不足或发布时间无效而无法核对")
    return {"checked": checked, "remaining": waiting, "missingUniverse": unavailable}


def comparison_payload(report_id: int, days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(database.CHINA_TZ)
    report = get_report(report_id)
    if not report:
        raise ValueError("预测档案不存在")
    universe = universe_for(report)
    base = {"reportId": report_id, "days": days, "strongest": None, "leaders": [], "selected": [],
            "conceptProvider": universe["provider"] if universe else single_provider(report["result"].get("concepts", [])),
            "coveredCount": 0, "totalCount": len(universe["boards"]) if universe else 0,
            "scope": universe["scope"] if universe else "等待建立比较范围", "universeAsOf": universe["asOf"] if universe else None,
            "status": "pending", "entryDate": None, "exitDate": None, "averageSelectedReturn": None,
            "missingCount": 0, "waitingCount": 0, "staleCount": 0, "lastCheckedAt": None, "message": None}
    if not universe:
        return base
    from .forecast_prices import CALENDAR_KEY
    try:
        saved_calendar = json.loads(database.get_meta(CALENDAR_KEY) or "{}")
        dates = saved_calendar.get("dates", []) if isinstance(saved_calendar, dict) else []
        calendar = sorted({date.fromisoformat(day).isoformat() for day in dates})
    except (TypeError, ValueError):
        calendar = []
    window, dates = observation_window(report, days, calendar, now)
    if not dates:
        return {**base, "message": window["note"] if window["dataStatus"] == "unavailable" else None}
    span = (dates[0], dates[-1])
    with database.connection() as db:
        rows = db.execute("SELECT * FROM comparison_outcomes WHERE report_id=? AND horizon_days=?", (report_id, days)).fetchall()
    pool = {board["code"]: board["name"] for board in universe["boards"]}
    ranked = []
    missing, stale, waiting, last_checked, reason, waiting_reason = 0, 0, 0, None, None, None
    observed = set()
    for row in rows:
        if row["code"] not in pool:
            continue
        value = json.loads(row["result_json"])
        observed.add(row["code"])
        last_checked = max(last_checked or "", row["updated_at"])
        if value.get("returnPct") is None and waiting_for_query(value):
            waiting += 1
            if value.get("errorKind") in {"unavailable", "failed"} or not waiting_reason:
                waiting_reason = ("此前请求等待超时或后台繁忙，尚未完成查询" if not value.get("errorKind") else value.get("note"))
        elif value.get("returnPct") is None:
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
    waiting += len(pool) - len(observed)
    message = None
    if missing:
        message = f"最近核对有 {missing} 个概念缺少完整日线。{reason or '历史行情来源暂不可用，请检查数据与任务中的核对错误。'}"
    elif stale:
        message = f"有 {stale} 个概念仅有较早区间的核对结果，等待补齐至 {span[1]} 后参与本期排名。"
    if waiting:
        note = f"有 {waiting} 个概念尚未取得可核对行情。{waiting_reason or '尚未开始查询或仍在等待'}；未计入日线缺失统计，可继续核对。"
        message = f"{message}{note}" if message else note
    return {**base, "status": "completed" if mature else "tracking", "entryDate": span[0], "exitDate": span[1],
            "strongest": strongest, "leaders": ranked[:5], "selected": selected, "coveredCount": len(ranked),
            "missingCount": missing, "waitingCount": waiting, "staleCount": stale, "lastCheckedAt": last_checked, "message": message,
            "selectedCount": len(selected_codes), "fullCoverage": len(ranked) == len(pool),
            "averageSelectedReturn": round(sum(item["returnPct"] for item in selected) / len(selected), 4) if selected else None}
