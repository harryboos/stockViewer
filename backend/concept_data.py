"""Bounded, source-backed evidence for concept research; no generated market data."""
from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import Any

import requests

from . import database
from .data_sources import market_data, number_or_none
from .eastmoney import COMMON_PARAMS, SPOT_FIELD_MAP, EastmoneyClient
from .storage_policy import CONCEPT_HISTORY_CACHE_VERSION

MARKET_RESEARCH_LIMIT = 12
MARKET_RESEARCH_TIMEOUT = 40

# Search vocabulary, not claims that an event has happened.
TOPIC_TERMS = (
    (("制糖", "糖业", "白糖", "甘蔗"), ("白糖", "糖价", "甘蔗"), "天气 厄尔尼诺 巴西 印度 产量 供需"),
    (("船舶", "造船"), ("造船", "船舶"), "新船订单 运价 交付 造船周期"),
    (("粮食", "农业", "种植"), ("粮食", "农业"), "天气 干旱 产量 出口政策 粮价"),
    (("半导体", "芯片", "光刻"), ("半导体", "芯片"), "出口限制 国产替代 库存 订单 资本开支"),
    (("算力", "通信技术", "服务器", "液冷"), ("算力", "数据中心"), "AI资本开支 订单 扩产 供需"),
    (("机器人", "减速器"), ("机器人", "人形机器人"), "量产 订单 技术进展 产业政策"),
    (("稀土", "稀有金属"), ("稀土", "稀有金属"), "出口政策 配额 供应 价格"),
    (("石油", "油气"), ("石油", "天然气"), "地缘政治 供给 库存 油价"),
    (("券商", "证券"), ("券商", "证券"), "资本市场政策 并购 成交活跃度"),
)


def research_terms(name: str) -> tuple[list[str], str]:
    base = name.removesuffix("概念") or name
    for markers, aliases, topics in TOPIC_TERMS:
        if any(marker in name for marker in markers):
            return list(dict.fromkeys([base, *aliases])), topics
    return [base], "产业政策 供需 价格 订单 技术进展 现实事件"


def clean_text(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()[:limit]


class ConceptResearchClient(EastmoneyClient):
    def _json(self, urls: list[str], params: dict[str, Any], callback: str | None = None) -> dict:
        # Bounded retries; do not inherit the much longer bulk-market retry loop.
        for index, url in enumerate(urls):
            with self._session(trust_env=index == len(urls) - 1) as session:
                try:
                    response = session.get(url, params=params, timeout=(3, 8))
                    response.raise_for_status()
                    raw = response.text.strip().rstrip(";")
                    if callback and raw.startswith(f"{callback}(") and raw.endswith(")"):
                        raw = raw[len(callback) + 1:-1]
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or ("data" in payload and not payload["data"]):
                        raise ValueError("invalid payload")
                    return payload
                except (requests.RequestException, ValueError):
                    continue
        raise RuntimeError("概念研究数据源暂不可用")

    def daily_history(self, code: str, trade_date: str) -> list[dict]:
        if not re.fullmatch(r"BK\d+", code):
            raise ValueError("概念代码格式不正确")
        end = datetime.strptime(trade_date, "%Y%m%d")
        cache_key = f"concept_history:{code}:{trade_date}:v{CONCEPT_HISTORY_CACHE_VERSION}"
        try:
            saved = json.loads(database.get_meta(cache_key) or "null")
        except (ValueError, TypeError):
            saved = None
        # Closed-session bars can be reused; intraday bars should keep refreshing.
        if (isinstance(saved, dict) and saved.get("closed") and saved.get("rows")
                and trade_date < database.china_date().replace("-", "")):
            return saved["rows"]
        try:
            payload = self._json([
                "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            ], {
                "secid": f"90.{code}", "klt": "101", "fqt": "0",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "beg": (end - timedelta(days=60)).strftime("%Y%m%d"), "end": trade_date, "lmt": "40",
            })
        except RuntimeError:
            try:
                age = (datetime.now(database.CHINA_TZ) - datetime.fromisoformat(saved["fetchedAt"])).total_seconds()
            except (ValueError, KeyError, TypeError):
                age = 900
            if isinstance(saved, dict) and saved.get("rows") and 0 <= age < 900:
                return saved["rows"]
            raise
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
        rows = sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])
        if rows and rows[-1]["date"] == trade_date:
            now = datetime.now(database.CHINA_TZ)
            closed = trade_date < now.strftime("%Y%m%d") or now.strftime("%H:%M") >= "15:10"
            database.set_meta(cache_key, json.dumps({"fetchedAt": now.isoformat(), "closed": closed, "rows": rows}))
        return rows

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
                         "amount": amount, "turnoverRate": number_or_none(raw.get("f8")), "tradeDate": quote_date,
                         "peDynamic": number_or_none(raw.get("f9")), "pb": number_or_none(raw.get("f23")),
                         "marketCap": number_or_none(raw.get("f20"))})
        unique = {row["code"]: row for row in rows}
        return sorted(unique.values(), key=lambda row: (-row["pctChg"], -row["amount"], row["code"]))[:8]

    def news(self, name: str, code: str) -> list[dict]:
        terms, _ = research_terms(name)
        keyword = terms[1] if len(terms) > 1 else terms[0]
        params = {"cb": "conceptResearch", "param": json.dumps({
            "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": 20, "preTag": "", "postTag": ""}},
        }, ensure_ascii=False)}
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        payload = self._json([url, url], params, "conceptResearch")
        now = datetime.now(database.CHINA_TZ)
        result = {}
        for raw in (payload.get("result") or {}).get("cmsArticleWebOld") or []:
            title, content = clean_text(raw.get("title"), 180), clean_text(raw.get("content"), 1600)
            article = str(raw.get("code") or "")
            try:
                published = datetime.fromisoformat(str(raw.get("date")))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=database.CHINA_TZ)
            except ValueError:
                continue
            if (not article.isdigit() or not title or not any(term.casefold() in (title + content).casefold() for term in terms)
                    or not now - timedelta(days=30) <= published <= now):
                continue
            result[article] = {
                "id": f"{code}:news:{article}", "kind": "news", "title": title, "excerpt": content,
                "url": f"https://finance.eastmoney.com/a/{article}.html",
                "source": clean_text(raw.get("mediaName"), 60) or "东方财富资讯",
                "publishedAt": published.isoformat(timespec="seconds"),
            }
        return sorted(result.values(), key=lambda row: row["publishedAt"], reverse=True)[:5]


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


