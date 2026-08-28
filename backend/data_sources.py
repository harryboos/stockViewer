from __future__ import annotations

import json
import math
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Iterator

from . import database
from .config import MARKET
from .eastmoney import EastmoneyClient


CATALOG_CACHE_SECONDS = 24 * 60 * 60
CONCEPT_CACHE_VERSION = "2"
MARKET_OVERVIEW_CACHE_VERSION = "1"
CONCEPT_EXCLUDED_MARKERS = (
    "昨日", "连板", "涨停", "融资融券", "沪股通", "深股通", "QFII", "MSCI",
    "基金重仓", "机构重仓", "预盈预增", "破净股", "高送转",
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
    def __init__(self, eastmoney_client: EastmoneyClient | None = None) -> None:
        self._eastmoney_client = eastmoney_client or EastmoneyClient()
        self._spot_lock = threading.RLock()
        self._baostock_lock = threading.RLock()
        self._spot_rows: list[dict[str, Any]] = []
        self._spot_fetched_at: datetime | None = None
        self._trade_date: str | None = None
        self._spot_failed_at: datetime | None = None
        self._overview_cache: dict[str, Any] | None = None
        self._overview_fetched_at: datetime | None = None

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
                trade_date = datetime.fromtimestamp(quote_timestamp, database.CHINA_TZ).strftime("%Y%m%d")
            else:
                if fallback_trade_date is None:
                    fallback_trade_date = self.latest_trade_date()
                trade_date = fallback_trade_date
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

    def _fund_flow_history(self) -> tuple[list[dict[str, Any]], str | None]:
        cache_key = f"market_fund_flow:v{MARKET_OVERVIEW_CACHE_VERSION}"
        cached = self._cached_json(cache_key)
        try:
            frame = self._akshare().stock_market_fund_flow()
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
                    }
                )
            rows.sort(key=lambda item: item["date"])
            if not rows:
                raise RuntimeError("大盘资金流接口没有返回有效数据")
            result = rows[-20:]
            database.set_meta(cache_key, json.dumps(result, ensure_ascii=False))
            return result, None
        except Exception as error:
            if isinstance(cached, list) and cached:
                return cached[-20:], f"资金流暂用最近成功缓存：{error}"
            return [], f"资金流暂不可用：{error}"

    def _index_turnover_history(self) -> tuple[list[dict[str, Any]], str | None]:
        cache_key = f"market_turnover:v{MARKET_OVERVIEW_CACHE_VERSION}"
        cached = self._cached_json(cache_key)
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
                f"market_turnover:v{MARKET_OVERVIEW_CACHE_VERSION}",
                json.dumps(turnover_history, ensure_ascii=False),
            )
            previous_turnover = next(
                (
                    float(item["turnover"])
                    for item in reversed(turnover_history)
                    if item["date"] < trade_date and float(item["turnover"]) > 0
                ),
                None,
            )
            turnover_delta = (
                current_hs_turnover - previous_turnover
                if previous_turnover is not None
                else None
            )
            turnover_delta_pct = (
                turnover_delta / previous_turnover * 100
                if turnover_delta is not None and previous_turnover
                else None
            )

            fund_flow_history, flow_error = self._fund_flow_history()
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
                "topTurnover": top_turnover,
                "warnings": [warning for warning in (turnover_error, flow_error) if warning],
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
