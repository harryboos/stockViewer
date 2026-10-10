"""Deterministic, bounded recall from actual same-provider quote/history data.

No network access, name preferences, cross-provider mappings, or synthetic
history belong here. Coverage describes what was actually available; a sparse
history sample must never be presented as a full-market technical scan.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .concept_identity import concept_provider
from .market_numbers import number_or_none as _finite
from .forecast_candidates import (_available_boards, _metric, _ranked_round_robin,
                                  daily_change_baseline, select_forecast_boards)

METHOD = "dated-discovery-round-robin-v2"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
CHANNELS = ("pctChg", "breadth", "relativeAmount", "momentum3d", "momentum5d", "eventClue", "relativeStrength5d", "amount")


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("预测候选数量必须是非负整数")


def _date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        parsed = date.fromisoformat(value)
        return parsed.isoformat() if len(value) == 10 else None
    except ValueError:
        return None


def select_discovery_seeds(boards: list[dict], limit: int = 48, trade_date: str | None = None) -> list[dict]:
    """Reserve half the reads for leaders and half for a name-blind sample.

    The sample rotates by snapshot date, not wall-clock time, so retries stay
    reproducible. This only allocates reads; it does not assert any strength.
    """
    _limit(limit)
    if trade_date is not None and _date(trade_date) is None:
        raise ValueError("候选发现需要有效的行情日期")
    available = _available_boards(boards)
    if trade_date is not None:
        available = [row for row in available if not row.get("tradeDate") or _date(row["tradeDate"]) == _date(trade_date)]
    if not limit:
        return []
    leaders = select_forecast_boards(available, (limit + 1) // 2)
    seen = {row["code"] for row in leaders}
    salt = _date(trade_date) if trade_date is not None else ""
    remainder = sorted((row for row in available if row["code"] not in seen), key=lambda row: (
        hashlib.sha256(f"{salt}:{row['code']}".encode()).hexdigest(), row["code"]))
    spread = remainder[:max(0, limit - len(leaders))]
    # Readers consume seeds in order under a time budget. Alternating prevents
    # exploratory reads from always being deferred behind every daily leader.
    ordered = []
    for index in range(max(len(leaders), len(spread))):
        if index < len(leaders):
            ordered.append(leaders[index])
        if index < len(spread):
            ordered.append(spread[index])
    return copy.deepcopy(ordered)


def _history_features(code: str, history: object, trade_date: str) -> dict:
    from .forecast_data import technical_metrics

    usable = {}
    future_count = invalid_count = 0
    for row in history if isinstance(history, list) else []:
        if not isinstance(row, dict):
            invalid_count += 1
            continue
        day, close = _date(row.get("date")), _finite(row.get("close"))
        if not day or close is None or close <= 0 or row.get("code", code) != code:
            invalid_count += 1
            continue
        if day > trade_date:
            future_count += 1
            continue
        amount = _finite(row.get("amount"))
        usable[day] = {**row, "date": day, "close": close, "amount": amount if amount and amount > 0 else None}
    bars = [usable[key] for key in sorted(usable)]
    technical = technical_metrics(bars, trade_date)
    aligned = bool(bars) and bars[-1]["date"] == trade_date
    features = {**technical, "historyBars": len(bars), "historyAligned": aligned,
                "ignoredFutureBars": future_count, "ignoredInvalidBars": invalid_count,
                "change5d": None, "previousChange3d": None, "momentumChange3d": None,
                "amountRatioPrevious5d": None,
                "historyContinuity": "not_calendar_verified",
                "amountRatioNote": "amountRatio5d剔除行情当日，比较此前两段各5根已提供日线的成交额；amountRatioPrevious5d含行情当日，盘中值尚未完整，不用于召回排序"}
    if not aligned:
        return features
    closes = [row["close"] for row in bars]
    if len(closes) >= 6:
        features["change5d"] = round((closes[-1] / closes[-6] - 1) * 100, 2)
        amounts = [row["amount"] for row in bars[-6:]]
        if all(value is not None for value in amounts):
            features["amountRatioPrevious5d"] = round(amounts[-1] / (sum(amounts[:-1]) / 5), 3)
    if len(closes) >= 7:
        previous = (closes[-4] / closes[-7] - 1) * 100
        current = (closes[-1] / closes[-4] - 1) * 100
        features["previousChange3d"] = round(previous, 2)
        features["momentumChange3d"] = round(current - previous, 2)
    return features


def _event_clues(rows: object, code: str, cutoff: datetime) -> list[dict]:
    earliest = cutoff - timedelta(days=15)
    valid = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("code", code) != code:
            continue
        title, raw_date, url = row.get("title"), row.get("publishedAt"), row.get("url")
        if not isinstance(title, str) or not title.strip() or not isinstance(raw_date, str) or not isinstance(url, str):
            continue
        try:
            parsed_url = urlparse(url)
            published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            published = published.replace(tzinfo=CHINA_TZ) if published.tzinfo is None else published.astimezone(CHINA_TZ)
        except ValueError:
            continue
        date_only = row.get("publishedAtPrecision") == "date" or _date(raw_date) is not None
        within_cutoff = (earliest.date() <= published.date() <= cutoff.date() if date_only else
                         earliest <= published <= cutoff)
        if (parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username
                or parsed_url.password or not within_cutoff):
            continue
        key = (url, title.strip())
        valid[key] = {"id": str(row.get("id") or f"{code}:event:{hashlib.sha256(url.encode()).hexdigest()[:12]}"),
                      "title": title.strip()[:400], "publishedAt": raw_date,
                      "timePrecision": "date" if date_only else "timestamp",
                      "publishedAtPrecision": "date" if date_only else "second", "url": url,
                      "source": str(row.get("source") or "公开事件线索")[:120],
                      "note": "只表示匹配到有日期的事件线索，需核对影响方向、传导链和首次公开时间；不证明利好",
                      "_published": published.isoformat()}
    result = sorted(valid.values(), key=lambda row: (row["_published"], row["id"]), reverse=True)[:3]
    return [{key: value for key, value in row.items() if key != "_published"} for row in result]


def _channel_value(row: dict, channel: str) -> float | None:
    if channel in {"pctChg", "breadth", "amount"}:
        return _metric(row, channel)
    feature = row["discoveryFeatures"]
    if channel == "relativeAmount":
        ratio = feature.get("amountRatio5d")
        return ratio if ratio is not None and ratio > 1 else None
    if channel in {"momentum3d", "momentum5d"}:
        days = "3d" if channel == "momentum3d" else "5d"
        change, acceleration = feature.get(f"change{days}"), feature.get(f"momentumChange{days}")
        # A decline that merely slows is not yet a positive-price confirmation.
        return acceleration if change is not None and change > 0 and acceleration is not None and acceleration > 0 else None
    if channel == "relativeStrength5d":
        relative = feature.get("relativeStrength", {})
        first, second = relative.get("excessShanghai5d"), relative.get("excessStar505d")
        return min(first, second) if first is not None and second is not None and min(first, second) > 0 else None
    if channel == "eventClue" and row.get("eventClues"):
        latest = row["eventClues"][0]["publishedAt"]
        published = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        published = published.replace(tzinfo=CHINA_TZ) if published.tzinfo is None else published.astimezone(CHINA_TZ)
        return float(published.date().toordinal())
    return None


def build_discovery_pool(boards: list[dict], histories: dict, trade_date: str,
                         event_sources: dict | None = None, limit: int = 12, *,
                         decision_at: str | None = None, relative_strength: dict | None = None) -> tuple[list[dict], dict]:
    """Recall at most ``limit`` concepts with explicit data/coverage provenance.

    Histories are real bars keyed by provider-specific concept code. Extra codes
    from the same provider are ignored; mixed providers are rejected. The caller
    supplies ``decision_at`` to allow already-published holiday/weekend events.
    Without it the cutoff is the end of the supplied snapshot date. Date-only
    publication times remain imprecise clues and never prove intraday knowledge.
    """
    _limit(limit)
    day = _date(trade_date)
    if day is None:
        raise ValueError("候选发现需要有效的行情日期")
    if (not isinstance(histories, dict) or (event_sources is not None and not isinstance(event_sources, dict))
            or (relative_strength is not None and not isinstance(relative_strength, dict))):
        raise ValueError("历史行情和事件线索必须按概念代码组织")
    cutoff = datetime.combine(date.fromisoformat(day), time.max, tzinfo=CHINA_TZ)
    if decision_at is not None:
        try:
            cutoff = datetime.fromisoformat(decision_at.replace("Z", "+00:00"))
            cutoff = cutoff.replace(tzinfo=CHINA_TZ) if cutoff.tzinfo is None else cutoff.astimezone(CHINA_TZ)
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValueError("事件发现需要有效的决策截止时间") from exc
    if cutoff.date().isoformat() < day:
        raise ValueError("决策截止时间不能早于行情日期")
    all_available = _available_boards(boards)
    provider = concept_provider(all_available[0]["code"]) if all_available else None
    sources = event_sources or {}
    relative_rows = relative_strength or {}
    if provider and any(concept_provider(code) not in {None, provider} for code in {*histories, *sources, *relative_rows}):
        raise ValueError("预测候选与历史、事件必须来自同一行情来源，不能混用概念指数")
    available = [row for row in all_available if not row.get("tradeDate") or _date(row["tradeDate"]) == day]
    enriched = []
    checked = aligned = enough = ignored_future = ignored_invalid = 0
    for board in available:
        code = board["code"]
        if code in histories:
            checked += 1
        features = _history_features(code, histories.get(code), day)
        relative = relative_rows.get(code)
        if isinstance(relative, dict) and _date(relative.get("tradeDate")) == day:
            features["relativeStrength"] = {
                "tradeDate": day, "excessShanghai5d": _finite(relative.get("excessShanghai5d")),
                "excessStar505d": _finite(relative.get("excessStar505d")),
                "note": "同区间近5日相对上证指数/科创50收益差，单位百分点；不是相对强度改善量"}
        aligned += int(features["historyAligned"])
        enough += int(features["historyAligned"] and features["historyBars"] >= 11)
        ignored_future += features["ignoredFutureBars"]
        ignored_invalid += features["ignoredInvalidBars"]
        row = {**copy.deepcopy(board), "discoveryFeatures": features}
        clues = _event_clues(sources.get(code), code, cutoff)
        if clues:
            row["eventClues"] = clues
        enriched.append(row)
    selected, channels = _ranked_round_robin(enriched, CHANNELS, _channel_value, limit)
    metadata = {"method": METHOD, "provider": provider, "tradeDate": day,
                "eventCutoff": cutoff.isoformat(timespec="seconds"),
                "availableCount": len(available), "snapshotCount": len(all_available),
                "excludedSnapshotDateCount": len(all_available) - len(available),
                "historyCheckedCount": checked, "historyUnseenCount": len(available) - checked,
                "historyAlignedCount": aligned, "historyMissingOrStaleCount": checked - aligned,
                "historyFeatureReadyCount": enough, "historyInsufficientCount": aligned - enough,
                "ignoredFutureBars": ignored_future, "ignoredInvalidBars": ignored_invalid,
                "selectedCount": len(selected), "limit": limit,
                "channels": channels,
                "baseline": {"dailyTop3Codes": daily_change_baseline(available),
                             "candidateEqualWeightCodes": [row["code"] for row in selected]},
                "channelDefinitions": {
                    "relativeAmount": "剔除行情当日，最近5根日线成交额相对之前5根的比值，大于1",
                    "momentum3d": "最近3根日线收益为正，且较此前3根改善；排序单位百分点",
                    "momentum5d": "最近5根日线收益为正，且较此前5根改善；排序单位百分点",
                    "relativeStrength5d": "同区间近5日分别跑赢上证和科创50，按两项超额的较小值排序；不是改善量",
                    "eventClue": "截止决策时间近15个自然日的可定位事件线索，不代表利好"},
                "note": "候选发现仅覆盖已提供的同来源快照；历史未读取、过期与不足单独记录。日线根数不证明交易日连续完整，需另行核对交易日历。事件为检索线索；成交额优先结合自身历史变化，不把绝对规模当作增长。"}
    return selected, metadata
