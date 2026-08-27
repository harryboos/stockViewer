from __future__ import annotations

import math
import statistics
from typing import Any, Callable

from .data_sources import market_data


FactorRow = dict[str, Any]
ScoreFunction = Callable[[FactorRow], float]


def _recent_bars(spot: FactorRow, history: list[FactorRow]) -> list[FactorRow]:
    bars = [dict(row) for row in history]
    spot_date = str(spot.get("tradeDate") or "")
    if not spot_date or spot.get("close") is None:
        return bars
    spot_bar = {
        "date": spot_date,
        "open": spot.get("open"),
        "high": spot.get("high"),
        "low": spot.get("low"),
        "close": spot.get("close"),
        "pctChg": spot.get("pctChg"),
        "vol": spot.get("vol"),
        "amount": spot.get("amount"),
        "peTtm": spot.get("peTtm"),
        "pb": spot.get("pb"),
    }
    if bars and bars[-1].get("date") == spot_date:
        bars[-1].update({key: value for key, value in spot_bar.items() if value is not None})
    elif not bars or str(bars[-1].get("date") or "") < spot_date:
        bars.append(spot_bar)
    return bars


def wilder_rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = statistics.fmean(gains[:period])
    average_loss = statistics.fmean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:], strict=False):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def _recent_bullish_cross_days(
    values: list[float],
    fast: int = 10,
    slow: int = 30,
    lookback: int = 5,
) -> int | None:
    if len(values) < slow + lookback + 1:
        return None
    for days_ago in range(lookback + 1):
        end = len(values) - days_ago
        previous_end = end - 1
        fast_now = statistics.fmean(values[end - fast:end])
        slow_now = statistics.fmean(values[end - slow:end])
        fast_previous = statistics.fmean(values[previous_end - fast:previous_end])
        slow_previous = statistics.fmean(values[previous_end - slow:previous_end])
        if fast_now > slow_now and fast_previous <= slow_previous:
            return days_ago
    return None


def build_factor_row(
    spot: FactorRow,
    history: list[FactorRow] | None = None,
) -> FactorRow | None:
    history = history if history is not None else market_data.history(spot["symbol"])
    if len(history) < 60:
        return None
    bars = _recent_bars(spot, history)
    closes = [float(row["close"]) for row in bars if row.get("close")]
    if len(closes) < 60:
        return None
    latest = closes[-1]

    def momentum(sessions: int) -> float | None:
        if len(closes) <= sessions:
            return None
        base = closes[-sessions - 1]
        return (latest / base - 1) * 100 if base else None

    changes = [float(row["pctChg"]) for row in bars[-120:] if row.get("pctChg") is not None]
    amounts = [float(row["amount"]) for row in bars[-20:] if row.get("amount") is not None]
    volumes = [float(row["vol"]) for row in bars[-20:] if row.get("vol") is not None]
    previous_amount = float(bars[-2]["amount"]) if len(bars) >= 2 and bars[-2].get("amount") is not None else None
    average_amount_20 = statistics.fmean(amounts) if len(amounts) == 20 else None
    previous_volumes = [float(row["vol"]) for row in bars[-6:-1] if row.get("vol") is not None]
    volume_ratio = spot.get("volumeRatio")
    volume_ratio_mode = "盘中量比"
    if volume_ratio is None and bars[-1].get("vol") is not None and len(previous_volumes) == 5:
        average_volume_5 = statistics.fmean(previous_volumes)
        volume_ratio = float(bars[-1]["vol"]) / average_volume_5 if average_volume_5 > 0 else None
        volume_ratio_mode = "日线估算量比"
    today_pct_chg = bars[-1].get("pctChg")
    if today_pct_chg is None and len(closes) >= 2 and closes[-2]:
        today_pct_chg = (closes[-1] / closes[-2] - 1) * 100
    volatility = statistics.stdev(changes) if len(changes) > 1 else None
    ma5 = statistics.fmean(closes[-5:])
    ma10 = statistics.fmean(closes[-10:])
    ma20 = statistics.fmean(closes[-20:])
    ma30 = statistics.fmean(closes[-30:])
    average_volume_20 = statistics.fmean(volumes) if len(volumes) == 20 else None
    average_volume_5 = statistics.fmean(volumes[-5:]) if len(volumes) == 20 else None
    volume_trend_5_20 = (
        average_volume_5 / average_volume_20
        if average_volume_5 is not None and average_volume_20 is not None and average_volume_20 > 0
        else None
    )
    rsi14 = wilder_rsi(closes)
    rsi14_three_days_ago = wilder_rsi(closes[:-3])
    return {
        "tsCode": spot["tsCode"],
        "symbol": spot["symbol"],
        "name": spot["name"],
        "industry": spot.get("industry") or "行业未分类",
        "latestClose": latest,
        "currentPrice": latest,
        "todayPctChg": float(today_pct_chg) if today_pct_chg is not None else None,
        "previousAmount": previous_amount,
        "change5d": momentum(5),
        "change10d": momentum(10),
        "change20d": momentum(20),
        "change60d": momentum(60),
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma30": ma30,
        "ma60": statistics.fmean(closes[-60:]),
        "ma20Gap": (latest / ma20 - 1) * 100 if ma20 else None,
        "maSpread10_30": (ma10 / ma30 - 1) * 100 if ma30 else None,
        "recentBullishCrossDays": _recent_bullish_cross_days(closes),
        "volumeTrend5To20": volume_trend_5_20,
        "rsi14": rsi14,
        "rsi14ThreeDaysAgo": rsi14_three_days_ago,
        "rsiRecovery": (
            rsi14 - rsi14_three_days_ago
            if rsi14 is not None and rsi14_three_days_ago is not None
            else None
        ),
        "averageAmount20": average_amount_20,
        "volumeRatio": float(volume_ratio) if volume_ratio is not None else None,
        "volumeRatioMode": volume_ratio_mode,
        "isSt": "ST" in str(spot["name"]).upper() or "退" in str(spot["name"]),
        "isStarMarket": str(spot["symbol"]).startswith(("688", "689")) or spot.get("market") == "科创板",
        "momentum6m": momentum(120),
        "momentum12m": momentum(240),
        "volatility": volatility,
        "averageAmount": average_amount_20 if average_amount_20 is not None else spot.get("amount"),
        "peTtm": (
            spot.get("peTtm") if spot.get("peTtm") and spot["peTtm"] > 0
            else bars[-1].get("peTtm") if bars[-1].get("peTtm") and bars[-1]["peTtm"] > 0
            else None
        ),
        "pb": (
            spot.get("pb") if spot.get("pb") and spot["pb"] > 0
            else bars[-1].get("pb") if bars[-1].get("pb") and bars[-1]["pb"] > 0
            else None
        ),
        "dividendYield": market_data.dividend_yield(spot["symbol"]),
        "totalMv": spot.get("totalMv"),
    }


