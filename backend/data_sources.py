from __future__ import annotations

import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Iterator

from . import database
from .config import MARKET
from .eastmoney import EastmoneyClient
from .tencent import TencentClient


CATALOG_CACHE_SECONDS = 24 * 60 * 60
CONCEPT_CACHE_VERSION = "3"
MARKET_TURNOVER_CACHE_KEY = "market_turnover:v1"
MARKET_TURNOVER_COMPAT_KEYS = ("market_turnover:v2",)
MARKET_FUND_FLOW_EAST_CACHE_KEY = "market_fund_flow:eastmoney:v1"
MARKET_FUND_FLOW_TENCENT_CACHE_KEY = "market_fund_flow:tencent:v1"
MARKET_FUND_FLOW_COMPAT_KEYS = ("market_fund_flow:v2", "market_fund_flow:v1")
MARKET_INTRADAY_PAIR_CACHE_VERSION = "1"
MARKET_INTRADAY_INDEX_CACHE_VERSION = "1"
SECTOR_OVERVIEW_CACHE_VERSION = "3"
SECTOR_DISPLAY_LIMIT = 6
SECTOR_TURNOVER_LIMIT = 6
CONCEPT_EXCLUDED_MARKERS = (
    "昨日", "连板", "涨停", "融资融券", "沪股通", "深股通", "QFII", "MSCI",
    "基金重仓", "机构重仓", "社保重仓", "券商金股", "预盈预增", "破净股", "高送转",
    "中报", "年报", "季报", "业绩预", "首亏", "扭亏", "续亏", "续盈",
    "低市净率", "高市净率", "低市盈率", "高市盈率", "高成长股", "题材股",
    "反转股", "趋势股", "历史新高", "历史新低", "百元股", "低价股", "高价股",
    "富时罗素", "标准普尔", "大盘股", "上证指数", "上证50", "沪深300",
    "深证成指", "深成500", "创业板指", "创业板综", "中证500", "中证1000",
    "HS300",
)


