"""Reproducible relative strength and overlap inputs; no network or trading rules.

The supplied histories are observations, not an exchange calendar. Dates must
align within each window; unavailable prices are never skipped or interpolated.
"""
from __future__ import annotations

import itertools
import math
import statistics
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit


BENCHMARKS = (("sh000001", "上证指数"), ("sh000688", "科创50"))
LIMITS = [
    "仅使用截至行情日期已提供的日线；historyAsOf 为价格观测日期，不保证该日已收盘。",
    "日期连续性只按已提供的基准和概念日期交叉核对，未经交易所完整交易日历核验；双方同时缺失的交易日仍可能无法识别。",
    "相关性与样本重合仅提示共同风险，不证明因果关系，不构成行业权重或买卖阈值。",
]


def _date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, pattern)
        except ValueError:
            continue
        # strptime also accepts abbreviated months/days; require canonical input.
        if parsed.strftime(pattern) == value:
            return parsed.date().isoformat()
    return None


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _history(rows: object, cutoff: str) -> dict:
    prices: dict[str, float] = {}
    dates: set[str] = set()
    invalid: set[str] = set()
    sources: set[str] = set()
    urls: set[str] = set()
    excluded = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            excluded += 1
            continue
        day = _date(row.get("date"))
        if day is None or day > cutoff:
            excluded += 1
            continue
        dates.add(day)
        close = _finite(row.get("close"))
        if close is None or close <= 0 or (day in prices and prices[day] != close):
            invalid.add(day)
        elif day not in invalid:
            prices[day] = close
        if isinstance(row.get("historySource"), str) and row["historySource"].strip():
            sources.add(row["historySource"].strip())
        if isinstance(row.get("historyUrl"), str) and row["historyUrl"].strip():
            urls.add(row["historyUrl"].strip())
    for day in invalid:
        prices.pop(day, None)
    return {"prices": prices, "dates": dates, "invalid": invalid,
            "asOf": max(dates, default=None), "sources": sorted(sources), "urls": sorted(urls),
            "excludedRows": excluded}


def _source(history: dict) -> dict:
    return {"names": history["sources"], "urls": history["urls"],
            "note": "调用方提供的日线" if not history["sources"] else "日线来源由调用方随原始行情提供"}


def _coverage(history: dict) -> dict:
    return {"observedDateCount": len(history["dates"]), "validCloseCount": len(history["prices"]),
            "invalidPriceDates": sorted(history["invalid"]), "excludedRowCount": history["excludedRows"]}


def _return(first: float, last: float) -> float | None:
    return _finite((last / first - 1) * 100)


def _relative_period(concept: dict, benchmark: dict, calendar: set[str], sessions: int) -> dict:
    result = {"status": "unavailable", "startDate": None, "endDate": None,
              "sessionCount": sessions, "priceCount": 0, "conceptReturnPct": None,
              "benchmarkReturnPct": None, "excessPctPoints": None, "reason": None}
    if not concept["asOf"] or not benchmark["asOf"]:
        result["reason"] = "概念或基准日线缺失"
        return result
    if concept["asOf"] != benchmark["asOf"]:
        result["reason"] = "概念与基准最新观测日期不同，未跨日期计算超额"
        return result
    end = concept["asOf"]
    days = sorted(day for day in calendar if day <= end)[-sessions - 1:]
    result.update(startDate=days[0] if days else None, endDate=end, priceCount=len(days))
    if len(days) < sessions + 1:
        result["reason"] = f"不足 {sessions + 1} 个同日期价格观测"
        return result
    missing = [day for day in days if day not in concept["prices"] or day not in benchmark["prices"]]
    if missing:
        result["reason"] = "窗口内缺少同日期有效收盘价：" + "、".join(missing)
        return result
    concept_return = _return(concept["prices"][days[0]], concept["prices"][end])
    benchmark_return = _return(benchmark["prices"][days[0]], benchmark["prices"][end])
    excess = _finite(concept_return - benchmark_return) if concept_return is not None and benchmark_return is not None else None
    if excess is None:
        result["reason"] = "价格计算超出有效数值范围"
        return result
    result.update(status="available", conceptReturnPct=round(concept_return, 4),
                  benchmarkReturnPct=round(benchmark_return, 4), excessPctPoints=round(excess, 4))
    return result