def _matches_volume_breakout(row: FactorRow) -> bool:
    required = (
        row.get("previousAmount"), row.get("change10d"), row.get("averageAmount20"),
        row.get("volumeRatio"), row.get("todayPctChg"), row.get("currentPrice"),
    )
    if any(value is None for value in required):
        return False
    return bool(
        float(row["previousAmount"]) > 1_000_000_000
        and float(row["change10d"]) > 30
        and float(row["averageAmount20"]) > 1_200_000_000
        and float(row["volumeRatio"]) < 1.5
        and float(row["todayPctChg"]) < 11
        and float(row["currentPrice"]) < 90
        and not row.get("isSt")
        and not row.get("isStarMarket")
    )


def volume_breakout_picks(rows: list[FactorRow]) -> list[FactorRow]:
    matched = sorted(
        (row for row in rows if _matches_volume_breakout(row)),
        key=lambda row: float(row["change10d"]),
        reverse=True,
    )
    return [
        {
            "code": row["symbol"],
            "name": row["name"],
            "industry": row["industry"],
            "score": round(float(row["change10d"])),
            "reason": (
                f"10日 +{float(row['change10d']):.1f}% · 昨额 {float(row['previousAmount']) / 100_000_000:.1f}亿 · "
                f"20日均额 {float(row['averageAmount20']) / 100_000_000:.1f}亿 · "
                f"量比 {float(row['volumeRatio']):.2f}（{row['volumeRatioMode']}） · "
                f"今日 {float(row['todayPctChg']):+.1f}% · 现价 {float(row['currentPrice']):.2f}"
            ),
        }
        for row in matched
    ]


def percentile(rows: list[FactorRow], key: str, higher_is_better: bool = True) -> ScoreFunction:
    values = sorted(
        float(row[key])
        for row in rows
        if row.get(key) is not None and math.isfinite(float(row[key]))
    )

    def score(row: FactorRow) -> float:
        value = row.get(key)
        if value is None or len(values) < 2:
            return 40.0
        rank = max(index for index, item in enumerate(values) if item <= float(value))
        result = rank / (len(values) - 1) * 100
        return result if higher_is_better else 100 - result

    return score


def top_picks(
    rows: list[FactorRow],
    scorer: ScoreFunction,
    reason: Callable[[FactorRow], str],
) -> list[FactorRow]:
    ranked = sorted(
        ((row, round(scorer(row))) for row in rows),
        key=lambda item: item[1],
        reverse=True,
    )[:4]
    return [
        {
            "code": row["symbol"],
            "name": row["name"],
            "industry": row["industry"],
            "score": score,
            "reason": reason(row),
        }
        for row, score in ranked
    ]


def show_factor(value: Any, suffix: str = "%") -> str:
    return "—" if value is None else f"{float(value):.1f}{suffix}"
