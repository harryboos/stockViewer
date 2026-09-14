"""Local retention rules. Unknown metadata and live task leases are preserved."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta

CONCEPT_CACHE_VERSION = "3"
SECTOR_OVERVIEW_CACHE_VERSION = "4"
MARKET_INTRADAY_PAIR_CACHE_VERSION = "1"
MARKET_INTRADAY_INDEX_CACHE_VERSION = "1"
CONCEPT_HISTORY_CACHE_VERSION = "1"
VERSIONS = {
    "hot_concepts": CONCEPT_CACHE_VERSION, "sector_overview": SECTOR_OVERVIEW_CACHE_VERSION,
    "market_intraday_pair": MARKET_INTRADAY_PAIR_CACHE_VERSION,
    "market_intraday_index": MARKET_INTRADAY_INDEX_CACHE_VERSION,
    "concept_history": CONCEPT_HISTORY_CACHE_VERSION, "sector_previous_amount": "1",
}
UNUSED_META = {"stock_catalog_source", "daily_strategy_error", "daily_ai_error", "sector_overview_error"}
DAILY_PREFIXES = {"hot_concepts", "sector_previous_amount", "market_intraday_pair"}
RESULT_DAYS = 90
FAILURE_DAYS = 14
DAILY_SESSIONS = 7
DAILY_MAX_AGE_DAYS = 30
HISTORY_SNAPSHOTS = 2
MAINTENANCE_KEY = "storage_maintenance"
HISTORY_PATTERN = re.compile(r"^concept_history:(BK\d+):(\d{8}):v(\d+)$")


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def old_history_keys(keys: list[str], today: date) -> set[str]:
    by_code: dict[str, list[tuple[date, str]]] = {}
    for key in keys:
        match = HISTORY_PATTERN.fullmatch(key)
        if not match or match[3] != CONCEPT_HISTORY_CACHE_VERSION:
            continue
        day = parse_date(match[2])
        if day is not None and day <= today:
            by_code.setdefault(match[1], []).append((day, key))
    return {key for rows in by_code.values() for _, key in sorted(rows, reverse=True)[HISTORY_SNAPSHOTS:]}


def cleanup(db: sqlite3.Connection, today: date, lease_cutoff) -> dict:
    remove: set[str] = set()
    daily: list[tuple[date, str, date]] = []
    histories = []
    for row in db.execute("SELECT key, value FROM app_meta"):
        key = row["key"]
        if key in UNUSED_META:
            remove.add(key)
            continue
        parts = key.split(":")
        prefix = parts[0]
        version = re.fullmatch(r"v(\d+)", parts[-1])
        if prefix not in VERSIONS or not version:
            continue
        if int(version[1]) < int(VERSIONS[prefix]):
            remove.add(key)
            continue
        if version[1] != VERSIONS[prefix]:
            continue  # Preserve newer/unknown formats if an older app is used.
        if prefix == "concept_history":
            histories.append(key)
        elif prefix in DAILY_PREFIXES and len(parts) == 3:
            key_day = parse_date(parts[1])
            if key_day is None or key_day > today:
                continue
            trade_day = key_day
            if prefix == "hot_concepts":
                try:
                    payload = json.loads(row["value"])
                    trade_day = parse_date(payload.get("tradeDate")) or key_day
                except (ValueError, TypeError, AttributeError):
                    pass
            if trade_day <= today:
                daily.append((trade_day, key, key_day))
    # Actual dated snapshots avoid inventing a trading calendar over long holidays.
    sessions = sorted({day for day, _, _ in daily}, reverse=True)[:DAILY_SESSIONS]
    age_limit = today - timedelta(days=DAILY_MAX_AGE_DAYS)
    remove.update(key for day, key, key_day in daily if day not in sessions or day < age_limit or key_day < age_limit)
    remove.update(old_history_keys(histories, today))
    db.executemany("DELETE FROM app_meta WHERE key = ?", [(key,) for key in remove])

    result_cutoff = (today - timedelta(days=RESULT_DAYS)).isoformat()
    failure_cutoff = (today - timedelta(days=FAILURE_DAYS)).isoformat()
    strategy_keys = [row["run_date"] for row in db.execute("SELECT run_date FROM strategy_runs")
                     if (parse_date(row["run_date"][:10]) is not None and row["run_date"][:10] < result_cutoff)]
    db.executemany("DELETE FROM strategy_runs WHERE run_date = ?", [(key,) for key in strategy_keys])
    ai_keys = []
    for row in db.execute("SELECT id, provider, run_date, status, started_at FROM ai_runs"):
        if parse_date(row["run_date"]) is None:
            continue
        if row["status"] == "succeeded" and row["run_date"] < result_cutoff:
            ai_keys.append((row["id"],))
        elif row["run_date"] < failure_cutoff and (
            row["status"] == "failed" or (row["status"] == "running" and row["started_at"] <= lease_cutoff(row["provider"]))
        ):
            ai_keys.append((row["id"],))
    db.executemany("DELETE FROM ai_runs WHERE id = ?", ai_keys)
    # Old prompt metadata is dispensable only after it was successfully migrated.
    prompt_keys = []
    for row in db.execute("SELECT key, value FROM app_meta WHERE key LIKE 'ai_prompt:%'"):
        match = re.fullmatch(r"ai_prompt:(.+):(\d{4}-\d{2}-\d{2}):v\d+", row["key"])
        if match:
            run = db.execute("SELECT prompt_version FROM ai_runs WHERE provider = ? AND run_date = ?", (match[1], match[2])).fetchone()
            if run is None or run["prompt_version"] is not None:
                prompt_keys.append((row["key"],))
    db.executemany("DELETE FROM app_meta WHERE key = ?", prompt_keys)
    return {"date": today.isoformat(), "metaRemoved": len(remove) + len(prompt_keys),
            "strategyRemoved": len(strategy_keys), "aiRemoved": len(ai_keys)}
