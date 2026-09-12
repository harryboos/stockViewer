"""Forecast features computed from dated quotes, never filled by the model."""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta

from .data_sources import number_or_none


def forecast_window(run_date: str) -> dict:
    start = date.fromisoformat(run_date)
    return {"generatedOn": run_date, "startDate": (start + timedelta(days=1)).isoformat(),
            "endDate": (start + timedelta(days=15)).isoformat(), "calendarDays": 15}


def technical_metrics(history: list[dict], trade_date: str) -> dict:
    bars = sorted({row["date"]: row for row in history
                   if row["date"] <= trade_date and (number_or_none(row.get("close")) or 0) > 0}.values(),
                  key=lambda row: row["date"])
    result = dict.fromkeys(("lastClose", "ma5", "ma10", "ma20", "rsi14", "distanceTo20dHigh", "amountRatio5d"))
    result["historyAsOf"] = bars[-1]["date"] if bars else None
    if not bars or bars[-1]["date"] != trade_date:
        return result
    closes = [float(row["close"]) for row in bars]
    result["lastClose"] = closes[-1]
    for size in (5, 10, 20):
        if len(closes) >= size:
            result[f"ma{size}"] = round(statistics.mean(closes[-size:]), 2)
    if len(closes) >= 20:
        result["distanceTo20dHigh"] = round((closes[-1] / max(closes[-20:]) - 1) * 100, 2)
    if len(closes) >= 15:
        changes = [right - left for left, right in zip(closes, closes[1:])]
        gain = statistics.mean(max(change, 0) for change in changes[:14])
        loss = statistics.mean(max(-change, 0) for change in changes[:14])
        for change in changes[14:]:
            gain, loss = (gain * 13 + max(change, 0)) / 14, (loss * 13 + max(-change, 0)) / 14
        result["rsi14"] = round(100 - 100 / (1 + gain / loss), 2) if loss else (100.0 if gain else 50.0)
    # Exclude the snapshot session even after close: the two windows are always full sessions.
    completed = bars[:-1]
    if len(completed) >= 10:
        amounts = [number_or_none(row.get("amount")) for row in completed[-10:]]
        if all(value is not None and value > 0 for value in amounts):
            result["amountRatio5d"] = round(sum(amounts[5:]) / sum(amounts[:5]), 2)
    return result


def add_forecast_metrics(candidate: dict, history: list[dict], updated_at: str) -> None:
    code = candidate["code"]
    technical = technical_metrics(history, candidate["tradeDate"])
    candidate["technicalData"] = technical
    valuations = [{key: stock.get(key) for key in ("code", "name", "peDynamic", "pb", "marketCap")}
                  for stock in candidate["stocks"]]
    covered = sum(any(row[key] is not None for key in ("peDynamic", "pb", "marketCap")) for row in valuations)
    candidate["fundamentalData"] = {
        "coverage": "valuation_only" if covered else "missing", "sampleSize": covered, "valuations": valuations,
        "note": "仅为已取得的强势成份股估值快照，不代表全概念；未接入完整财报，收入、利润、订单与供需需结合有日期的公告或新闻核验。动态市盈率不等于TTM市盈率，负值不代表低估。",
    }
    for suffix, title, data in (("technical", "概念技术指标", technical),
                                 ("fundamentals", "成份股估值与基本面数据覆盖", candidate["fundamentalData"])):
        candidate["evidence"].append({
            "id": f"{code}:{suffix}", "kind": "data", "title": title,
            "source": "东方财富行情；技术指标由历史收盘价计算", "publishedAt": updated_at,
            "url": f"https://quote.eastmoney.com/bk/90.{code}.html",
            "excerpt": json.dumps(data, ensure_ascii=False),
        })
    if technical["ma20"] is None:
        candidate["warnings"].append("技术历史不足或日期不一致，20日趋势无法完整验证")
    candidate["warnings"].append("基本面仅有成份股估值与检索资料，未覆盖完整财务报表")
