"""On-demand stock research and shared, dated market histories."""
from __future__ import annotations

import json
import threading
import time as clock
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, time
from weakref import WeakValueDictionary

from . import database
from .concept_data import ConceptResearchClient
from .concept_identity import CONCEPT_PROVIDERS, single_provider
from .data_sources import market_data
from .forecast_prices import FeedbackPriceClient
from .history_sources import history_covers_window
from .research_store import cache_get, cache_put
from .research_pool import HISTORY_WAITING_KINDS, fetch_batch, history_result

_locks_guard = threading.Lock()
_locks: WeakValueDictionary = WeakValueDictionary()
_recent_failures: OrderedDict = OrderedDict()


def shared_read(key: str, fetch, seconds: int = 900, *, empty_seconds: float = 0, complete=None):
    def read_cached():
        value = cache_get(key, seconds)
        # Incomplete windows need an early retry when a provider catches up;
        # complete closed-price windows can retain the usual one-hour cache.
        if value is not None and complete is not None and not complete(value):
            value = cache_get(key, min(seconds, 60))
        if value is not None:
            return value
        with _locks_guard:
            failure = _recent_failures.get((str(database.DATABASE_PATH), key))
            if failure and failure[0] > clock.monotonic():
                return deepcopy(failure[1])
        return None

    cached = read_cached()
    if cached is not None:
        return cached
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        cached = read_cached()
        if cached is not None:
            return cached
        value = fetch()
        if value and (not isinstance(value, dict) or "rows" not in value or value["rows"]):
            cache_put(key, value)
        elif empty_seconds and isinstance(value, dict) and "rows" in value:
            # Share one outage response with waiting readers without saving an
            # empty daily series to durable storage or creating a retry stampede.
            with _locks_guard:
                failure_key = (str(database.DATABASE_PATH), key)
                _recent_failures[failure_key] = (clock.monotonic() + empty_seconds, deepcopy(value))
                _recent_failures.move_to_end(failure_key)
                while len(_recent_failures) > 512:
                    _recent_failures.popitem(last=False)
        return value


def stock_history(symbol: str) -> list[dict]:
    # The complete adjusted window comes from one fetch; never splice adjustment bases.
    return shared_read(f"stock-history:{symbol}", lambda: market_data.history(symbol, 740), 900)


def history_request(code: str, start: str, end: str, *, closed: str | None = None) -> tuple[str, str, str]:
    # Recent overlapping reports and both horizons share one closed-price window per symbol.
    closed = closed or last_closed_day()
    recent = (datetime.fromisoformat(closed).date() - timedelta(days=100)).isoformat()
    if start >= recent:
        start, end = recent, closed
    return code, start, end


def index_history(code: str, start: str, end: str) -> dict:
    code, start, end = history_request(code, start, end)
    return shared_read(f"evaluation:{code}:{start}:{end}",
                       lambda: FeedbackPriceClient().history(code, start, end), 3600,
                       empty_seconds=30, complete=lambda value: history_covers_window(
                           value.get("rows", []), start, end, FeedbackPriceClient.known_calendar()))


def stock_basic(symbol: str) -> dict:
    with database.connection() as db:
        basic = db.execute("SELECT ts_code FROM stock_basics WHERE symbol=?", (symbol,)).fetchone()
    if not basic:
        market_data.sync_catalog()
        with database.connection() as db:
            basic = db.execute("SELECT ts_code FROM stock_basics WHERE symbol=?", (symbol,)).fetchone()
        if not basic:
            raise ValueError("未找到该股票的基本资料，日线仍可独立查看")
    return database.get_stock_basics([basic["ts_code"]])[0]


