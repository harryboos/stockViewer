"""A forecast needs a dated, continuous price window, not merely some cached bars."""
from __future__ import annotations

from bisect import bisect_right
from datetime import date
from functools import lru_cache

from .market_numbers import number_or_none

MIN_HISTORY_SESSIONS = 21  # Twenty-session returns require twenty-one prices.


@lru_cache(maxsize=8)
def _calendar_sessions(calendar: tuple[str, ...]) -> tuple[str, ...]:
    # Calendar values, not provider/DB state, key this bounded pure cache.
    return tuple(sorted({date.fromisoformat(day).strftime("%Y%m%d") for day in calendar}))


def verified_history(rows: list[dict], trade_date: str, calendar: list[str]) -> tuple[list[dict], dict]:
    """Return only the continuous suffix verified against the exchange calendar.

    Missing older bars shorten the indicator lookback rather than joining prices
    across gaps. The latest 21 sessions must all exist, regardless of direction.
    The last bar can be intraday when the quote itself is intraday.
    """
    quality = {"status": "calendar_unavailable", "requiredSessions": MIN_HISTORY_SESSIONS,
               "validSessions": 0, "historyAsOf": None, "missingDates": [],
               "reason": "交易日历不足，暂不能核验技术历史完整性"}
    try:
        end = date.fromisoformat(trade_date).strftime("%Y%m%d")
        sessions = _calendar_sessions(tuple(calendar))
    except (ValueError, TypeError):
        return [], quality
    last = bisect_right(sessions, end)
    expected = sessions[max(0, last - 45):last]
    if not sessions or sessions[-1] < end or len(expected) < MIN_HISTORY_SESSIONS or expected[-1] != end:
        return [], quality
    prices, invalid = {}, set()
    for row in rows:
        try:
            day = date.fromisoformat(row["date"]).strftime("%Y%m%d")
        except (KeyError, ValueError, TypeError):
            continue
        if day > end:
            continue
        close = number_or_none(row.get("close"))
        if close is None or close <= 0 or (day in prices and prices[day]["close"] != close):
            invalid.add(day)
            continue
        prices[day] = {**row, "date": day, "close": close}
    for day in invalid:
        prices.pop(day, None)
    quality["historyAsOf"] = max(prices, default=None)
    quality["missingDates"] = [day for day in expected[-MIN_HISTORY_SESSIONS:] if day not in prices]
    suffix = []
    for day in reversed(expected[-45:]):
        if day not in prices:
            break
        suffix.append(prices[day])
    quality["validSessions"] = len(suffix)
    if quality["missingDates"]:
        quality.update(status="incomplete", reason=f"最近 {MIN_HISTORY_SESSIONS} 个交易日缺少有效收盘价，补齐后再参与预测")
        return [], quality
    quality.update(status="ready", reason=f"已核验最近 {len(suffix)} 个连续交易日；不以涨跌方向作为准入条件")
    return list(reversed(suffix)), quality
