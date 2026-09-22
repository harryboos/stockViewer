"""Independent history transports; concept namespaces must never be interchanged."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import date, timedelta

import requests

from .eastmoney import EastmoneyClient


def normalize_bars(rows: list, start: str, end: str) -> list[dict]:
    result = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row[0])).isoformat()
            values = [float(value) for value in row[1:5]]
            if len(values) != 4 or not all(math.isfinite(value) and value > 0 for value in values):
                continue
            opening, close, high, low = values
            if high < max(opening, close) or low > min(opening, close) or not start <= day <= end:
                continue
            result[day] = {"date": day, "open": opening, "close": close, "high": high, "low": low}
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(result.values(), key=lambda row: row["date"])


def validate_window(start: str, end: str) -> None:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or (last - first).days > 730:
        raise ValueError("日线查询区间必须有序且不超过两年")


def history_covers_window(rows: list[dict], start: str, end: str, calendar: tuple[str, ...] = ()) -> bool:
    """Conservative fast path only; actual returns still use the exchange calendar.

    Reaching the last date does not prove the first/interior sessions exist.
    Prefer an already loaded exchange calendar; this check itself does not
    query a database or remote service. Without a calendar covering the range,
    weekday holidays may conservatively trigger another provider read.
    """
    available = {row["date"] for row in rows}
    if calendar and calendar[0] <= start and calendar[-1] >= end:
        return bool(rows) and all(day in available for day in calendar if start <= day <= end)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return bool(rows) and all(
        day.weekday() >= 5 or day.isoformat() in available
        for day in (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    )


def _get_text(url: str, params: dict, referer: str) -> str:
    # At most two bounded reads; do not echo provider responses or proxy credentials.
    for trust_env in [False] + ([True] if requests.utils.get_environ_proxies(url) else []):
        try:
            with EastmoneyClient._session(trust_env=trust_env) as session:
                response = session.get(url, params=params, headers={"Referer": referer}, timeout=(3, 8))
                response.raise_for_status()
                return response.text
        except requests.RequestException:
            continue
    raise RuntimeError("历史行情连接失败或等待超时")


def sina_index_history(code: str, start: str, end: str) -> dict:
    if code not in ("sh000001", "sh000688"):
        raise ValueError("新浪备用源仅用于上证指数和科创50")
    validate_window(start, end)
    payload = json.loads(_get_text(
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
        {"symbol": code, "scale": 240, "ma": "no", "datalen": 1023}, "https://finance.sina.com.cn/"))
    if not isinstance(payload, list):
        raise ValueError("新浪未返回有效指数日线")
    rows = normalize_bars([[r.get("day"), r.get("open"), r.get("close"), r.get("high"), r.get("low")]
                           for r in payload if isinstance(r, dict)], start, end)
    return {"rows": rows, "source": "新浪财经指数日线",
            "url": f"https://finance.sina.com.cn/realstock/company/{code}/nc.shtml"}


def ths_concept_history(code: str, start: str, end: str) -> dict:
    """Explicit THS namespace for diagnostics/research, never a BK forecast fallback."""
    if not re.fullmatch(r"THS:88\d{4}", code):
        raise ValueError("同花顺概念必须使用 THS:88xxxx 代码，不能传入东财 BK 代码")
    validate_window(start, end)
    symbol, raw_rows = code.split(":")[1], []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        callback = f"quotebridge_v4_line_bk_{symbol}_01_{year}"
        text = _get_text(f"https://d.10jqka.com.cn/v4/line/bk_{symbol}/01/{year}.js", {},
                         "https://q.10jqka.com.cn/").strip().rstrip(";")
        if not text.startswith(callback + "(") or not text.endswith(")"):
            raise ValueError("同花顺日线代码或响应格式不匹配")
        data = json.loads(text[len(callback) + 1:-1])
        if not isinstance(data, dict) or not isinstance(data.get("data"), str):
            raise ValueError("同花顺日线格式异常")
        for line in data.get("data", "").split(";"):
            row = line.split(",")
            if len(row) >= 5 and re.fullmatch(r"\d{8}", row[0]):
                day = row[0]
                raw_rows.append([f"{day[:4]}-{day[4:6]}-{day[6:]}", row[1], row[4], row[2], row[3]])
    return {"rows": normalize_bars(raw_rows, start, end), "source": "同花顺概念指数日线（独立口径）",
            "url": "https://q.10jqka.com.cn/gn/", "code": code}


class TushareHistoryClient:
    """Optional same-index transport, with bounded concurrency and error cooldown."""
    _lock = threading.Lock()
    _credential = ""
    _retry_at = 0.0
    _next_at = 0.0
    _failure = ""

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("TUSHARE_TOKEN", "").strip())

    def history(self, code: str, start: str, end: str) -> dict:
        if not re.fullmatch(r"BK\d+", code):
            raise ValueError("Tushare 东财日线仅接受 BK 概念代码")
        validate_window(start, end)
        token = os.getenv("TUSHARE_TOKEN", "").strip()
        if not token:
            raise RuntimeError("未配置 TUSHARE_TOKEN，东财概念独立备用源未启用")
        cls = type(self)
        # Serialise this optional provider across comparison and prediction workers.
        if not cls._lock.acquire(timeout=12):
            raise RuntimeError("Tushare 日线正在查询，请稍后重试")
        try:
            identity = hashlib.sha256(token.encode()).hexdigest()
            if identity != cls._credential:
                cls._credential, cls._retry_at, cls._next_at = identity, 0.0, 0.0
            if time.monotonic() < cls._retry_at:
                raise RuntimeError(cls._failure)
            time.sleep(max(0, cls._next_at - time.monotonic()))
            try:
                result = self._read(token, code, start, end)
            except RuntimeError as error:
                cls._failure = str(error)
                cls._retry_at = time.monotonic() + (600 if "凭证" in str(error) or "权限" in str(error) else 60)
                raise
            finally:
                cls._next_at = time.monotonic() + 0.35
            return result
        finally:
            cls._lock.release()

    @staticmethod
    def _read(token: str, code: str, start: str, end: str) -> dict:
        # HTTPS only; no redirects to prevent credentials reaching another host.
        url, payload = "https://api.tushare.pro", None
        for trust_env in [False] + ([True] if requests.utils.get_environ_proxies(url) else []):
            try:
                with EastmoneyClient._session(trust_env=trust_env) as session:
                    response = session.post(url, json={
                        "api_name": "dc_daily", "token": token,
                        "params": {"ts_code": f"{code}.DC", "start_date": start.replace("-", ""),
                                   "end_date": end.replace("-", "")},
                        "fields": "ts_code,trade_date,open,close,high,low,amount,pct_change",
                    }, timeout=(3, 8), allow_redirects=False)
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise RuntimeError("Tushare 服务响应异常")
                    payload = response.json()
                    break
            except (requests.RequestException, ValueError):
                continue
        if payload is None:
            raise RuntimeError("Tushare 日线连接失败或响应无效") from None
        if not isinstance(payload, dict):
            raise RuntimeError("Tushare 日线格式异常")
        if str(payload.get("code")) != "0":
            message = str(payload.get("msg", "")).lower()
            if str(payload.get("code")) == "40101":
                raise RuntimeError("Tushare 凭证无效，请检查 TUSHARE_TOKEN")
            if "频" in message or "limit" in message or "每分钟" in message:
                raise RuntimeError("Tushare 请求频率受限，稍后自动重试")
            if "权限" in message or "积分" in message or str(payload.get("code")) == "2002":
                raise RuntimeError("Tushare dc_daily 权限不足，请检查账户接口权限（官方要求6000积分）")
            if "token" in message:
                raise RuntimeError("Tushare 凭证无效，请检查 TUSHARE_TOKEN")
            raise RuntimeError("Tushare 日线服务暂不可用")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise RuntimeError("Tushare 日线格式异常")
        fields = data["fields"]
        required = {"ts_code", "trade_date", "open", "close", "high", "low"}
        if (not all(isinstance(field, str) for field in fields) or not required.issubset(fields)
                or len(set(fields)) != len(fields) or len(data["items"]) >= 2000):
            raise RuntimeError("Tushare 日线字段缺失或结果被截断")
        raw_rows, extras = [], {}
        for values in data["items"]:
            if not isinstance(values, list) or len(values) != len(fields):
                continue
            row = dict(zip(fields, values))
            if row["ts_code"] != f"{code}.DC":
                raise RuntimeError("Tushare 日线标的代码不匹配，未采用该数据")
            day = str(row["trade_date"])
            if not re.fullmatch(r"\d{8}", day):
                continue
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            raw_rows.append([day, row["open"], row["close"], row["high"], row["low"]])
            extras[day] = {}
            for key, field in (("amount", "amount"), ("pctChg", "pct_change")):
                try:
                    value = float(row.get(field))
                    if math.isfinite(value) and (key != "amount" or value >= 0):
                        extras[day][key] = value
                except (TypeError, ValueError):
                    pass
        rows = [{**r, **extras.get(r["date"], {})} for r in normalize_bars(raw_rows, start, end)]
        return {"rows": rows, "source": "Tushare · 东方财富概念指数日线",
                "url": "https://tushare.pro/document/2?doc_id=382"}
