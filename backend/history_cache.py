"""Reuse closed OHLC windows without changing an index's price basis."""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import database
from .history_sources import history_covers_window, normalize_bars

ARCHIVE_DAYS = 180
ARCHIVE_ROWS = 260


def archive_key(code: str) -> str:
    return f"evaluation:{code}:archive"


def clean_history(value: dict, start: str, end: str) -> dict:
    """Validate cached OHLC, retaining volume/return metadata needed by forecasts."""
    raw = value.get("rows", [])
    raw = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    by_day, conflicts = {}, set()
    for row in raw:
        valid = normalize_bars([[row.get(key) for key in ("date", "open", "close", "high", "low")]], start, end)
        if not valid:
            continue
        bar = valid[0]
        day = bar["date"]
        previous = by_day.get(day)
        if previous and any(abs(previous[key] - bar[key]) > 1e-7 for key in ("open", "close", "high", "low")):
            conflicts.add(day)
        else:
            # Identical duplicate bars are harmless; contradictory prices are
            # not resolved by row order. Their dates remain gaps to be repaired.
            by_day[day] = {**(previous or {}), **row, **bar}
    return {**value, "rows": [by_day[day] for day in sorted(by_day) if day not in conflicts]}


def join_histories(older: dict, newer: dict) -> dict | None:
    """Join only the identical source with agreeing overlap, never different indexes."""
    if not older.get("source") or not older.get("url") or any(
            older.get(key) != newer.get(key) for key in ("source", "url")):
        return None
    before = {row["date"]: row for row in older.get("rows", [])}
    after = {row["date"]: row for row in newer.get("rows", [])}
    overlap = before.keys() & after.keys()
    if not overlap or any(abs(before[day][key] - after[day][key]) > 1e-7
                          for day in overlap for key in ("open", "close", "high", "low")):
        return None
    rows = {**before, **after}
    for day in overlap:
        # A later OHLC-only transport must not erase already observed amount data.
        rows[day] = {**before[day], **{key: value for key, value in after[day].items() if value is not None}}
    return {**newer, "rows": [rows[day] for day in sorted(rows)],
            "cacheAsOf": max(older.get("cacheAsOf", ""), newer.get("cacheAsOf", ""))}


def cached_history(code: str, start: str, end: str, calendar: tuple[str, ...] = ()) -> dict | None:
    prefix = f"evaluation:{code}:"
    with database.connection() as db:
        saved = db.execute("""SELECT payload_json,updated_at FROM research_cache
            WHERE cache_key >= ? AND cache_key < ? ORDER BY updated_at DESC LIMIT 32""",
                           (prefix, prefix + "\uffff")).fetchall()
    choices = []
    for record in saved:
        try:
            value = json.loads(record["payload_json"])
            if not isinstance(value, dict) or (value.get("code") and value["code"] != code):
                continue
            value = clean_history(value, start, end)
            if not value["rows"]:
                continue
            value["cacheAsOf"] = value.get("cacheAsOf") or record["updated_at"]
            # An earlier failed refresh does not alter the saved bars' provenance.
            for key in ("error", "errorKind", "refreshError"):
                value.pop(key, None)
        except (ValueError, TypeError, KeyError):
            continue
        for index, choice in enumerate(choices):
            merged = join_histories(value, choice)  # Newest values keep precedence.
            if merged is not None:
                choices[index] = merged
                break
        else:
            choices.append(value)
    return max(choices, key=lambda value: (
        history_covers_window(value["rows"], start, end, calendar), value["rows"][-1]["date"],
        len(value["rows"]), value.get("cacheAsOf", "")), default=None)


def missing_window(value: dict, start: str, end: str, calendar: tuple[str, ...]) -> tuple[str, str]:
    available = {row["date"] for row in value.get("rows", [])}
    if calendar and calendar[0] <= start and calendar[-1] >= end:
        expected = [day for day in calendar if start <= day <= end]
    else:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        expected = [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
                    if (first + timedelta(days=offset)).weekday() < 5]
    missing = [day for day in expected if day not in available]
    if not missing:
        return start, end
    # Include a verified overlap on either side to establish identical price basis.
    before = [day for day in available if day < missing[0]]
    after = [day for day in available if day > missing[-1]]
    return (max(before) if before else missing[0], min(after) if after else missing[-1])