def collect_concept_evidence(*, forecast: bool = False) -> dict:
    overview = market_data.sector_overview(False)
    trade_date = overview["tradeDate"]
    if not str(overview.get("updatedAt") or "").startswith(database.china_date()):
        raise RuntimeError("板块数据仍是旧缓存，请先更新板块行情后再生成推荐")
    boards = {row["code"]: row for row in [
        *overview.get("researchConcepts", overview["conceptBoards"]),
        *(row for row in overview.get("turnoverBoards", []) if row["kind"] == "concept"),
    ] if not any(marker in row["name"] for marker in ("风格", "小盘股", "做市商"))}
    client = ConceptResearchClient()

    def candidate_view(board: dict, history: list[dict], stocks: list[dict], warnings: list[str]) -> dict:
        code = board["code"]
        metrics = recent_metrics(history, trade_date)
        if metrics["change5d"] is None:
            warnings.append("近5日趋势暂无法核验，预测的技术面依据不完整" if forecast else
                            "近5日趋势暂无法核验，按当日表现观察，不视为已确认的持续强势")
        if metrics["change10d"] is None:
            warnings.append("近10日涨幅暂缺")
        if metrics["historyAsOf"] and metrics["historyAsOf"] != trade_date:
            warnings.append(f"历史行情仅更新至 {metrics['historyAsOf']}，未计作当日趋势")
        if not stocks:
            warnings.append("暂未取得同一交易日上涨且成交额达到1亿元的成份股行情")
        sustained = any(metrics[key] is not None and metrics[key] > 0 for key in ("change5d", "change10d"))
        candidate = {
            "code": code, "name": board["name"], "tradeDate": trade_date,
            "pctChg": board["pctChg"], "amount": board.get("amount"),
            "amountDelta": board.get("amountDelta"),
            "breadth": board["breadth"], "upCount": board["upCount"], "downCount": board["downCount"],
            "mainNetInflow": board.get("mainNetInflow"), **metrics, "stocks": stocks, "warnings": warnings,
            "strengthStatus": "recent_strength" if sustained else "today_active",
            "evidence": [{"id": f"{code}:market", "kind": "data", "title": "概念行情与可用历史表现",
                          "source": "东方财富概念板块", "publishedAt": overview["updatedAt"],
                          "url": f"https://quote.eastmoney.com/bk/90.{code}.html", "excerpt": "见本卡片行情指标与成份股快照"}],
        }
        if forecast:
            from .forecast_data import add_forecast_metrics
            add_forecast_metrics(candidate, history, overview["updatedAt"])
        return candidate

    def enrich(board: dict) -> dict:
        warnings = []
        try:
            history = client.daily_history(board["code"], trade_date)
        except Exception:
            history = []
            warnings.append("历史行情接口暂不可用")
        try:
            stocks = client.strong_stocks(board["code"], trade_date)
        except Exception:
            stocks = []
            warnings.append("成份股行情接口暂不可用")
        return candidate_view(board, history, stocks, warnings)

    selected = list(boards.values())[:MARKET_RESEARCH_LIMIT]
    if not selected:
        raise RuntimeError("暂未取得可用的概念板块行情，请更新板块后重试")
    executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="concept-evidence")
    futures = {executor.submit(enrich, board): board for board in selected}
    candidates = []
    try:
        done, pending = wait(futures, timeout=MARKET_RESEARCH_TIMEOUT)
        for future in done:
            try:
                candidates.append(future.result())
            except (ValueError, KeyError, TypeError):
                candidates.append(candidate_view(futures[future], [], [], ["扩展行情格式异常，保留板块快照"]))
        for future in pending:
            future.cancel()
            candidates.append(candidate_view(futures[future], [], [], ["扩展行情收集超时，保留已取得的板块快照"]))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if not forecast:
        candidates = [row for row in candidates if row["strengthStatus"] == "recent_strength" or row["pctChg"] > 0]
    candidates.sort(key=lambda row: (row["strengthStatus"] == "recent_strength", bool(row["stocks"]),
                                     row["change5d"] if row["change5d"] is not None else row["pctChg"], row["code"]), reverse=True)
    return {"tradeDate": trade_date, "dataAsOf": overview["updatedAt"],
            "comparisonUniverse": {"asOf": overview["updatedAt"],
                "scope": "预测当时可获取的全部有效概念" if overview.get("conceptUniverse") else "预测当时可获取的概念候选（非全市场）",
                "boards": overview.get("conceptUniverse") or [{"code": row["code"], "name": row["name"]} for row in selected]},
            "scope": (f"从概念涨幅榜与成交额榜选取{len(selected)}个活跃方向比较（非全市场穷举）。"
                      + ("保留短期回调方向，预测的是候选间的相对强势机会。" if forecast else
                         "优先近5/10日正收益方向；历史缺失或短期回调时，仅将当日上涨方向列为活跃观察。")),
            "warnings": overview.get("warnings", []), "candidates": candidates}