def number_or_none(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def market_info(symbol: str) -> tuple[str, str, str]:
    if symbol.startswith("6"):
        return f"{symbol}.SH", "SSE", "科创板" if symbol.startswith("68") else "主板"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ", "SZSE", "创业板" if symbol.startswith("30") else "主板"
    return f"{symbol}.BJ", "BSE", "北交所"


def baostock_code(ts_code: str) -> str:
    symbol, exchange = ts_code.split(".")
    prefix = "sh" if exchange == "SH" else "sz" if exchange == "SZ" else "bj"
    return f"{prefix}.{symbol}"


def _last_weekday(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


class MarketDataService:
    def __init__(
        self,
        eastmoney_client: EastmoneyClient | None = None,
        tencent_client: TencentClient | None = None,
    ) -> None:
        self._eastmoney_client = eastmoney_client or EastmoneyClient()
        self._tencent_client = tencent_client or TencentClient()
        self._spot_lock = threading.RLock()
        self._sector_lock = threading.RLock()
        self._baostock_lock = threading.RLock()
        self._spot_rows: list[dict[str, Any]] = []
        self._spot_fetched_at: datetime | None = None
        self._trade_date: str | None = None
        self._spot_failed_at: datetime | None = None
        self._overview_cache: dict[str, Any] | None = None
        self._overview_fetched_at: datetime | None = None
        self._sector_cache: dict[str, Any] | None = None
        self._sector_fetched_at: datetime | None = None

    @staticmethod
    def _akshare() -> Any:
        import akshare as ak

        return ak

    @staticmethod
    def _baostock() -> Any:
        import baostock as bs

        return bs

    @contextmanager
    def _baostock_session(self) -> Iterator[Any]:
        with self._baostock_lock:
            client = self._baostock()
            login = client.login()
            if login.error_code != "0":
                raise RuntimeError(login.error_msg or "BaoStock 登录失败")
            try:
                yield client
            finally:
                client.logout()

    def latest_trade_date(self) -> str:
        if self._trade_date:
            return self._trade_date
        today = datetime.now(database.CHINA_TZ).date()
        try:
            calendar = self._akshare().tool_trade_date_hist_sina()
            dates = [item.date() if hasattr(item, "date") else date.fromisoformat(str(item)[:10]) for item in calendar["trade_date"]]
            self._trade_date = max(item for item in dates if item <= today).strftime("%Y%m%d")
        except Exception:
            self._trade_date = _last_weekday(today).strftime("%Y%m%d")
        return self._trade_date

    def _spot_frame(self) -> tuple[Any, str, str]:
        errors: list[str] = []
        if MARKET.eastmoney_delay_enabled:
            try:
                return (
                    self._eastmoney_client.spot_frame(),
                    "AKShare 兼容 · 东方财富备用线路",
                    "push2delay 直连（自动绕过异常系统代理）",
                )
            except Exception as error:
                errors.append(f"备用线路：{error}")
        try:
            return self._akshare().stock_zh_a_spot_em(), "AKShare · 东方财富", "AKShare 标准线路"
        except Exception as error:
            errors.append(f"AKShare：{error}")
        raise RuntimeError("；".join(errors) or "实时行情获取失败")

    def _normalize_spot(self, frame: Any, source: str) -> list[dict[str, Any]]:
        fetched_at = database.now_iso()
        fallback_trade_date: str | None = None
        result: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            symbol = str(row.get("代码", "")).zfill(6)
            name = str(row.get("名称", "")).strip()
            close = number_or_none(row.get("最新价"))
            if len(symbol) != 6 or not name or close is None or close <= 0:
                continue
            quote_timestamp = number_or_none(row.get("行情时间"))
            if quote_timestamp is not None and quote_timestamp >= 1_000_000_000:
                quote_datetime = datetime.fromtimestamp(quote_timestamp, database.CHINA_TZ)
                trade_date = quote_datetime.strftime("%Y%m%d")
                quote_time = quote_datetime.strftime("%H:%M")
            else:
                if fallback_trade_date is None:
                    fallback_trade_date = self.latest_trade_date()
                trade_date = fallback_trade_date
                quote_time = None
            ts_code, exchange, market = market_info(symbol)
            result.append(
                {
                    "tsCode": ts_code,
                    "symbol": symbol,
                    "name": name,
                    "area": None,
                    "industry": None,
                    "market": market,
                    "exchange": exchange,
                    "listDate": None,
                    "tradeDate": trade_date,
                    "quoteTime": quote_time,
                    "open": number_or_none(row.get("今开")),
                    "high": number_or_none(row.get("最高")),
                    "low": number_or_none(row.get("最低")),
                    "close": close,
                    "preClose": number_or_none(row.get("昨收")),
                    "change": number_or_none(row.get("涨跌额")),
                    "pctChg": number_or_none(row.get("涨跌幅")),
                    "vol": number_or_none(row.get("成交量")),
                    "amount": number_or_none(row.get("成交额")),
                    "turnoverRate": number_or_none(row.get("换手率")),
                    "volumeRatio": number_or_none(row.get("量比")),
                    "amplitude": number_or_none(row.get("振幅")),
                    "peTtm": number_or_none(row.get("市盈率-动态")),
                    "pb": number_or_none(row.get("市净率")),
                    "totalMv": number_or_none(row.get("总市值")),
                    "floatMv": number_or_none(row.get("流通市值")),
                    "source": source,
                    "fetchedAt": fetched_at,
                }
            )
        return result

    def market_snapshot(self, force: bool = False) -> list[dict[str, Any]]:
        with self._spot_lock:
            if (
                not force
                and self._spot_rows
                and self._spot_fetched_at
                and (datetime.now(database.CHINA_TZ) - self._spot_fetched_at).total_seconds() < MARKET.spot_cache_seconds
            ):
                return self._spot_rows
            if (
                not force
                and self._spot_failed_at
                and (datetime.now(database.CHINA_TZ) - self._spot_failed_at).total_seconds() < MARKET.primary_failure_backoff_seconds
            ):
                raise RuntimeError("实时主行情源刚刚连接失败，正在等待自动重试")
            try:
                frame, source, transport = self._spot_frame()
                rows = self._normalize_spot(frame, source)
            except Exception:
                self._spot_failed_at = datetime.now(database.CHINA_TZ)
                raise
            if not rows:
                raise RuntimeError("AKShare 暂未返回有效 A 股行情")
            self._spot_rows = rows
            self._spot_fetched_at = datetime.now(database.CHINA_TZ)
            self._spot_failed_at = None
            database.set_meta("market_data_source", source)
            database.set_meta("market_data_transport", transport)
            database.set_meta("market_data_updated_at", database.now_iso())
            database.set_meta("market_data_error", "")
            return rows

    def sync_catalog(self, force: bool = False) -> int:
        last_sync = database.get_meta("stock_catalog_synced_at")
        if not force and last_sync:
            try:
                if (datetime.now(database.CHINA_TZ) - datetime.fromisoformat(last_sync)).total_seconds() < CATALOG_CACHE_SECONDS:
                    return 0
            except ValueError:
                pass
        try:
            rows = self.market_snapshot(force=force)
            source = str(rows[0].get("source") or "AKShare · 东方财富") if rows else "AKShare · 东方财富"
        except Exception:
            rows = self._baostock_catalog()
            source = "BaoStock"
        database.upsert_stock_basics(rows)
        database.set_meta("stock_catalog_synced_at", database.now_iso())
        database.set_meta("stock_catalog_source", source)
        return len(rows)

    def _baostock_catalog(self) -> list[dict[str, Any]]:
        with self._baostock_session() as bs:
            result = bs.query_stock_basic()
            rows: list[dict[str, Any]] = []
            while result.error_code == "0" and result.next():
                raw = dict(zip(result.fields, result.get_row_data(), strict=False))
                code = str(raw.get("code", ""))
                if raw.get("type") != "1" or raw.get("status") != "1" or "." not in code:
                    continue
                prefix, symbol = code.split(".", 1)
                exchange = "SSE" if prefix == "sh" else "SZSE" if prefix == "sz" else "BSE"
                suffix = "SH" if prefix == "sh" else "SZ" if prefix == "sz" else "BJ"
                market = "科创板" if symbol.startswith("68") else "创业板" if symbol.startswith("30") else "北交所" if suffix == "BJ" else "主板"
                rows.append(
                    {
                        "tsCode": f"{symbol}.{suffix}",
                        "symbol": symbol,
                        "name": str(raw.get("code_name", "")).strip(),
                        "area": None,
                        "industry": None,
                        "market": market,
                        "exchange": exchange,
                        "listDate": str(raw.get("ipoDate", "")).replace("-", "") or None,
                    }
                )
            if not rows:
                raise RuntimeError("BaoStock 暂未返回股票目录")
            return rows

    def _baostock_latest_quote(self, ts_code: str) -> dict[str, Any] | None:
        with self._baostock_session() as bs:
            end = datetime.now(database.CHINA_TZ).date()
            start = end - timedelta(days=35)
            fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg,tradestatus"
            result = bs.query_history_k_data_plus(
                baostock_code(ts_code),
                fields,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="2",
            )
            rows: list[list[str]] = []
            while result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
            for values in reversed(rows):
                row = dict(zip(fields.split(","), values, strict=False))
                close = number_or_none(row.get("close"))
                if row.get("tradestatus") != "1" or close is None:
                    continue
                pre_close = number_or_none(row.get("preclose"))
                change = close - pre_close if pre_close is not None else None
                return {
                    "tsCode": ts_code,
                    "tradeDate": str(row["date"]).replace("-", ""),
                    "open": number_or_none(row.get("open")),
                    "high": number_or_none(row.get("high")),
                    "low": number_or_none(row.get("low")),
                    "close": close,
                    "preClose": pre_close,
                    "change": change,
                    "pctChg": number_or_none(row.get("pctChg")),
                    "vol": number_or_none(row.get("volume")),
                    "amount": number_or_none(row.get("amount")),
                    "turnoverRate": None,
                    "volumeRatio": None,
                    "amplitude": None,
                    "peTtm": None,
                    "pb": None,
                    "totalMv": None,
                    "floatMv": None,
                    "source": "BaoStock",
                    "fetchedAt": database.now_iso(),
                }
        return None

    def refresh_quotes(self, ts_codes: list[str], force: bool = False) -> dict[str, dict[str, Any]]:
        cached = database.get_quotes(ts_codes)
        if not force and cached:
            fresh = True
            for code in ts_codes:
                item = cached.get(code)
                if not item:
                    fresh = False
                    break
                try:
                    age = (datetime.now(database.CHINA_TZ) - datetime.fromisoformat(item["fetchedAt"])).total_seconds()
                except (TypeError, ValueError):
                    age = MARKET.spot_cache_seconds + 1
                if age >= MARKET.spot_cache_seconds:
                    fresh = False
                    break
            if fresh:
                return cached

        try:
            snapshot = self.market_snapshot(force=force)
            by_code = {row["tsCode"]: row for row in snapshot}
            selected = [by_code[code] for code in ts_codes if code in by_code]
            database.upsert_stock_basics(selected)
            database.upsert_quotes(selected)
            self.sync_catalog()
            return database.get_quotes(ts_codes)
        except Exception as primary_error:
            fallback: list[dict[str, Any]] = []
            for code in ts_codes:
                try:
                    quote = self._baostock_latest_quote(code)
                    if quote:
                        fallback.append(quote)
                except Exception:
                    continue
            if fallback:
                database.upsert_quotes(fallback)
                database.set_meta("market_data_source", "BaoStock（AKShare 降级）")
                database.set_meta("market_data_transport", "BaoStock 日线降级")
                database.set_meta("market_data_updated_at", database.now_iso())
                database.set_meta("market_data_error", str(primary_error))
                try:
                    self.sync_catalog()
                except Exception:
                    pass
                return database.get_quotes(ts_codes)
            database.set_meta("market_data_error", str(primary_error))
            if cached:
                return cached
            raise RuntimeError(f"免费行情源暂不可用：{primary_error}") from primary_error

    def history(self, symbol: str, days: int = 420) -> list[dict[str, Any]]:
        end = datetime.now(database.CHINA_TZ).date()
        start = end - timedelta(days=days)
        if (database.get_meta("market_data_source") or "").startswith("BaoStock"):
            return self._baostock_history(symbol, start, end)
        try:
            frame = self._akshare().stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",
            )
            rows: list[dict[str, Any]] = []
            for _, row in frame.iterrows():
                close = number_or_none(row.get("收盘"))
                if close is None:
                    continue
                rows.append(
                    {
                        "date": str(row.get("日期"))[:10].replace("-", ""),
                        "open": number_or_none(row.get("开盘")),
                        "high": number_or_none(row.get("最高")),
                        "low": number_or_none(row.get("最低")),
                        "close": close,
                        "pctChg": number_or_none(row.get("涨跌幅")),
                        "vol": number_or_none(row.get("成交量")),
                        "amount": number_or_none(row.get("成交额")),
                    }
                )
            if rows:
                return sorted(rows, key=lambda item: item["date"])
        except Exception:
            pass
        return self._baostock_history(symbol, start, end)

    def _baostock_history(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        try:
            with self._baostock_session() as bs:
                return self._query_baostock_history(bs, symbol, start, end)
        except RuntimeError:
            return []

    @staticmethod
    def _query_baostock_history(bs: Any, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        ts_code, _, _ = market_info(symbol)
        fields = "date,open,high,low,close,volume,amount,pctChg,turn,peTTM,pbMRQ,tradestatus"
        result = bs.query_history_k_data_plus(
            baostock_code(ts_code), fields, start_date=start.isoformat(), end_date=end.isoformat(),
            frequency="d", adjustflag="2",
        )
        rows: list[dict[str, Any]] = []
        while result.error_code == "0" and result.next():
            raw = dict(zip(fields.split(","), result.get_row_data(), strict=False))
            close = number_or_none(raw.get("close"))
            if raw.get("tradestatus") != "1" or close is None:
                continue
            rows.append(
                {
                    "date": str(raw["date"]).replace("-", ""),
                    "open": number_or_none(raw.get("open")),
                    "high": number_or_none(raw.get("high")),
                    "low": number_or_none(raw.get("low")),
                    "close": close,
                    "pctChg": number_or_none(raw.get("pctChg")),
                    "vol": number_or_none(raw.get("volume")),
                    "amount": number_or_none(raw.get("amount")),
                    "turnoverRate": number_or_none(raw.get("turn")),
                    "peTtm": number_or_none(raw.get("peTTM")),
                    "pb": number_or_none(raw.get("pbMRQ")),
                }
            )
        return rows

    def history_batch(self, symbols: list[str], days: int = 420) -> dict[str, list[dict[str, Any]]]:
        """Fetch strategy history with one BaoStock login for the complete candidate batch."""
        end = datetime.now(database.CHINA_TZ).date()
        start = end - timedelta(days=days)
        try:
            with self._baostock_session() as bs:
                histories: dict[str, list[dict[str, Any]]] = {}
                for symbol in dict.fromkeys(symbols):
                    try:
                        histories[symbol] = self._query_baostock_history(bs, symbol, start, end)
                    except Exception:
                        histories[symbol] = []
                return histories
        except RuntimeError:
            return {}

    def dividend_yield(self, symbol: str) -> float | None:
        if (database.get_meta("market_data_source") or "").startswith("BaoStock"):
            return None
        try:
            frame = self._akshare().stock_a_lg_indicator(symbol=symbol)
            if frame.empty:
                return None
            row = frame.iloc[-1]
            return number_or_none(row.get("dv_ttm")) or number_or_none(row.get("dv_ratio"))
        except Exception:
            return None

    def _concept_name_frame(self) -> tuple[Any, str]:
        errors: list[str] = []
        if MARKET.eastmoney_delay_enabled:
            try:
                return self._eastmoney_client.concept_name_frame(), "东方财富概念板块备用线路"
            except Exception as error:
                errors.append(f"备用线路：{error}")
        try:
            return self._akshare().stock_board_concept_name_em(), "AKShare · 东方财富概念板块"
        except Exception as error:
            errors.append(f"AKShare：{error}")
        raise RuntimeError("；".join(errors) or "概念板块获取失败")

    def _industry_name_frame(self) -> tuple[Any, str]:
        errors: list[str] = []
        if MARKET.eastmoney_delay_enabled:
            try:
                return self._eastmoney_client.industry_name_frame(), "东方财富行业板块备用线路"
            except Exception as error:
                errors.append(f"备用线路：{error}")
        try:
            return self._akshare().stock_board_industry_name_em(), "AKShare · 东方财富行业板块"
        except Exception as error:
            errors.append(f"AKShare：{error}")
        raise RuntimeError("；".join(errors) or "行业板块获取失败")

    def _sector_fund_flow_frame(self, kind: str) -> tuple[Any, str]:
        errors: list[str] = []
        if MARKET.eastmoney_delay_enabled:
            try:
                return self._eastmoney_client.sector_fund_flow_frame(kind), "东方财富板块资金备用线路"
            except Exception as error:
                errors.append(f"备用线路：{error}")
        try:
            sector_type = "行业资金流" if kind == "industry" else "概念资金流"
            return (
                self._akshare().stock_sector_fund_flow_rank(
                    indicator="今日",
                    sector_type=sector_type,
                ),
                "AKShare · 东方财富板块资金",
            )
        except Exception as error:
            errors.append(f"AKShare：{error}")
        raise RuntimeError("；".join(errors) or "板块资金流获取失败")

    def _sector_history_frame(self, kind: str, code: str) -> tuple[Any, str]:
        errors: list[str] = []
        if MARKET.eastmoney_delay_enabled:
            try:
                return self._eastmoney_client.sector_history_frame(code), "东方财富板块历史备用线路"
            except Exception as error:
                errors.append(f"备用线路：{error}")
        today = datetime.now(database.CHINA_TZ).date()
        start = today - timedelta(days=14)
        try:
            if kind == "industry":
                frame = self._akshare().stock_board_industry_hist_em(
                    symbol=code,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=today.strftime("%Y%m%d"),
                    period="日k",
                    adjust="",
                )
            else:
                frame = self._akshare().stock_board_concept_hist_em(
                    symbol=code,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=today.strftime("%Y%m%d"),
                    adjust="",
                )
            return frame, "AKShare · 东方财富板块历史"
        except Exception as error:
            errors.append(f"AKShare：{error}")
        raise RuntimeError("；".join(errors) or f"板块 {code} 历史成交额获取失败")

    def _sector_amount_pair(
        self,
        kind: str,
        code: str,
        trade_date: str,
    ) -> tuple[float | None, float | None, str]:
        frame, source = self._sector_history_frame(kind, code)
        points: list[tuple[str, float]] = []
        for _, row in frame.iterrows():
            raw_date = str(row.get("日期", "")).replace("-", "")[:8]
            amount = number_or_none(row.get("成交额"))
            if len(raw_date) == 8 and raw_date.isdigit() and amount is not None and amount >= 0:
                points.append((raw_date, amount))
        points.sort(key=lambda item: item[0])
        current = next((amount for date_key, amount in reversed(points) if date_key == trade_date), None)
        previous = next((amount for date_key, amount in reversed(points) if date_key < trade_date), None)
        return current, previous, source

    def _enrich_board_turnover(
        self,
        kind: str,
        boards: list[dict[str, Any]],
        trade_date: str,
    ) -> tuple[list[str], int]:
        cache_key = f"sector_previous_amount:{trade_date}:v1"
        cached = self._cached_json(cache_key)
        previous_by_code = dict(cached) if isinstance(cached, dict) else {}
        targets = [
            board for board in boards
            if number_or_none(previous_by_code.get(board["code"])) is None
            or number_or_none(board.get("amount")) is None
        ]
        sources: list[str] = []
        failures = 0
        if targets:
            with ThreadPoolExecutor(max_workers=min(3, len(targets))) as executor:
                futures = {
                    executor.submit(self._sector_amount_pair, kind, board["code"], trade_date): board
                    for board in targets
                }
                for future in as_completed(futures):
                    board = futures[future]
                    try:
                        current, previous, source = future.result()
                        sources.append(source)
                        if number_or_none(board.get("amount")) is None:
                            board["amount"] = current
                        if previous is not None:
                            previous_by_code[board["code"]] = previous
                    except Exception:
                        failures += 1

        for board in boards:
            amount = number_or_none(board.get("amount"))
            previous = number_or_none(previous_by_code.get(board["code"]))
            board["amount"] = amount
            board["previousAmount"] = previous
            board["amountDelta"] = amount - previous if amount is not None and previous is not None else None
        if previous_by_code:
            database.set_meta(cache_key, json.dumps(previous_by_code, ensure_ascii=False))
        return list(dict.fromkeys(sources)), failures

    def _concept_cons_frame(self, code: str) -> Any:
        errors: list[str] = []
        if MARKET.eastmoney_delay_enabled:
            try:
                return self._eastmoney_client.concept_constituent_frame(code)
            except Exception as error:
                errors.append(f"备用线路：{error}")
        try:
            return self._akshare().stock_board_concept_cons_em(symbol=code)
        except Exception as error:
            errors.append(f"AKShare：{error}")
        raise RuntimeError("；".join(errors) or f"概念 {code} 成份股获取失败")

    def hot_concept_snapshot(self, force: bool = False) -> dict[str, Any]:
        """Return today's strongest real-time concept boards and their liquid constituents.

        A successful result is cached by China calendar date. We deliberately do not fall
        back to a previous date because an old concept ranking must not be presented as
        today's market theme.
        """
        run_date = database.china_date()
        cache_key = f"hot_concepts:{run_date}:v{CONCEPT_CACHE_VERSION}"
        cached = database.get_meta(cache_key)
        if cached and not force:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass

        try:
            frame, concept_source = self._concept_name_frame()
            concepts: list[dict[str, Any]] = []
            for _, row in frame.iterrows():
                name = str(row.get("板块名称", "")).strip()
                code = str(row.get("板块代码", "")).strip()
                pct_chg = number_or_none(row.get("涨跌幅"))
                up_count = number_or_none(row.get("上涨家数"))
                down_count = number_or_none(row.get("下跌家数"))
                if (
                    not name
                    or not code
                    or pct_chg is None
                    or pct_chg <= 0
                    or up_count is None
                    or down_count is None
                    or up_count <= down_count
                    or any(marker.upper() in name.upper() for marker in CONCEPT_EXCLUDED_MARKERS)
                ):
                    continue
                breadth = up_count / max(up_count + down_count, 1)
                concepts.append(
                    {
                        "name": name,
                        "code": code,
                        "pctChg": pct_chg,
                        "upCount": int(up_count),
                        "downCount": int(down_count),
                        "breadth": breadth,
                        "turnoverRate": number_or_none(row.get("换手率")),
                        "leadingStock": str(row.get("领涨股票", "")).strip() or None,
                    }
                )

            concepts.sort(key=lambda item: (item["pctChg"], item["breadth"]), reverse=True)
            selected = concepts[:MARKET.hot_concept_limit]
            for concept in selected:
                constituent_frame = self._concept_cons_frame(concept["code"])
                stocks: list[dict[str, Any]] = []
                for _, stock in constituent_frame.iterrows():
                    symbol = str(stock.get("代码", "")).zfill(6)
                    name = str(stock.get("名称", "")).strip()
                    price = number_or_none(stock.get("最新价"))
                    pct_chg = number_or_none(stock.get("涨跌幅"))
                    amount = number_or_none(stock.get("成交额"))
                    if (
                        len(symbol) != 6
                        or not symbol.isdigit()
                        or not name
                        or "ST" in name.upper()
                        or "退" in name
                        or price is None
                        or price <= 0
                        or pct_chg is None
                        or pct_chg < 0
                        or pct_chg >= 11
                        or amount is None
                        or amount < 100_000_000
                    ):
                        continue
                    stocks.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "price": price,
                            "pctChg": pct_chg,
                            "amount": amount,
                            "turnoverRate": number_or_none(stock.get("换手率")),
                        }
                    )
                stocks.sort(key=lambda item: (item["amount"], item["pctChg"]), reverse=True)
                concept["stocks"] = stocks[:30]

            if not selected:
                raise RuntimeError("概念板块没有返回符合强度与广度条件的数据")
            result = {
                "tradeDate": self.latest_trade_date(),
                "source": concept_source,
                "concepts": selected,
                "error": None,
            }
            database.set_meta(cache_key, json.dumps(result, ensure_ascii=False))
            return result
        except Exception as error:
            if cached:
                try:
                    return json.loads(cached)
                except json.JSONDecodeError:
                    pass
            return {
                "tradeDate": self.latest_trade_date(),
                "source": "AKShare · 东方财富概念板块",
                "concepts": [],
                "error": str(error),
            }

    @staticmethod
    def _normalized_symbol(value: Any) -> str | None:
        raw = str(value or "").strip().split(".", 1)[0]
        if raw.endswith(".0"):
            raw = raw[:-2]
        return raw.zfill(6) if raw.isdigit() and len(raw) <= 6 else None

    @staticmethod
    def _leader_view(
        name: str,
        role: str,
        stock_by_name: dict[str, dict[str, Any]],
        stock_by_symbol: dict[str, dict[str, Any]],
        code: str | None = None,
        fallback_pct_chg: float | None = None,
    ) -> dict[str, Any] | None:
        normalized_name = name.strip()
        if not normalized_name or "ST" in normalized_name.upper() or "退" in normalized_name:
            return None
        stock = stock_by_symbol.get(code or "") or stock_by_name.get(normalized_name)
        return {
            "role": role,
            "code": str(stock.get("symbol")) if stock else code,
            "name": str(stock.get("name")) if stock else normalized_name,
            "price": number_or_none(stock.get("close")) if stock else None,
            "pctChg": (
                number_or_none(stock.get("pctChg"))
                if stock else fallback_pct_chg
            ),
            "amount": number_or_none(stock.get("amount")) if stock else None,
        }

    def _board_rows(
        self,
        kind: str,
        board_frame: Any,
        fund_frame: Any | None,
        snapshot: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        stock_by_name = {
            str(stock.get("name") or "").strip(): stock
            for stock in snapshot
            if str(stock.get("name") or "").strip()
        }
        stock_by_symbol = {
            str(stock.get("symbol") or ""): stock
            for stock in snapshot
            if stock.get("symbol")
        }
        fund_by_name: dict[str, Any] = {}
        if fund_frame is not None:
            for _, row in fund_frame.iterrows():
                name = str(row.get("名称", "")).strip()
                if name:
                    fund_by_name[name] = row

        boards: list[dict[str, Any]] = []
        for _, row in board_frame.iterrows():
            name = str(row.get("板块名称", "")).strip()
            code = str(row.get("板块代码", "")).strip()
            pct_chg = number_or_none(row.get("涨跌幅"))
            up_count = number_or_none(row.get("上涨家数"))
            down_count = number_or_none(row.get("下跌家数"))
            if (
                not name
                or not code
                or pct_chg is None
                or up_count is None
                or down_count is None
                or (kind == "concept" and any(
                    marker.upper() in name.upper()
                    for marker in CONCEPT_EXCLUDED_MARKERS
                ))
            ):
                continue

            flow = fund_by_name.get(name)
            main_net_inflow = number_or_none(flow.get("主力净流入-净额")) if flow is not None else None
            main_net_ratio = number_or_none(flow.get("主力净流入-净占比")) if flow is not None else None
            leaders: list[dict[str, Any]] = []
            price_leader = self._leader_view(
                str(row.get("领涨股票", "")),
                "领涨龙头",
                stock_by_name,
                stock_by_symbol,
                fallback_pct_chg=number_or_none(row.get("领涨股票-涨跌幅")),
            )
            if price_leader:
                leaders.append(price_leader)
            if flow is not None:
                flow_leader = self._leader_view(
                    str(flow.get("主力净流入最大股", "")),
                    "资金龙头",
                    stock_by_name,
                    stock_by_symbol,
                    code=self._normalized_symbol(flow.get("主力净流入最大股代码")),
                )
                if flow_leader and all(
                    (leader.get("code") or leader["name"])
                    != (flow_leader.get("code") or flow_leader["name"])
                    for leader in leaders
                ):
                    leaders.append(flow_leader)

            total = int(up_count + down_count)
            boards.append(
                {
                    "kind": kind,
                    "code": code,
                    "name": name,
                    "pctChg": pct_chg,
                    "amount": number_or_none(row.get("成交额")),
                    "previousAmount": None,
                    "amountDelta": None,
                    "turnoverRate": number_or_none(row.get("换手率")),
                    "marketCap": number_or_none(row.get("总市值")),
                    "upCount": int(up_count),
                    "downCount": int(down_count),
                    "breadth": up_count / max(total, 1) * 100,
                    "mainNetInflow": main_net_inflow,
                    "mainNetInflowRatio": main_net_ratio,
                    "leaders": leaders,
                }
            )
        boards.sort(
            key=lambda item: (
                item["pctChg"],
                item["breadth"],
                item["mainNetInflow"] if item["mainNetInflow"] is not None else -math.inf,
            ),
            reverse=True,
        )
        return boards

    def sector_overview(self, force: bool = False) -> dict[str, Any]:
        with self._sector_lock:
            if (
                not force
                and self._sector_cache
                and self._sector_fetched_at
                and (datetime.now(database.CHINA_TZ) - self._sector_fetched_at).total_seconds()
                < MARKET.spot_cache_seconds
            ):
                return self._sector_cache

            cache_key = f"sector_overview:v{SECTOR_OVERVIEW_CACHE_VERSION}"
            cached = self._cached_json(cache_key)
            warnings: list[str] = []
            debug_errors: list[str] = []
            sources: list[str] = []
            try:
                snapshot = self.market_snapshot(force=False)
            except Exception as error:
                snapshot = []
                warnings.append("龙头股行情暂不可用")
                debug_errors.append(f"行情快照：{error}")
            trade_dates = [
                str(stock.get("tradeDate"))
                for stock in snapshot
                if len(str(stock.get("tradeDate") or "")) == 8
            ]
            trade_date = max(trade_dates, default=self.latest_trade_date())

            category_rows: dict[str, list[dict[str, Any]]] = {}
            counts: dict[str, int] = {}
            rising_counts: dict[str, int] = {}
            for kind in ("industry", "concept"):
                label = "行业" if kind == "industry" else "概念"
                try:
                    board_frame, board_source = (
                        self._industry_name_frame()
                        if kind == "industry"
                        else self._concept_name_frame()
                    )
                    sources.append(board_source)
                except Exception as error:
                    debug_errors.append(f"{label}板块：{error}")
                    warnings.append(f"{label}板块暂不可用")
                    category_rows[kind] = []
                    counts[kind] = 0
                    rising_counts[kind] = 0
                    continue

                fund_frame = None
                try:
                    fund_frame, fund_source = self._sector_fund_flow_frame(kind)
                    sources.append(fund_source)
                except Exception as error:
                    debug_errors.append(f"{label}资金：{error}")
                    warnings.append(f"{label}资金流暂不可用")

                rows = self._board_rows(kind, board_frame, fund_frame, snapshot)
                category_rows[kind] = rows
                counts[kind] = len(rows)
                rising_counts[kind] = sum(1 for item in rows if item["pctChg"] > 0)

            if not category_rows.get("industry") and not category_rows.get("concept"):
                if isinstance(cached, dict):
                    fallback = {**cached}
                    fallback["warnings"] = ["板块数据已使用最近成功缓存"]
                    return fallback
                raise RuntimeError("行业与概念板块数据暂不可用")

            industry_boards = category_rows.get("industry", [])[:SECTOR_DISPLAY_LIMIT]
            concept_boards = category_rows.get("concept", [])[:SECTOR_DISPLAY_LIMIT]
            all_boards = [*category_rows.get("industry", []), *category_rows.get("concept", [])]
            turnover_boards = sorted(
                (item for item in all_boards if number_or_none(item.get("amount")) is not None),
                key=lambda item: float(item["amount"]),
                reverse=True,
            )[:SECTOR_TURNOVER_LIMIT]
            for kind, displayed_boards in (
                ("industry", industry_boards),
                ("concept", concept_boards),
            ):
                selected_by_code = {board["code"]: board for board in displayed_boards}
                selected_by_code.update({
                    board["code"]: board
                    for board in turnover_boards
                    if board["kind"] == kind
                })
                if not selected_by_code:
                    continue
                turnover_sources, turnover_failures = self._enrich_board_turnover(
                    kind,
                    list(selected_by_code.values()),
                    trade_date,
                )
                sources.extend(turnover_sources)
                if turnover_failures:
                    label = "行业" if kind == "industry" else "概念"
                    warnings.append(f"部分{label}板块昨日成交额暂不可用")
            top_board = max(all_boards, key=lambda item: item["pctChg"], default=None)
            boards_with_flow = [item for item in all_boards if item["mainNetInflow"] is not None]
            top_fund_board = max(
                boards_with_flow,
                key=lambda item: float(item["mainNetInflow"]),
                default=None,
            )
            result = {
                "tradeDate": trade_date,
                "updatedAt": database.now_iso(),
                "source": " · ".join(dict.fromkeys(sources)) or "AKShare · 东方财富板块",
                "summary": {
                    "industryCount": counts.get("industry", 0),
                    "conceptCount": counts.get("concept", 0),
                    "risingIndustryCount": rising_counts.get("industry", 0),
                    "risingConceptCount": rising_counts.get("concept", 0),
                    "topBoard": top_board,
                    "topFundBoard": top_fund_board,
                },
                "industryBoards": industry_boards,
                "conceptBoards": concept_boards,
                "turnoverBoards": turnover_boards,
                "warnings": list(dict.fromkeys(warnings)),
            }
            database.set_meta(cache_key, json.dumps(result, ensure_ascii=False))
            database.set_meta("sector_overview_error", "；".join(debug_errors))
            self._sector_cache = result
            self._sector_fetched_at = datetime.now(database.CHINA_TZ)
            return result

    @staticmethod
    def _limit_threshold(row: dict[str, Any]) -> float:
        name = str(row.get("name") or "").upper()
        symbol = str(row.get("symbol") or "")
        if "ST" in name:
            return 4.8
        if symbol.startswith(("30", "68")):
            return 19.8
        if str(row.get("exchange")) == "BSE":
            return 29.8
        return 9.8

    @staticmethod
    def _cached_json(key: str) -> Any:
        raw = database.get_meta(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @classmethod
    def _cached_json_any(cls, keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = cls._cached_json(key)
            if value is not None:
                return value
        return None

    @staticmethod
    def _metric_status(
        state: str,
        source: str | None,
        updated_at: str | None,
        message: str | None = None,
    ) -> dict[str, Any]:
        return {
            "state": state,
            "source": source,
            "updatedAt": updated_at,
            "message": message,
        }

    @staticmethod
    def _flow_cache_rows(payload: Any, default_source: str) -> tuple[list[dict[str, Any]], str | None]:
        cached_at: str | None = None
        if isinstance(payload, dict):
            raw_rows = payload.get("rows")
            cached_at = str(payload.get("cachedAt") or "") or None
        else:
            raw_rows = payload
        if not isinstance(raw_rows, list):
            return [], cached_at
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            trade_date = str(raw.get("date") or "")
            main = number_or_none(raw.get("mainNetInflow"))
            if len(trade_date) != 8 or main is None:
                continue
            rows.append({**raw, "mainNetInflow": main, "source": str(raw.get("source") or default_source)})
        rows.sort(key=lambda item: item["date"])
        return rows, cached_at

    @staticmethod
    def _fund_flow_frame_rows(frame: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            raw_date = row.get("日期")
            trade_date = str(raw_date).replace("-", "")[:8]
            main = number_or_none(row.get("主力净流入-净额"))
            if len(trade_date) != 8 or main is None:
                continue
            rows.append(
                {
                    "date": trade_date,
                    "shClose": number_or_none(row.get("上证-收盘价")),
                    "shPctChg": number_or_none(row.get("上证-涨跌幅")),
                    "szClose": number_or_none(row.get("深证-收盘价")),
                    "szPctChg": number_or_none(row.get("深证-涨跌幅")),
                    "mainNetInflow": main,
                    "mainNetInflowRatio": number_or_none(row.get("主力净流入-净占比")),
                    "superLargeNetInflow": number_or_none(row.get("超大单净流入-净额")),
                    "largeNetInflow": number_or_none(row.get("大单净流入-净额")),
                    "source": "东方财富大盘资金流",
                }
            )
        rows.sort(key=lambda item: item["date"])
        return rows

    @staticmethod
    def _merge_flow_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_date: dict[str, dict[str, Any]] = {}
        for rows in groups:
            for row in rows:
                by_date[str(row["date"])] = row
        return [by_date[key] for key in sorted(by_date)][-20:]

    def _fund_flow_history(
        self,
        trade_date: str,
        force: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
        legacy_payload = self._cached_json_any(MARKET_FUND_FLOW_COMPAT_KEYS)
        legacy_rows, legacy_at = self._flow_cache_rows(
            legacy_payload,
            "东方财富大盘资金流",
        )
        east_payload = self._cached_json(MARKET_FUND_FLOW_EAST_CACHE_KEY)
        east_rows, east_at = self._flow_cache_rows(
            east_payload,
            "东方财富大盘资金流",
        )
        east_rows = east_rows or legacy_rows
        east_at = east_at or legacy_at
        try:
            rows = self._fund_flow_frame_rows(self._eastmoney_client.market_fund_flow_frame())
            if not rows:
                raise RuntimeError("大盘资金流接口没有返回有效数据")
            result = rows[-20:]
            cached_at = database.now_iso()
            database.set_meta(
                MARKET_FUND_FLOW_EAST_CACHE_KEY,
                json.dumps({"cachedAt": cached_at, "rows": result}, ensure_ascii=False),
            )
            return result, None, self._metric_status(
                "live", "东方财富大盘资金流", cached_at
            )
        except Exception as east_error:
            tencent_payload = self._cached_json(MARKET_FUND_FLOW_TENCENT_CACHE_KEY)
            tencent_rows, tencent_at = self._flow_cache_rows(
                tencent_payload,
                "腾讯证券逐股资金汇总",
            )
            has_today = any(row["date"] == trade_date for row in tencent_rows)
            if force or not has_today:
                try:
                    current = self._tencent_client.market_fund_flow_snapshot(trade_date)
                    tencent_rows = self._merge_flow_rows(tencent_rows, [current])
                    tencent_at = database.now_iso()
                    database.set_meta(
                        MARKET_FUND_FLOW_TENCENT_CACHE_KEY,
                        json.dumps(
                            {"cachedAt": tencent_at, "rows": tencent_rows},
                            ensure_ascii=False,
                        ),
                    )
                    merged = self._merge_flow_rows(east_rows, tencent_rows)
                    warning = f"资金流已降级至腾讯证券逐股汇总：{east_error}"
                    return merged, warning, self._metric_status(
                        "fallback",
                        "腾讯证券逐股资金汇总",
                        tencent_at,
                        "东方财富资金流不可用，已自动切换腾讯证券",
                    )
                except Exception as tencent_error:
                    fallback = self._merge_flow_rows(east_rows, tencent_rows)
                    if fallback:
                        warning = (
                            "资金流暂用最近成功缓存："
                            f"东方财富 {east_error}；腾讯证券 {tencent_error}"
                        )
                        source = str(fallback[-1].get("source") or "最近成功缓存")
                        return fallback, warning, self._metric_status(
                            "cached", source, tencent_at or east_at, "两个实时来源均不可用"
                        )
                    warning = f"资金流暂不可用：东方财富 {east_error}；腾讯证券 {tencent_error}"
                    return [], warning, self._metric_status(
                        "unavailable", None, None, "两个资金流来源均不可用"
                    )

            merged = self._merge_flow_rows(east_rows, tencent_rows)
            warning = f"资金流已使用腾讯证券当日缓存：{east_error}"
            return merged, warning, self._metric_status(
                "cached",
                "腾讯证券逐股资金汇总",
                tencent_at,
                "东方财富资金流不可用，已使用腾讯证券当日缓存",
            )

    def _index_turnover_history(self) -> tuple[list[dict[str, Any]], str | None]:
        cache_key = MARKET_TURNOVER_CACHE_KEY
        cached = self._cached_json_any((cache_key, *MARKET_TURNOVER_COMPAT_KEYS))
        today = datetime.now(database.CHINA_TZ).date()
        start = today - timedelta(days=45)
        try:
            by_date: dict[str, float] = {}
            for symbol in ("sh000001", "sz399106"):
                frame = self._akshare().stock_zh_index_daily_em(
                    symbol=symbol,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=today.strftime("%Y%m%d"),
                )
                for _, row in frame.iterrows():
                    trade_date = str(row.get("date", "")).replace("-", "")[:8]
                    amount = number_or_none(row.get("amount"))
                    if len(trade_date) == 8 and amount is not None and amount >= 0:
                        by_date[trade_date] = by_date.get(trade_date, 0.0) + amount
            if not by_date:
                raise RuntimeError("沪深指数没有返回量能历史")
            result = [{"date": key, "turnover": by_date[key]} for key in sorted(by_date)][-20:]
            database.set_meta(cache_key, json.dumps(result, ensure_ascii=False))
            return result, None
        except Exception as error:
            if isinstance(cached, list) and cached:
                return cached[-20:], f"量能历史暂用最近成功缓存：{error}"
            return [], f"量能历史暂不可用：{error}"

    @staticmethod
    def _market_comparison_time(trade_date: str, snapshot: list[dict[str, Any]]) -> str:
        quote_times = [
            str(row.get("quoteTime"))
            for row in snapshot
            if row.get("tradeDate") == trade_date
            and len(str(row.get("quoteTime") or "")) == 5
        ]
        now = datetime.now(database.CHINA_TZ)
        raw_time = max(quote_times, default=(now.strftime("%H:%M") if trade_date == now.strftime("%Y%m%d") else "15:00"))
        if raw_time < "09:30":
            return "09:30"
        if "11:30" < raw_time < "13:00":
            return "11:30"
        if raw_time > "15:00":
            return "15:00"
        return raw_time

    def _intraday_turnover_comparison(
        self,
        trade_date: str,
        snapshot: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
        comparison_time = self._market_comparison_time(trade_date, snapshot)
        points_by_index: dict[str, list[tuple[str, str, float]]] = {}
        index_errors: list[str] = []
        cached_indexes = 0
        fetched_at: str | None = None
        for secid in ("1.000001", "0.399106"):
            cache_key = f"market_intraday_index:{secid}:v{MARKET_INTRADAY_INDEX_CACHE_VERSION}"
            try:
                points = self._eastmoney_client.index_intraday_turnover_points(secid)
                points_by_index[secid] = points
                fetched_at = database.now_iso()
                database.set_meta(
                    cache_key,
                    json.dumps({"cachedAt": fetched_at, "points": points}, ensure_ascii=False),
                )
            except Exception as error:
                index_errors.append(f"{secid} {error}")
                cached = self._cached_json(cache_key)
                raw_points = cached.get("points") if isinstance(cached, dict) else None
                parsed: list[tuple[str, str, float]] = []
                if isinstance(raw_points, list):
                    for point in raw_points:
                        if not isinstance(point, (list, tuple)) or len(point) != 3:
                            continue
                        amount = number_or_none(point[2])
                        if amount is not None:
                            parsed.append((str(point[0]), str(point[1]), amount))
                if parsed:
                    points_by_index[secid] = parsed
                    cached_indexes += 1
                    fetched_at = str(cached.get("cachedAt") or "") or fetched_at

        pair_cache_key = f"market_intraday_pair:{trade_date}:v{MARKET_INTRADAY_PAIR_CACHE_VERSION}"
        try:
            result = self._eastmoney_client.market_intraday_pair_from_points(
                points_by_index,
                trade_date,
                comparison_time,
            )
            source = "东方财富指数分时"
            state = "cached" if cached_indexes else "live"
            warning = "实时量能同比已使用东方财富逐指数缓存" if cached_indexes else None
            fetched_at = fetched_at or database.now_iso()
            database.set_meta(
                pair_cache_key,
                json.dumps(
                    {"cachedAt": fetched_at, "source": source, "result": result},
                    ensure_ascii=False,
                ),
            )
            return result, warning, self._metric_status(state, source, fetched_at)
        except Exception as east_error:
            try:
                result = self._tencent_client.index_intraday_turnover_pair(
                    trade_date,
                    comparison_time,
                )
                fetched_at = database.now_iso()
                source = "腾讯证券指数分时"
                database.set_meta(
                    pair_cache_key,
                    json.dumps(
                        {"cachedAt": fetched_at, "source": source, "result": result},
                        ensure_ascii=False,
                    ),
                )
                warning = f"实时量能同比已降级至腾讯证券：{east_error}"
                return result, warning, self._metric_status(
                    "fallback",
                    source,
                    fetched_at,
                    "东方财富指数分时不可用，已自动切换腾讯证券",
                )
            except Exception as tencent_error:
                cached_pair = self._cached_json(pair_cache_key)
                cached_result = cached_pair.get("result") if isinstance(cached_pair, dict) else None
                if isinstance(cached_result, dict) and cached_result.get("date") == trade_date:
                    source = str(cached_pair.get("source") or "最近成功缓存")
                    warning = (
                        "实时量能同比已使用最近成功缓存："
                        f"东方财富 {'；'.join(index_errors) or east_error}；腾讯证券 {tencent_error}"
                    )
                    return cached_result, warning, self._metric_status(
                        "cached",
                        source,
                        str(cached_pair.get("cachedAt") or "") or None,
                        "两个实时来源均不可用",
                    )
                warning = (
                    "实时量能同比暂不可用："
                    f"东方财富 {'；'.join(index_errors) or east_error}；腾讯证券 {tencent_error}"
                )
                return None, warning, self._metric_status(
                    "unavailable", None, None, "两个指数分时来源均不可用"
                )

    def market_overview(self, force: bool = False) -> dict[str, Any]:
        with self._spot_lock:
            if (
                not force
                and self._overview_cache
                and self._overview_fetched_at
                and (datetime.now(database.CHINA_TZ) - self._overview_fetched_at).total_seconds()
                < MARKET.spot_cache_seconds
            ):
                return self._overview_cache

            snapshot = self.market_snapshot(force=force)
            pct_values = [
                float(row["pctChg"])
                for row in snapshot
                if row.get("pctChg") is not None
            ]
            amount_values = [
                float(row["amount"])
                for row in snapshot
                if row.get("amount") is not None and float(row["amount"]) >= 0
            ]
            trade_date = max(
                (str(row.get("tradeDate") or "") for row in snapshot),
                default=self.latest_trade_date(),
            )
            advancers = sum(1 for value in pct_values if value > 0)
            decliners = sum(1 for value in pct_values if value < 0)
            flat = len(pct_values) - advancers - decliners
            limit_up = sum(
                1
                for row in snapshot
                if row.get("pctChg") is not None
                and float(row["pctChg"]) >= self._limit_threshold(row)
            )
            limit_down = sum(
                1
                for row in snapshot
                if row.get("pctChg") is not None
                and float(row["pctChg"]) <= -self._limit_threshold(row)
            )
            current_turnover = sum(amount_values)
            current_hs_turnover = sum(
                float(row["amount"])
                for row in snapshot
                if row.get("amount") is not None
                and float(row["amount"]) >= 0
                and str(row.get("exchange")) != "BSE"
            )

            turnover_history, turnover_error = self._index_turnover_history()
            turnover_by_date = {
                str(item.get("date")): float(item.get("turnover") or 0)
                for item in turnover_history
                if item.get("date")
            }
            # 指数历史只覆盖沪深两市；今日柱与日增量沿用同一口径，避免把
            # 北交所成交额只计入当天、却没有计入上一交易日。
            turnover_by_date[trade_date] = current_hs_turnover
            turnover_history = [
                {"date": key, "turnover": turnover_by_date[key]}
                for key in sorted(turnover_by_date)
            ][-12:]
            database.set_meta(
                MARKET_TURNOVER_CACHE_KEY,
                json.dumps(turnover_history, ensure_ascii=False),
            )
            intraday_comparison, comparison_error, comparison_status = self._intraday_turnover_comparison(
                trade_date,
                snapshot,
            )
            comparison_turnover = (
                number_or_none(intraday_comparison.get("currentTurnover"))
                if intraday_comparison
                else None
            )
            previous_turnover = (
                number_or_none(intraday_comparison.get("previousTurnover"))
                if intraday_comparison
                else None
            )
            turnover_delta = (
                comparison_turnover - previous_turnover
                if comparison_turnover is not None and previous_turnover is not None
                else None
            )
            turnover_delta_pct = (
                turnover_delta / previous_turnover * 100
                if turnover_delta is not None and previous_turnover
                else None
            )

            fund_flow_history, flow_error, flow_status = self._fund_flow_history(
                trade_date,
                force=force,
            )
            latest_flow = fund_flow_history[-1] if fund_flow_history else None
            top_turnover = sorted(
                (
                    {
                        "code": row["symbol"],
                        "name": row["name"],
                        "pctChg": row.get("pctChg"),
                        "amount": row.get("amount"),
                    }
                    for row in snapshot
                    if row.get("amount") is not None
                ),
                key=lambda item: float(item["amount"] or 0),
                reverse=True,
            )[:6]
            source = str(snapshot[0].get("source") or "AKShare · 东方财富")
            result = {
                "tradeDate": trade_date,
                "updatedAt": database.now_iso(),
                "source": source,
                "snapshot": {
                    "turnover": current_turnover,
                    "previousTurnover": previous_turnover,
                    "turnoverDelta": turnover_delta,
                    "turnoverDeltaPct": turnover_delta_pct,
                    "turnoverComparisonDate": (
                        intraday_comparison.get("previousDate")
                        if intraday_comparison
                        else None
                    ),
                    "turnoverComparisonTime": (
                        intraday_comparison.get("comparisonTime")
                        if intraday_comparison
                        else None
                    ),
                    "advancers": advancers,
                    "decliners": decliners,
                    "flat": flat,
                    "limitUp": limit_up,
                    "limitDown": limit_down,
                    "medianPctChg": median(pct_values) if pct_values else None,
                    "breadth": advancers / max(advancers + decliners, 1) * 100,
                },
                "turnoverHistory": turnover_history,
                "fundFlowHistory": fund_flow_history[-12:],
                "latestFlow": latest_flow,
                "dataStatus": {
                    "turnoverComparison": comparison_status,
                    "fundFlow": flow_status,
                },
                "topTurnover": top_turnover,
                "warnings": [
                    warning
                    for warning in (turnover_error, comparison_error, flow_error)
                    if warning
                ],
            }
            self._overview_cache = result
            self._overview_fetched_at = datetime.now(database.CHINA_TZ)
            return result

    def status(self) -> dict[str, Any]:
        source = database.get_meta("market_data_source") or "等待首次获取"
        using_fallback = source.startswith("BaoStock")
        retry_at: str | None = None
        if self._spot_failed_at:
            candidate = self._spot_failed_at + timedelta(seconds=MARKET.primary_failure_backoff_seconds)
            if candidate > datetime.now(database.CHINA_TZ):
                retry_at = candidate.isoformat(timespec="seconds")
        return {
            "primary": "AKShare / 东方财富备用线路",
            "fallback": "BaoStock",
            "source": source,
            "transport": database.get_meta("market_data_transport") or "等待首次获取",
            "updatedAt": database.get_meta("market_data_updated_at"),
            "error": database.get_meta("market_data_error") or None,
            "usingFallback": using_fallback,
            "health": "degraded" if using_fallback else "waiting" if source == "等待首次获取" else "healthy",
            "retryAt": retry_at,
        }


market_data = MarketDataService()
