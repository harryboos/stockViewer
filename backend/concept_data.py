"""Bounded, source-backed evidence for concept research; no generated market data."""
from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import requests

from . import database
from .data_sources import market_data, number_or_none
from .eastmoney import COMMON_PARAMS, SPOT_FIELD_MAP, EastmoneyClient


def clean_text(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()[:limit]


class ConceptResearchClient(EastmoneyClient):
    def _json(self, urls: list[str], params: dict[str, Any], callback: str | None = None) -> dict:
        # Bounded retries; do not inherit the much longer bulk-market retry loop.
        for index, url in enumerate(urls):
            with self._session(trust_env=index == len(urls) - 1) as session:
                try:
                    response = session.get(url, params=params, timeout=(4, 12))
                    response.raise_for_status()
                    raw = response.text.strip().rstrip(";")
                    if callback and raw.startswith(f"{callback}(") and raw.endswith(")"):
                        raw = raw[len(callback) + 1:-1]
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("invalid payload")
                    return payload
                except (requests.RequestException, ValueError):
                    continue
        raise RuntimeError("概念研究数据源暂不可用")

    def daily_history(self, code: str, trade_date: str) -> list[dict]:
        if not re.fullmatch(r"BK\d+", code):
            raise ValueError("概念代码格式不正确")
        end = datetime.strptime(trade_date, "%Y%m%d")
        payload = self._json([
            "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        ], {
            "secid": f"90.{code}", "klt": "101", "fqt": "0",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "beg": (end - timedelta(days=60)).strftime("%Y%m%d"), "end": trade_date, "lmt": "40",
        })
        data = payload.get("data") or {}
        rows = []
        for raw in data.get("klines") or []:
            fields = str(raw).split(",")
            if len(fields) < 9:
                continue
            date_key = fields[0].replace("-", "")
            close = number_or_none(fields[2])
            if re.fullmatch(r"\d{8}", date_key) and date_key <= trade_date and close and close > 0:
                rows.append({"date": date_key, "close": close, "amount": number_or_none(fields[6]),
                             "pctChg": number_or_none(fields[8])})
        return sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])

    def strong_stocks(self, code: str, trade_date: str) -> list[dict]:
        urls = [f"https://{host}/api/qt/clist/get" for host in self._hosts("29")[:2]]
        payload = self._json(urls, {
            **COMMON_PARAMS, "fs": f"b:{code} f:!50", "fid": "f3", "pn": "1", "pz": "100",
            "fields": ",".join(SPOT_FIELD_MAP.values()),
        })
        rows = []
        for raw in (payload.get("data") or {}).get("diff") or []:
            symbol, name = str(raw.get("f12") or ""), clean_text(raw.get("f14"), 50)
            price, change, amount = (number_or_none(raw.get(field)) for field in ("f2", "f3", "f6"))
            timestamp = number_or_none(raw.get("f124"))
            try:
                quote_date = datetime.fromtimestamp(timestamp or 0, database.CHINA_TZ).strftime("%Y%m%d")
            except (ValueError, OverflowError, OSError):
                continue
            if (not re.fullmatch(r"\d{6}", symbol) or not name or "ST" in name.upper() or "退" in name
                    or price is None or price <= 0 or change is None or change <= 0
                    or amount is None or amount < 100_000_000 or quote_date != trade_date):
                continue
            rows.append({"code": symbol, "name": name, "price": price, "pctChg": change,
                         "amount": amount, "turnoverRate": number_or_none(raw.get("f8")), "tradeDate": quote_date})
        unique = {row["code"]: row for row in rows}
        return sorted(unique.values(), key=lambda row: (-row["pctChg"], -row["amount"], row["code"]))[:8]

    def news(self, name: str, code: str) -> list[dict]:
        keyword = name.removesuffix("概念") or name
        params = {"cb": "conceptResearch", "param": json.dumps({
            "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": 10, "preTag": "", "postTag": ""}},
        }, ensure_ascii=False)}
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        payload = self._json([url, url], params, "conceptResearch")
        now = datetime.now(database.CHINA_TZ)
        result = {}
        for raw in (payload.get("result") or {}).get("cmsArticleWebOld") or []:
            title, content = clean_text(raw.get("title"), 160), clean_text(raw.get("content"), 280)
            article = str(raw.get("code") or "")
            try:
                published = datetime.fromisoformat(str(raw.get("date")))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=database.CHINA_TZ)
            except ValueError:
                continue
            if (not article.isdigit() or not title or keyword.casefold() not in (title + content).casefold()
                    or not now - timedelta(days=7) <= published <= now):
                continue
            result[article] = {
                "id": f"{code}:news:{article}", "kind": "news", "title": title, "excerpt": content,
                "url": f"https://finance.eastmoney.com/a/{article}.html",
                "source": clean_text(raw.get("mediaName"), 60) or "东方财富资讯",
                "publishedAt": published.isoformat(timespec="seconds"),
            }
        return sorted(result.values(), key=lambda row: row["publishedAt"], reverse=True)[:4]


