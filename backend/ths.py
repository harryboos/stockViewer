"""Bounded reads of THS's public pages and explicitly dated quote resources.

This is a sampled, independent index universe, never a replacement for BK indices.
No browser challenge, login token, or generated cookie is used.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

CHINA_TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "同花顺公开行情（独立口径·有限样本）"
ROOT = "https://q.10jqka.com.cn"
QUOTE_ROOT = "https://d.10jqka.com.cn/v4/realhead"
BOARD_LIMITS = {"concept": 24, "industry": 12}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class _Page(HTMLParser):
    """Only read inputs, visible links and table cells; never execute page JS."""
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self.rows: list[list[str]] = []
        self._link: tuple[str, list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("id"):
            self.inputs[str(values["id"])] = str(values.get("value") or "")
        elif tag == "a":
            self._link = (str(values.get("href") or ""), [])
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._link[1].append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            self.links.append((self._link[0], "".join(self._link[1]).strip()))
            self._link = None
        elif tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


class ThsClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._pending: dict[str, Future] = {}
        self._mapping: dict[str, dict] = {}
        # Shared across board reads and research calls, rather than four new
        # workers per concept. Requests already in flight have short timeouts.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ths-public")

    def _cached(self, key: str, ttl: float, deadline: float, load: Callable,
                force: bool = False) -> Any:
        with self._lock:
            cached = self._cache.get(key)
            if not force and cached and time.monotonic() - cached[0] < ttl:
                return cached[1]
            pending = self._pending.get(key)
            owner = pending is None
            if owner:
                pending = Future()
                self._pending[key] = pending
        if not owner:
            return pending.result(timeout=max(0.01, deadline - time.monotonic()))
        try:
            value = load()
            with self._lock:
                self._cache[key] = (time.monotonic(), value)
                # Bound cached quotes/member pages for a long-running process.
                if len(self._cache) > 1024:
                    oldest = min(self._cache, key=lambda item: self._cache[item][0])
                    del self._cache[oldest]
            pending.set_result(value)
            return value
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)

    @staticmethod
    def _get_text(url: str, deadline: float) -> str:
        last_error = None
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("同花顺公开行情请求超时") from last_error
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.get(url, headers={
                        "Referer": ROOT + "/", "User-Agent": "Mozilla/5.0",
                        "Accept": "text/html,application/json,*/*",
                    }, timeout=(min(3, remaining / 2), min(5, remaining / 2)))
                    if response.status_code in {401, 403, 429}:
                        # Do not retry an explicit access restriction.
                        raise RuntimeError("同花顺公开页面限制访问，请稍后重试")
                    if response.status_code in {502, 503, 504}:
                        last_error = RuntimeError("同花顺公开行情上游暂不可用")
                        continue
                    response.raise_for_status()
                    # The public HTML is GBK; JSONP is UTF-8 with escaped names.
                    response.encoding = "gbk" if url.startswith(ROOT) else "utf-8"
                    return response.text
            except (requests.ConnectionError, requests.Timeout) as error:
                last_error = error
            except requests.RequestException as error:
                raise RuntimeError("同花顺公开行情响应异常") from error
        raise RuntimeError("同花顺公开行情连接失败或等待超时") from last_error

    @staticmethod
    def _parse_directory(text: str, kind: str) -> tuple[list[dict], int]:
        page = _Page(text)
        if kind == "concept":
            try:
                raw = json.loads(page.inputs.get("gnSection", ""))
            except (TypeError, ValueError) as error:
                raise RuntimeError("同花顺概念目录未返回有效公开数据") from error
            if not isinstance(raw, dict):
                raise RuntimeError("同花顺概念目录格式异常")
            rows = []
            for item in raw.values():
                if not isinstance(item, dict):
                    continue
                symbol, cid = str(item.get("platecode", "")), str(item.get("cid", ""))
                name, change = str(item.get("platename", "")).strip(), _number(item.get("199112"))
                if re.fullmatch(r"88\d{4}", symbol) and re.fullmatch(r"30\d{4}", cid) and name and change is not None:
                    rows.append({"code": "THS:" + symbol, "name": name, "rankChange": change,
                                 "url": f"{ROOT}/gn/detail/code/{cid}/"})
            total = len({match.group(1) for url, _ in page.links
                         if (match := re.fullmatch(r"https?://q\.10jqka\.com\.cn/gn/detail/code/(30\d{4})/?", url))})
        else:
            names = {name: match.group(1) for url, name in page.links
                     if (match := re.fullmatch(r"https?://q\.10jqka\.com\.cn/thshy/detail/code/(88\d{4})/?", url))}
            rows = []
            for row in page.rows:
                if len(row) != 12 or row[1] not in names or _number(row[2]) is None:
                    continue
                symbol = names[row[1]]
                rows.append({"code": "THS:" + symbol, "name": row[1], "rankChange": float(row[2]),
                             "url": f"{ROOT}/thshy/detail/code/{symbol}/",
                             "leaderName": row[9], "leaderChange": _number(row[11])})
            total = len(set(names.values()))
        unique = {row["code"]: row for row in rows}
        if not unique:
            raise RuntimeError("同花顺板块目录没有可用候选")
        return sorted(unique.values(), key=lambda row: (-row["rankChange"], row["code"])), max(total, len(unique))

    def _directory(self, kind: str, deadline: float, force: bool = False) -> tuple[list[dict], int]:
        path = "gn" if kind == "concept" else "thshy"
        result = self._cached(f"directory:{kind}", 60, deadline,
                              lambda: self._parse_directory(self._get_text(f"{ROOT}/{path}/", deadline), kind), force)
        with self._lock:
            self._mapping.update({row["code"]: row for row in result[0]})
        return result

    @staticmethod
    def _parse_quote(text: str, symbol: str) -> dict:
        callback = f"quotebridge_v4_realhead_{symbol}_last"
        raw = text.strip().rstrip(";")
        if not raw.startswith(callback + "(") or not raw.endswith(")"):
            raise RuntimeError("同花顺报价代码或响应格式不匹配")
        try:
            payload = json.loads(raw[len(callback) + 1:-1])
            item = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(item, dict) or str(item.get("5")) != symbol.split("_", 1)[1]:
                raise ValueError("code")
            # `time` is response generation time, including weekends/holidays.
            # Only updateTime dates the underlying market quote.
            update_time = str(item["updateTime"])
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", update_time):
                raise ValueError("quote time")
            quoted_at = datetime.fromisoformat(update_time)
            if quoted_at.tzinfo is None:
                quoted_at = quoted_at.replace(tzinfo=CHINA_TZ)
            if quoted_at > datetime.now(CHINA_TZ) + timedelta(minutes=5):
                raise ValueError("future quote")
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("同花顺报价缺少匹配的代码或有效行情时间") from error
        price, change, amount = (_number(item.get(key)) for key in ("10", "199112", "19"))
        if price is None or price <= 0 or change is None or amount is None or amount < 0:
            raise RuntimeError("同花顺报价缺少有效价格、涨幅或成交额")
        return {**item, "quotedAt": quoted_at, "price": price, "pctChg": change, "amount": amount}

    def _quote(self, symbol: str, deadline: float, force: bool = False) -> dict:
        if not re.fullmatch(r"(?:bk_88\d{4}|hs_\d{6})", symbol):
            raise ValueError("同花顺报价代码格式不正确")
        return self._cached(f"quote:{symbol}", 60, deadline,
                            lambda: self._parse_quote(self._get_text(f"{QUOTE_ROOT}/{symbol}/last.js", deadline), symbol), force)

    def _quotes(self, symbols: list[str], deadline: float, force: bool = False) -> dict[str, dict]:
        pending = {self._pool.submit(self._quote, symbol, deadline, force): symbol for symbol in dict.fromkeys(symbols)}
        done, remaining = wait(pending, timeout=max(0, deadline - time.monotonic()))
        for task in remaining:
            task.cancel()
        result = {}
        for task in done:
            try:
                result[pending[task]] = task.result()
            except (RuntimeError, ValueError, TimeoutError):
                pass
        return result

    def board_frame(self, kind: str, force: bool = False) -> pd.DataFrame:
        if kind not in BOARD_LIMITS:
            raise ValueError("kind 必须是 industry 或 concept")
        deadline = time.monotonic() + 24
        result = self._cached(f"board:{kind}", 60, deadline,
                              lambda: self._board_frame(kind, deadline, force), force)
        frame = result.copy(deep=True)
        frame.attrs = copy.deepcopy(result.attrs)
        return frame

    def _board_frame(self, kind: str, deadline: float, force: bool) -> pd.DataFrame:
        directory, total = self._directory(kind, deadline, force)
        candidates = directory[:BOARD_LIMITS[kind]]
        quotes = self._quotes(["bk_" + row["code"][4:] for row in candidates], deadline, force)
        rows = []
        for candidate in candidates:
            quote = quotes.get("bk_" + candidate["code"][4:])
            if quote is None:
                continue
            up, down = (_number(quote.get(key)) for key in ("38", "39"))
            if up is None or down is None or min(up, down) < 0 or not up.is_integer() or not down.is_integer():
                continue
            rows.append({"板块代码": candidate["code"], "板块名称": str(quote.get("name") or candidate["name"]),
                         "最新价": quote["price"], "涨跌幅": quote["pctChg"], "涨跌额": _number(quote.get("264648")),
                         "成交额": quote["amount"], "总市值": _number(quote.get("3541450")),
                         "换手率": _number(quote.get("1968584")), "上涨家数": int(up), "下跌家数": int(down),
                         # Directory leaders have no quote date. Only separately
                         # verified strong_stocks may populate dated leaders.
                         "领涨股票": "", "领涨股票-涨跌幅": None,
                         "行情时间": quote["quotedAt"].timestamp(), "来源链接": candidate["url"]})
        if not rows:
            raise RuntimeError("同花顺公开板块候选未返回带有效交易日期的报价")
        latest_date = max(datetime.fromtimestamp(row["行情时间"], CHINA_TZ).date() for row in rows)
        rows = [row for row in rows if datetime.fromtimestamp(row["行情时间"], CHINA_TZ).date() == latest_date]
        frame = pd.DataFrame(rows)
        scope = {"kind": "sample", "directoryTotal": total, "rankedCandidates": len(directory),
                 "requestedCount": len(candidates), "validCount": len(rows), "isComplete": False}
        frame.attrs = {"source": SOURCE, "sourceUrl": f"{ROOT}/{'gn' if kind == 'concept' else 'thshy'}/",
                       "quoteAsOf": datetime.fromtimestamp(min(row["行情时间"] for row in rows), CHINA_TZ).isoformat(),
                       "scope": scope, "coverage": scope,
                       "warnings": [f"同花顺公开{'概念' if kind == 'concept' else '行业'}样本：目录 {total} 个，"
                                    f"按公开涨幅选取 {len(candidates)} 个候选，取得 {len(rows)} 个有效报价；不代表全市场。"]}
        return frame

    def _members(self, code: str, deadline: float) -> tuple[list[str], str]:
        with self._lock:
            board = self._mapping.get(code)
        if board is None:
            self._directory("concept", deadline)
            with self._lock:
                board = self._mapping.get(code)
        if board is None:
            raise RuntimeError("同花顺公开目录中尚无该指数与概念页面的已核实映射")
        url = board["url"]
        page = _Page(self._get_text(url, deadline))
        if page.inputs.get("clid") != code[4:]:
            raise RuntimeError("同花顺成员页面与概念指数代码不匹配")
        symbols = []
        for row in page.rows:
            if len(row) == 14 and re.fullmatch(r"\d{6}", row[1]):
                symbols.append(row[1])
        if not symbols:
            raise RuntimeError("同花顺公开页面未返回有效成员股")
        return list(dict.fromkeys(symbols))[:10], url

    def strong_stocks(self, code: str, trade_date: str) -> list[dict]:
        if not re.fullmatch(r"THS:88\d{4}", code) or not re.fullmatch(r"\d{8}", trade_date):
            raise ValueError("同花顺概念代码或行情日期格式不正确")
        deadline = time.monotonic() + 20
        symbols, url = self._cached(f"members:{code}", 900, deadline, lambda: self._members(code, deadline))
        quotes = self._quotes(["hs_" + symbol for symbol in symbols], deadline)
        rows = []
        for symbol in symbols:
            quote = quotes.get("hs_" + symbol)
            if quote is None:
                continue
            name = str(quote.get("name") or "").strip()
            quote_date = quote["quotedAt"].strftime("%Y%m%d")
            if not name or "ST" in name.upper() or "退" in name or quote["pctChg"] <= 0 or quote["amount"] < 100_000_000 or quote_date != trade_date:
                continue
            rows.append({"code": symbol, "name": name, "price": quote["price"], "pctChg": quote["pctChg"],
                         "amount": quote["amount"], "turnoverRate": _number(quote.get("1968584")),
                         "tradeDate": quote_date, "quotedAt": quote["quotedAt"].isoformat(),
                         # The public quote does not identify its PE convention.
                         "peDynamic": None, "pb": _number(quote.get("592920")),
                         "marketCap": _number(quote.get("3541450")), "source": "同花顺公开成员股报价",
                         "sourceUrl": url, "quoteUrl": f"{QUOTE_ROOT}/hs_{symbol}/last.js"})
        return sorted(rows, key=lambda row: (-row["pctChg"], -row["amount"], row["code"]))[:8]


ths_client = ThsClient()
