"""Bounded discovery before expensive concept analysis, reusing dated local data."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import datetime, timedelta
from itertools import zip_longest

from . import database
from .concept_data import ConceptResearchClient, candidate_view
from .concept_identity import single_provider
from .concept_news import discover_world_news
from .forecast_discovery import build_discovery_pool, select_discovery_seeds
from .forecast_risk import enrich_risk_context
from .research_pool import fetch_batch
from .research_prompt import publication_time

DISCOVERY_HISTORY_LIMIT = 48
DISCOVERY_BUDGET_SECONDS = 55
HISTORY_BUDGET_SECONDS = 24
STOCK_BUDGET_SECONDS = 18


def normalized_history(rows: object, trade_date: str, source: str | None = None, url: str | None = None) -> list[dict]:
    """Trim dates without joining different providers' index bases or fabricating bars."""
    dated, rejected = {}, set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        day = str(row.get("date", "")).replace("-", "")
        try:
            valid = datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") == day
        except ValueError:
            continue
        if not valid or day > trade_date:
            continue
        close = row.get("close")
        if isinstance(close, bool) or not isinstance(close, (float, int)) or not math.isfinite(close) or close <= 0:
            rejected.add(day)
            continue
        if day in dated and dated[day]["close"] != close:
            rejected.add(day)
        dated[day] = {**row, "date": day, "historySource": row.get("historySource") or source,
                      "historyUrl": row.get("historyUrl") or url}
    # A corrupt/conflicting price must not silently disappear and turn a gap
    # into a contiguous technical series. Reject this cache/fetch as a whole.
    return [] if rejected else sorted(dated.values(), key=lambda row: row["date"])[-45:]


def _history_quality(rows: list[dict]) -> tuple:
    amounts = sum(isinstance(row.get("amount"), (int, float)) and not isinstance(row.get("amount"), bool)
                  and math.isfinite(row["amount"]) and row["amount"] > 0 for row in rows[-11:])
    return len(rows) >= 21, amounts, len(rows)


def _cache_snapshots(db, codes: set[str]):
    """Use exact code-prefix index ranges before loading potentially large JSON."""
    ordered = sorted(codes)
    for table, key, payload, stamp, namespace in (
        ("app_meta", "key", "value", "NULL", "concept_history"),
        ("research_cache", "cache_key", "payload_json", "updated_at", "evaluation"),
    ):
        # Keep parameters below SQLite's conservative 999-variable limit.
        for offset in range(0, len(ordered), 200):
            prefixes = [f"{namespace}:{code}:" for code in ordered[offset:offset + 200]]
            bounds = [bound for prefix in prefixes for bound in (prefix, prefix + "\uffff")]
            placeholders = ",".join("(?,?)" for _ in prefixes)
            query = f"""WITH wanted(first,last) AS (VALUES {placeholders})
                SELECT cache.{key} AS key, cache.{payload} AS value, {stamp} AS stored_at
                FROM wanted JOIN {table} AS cache ON cache.{key} >= first AND cache.{key} < last"""
            yield from db.execute(query, bounds)


def cached_histories(codes: set[str], trade_date: str, decision_at: str) -> dict:
    """Read existing caches once. Only already available, aligned snapshots qualify."""
    cutoff, _ = publication_time(decision_at)
    if not codes or cutoff is None:
        return {}
    found = {}
    with database.connection() as db:
        snapshots = [(row["key"], row["value"], row["stored_at"]) for row in _cache_snapshots(db, codes)]
    for key, encoded, stored_at in snapshots:
        match = re.match(r"^(?:concept_history|evaluation):(BK\d+|THS:88\d{4}|sh000001|sh000688):", key)
        if not match or match[1] not in codes:
            continue
        try:
            value = json.loads(encoded)
            if not isinstance(value, dict):
                continue
            observed, _ = publication_time(stored_at or value.get("fetchedAt"))
            if observed is None or cutoff is None or observed > cutoff:
                continue
            if key.startswith("concept_history:") and not value.get("closed"):
                continue
            rows = normalized_history(value.get("rows"), trade_date, value.get("source"), value.get("url"))
            if rows and rows[-1]["date"] == trade_date and _history_quality(rows) > _history_quality(found.get(match[1], [])):
                found[match[1]] = rows
        except (ValueError, TypeError, AttributeError):
            continue
    return found


def attach_risk_context(evidence: dict) -> dict:
    """Run again after targeted news so shared articles also become visible risks."""
    risk = enrich_risk_context(evidence["candidates"], evidence.get("_histories", {}),
                               evidence.get("_benchmarks", {}), evidence["tradeDate"])
    evidence["riskContext"] = risk["riskContext"]
    for candidate in evidence["candidates"]:
        relative = risk["relativeStrengthByCode"].get(candidate["code"])
        candidate["relativeStrength"] = relative
        source_id = f"{candidate['code']}:relative"
        candidate["evidence"] = [row for row in candidate["evidence"] if row["id"] != source_id]
        if relative and any(period.get("status") == "available" for benchmark in relative["benchmarks"]
                            for period in benchmark["periods"].values()):
            urls = relative["source"]["urls"] + [url for benchmark in relative["benchmarks"] for url in benchmark["source"]["urls"]]
            if not urls:
                continue  # A computed metric without a traceable source is not a citable fact.
            candidate["evidence"].append({"id": source_id, "kind": "data", "title": "同区间基准相对强度",
                "source": "概念与基准日线计算；各序列来源见相对强度明细", "url": urls[0],
                "publishedAt": evidence.get("decisionAt") or database.now_iso(),
                "excerpt": "见本概念relativeStrength字段的日期、来源与覆盖说明"})
    return evidence