def stock_profile(symbol: str) -> dict:
    stock = stock_basic(symbol)
    with database.connection() as db:
        selections = db.execute("SELECT * FROM selection_reports ORDER BY published_at DESC LIMIT 1000").fetchall()
        forecasts = db.execute("SELECT result_json, published_at FROM forecast_reports ORDER BY id DESC LIMIT 100").fetchall()
    hits = []
    for report in selections:
        pick = next((pick for pick in json.loads(report["picks_json"]) if pick["code"] == symbol), None)
        if pick:
            hits.append({"date": report["run_date"], "publishedAt": report["published_at"],
                         "strategy": report["name"], "reason": pick.get("reason", ""), "score": pick.get("score")})
    from .report_storage import decode_result
    concepts = {}
    for report in forecasts:
        for concept in decode_result(report["result_json"]).get("concepts", []):
            if any(item["code"] == symbol for item in concept.get("stocks", [])):
                concepts.setdefault(concept["code"], {"code": concept["code"], "name": concept["name"],
                                                       "asOf": report["published_at"], "source": "历史概念研究成份股"})
    sectors = cache_get("latest-sectors") or {}
    for board in sectors.get("conceptBoards", []) + sectors.get("researchConcepts", []):
        if any(leader.get("code") == symbol for leader in board.get("leaders", [])):
            concepts[board["code"]] = {"code": board["code"], "name": board["name"],
                                        "asOf": sectors.get("updatedAt"), "source": "板块成份股行情"}
    return {"stock": stock, "quote": database.get_quotes([stock["tsCode"]]).get(stock["tsCode"]),
            "selections": hits[:40], "concepts": list(concepts.values()),
            "conceptScope": "已采集到的概念关联，非完整成份关系；显示关联记录日期"}


def stock_series(symbol: str) -> dict:
    rows = stock_history(symbol)
    return {"rows": rows[-120:], "asOf": database.now_iso(), "adjustment": "前复权",
            "source": "AKShare / BaoStock 日线", "warning": None if rows else "日线暂不可用，请稍后重试"}


def stock_news(symbol: str) -> dict:
    def fetch():
        stock = stock_basic(symbol)
        return {"items": ConceptResearchClient().news(stock["name"], symbol),
                "asOf": database.now_iso()}
    return shared_read(f"stock-news:{symbol}", fetch, 1800)


def last_closed_day(now: datetime | None = None) -> str:
    now = now or datetime.now(database.CHINA_TZ)
    return (now.date() if now.time() >= time(15, 10) else now.date() - timedelta(days=1)).isoformat()


def latest_universe(provider: str | None = None) -> dict | None:
    if provider is not None and provider not in CONCEPT_PROVIDERS:
        raise ValueError("不支持的概念范围来源")
    with database.connection() as db:
        row = db.execute("""SELECT * FROM concept_universes WHERE (? IS NULL OR provider=?)
            ORDER BY as_of DESC, rowid DESC LIMIT 1""", (provider, provider)).fetchone()
    return {"asOf": row["as_of"], "provider": row["provider"], "scope": row["scope"],
            "boards": json.loads(row["boards_json"])} if row else None


