from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .config import CHINA_TZ, DATABASE_PATH


DEFAULT_WATCHLIST = [
    "600519.SH",
    "300750.SZ",
    "601318.SH",
    "000858.SZ",
    "600036.SH",
    "688981.SH",
]

SEED_STOCKS = [
    ("600519.SH", "600519", "贵州茅台", "贵州", "白酒", "主板", "SSE"),
    ("300750.SZ", "300750", "宁德时代", "福建", "电池", "创业板", "SZSE"),
    ("601318.SH", "601318", "中国平安", "深圳", "保险", "主板", "SSE"),
    ("000858.SZ", "000858", "五粮液", "四川", "白酒", "主板", "SZSE"),
    ("600036.SH", "600036", "招商银行", "深圳", "银行", "主板", "SSE"),
    ("688981.SH", "688981", "中芯国际", "上海", "半导体", "科创板", "SSE"),
    ("600900.SH", "600900", "长江电力", "北京", "水力发电", "主板", "SSE"),
    ("601088.SH", "601088", "中国神华", "北京", "煤炭开采", "主板", "SSE"),
    ("000333.SZ", "000333", "美的集团", "广东", "家用电器", "主板", "SZSE"),
    ("600276.SH", "600276", "恒瑞医药", "江苏", "化学制药", "主板", "SSE"),
    ("601899.SH", "601899", "紫金矿业", "福建", "黄金", "主板", "SSE"),
    ("300308.SZ", "300308", "中际旭创", "山东", "通信设备", "创业板", "SZSE"),
]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL UNIQUE,
        position_weight REAL NOT NULL DEFAULT 0,
        added_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS stock_basics (
        ts_code TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        name TEXT NOT NULL,
        area TEXT,
        industry TEXT,
        market TEXT,
        exchange TEXT NOT NULL,
        list_date TEXT,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_stock_basics_name ON stock_basics(name)",
    "CREATE INDEX IF NOT EXISTS idx_stock_basics_symbol ON stock_basics(symbol)",
    """CREATE TABLE IF NOT EXISTS quote_snapshots (
        ts_code TEXT PRIMARY KEY,
        trade_date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL NOT NULL,
        pre_close REAL,
        change REAL,
        pct_chg REAL,
        vol REAL,
        amount REAL,
        turnover_rate REAL,
        volume_ratio REAL,
        amplitude REAL,
        pe_ttm REAL,
        pb REAL,
        total_mv REAL,
        float_mv REAL,
        source TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ai_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        status TEXT NOT NULL,
        result_json TEXT,
        error TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE(run_date, provider)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_runs_status ON ai_runs(status)",
    """CREATE TABLE IF NOT EXISTS strategy_runs (
        run_date TEXT PRIMARY KEY,
        trade_date TEXT NOT NULL,
        result_json TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
]

_write_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(CHINA_TZ).isoformat(timespec="seconds")


def china_date() -> str:
    return datetime.now(CHINA_TZ).strftime("%Y-%m-%d")


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 20000")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize() -> None:
    with _write_lock, connection() as db:
        db.execute("PRAGMA journal_mode = WAL")
        for statement in SCHEMA:
            db.execute(statement)
        quote_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(quote_snapshots)").fetchall()}
        if "volume_ratio" not in quote_columns:
            db.execute("ALTER TABLE quote_snapshots ADD COLUMN volume_ratio REAL")
        timestamp = now_iso()
        db.executemany(
            """INSERT INTO stock_basics
            (ts_code, symbol, name, area, industry, market, exchange, list_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(ts_code) DO UPDATE SET
              name = excluded.name,
              area = COALESCE(stock_basics.area, excluded.area),
              industry = COALESCE(stock_basics.industry, excluded.industry),
              market = COALESCE(stock_basics.market, excluded.market),
              updated_at = excluded.updated_at""",
            [(*row, timestamp) for row in SEED_STOCKS],
        )
        db.executemany(
            "INSERT OR IGNORE INTO watchlist (ts_code, position_weight, added_at) VALUES (?, 0, ?)",
            [(code, timestamp) for code in DEFAULT_WATCHLIST],
        )
        db.execute("PRAGMA optimize")


def get_meta(key: str) -> str | None:
    with connection() as db:
        row = db.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_meta(key: str, value: str) -> None:
    with _write_lock, connection() as db:
        db.execute(
            """INSERT INTO app_meta (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, now_iso()),
        )


def upsert_stock_basics(stocks: list[dict[str, Any]]) -> None:
    if not stocks:
        return
    timestamp = now_iso()
    rows = [
        (
            stock["tsCode"], stock["symbol"], stock["name"], stock.get("area"),
            stock.get("industry"), stock.get("market"), stock["exchange"],
            stock.get("listDate"), timestamp,
        )
        for stock in stocks
    ]
    with _write_lock, connection() as db:
        db.executemany(
            """INSERT INTO stock_basics
            (ts_code, symbol, name, area, industry, market, exchange, list_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts_code) DO UPDATE SET
              symbol = excluded.symbol,
              name = excluded.name,
              area = COALESCE(excluded.area, stock_basics.area),
              industry = COALESCE(excluded.industry, stock_basics.industry),
              market = COALESCE(excluded.market, stock_basics.market),
              exchange = excluded.exchange,
              list_date = COALESCE(excluded.list_date, stock_basics.list_date),
              updated_at = excluded.updated_at""",
            rows,
        )


def upsert_quotes(quotes: list[dict[str, Any]]) -> None:
    if not quotes:
        return
    rows = [
        (
            q["tsCode"], q["tradeDate"], q.get("open"), q.get("high"), q.get("low"),
            q["close"], q.get("preClose"), q.get("change"), q.get("pctChg"), q.get("vol"),
            q.get("amount"), q.get("turnoverRate"), q.get("volumeRatio"), q.get("amplitude"), q.get("peTtm"),
            q.get("pb"), q.get("totalMv"), q.get("floatMv"), q.get("source", "未知"), q["fetchedAt"],
        )
        for q in quotes
    ]
    with _write_lock, connection() as db:
        db.executemany(
            """INSERT INTO quote_snapshots
            (ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol,
             amount, turnover_rate, volume_ratio, amplitude, pe_ttm, pb, total_mv, float_mv, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts_code) DO UPDATE SET
              trade_date = excluded.trade_date, open = excluded.open, high = excluded.high,
              low = excluded.low, close = excluded.close, pre_close = excluded.pre_close,
              change = excluded.change, pct_chg = excluded.pct_chg, vol = excluded.vol,
              amount = excluded.amount, turnover_rate = excluded.turnover_rate,
              volume_ratio = excluded.volume_ratio, amplitude = excluded.amplitude,
              pe_ttm = excluded.pe_ttm, pb = excluded.pb,
              total_mv = excluded.total_mv, float_mv = excluded.float_mv,
              source = excluded.source, fetched_at = excluded.fetched_at""",
            rows,
        )


def list_watch_codes() -> list[str]:
    with connection() as db:
        rows = db.execute("SELECT ts_code FROM watchlist ORDER BY id").fetchall()
    return [str(row["ts_code"]) for row in rows]


def get_watchlist_rows() -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """SELECT b.ts_code AS tsCode, b.symbol, b.name, b.area, b.industry,
            b.market, b.exchange, b.list_date AS listDate, w.position_weight AS positionWeight,
            q.trade_date AS tradeDate, q.open, q.high, q.low, q.close, q.pre_close AS preClose,
            q.change, q.pct_chg AS pctChg, q.vol, q.amount, q.source, q.fetched_at AS fetchedAt
            FROM watchlist w
            JOIN stock_basics b ON b.ts_code = w.ts_code
            LEFT JOIN quote_snapshots q ON q.ts_code = b.ts_code
            ORDER BY w.id"""
        ).fetchall()
    result: list[dict[str, Any]] = []
    quote_keys = {"tradeDate", "open", "high", "low", "close", "preClose", "change", "pctChg", "vol", "amount", "source", "fetchedAt"}
    for row in rows:
        item = dict(row)
        quote = {key: item.pop(key) for key in quote_keys}
        item["quote"] = quote if quote["close"] is not None else None
        result.append(item)
    return result


def search_stocks(query: str, limit: int = 20) -> list[dict[str, Any]]:
    like = f"%{query.strip()}%"
    with connection() as db:
        rows = db.execute(
            """SELECT ts_code AS tsCode, symbol, name, area, industry, market, exchange,
            list_date AS listDate FROM stock_basics
            WHERE symbol LIKE ? OR name LIKE ? OR COALESCE(industry, '') LIKE ?
            ORDER BY CASE WHEN symbol = ? THEN 0 WHEN name = ? THEN 1 ELSE 2 END, symbol
            LIMIT ?""",
            (like, like, like, query.strip(), query.strip(), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def add_watch_stock(ts_code: str) -> None:
    with _write_lock, connection() as db:
        exists = db.execute("SELECT 1 FROM stock_basics WHERE ts_code = ?", (ts_code,)).fetchone()
        if not exists:
            raise ValueError("股票代码不存在，请先搜索后添加")
        db.execute(
            "INSERT OR IGNORE INTO watchlist (ts_code, position_weight, added_at) VALUES (?, 0, ?)",
            (ts_code, now_iso()),
        )


def remove_watch_stock(ts_code: str) -> None:
    with _write_lock, connection() as db:
        db.execute("DELETE FROM watchlist WHERE ts_code = ?", (ts_code,))


def get_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    with connection() as db:
        rows = db.execute(
            f"""SELECT ts_code AS tsCode, trade_date AS tradeDate, open, high, low, close,
            pre_close AS preClose, change, pct_chg AS pctChg, vol, amount, turnover_rate AS turnoverRate,
            volume_ratio AS volumeRatio, amplitude, pe_ttm AS peTtm, pb, total_mv AS totalMv, float_mv AS floatMv,
            source, fetched_at AS fetchedAt FROM quote_snapshots WHERE ts_code IN ({placeholders})""",
            codes,
        ).fetchall()
    return {str(row["tsCode"]): dict(row) for row in rows}


def get_stock_basics(codes: list[str]) -> list[dict[str, Any]]:
    if not codes:
        return []
    placeholders = ",".join("?" for _ in codes)
    with connection() as db:
        rows = db.execute(
            f"""SELECT ts_code AS tsCode, symbol, name, area, industry, market, exchange,
            list_date AS listDate FROM stock_basics WHERE ts_code IN ({placeholders})""",
            codes,
        ).fetchall()
    by_code = {str(row["tsCode"]): dict(row) for row in rows}
    return [by_code[code] for code in codes if code in by_code]


def get_strategy_run(run_date: str) -> list[dict[str, Any]] | None:
    with connection() as db:
        row = db.execute("SELECT result_json FROM strategy_runs WHERE run_date = ?", (run_date,)).fetchone()
    if not row:
        return None
    return json.loads(str(row["result_json"]))


def save_strategy_run(run_date: str, trade_date: str, result: list[dict[str, Any]], source: str) -> None:
    with _write_lock, connection() as db:
        db.execute(
            """INSERT INTO strategy_runs (run_date, trade_date, result_json, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_date) DO UPDATE SET trade_date = excluded.trade_date,
            result_json = excluded.result_json, source = excluded.source, created_at = excluded.created_at""",
            (run_date, trade_date, json.dumps(result, ensure_ascii=False), source, now_iso()),
        )


def read_ai_run(provider: str, run_date: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """SELECT provider, model, status, result_json AS resultJson, error,
            finished_at AS finishedAt FROM ai_runs WHERE run_date = ? AND provider = ?""",
            (run_date, provider),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["result"] = json.loads(result.pop("resultJson")) if result["resultJson"] else None
    return result


def start_ai_run(provider: str, model: str, run_date: str) -> None:
    with _write_lock, connection() as db:
        db.execute(
            """INSERT INTO ai_runs (run_date, provider, model, status, started_at)
            VALUES (?, ?, ?, 'running', ?)
            ON CONFLICT(run_date, provider) DO UPDATE SET model = excluded.model, status = 'running',
            result_json = NULL, error = NULL, started_at = excluded.started_at, finished_at = NULL""",
            (run_date, provider, model, now_iso()),
        )


def finish_ai_run(provider: str, run_date: str, result: dict[str, Any] | None, error: str | None) -> None:
    with _write_lock, connection() as db:
        db.execute(
            """UPDATE ai_runs SET status = ?, result_json = ?, error = ?, finished_at = ?
            WHERE run_date = ? AND provider = ?""",
            (
                "succeeded" if result else "failed",
                json.dumps(result, ensure_ascii=False) if result else None,
                error,
                now_iso(),
                run_date,
                provider,
            ),
        )