def collect_forecast_evidence(overview: dict, freshness_note: str | None, key: str | None) -> dict:
    from .research_data import index_history
    deadline = time.monotonic() + DISCOVERY_BUDGET_SECONDS
    trade_date = overview["tradeDate"]
    boards = [row for row in overview["forecastDiscoveryBoards"]
              if not any(marker in row["name"] for marker in ("风格", "小盘股", "做市商"))]
    if not boards or single_provider(boards) is None:
        raise RuntimeError("预测发现范围缺失或混用了不同来源，无法核对概念")
    client = ConceptResearchClient()
    event_sources, news_status = asyncio.run(discover_world_news(boards, key or ""))
    cutoff = database.now_iso()
    cached = cached_histories({row["code"] for row in boards} | {"sh000001", "sh000688"}, trade_date, cutoff)
    seeds = select_discovery_seeds(boards, limit=DISCOVERY_HISTORY_LIMIT, trade_date=trade_date)
    # Event matches get a bounded chance even if they are not on today's leaderboards.
    event_codes = sorted(event_sources, key=lambda code: (-max(
        publication_time(row["publishedAt"])[0].timestamp() for row in event_sources[code]), code))[:8]
    requested = list(dict.fromkeys(code for pair in zip_longest(event_codes, [row["code"] for row in seeds])
                                   for code in pair if code))[:DISCOVERY_HISTORY_LIMIT]
    requests = [code for code in ["sh000001", "sh000688", *requested] if code not in cached]
    end = datetime.strptime(trade_date, "%Y%m%d").date()
    start = (end - timedelta(days=80)).isoformat()

    def history(code):
        if code.startswith("sh"):
            value = index_history(code, start, end.isoformat())
            return normalized_history(value.get("rows"), trade_date, value.get("source"), value.get("url"))
        return normalized_history(client.daily_history(code, trade_date), trade_date)

    fresh = fetch_batch(requests, history, seconds=max(0, min(HISTORY_BUDGET_SECONDS, deadline - time.monotonic())))
    cached.update({code: rows for code, rows in fresh.items() if rows})
    histories = {row["code"]: cached[row["code"]] for row in boards if row["code"] in cached}
    benchmarks = {code: cached[code] for code in ("sh000001", "sh000688") if code in cached}
    relative = enrich_risk_context(boards, histories, benchmarks, trade_date, include_pairs=False)["relativeStrengthByCode"]
    relative_recall = {}
    for code, value in relative.items():
        periods = {item["code"]: item["periods"]["5d"] for item in value["benchmarks"]}
        first, second = periods["sh000001"], periods["sh000688"]
        if first["endDate"] == second["endDate"]:
            relative_recall[code] = {"tradeDate": first["endDate"], "excessShanghai5d": first["excessPctPoints"],
                                     "excessStar505d": second["excessPctPoints"]}
    selected, selection = build_discovery_pool(boards, histories, trade_date, event_sources,
        decision_at=cutoff, relative_strength=relative_recall)
    baseline_codes = selection["baseline"]["dailyTop3Codes"]
    snapshot = {row["code"]: row for row in boards}
    selection["baseline"]["dailyTop3Snapshot"] = [
        {"code": code, "name": snapshot[code]["name"], "tradeDate": trade_date,
         "pctChg": snapshot[code]["pctChg"]} for code in baseline_codes]
    selection["newsDiscovery"] = news_status
    selection["historyRequests"] = {"limit": DISCOVERY_HISTORY_LIMIT, "attempted": len(fresh.attempted),
                                    "returned": len(fresh), "deferred": len(requests) - len(fresh),
                                    "note": "读取预算包含两个指数；超时或未开始不等于不存在历史行情"}
    stock_rows = fetch_batch([row["code"] for row in selected], lambda code: client.strong_stocks(code, trade_date),
                             seconds=max(0, min(STOCK_BUDGET_SECONDS, deadline - time.monotonic())))
    candidates = []
    for board in selected:
        code = board["code"]
        warnings = [] if code in stock_rows else ["本批成份股请求未完成，未补造强势股名单"]
        candidate = candidate_view(board, histories.get(code, []), stock_rows.get(code, []), warnings,
                                   overview=overview, forecast=True)
        candidate.update(discoveryFeatures=board["discoveryFeatures"], eventClues=board.get("eventClues", []))
        candidate["evidence"].extend(event_sources.get(code, []))
        candidates.append(candidate)
    warnings = list(overview.get("warnings", []))
    if freshness_note:
        warnings.append(freshness_note)
    if news_status["status"] != "available":
        warnings.append("候选阶段的事件搜索不完整；已保留可用行情通道，未命中不代表没有催化")
    source_scope = overview.get("scope") or "当前来源可获取的概念行情"
    scope = f"{source_scope}；在 {len(boards)} 个概念快照中多路发现，历史覆盖以实际取得数量为准，最终研究 {len(candidates)} 个；非全市场穷举。"
    return attach_risk_context({"tradeDate": trade_date, "dataAsOf": overview["updatedAt"],
        "quoteAsOf": overview.get("quoteAsOf"), "scope": scope, "warnings": list(dict.fromkeys(warnings)),
        "candidateSelection": selection, "candidates": candidates,
        "comparisonUniverse": {"asOf": overview["updatedAt"], "scope": source_scope,
                               "boards": [{"code": row["code"], "name": row["name"]} for row in boards]},
        "_histories": {row["code"]: histories.get(row["code"], []) for row in selected}, "_benchmarks": benchmarks})