def _correlation(left: dict, right: dict, benchmark_dates: set[str]) -> dict:
    result = {"status": "unavailable", "value": None, "startDate": None, "endDate": None,
              "returnCount": 0, "requestedReturnCount": 20, "smallSample": True,
              "source": "同日期相邻收盘价的日收益率 Pearson 相关系数", "reason": None}
    if not left["asOf"] or not right["asOf"] or not left["prices"] or not right["prices"]:
        result["reason"] = "至少一个概念缺少有效日线"
        return result
    if left["asOf"] != right["asOf"]:
        result["reason"] = "概念最新观测日期不同，未跳过末端缺失日期"
        return result
    end = left["asOf"]
    calendar = benchmark_dates | left["dates"] | right["dates"]
    days = sorted(day for day in calendar if day <= end)[-21:]
    first_common_start = max(min(left["prices"]), min(right["prices"]))
    days = [day for day in days if day >= first_common_start]
    result.update(startDate=days[0] if days else None, endDate=end, smallSample=len(days) < 21)
    if len(days) < 11:
        result["reason"] = "不足 10 个同日期连续日收益配对"
        return result
    missing = [day for day in days if day not in left["prices"] or day not in right["prices"]]
    if missing:
        result["reason"] = "窗口内日线缺失，未拼接跨日收益：" + "、".join(missing)
        return result
    result["returnCount"] = len(days) - 1
    changes = []
    for history in (left, right):
        changes.append([_return(history["prices"][before], history["prices"][after])
                        for before, after in zip(days, days[1:])])
    if any(value is None for series in changes for value in series):
        result["reason"] = "价格计算超出有效数值范围"
        return result
    # Scaling before centering avoids overflow for large but finite inputs.
    centered = []
    for series in changes:
        scale = max(abs(value) for value in series)
        if max(series) - min(series) <= max(1e-12, scale * 1e-12):
            result["reason"] = "至少一个概念日收益无有效波动，相关系数无定义"
            return result
        normalized = [value / scale for value in series] if scale else [0.0] * len(series)
        mean = statistics.mean(normalized)
        centered.append([value - mean for value in normalized])
    denominator = math.sqrt(sum(value * value for value in centered[0]) * sum(value * value for value in centered[1]))
    if denominator == 0:
        result["reason"] = "至少一个概念日收益无有效波动，相关系数无定义"
        return result
    value = sum(a * b for a, b in zip(*centered)) / denominator
    result.update(status="available", value=round(max(-1.0, min(1.0, value)), 6),
                  reason="配对日收益不足 20 个，短样本仅供观察" if len(days) < 21 else None)
    return result


def _stock_codes(candidate: dict) -> set[str]:
    codes = set()
    for row in candidate.get("stocks") or []:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 999999:
            code = str(code).zfill(6)
        if isinstance(code, str):
            code = code.strip()
            if len(code) == 6 and code.isascii() and code.isdigit():
                codes.add(code)
    return codes


def _sample_overlap(left: dict, right: dict) -> dict:
    a, b = _stock_codes(left), _stock_codes(right)
    shared = a & b
    return {"leftSampleSize": len(a), "rightSampleSize": len(b), "sharedCodes": sorted(shared),
            "sharedCount": len(shared) if a and b else None,
            "jaccardPct": round(len(shared) / len(a | b) * 100, 2) if a and b else None,
            "overlapOfSmallerSamplePct": round(len(shared) / min(len(a), len(b)) * 100, 2) if a and b else None,
            "source": "当前取得的强势股样本代码交集",
            "note": "仅为强势股样本重合，不代表全部成份股重合、真实指数权重或交易暴露；缺少任一侧样本时未知。"}