def refresh_rotation(progress) -> dict:
    provider = market_data._sector_source
    universe = latest_universe(provider)
    if not universe or universe["asOf"][:10] != database.china_date():
        market_data.sector_overview(False)
        universe = latest_universe(provider)
    if not universe:
        raise RuntimeError("暂无可用概念列表，请先更新板块行情")
    calendar = FeedbackPriceClient().calendar()
    if not calendar or calendar[-1] < last_closed_day():
        raise RuntimeError("交易日历未覆盖最近收盘日期，保留上次轮动结果")
    closed = [day for day in calendar if day <= last_closed_day()]
    if len(closed) < 26:
        raise RuntimeError("交易日历覆盖不足，暂不能计算板块轮动")
    end, start = closed[-1], closed[-26]
    saved = cache_get("rotation") or {"items": []}
    records = {row["code"]: row for row in saved["items"]}
    pending = [board for board in universe["boards"] if records.get(board["code"], {}).get("asOf") != end]
    # Failed concepts rotate to the back on the next batch, allowing healthy sources to progress.
    attempted = saved.get("attempted", {})
    pending.sort(key=lambda board: attempted.get(board["code"], ""))
    progress(0, min(40, len(pending)), "核对概念轮动日线")
    prices = fetch_batch([board["code"] for board in pending[:40]], lambda code: index_history(code, start, end), background=True)
    refreshed, missing, waiting = 0, 0, 0
    attempted_requests = getattr(prices, "attempted", set(prices))
    # Incremental batches keep hundreds of concept histories from blocking other modules.
    for board in pending[:40]:
        if board["code"] not in attempted_requests:
            waiting += 1
            continue
        data = history_result(prices, board["code"])
        attempted[board["code"]] = database.now_iso()
        if data.get("errorKind") in HISTORY_WAITING_KINDS:
            waiting += 1
            continue
        rows = {row["date"]: row for row in data.get("rows", [])}
        expected = closed[-26:]
        if all(day in rows for day in expected):
            refreshed += 1
            closes = [rows[day]["close"] for day in expected]
            returns = {str(n): round((closes[-1] / closes[-n-1] - 1) * 100, 4) for n in (5, 10, 20)}
            previous = {str(n): round((closes[-6] / closes[-n-6] - 1) * 100, 4) for n in (5, 10, 20)}
            records[board["code"]] = {**board, "returns": returns, "previousReturns": previous,
                                       "asOf": end, "path": [{"date": day, "close": rows[day]["close"]} for day in expected]}
        else:
            missing += 1
        progress(refreshed + missing, min(40, len(pending)), "核对概念轮动日线")
    allowed = {board["code"] for board in universe["boards"]}
    updated = sum(row.get('asOf') == end for code, row in records.items() if code in allowed)
    result = {"items": [row for code, row in records.items() if code in allowed], "asOf": end if updated else saved.get('asOf', end),
              "totalCount": len(allowed), "universeAsOf": universe["asOf"], "updatedAt": database.now_iso(),
              "sourceProvider": universe.get("provider") or single_provider(universe["boards"]),
              "scope": universe.get("scope") or "已保存的概念范围，排名仅代表已核对样本",
              "attempted": {code: value for code, value in attempted.items() if code in allowed}}
    cache_put("rotation", result)
    if missing:
        raise RuntimeError(f'本批已核对 {refreshed} 项，仍有 {missing} 项概念日线不完整，另有 {waiting} 项等待读取；已保留此前结果，稍后可继续分批核对')
    if waiting:
        raise RuntimeError(f'本批已核对 {refreshed} 项，仍有 {waiting} 项轮动行情尚未取得查询结果；不计作日线缺失，可继续核对')
    return result


def rotation_payload() -> dict:
    value = cache_get("rotation") or {"items": [], "asOf": None, "totalCount": 0, "updatedAt": None}
    provider = value.get("sourceProvider") or single_provider(value["items"])
    # Rank changes use the same fully observed population on both dates.
    current = [row for row in value["items"] if row["asOf"] == value["asOf"]]
    for days in ("5", "10", "20"):
        today = sorted(current, key=lambda row: (-row["returns"][days], row["code"]))
        before = sorted(current, key=lambda row: (-row["previousReturns"][days], row["code"]))
        ranks = {row["code"]: rank + 1 for rank, row in enumerate(before)}
        for rank, row in enumerate(today):
            row.setdefault("ranks", {})[days] = rank + 1
            row.setdefault("rankChanges", {})[days] = ranks[row["code"]] - rank - 1
    snapshot = cache_get("rotation-snapshot") or {}
    snapshot_provider = snapshot.get("sourceProvider") or single_provider(snapshot.get("boards", []))
    if provider and snapshot_provider and provider != snapshot_provider:
        snapshot = {}  # Another provider's snapshot date does not describe these rows.
    metrics = {row["code"]: row for row in snapshot.get("boards", [])}
    return {**value, "items": [{**row, "snapshot": metrics.get(row["code"])} for row in current],
            "sourceProvider": provider,
            "scope": value.get("scope") or (f"{CONCEPT_PROVIDERS[provider]}已保存概念范围，排名仅代表已核对样本" if provider else None),
            "coveredCount": len(current), "snapshotAsOf": snapshot.get("asOf")}