def recent_metrics(history: list[dict], trade_date: str) -> dict:
    """Use published bars only; stale history must never be presented as today's return."""
    bars = sorted({row["date"]: row for row in history if row["date"] <= trade_date}.values(),
                  key=lambda row: row["date"])
    def change(sessions: int) -> float | None:
        if len(bars) <= sessions or bars[-1]["date"] != trade_date:
            return None
        return round((bars[-1]["close"] / bars[-sessions - 1]["close"] - 1) * 100, 2)
    return {"change5d": change(5), "change10d": change(10),
            "historySessions": len(bars), "historyAsOf": bars[-1]["date"] if bars else None}


def collect_concept_evidence() -> dict:
    overview = market_data.sector_overview(False)
    trade_date = overview["tradeDate"]
    if not str(overview.get("updatedAt") or "").startswith(database.china_date()):
        raise RuntimeError("板块数据仍是旧缓存，请先更新板块行情后再生成推荐")
    boards = {row["code"]: row for row in [
        *overview["conceptBoards"],
        *(row for row in overview.get("turnoverBoards", []) if row["kind"] == "concept"),
    ]}
    client = ConceptResearchClient()

    def enrich(board: dict) -> dict | None:
        code = board["code"]
        warnings = []
        try:
            history = client.daily_history(code, trade_date)
        except Exception:
            history = []
        metrics = recent_metrics(history, trade_date)
        if metrics["change5d"] is None or metrics["change5d"] <= 0:
            return None
        if metrics["change10d"] is None:
            warnings.append("近10个交易日历史不足")
        try:
            stocks = client.strong_stocks(code, trade_date)
        except Exception:
            stocks = []
        if not stocks:
            return None
        try:
            news = client.news(board["name"], code)
        except Exception:
            news = []
        if not news:
            warnings.append("未取得近7天相关资讯，事件催化因素只能作为待验证假设")
        amount, previous = board.get("amount"), board.get("previousAmount")
        return {
            "code": code, "name": board["name"], "tradeDate": trade_date,
            "pctChg": board["pctChg"], "amount": amount,
            "amountDelta": amount - previous if amount is not None and previous is not None else None,
            "breadth": board["breadth"], "upCount": board["upCount"], "downCount": board["downCount"],
            "mainNetInflow": board["mainNetInflow"], **metrics, "stocks": stocks, "warnings": warnings,
            "evidence": [{"id": f"{code}:market", "kind": "data", "title": "概念行情与近5/10日表现",
                          "source": "东方财富概念板块", "publishedAt": overview["updatedAt"],
                          "url": f"https://quote.eastmoney.com/bk/90.{code}.html", "excerpt": "见本卡片行情指标与成份股快照"}, *news],
        }

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="concept-evidence") as executor:
        candidates = [row for row in executor.map(enrich, list(boards.values())[:8]) if row]
    if not candidates:
        raise RuntimeError("暂无同时具备近5日正收益、有效历史与当日强势股的概念，请稍后重试")
    return {"tradeDate": trade_date, "dataAsOf": overview["updatedAt"],
            "scope": "在当日概念强度榜与成交额榜的最多8个活跃概念中，比较近5/10个交易日表现；不代表全市场穷举。",
            "warnings": overview.get("warnings", []), "candidates": candidates}