def _news_urls(candidate: dict) -> set[str]:
    urls = set()
    for item in candidate.get("evidence") or []:
        if not isinstance(item, dict) or item.get("kind") != "news" or not isinstance(item.get("url"), str):
            continue
        try:
            parsed = urlsplit(item["url"].strip())
            if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                continue
            urls.add(urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")))
        except ValueError:
            continue
    return urls


def enrich_risk_context(candidates: list[dict], histories_by_code: dict[str, list[dict]],
                        benchmark_histories: dict[str, list[dict]], trade_date: str,
                        *, include_pairs: bool = True) -> dict:
    """Return global pair risk data and per-candidate relative strength, without mutation.

    ``benchmark_histories`` uses ``sh000001`` and ``sh000688`` keys. Histories use
    ``date`` and positive ``close`` values, optionally ``historySource/historyUrl``.
    Invalid/future dates are excluded; conflicting duplicate prices invalidate
    that date. No dates or returns are invented when data is unavailable. Set
    ``include_pairs=False`` for broad candidate recall without quadratic work.
    """
    cutoff = _date(trade_date)
    if cutoff is None:
        raise ValueError("trade_date must be YYYYMMDD or YYYY-MM-DD")
    unique_candidates = {row["code"]: row for row in candidates
                         if isinstance(row, dict) and isinstance(row.get("code"), str) and row["code"]}
    histories = {code: _history(histories_by_code.get(code), cutoff) for code in unique_candidates}
    benchmarks = {code: _history(benchmark_histories.get(code), cutoff) for code, _ in BENCHMARKS}
    benchmark_dates = set().union(*(history["dates"] for history in benchmarks.values()))
    relative = {}
    for code, history in histories.items():
        relative[code] = {
            "historyAsOf": history["asOf"], "source": _source(history), "coverage": _coverage(history),
            "benchmarks": [
                {"code": benchmark_code, "name": name, "historyAsOf": benchmark["asOf"],
                 "source": _source(benchmark), "coverage": _coverage(benchmark),
                 "periods": {f"{sessions}d": _relative_period(history, benchmark, benchmark_dates | history["dates"], sessions)
                             for sessions in (5, 10)}}
                for benchmark_code, name in BENCHMARKS for benchmark in (benchmarks[benchmark_code],)
            ],
            "limits": LIMITS[:2],
        }
    pairs = []
    for left_code, right_code in itertools.combinations(unique_candidates, 2) if include_pairs else ():
        left, right = unique_candidates[left_code], unique_candidates[right_code]
        left_urls, right_urls = _news_urls(left), _news_urls(right)
        pairs.append({
            "leftCode": left_code, "rightCode": right_code,
            "correlation20d": _correlation(histories[left_code], histories[right_code], benchmark_dates),
            "strongStockSampleOverlap": _sample_overlap(left, right),
            "sharedNewsSources": {"urls": sorted(left_urls & right_urls), "count": len(left_urls & right_urls),
                                  "leftSourceCount": len(left_urls), "rightSourceCount": len(right_urls),
                                  "source": "两概念新闻证据的相同 URL 交集（仅移除 fragment）",
                                  "note": "同一来源页面不等于同一事件；不同 URL 不证明事件独立，未进行事件语义聚类。"},
        })
    return {
        "riskContext": {"version": "risk-context-v1", "asOf": cutoff,
                        "source": "调用方提供的概念/基准日线、强势股样本与已筛选新闻证据",
                        "coverage": {"candidateCount": len(unique_candidates), "pairCount": len(pairs),
                                     "pairAnalysisIncluded": include_pairs,
                                     "availableCorrelationPairCount": sum(pair["correlation20d"]["status"] == "available" for pair in pairs),
                                     "benchmarkObservedDateCount": len(benchmark_dates)},
                        "limits": list(LIMITS), "pairs": pairs},
        "relativeStrengthByCode": relative,
    }
