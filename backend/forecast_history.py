"""Immutable forecast versions and compact, independently retained outcomes."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import database
from .report_storage import decode_result, encode_result

logger = logging.getLogger(__name__)
HORIZONS = (15, 30)
BENCHMARKS = {"sh000001": "上证指数", "sh000688": "科创50"}
REFRESH_KEY = "forecast_feedback_refresh:v1"


def initialize_archive(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS forecast_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT NOT NULL UNIQUE,
        run_date TEXT NOT NULL, published_at TEXT NOT NULL, model TEXT NOT NULL,
        prompt_version TEXT, result_json TEXT NOT NULL, overview_json TEXT NOT NULL, checked_at TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_forecast_reports_day ON forecast_reports(run_date, id)")
    db.execute("""CREATE TABLE IF NOT EXISTS forecast_feedback (
        report_id INTEGER NOT NULL REFERENCES forecast_reports(id), concept_code TEXT NOT NULL,
        horizon_days INTEGER NOT NULL CHECK(horizon_days IN (15,30)),
        result_json TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(report_id, concept_code, horizon_days)
    )""")
    # Import only real, successful cached forecasts. A missing publication time
    # cannot safely establish a historical observation window.
    for row in db.execute("""SELECT * FROM ai_runs WHERE provider = 'forecast:glm'
                            AND status = 'succeeded' AND finished_at IS NOT NULL
                            ORDER BY finished_at, id""").fetchall():
        archive_run(db, row)


def archive_run(db, row) -> None:
    try:
        report = decode_result(row["result_json"])
        datetime.fromisoformat(row["finished_at"])
        datetime.fromisoformat(report["window"]["startDate"])
        if not isinstance(report["concepts"], list) or not isinstance(report["summary"], str):
            return
        overview = {"window": report["window"], "summary": report["summary"],
                    "concepts": [{"code": c["code"], "name": c["name"]} for c in report["concepts"]]}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        logger.warning("Skipping forecast without an auditable report/date: %s", row["id"])
        return
    db.execute("""INSERT OR IGNORE INTO forecast_reports
        (run_key, run_date, published_at, model, prompt_version, result_json, overview_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)""", (
        row["run_token"] or f"legacy:{row['id']}:{row['finished_at']}", row["run_date"],
        row["finished_at"], row["model"], row["prompt_version"], encode_result(report),
        json.dumps(overview, ensure_ascii=False)))


def _report(row) -> dict:
    return {"id": row["id"], "runDate": row["run_date"], "publishedAt": row["published_at"],
            "model": row["model"], "promptVersion": row["prompt_version"],
            "result": decode_result(row["result_json"]) if "result_json" in row.keys() else json.loads(row["overview_json"]),
            "checkedAt": row["checked_at"]}


OVERVIEW_COLUMNS = "id, run_date, published_at, model, prompt_version, overview_json, checked_at"


def get_report(report_id: int) -> dict | None:
    with database.connection() as db:
        row = db.execute("SELECT * FROM forecast_reports WHERE id = ?", (report_id,)).fetchone()
    return _report(row) if row else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(outcomes: list[dict]) -> dict:
    complete = [item for item in outcomes if item["status"] == "completed" and item.get("returnPct") is not None]
    returns = [item["returnPct"] for item in complete]
    benchmarks = {}
    for code, name in BENCHMARKS.items():
        pairs = [item["benchmarks"][code] for item in complete
                 if item.get("benchmarks", {}).get(code, {}).get("excessPct") is not None]
        benchmarks[code] = {"name": name, "sampleCount": len(pairs),
                            "averageReturnPct": _mean([item["returnPct"] for item in pairs]),
                            "averageExcessPct": _mean([item["excessPct"] for item in pairs]),
                            "outperformRate": _mean([100.0 if item["excessPct"] > 0 else 0.0 for item in pairs])}
    return {"sampleCount": len(complete), "totalCount": len(outcomes),
            "trackingCount": sum(item["status"] in ("tracking", "pending") for item in outcomes),
            "missingCount": sum(item["status"] == "missing_data" for item in outcomes),
            "averageReturnPct": _mean(returns),
            "positiveRate": _mean([100.0 if value > 0 else 0.0 for value in returns]),
            "averageDrawdownPct": _mean([item["maxDrawdownPct"] for item in complete
                                         if item.get("maxDrawdownPct") is not None]),
            "benchmarks": benchmarks}


def refresh_status() -> dict:
    raw = database.get_meta(REFRESH_KEY)
    status = json.loads(raw) if raw else {"status": "idle", "finishedAt": None, "error": None}
    if status["status"] == "running":
        elapsed = datetime.now(database.CHINA_TZ) - datetime.fromisoformat(status["startedAt"])
        if elapsed.total_seconds() > 900:
            return {**status, "status": "failed", "error": "上次更新已中断，可重新更新实际表现"}
    return status


def history_payload(page: int = 1, page_size: int = 10, *, as_of: datetime | None = None) -> dict:
    from .forecast_feedback import pending_outcome
    now = as_of or datetime.now(database.CHINA_TZ)
    refresh = refresh_status()
    # Only first daily reports contribute to statistics. Extra versions are read
    # only when they appear on this page; long stored price paths are not UI rows.
    selected = """WITH primary_ids AS (SELECT MIN(id) AS id FROM forecast_reports GROUP BY run_date),
        page_ids AS (SELECT id FROM forecast_reports ORDER BY id DESC LIMIT ? OFFSET ?),
        selected_ids AS (SELECT id FROM primary_ids UNION SELECT id FROM page_ids) """
    pagination = (page_size, (page - 1) * page_size)
    with database.connection() as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) FROM forecast_reports").fetchone()[0]
        rows = db.execute(selected + f"""SELECT {OVERVIEW_COLUMNS},
            id IN (SELECT id FROM primary_ids) AS is_primary,
            id IN (SELECT id FROM page_ids) AS on_page
            FROM forecast_reports WHERE id IN (SELECT id FROM selected_ids) ORDER BY id DESC""", pagination).fetchall()
        saved = {(row["report_id"], row["concept_code"], row["horizon_days"]): json.loads(row["result_json"])
                 for row in db.execute(selected + """SELECT report_id, concept_code, horizon_days,
                     json_remove(result_json, '$.path') AS result_json FROM forecast_feedback
                     WHERE report_id IN (SELECT id FROM selected_ids)""", pagination)}
    cohorts = {str(days): [] for days in HORIZONS}
    entries, abstentions = [], 0
    for row in rows:
        report = _report(row)
        primary = bool(row["is_primary"])
        abstentions += int(primary and not report["result"]["concepts"])
        concepts = []
        for concept in report["result"]["concepts"]:
            outcomes = {}
            for days in HORIZONS:
                outcome = saved.get((row["id"], concept["code"], days))
                if not outcome or outcome["status"] != "completed":
                    pending = pending_outcome(report, days, now)
                    # Stale tracking prices remain dated but must not become final
                    # just because the calendar has since advanced.
                    outcome = {**pending, **(outcome or {}), "status": pending["status"],
                               "targetDate": pending["targetDate"]}
                    if outcome.get("returnPct") is None:
                        if pending["dataStatus"] == "waiting_for_close":
                            outcome.update(dataStatus="waiting_for_close", note=pending["note"])
                        elif (refresh.get("status") == "failed" and refresh.get("error")
                              and (refresh.get("finishedAt") or "") >= report["publishedAt"]):
                            outcome.update(dataStatus="unavailable", note=refresh["error"])
                    elif pending["status"] == "missing_data" and outcome.get("dataStatus") != "unavailable":
                        outcome["note"] = pending["note"]
                outcomes[str(days)] = {key: value for key, value in outcome.items() if key != "path"}
                if primary:
                    cohorts[str(days)].append(outcome)
            concepts.append({"code": concept["code"], "name": concept["name"], "outcomes": outcomes})
        if row["on_page"]:
            entries.append({key: value for key, value in report.items() if key != "result"} | {
                "includedInStats": primary, "window": report["result"]["window"],
                "summary": report["result"]["summary"], "concepts": concepts})
    return {"reports": entries, "page": page, "pageSize": page_size, "totalReports": total,
            "forecastDays": sum(row["is_primary"] for row in rows), "abstentionDays": abstentions,
            "summaries": {days: summarize(items) for days, items in cohorts.items()},
            "refresh": refresh, "asOf": now.isoformat(timespec="seconds")}


def feedback_context(as_of: datetime | None = None) -> dict:
    """Only completed outcomes known before this prediction enter its prompt."""
    now = as_of or datetime.now(database.CHINA_TZ)
    cohorts = {str(days): [] for days in HORIZONS}
    cases, reports = [], {}
    with database.connection() as db:
        rows = db.execute("""SELECT f.*, r.run_date, r.published_at
            FROM forecast_feedback f JOIN forecast_reports r ON r.id = f.report_id
            WHERE r.id = (SELECT MIN(r2.id) FROM forecast_reports r2 WHERE r2.run_date = r.run_date)
            ORDER BY f.updated_at DESC, r.id DESC""").fetchall()
    for row in rows:
        item = json.loads(row["result_json"])
        if (item["status"] != "completed" or item.get("returnPct") is None
                or datetime.fromisoformat(row["updated_at"]) >= now
                or datetime.fromisoformat(row["published_at"]) >= now
                or item["targetDate"] >= now.date().isoformat()):
            continue
        cohorts[str(row["horizon_days"])].append(item)
        if len(cases) < 12:
            if row["report_id"] not in reports:
                reports[row["report_id"]] = get_report(row["report_id"])["result"]
            report = reports[row["report_id"]]
            concept = next((c for c in report["concepts"] if c["code"] == row["concept_code"]), None)
            if concept:
                cases.append({"predictedOn": row["run_date"], "horizonDays": row["horizon_days"],
                              **{key: concept.get(key) for key in ("code", "name", "thesis", "confirmation", "invalidation", "conviction")},
                              **{key: item.get(key) for key in ("entryDate", "exitDate", "returnPct", "maxDrawdownPct", "benchmarks")}})
    return {"asOf": now.isoformat(), "sampleUnit": "每日首次成功预测中的每个概念，等权",
            "summaries": {days: summarize(items) for days, items in cohorts.items()}, "recentCases": cases,
            "limits": "仅已到期且已核对结果；重叠区间与重复概念非独立样本。30日是原15日预测的延伸观察。收益不证明因果或失效条件已经发生。"}


def reports_to_refresh(limit: int = 12) -> list[dict]:
    with database.connection() as db:
        rows = db.execute(f"SELECT {OVERVIEW_COLUMNS} FROM forecast_reports ORDER BY checked_at IS NOT NULL, checked_at, id").fetchall()
        saved = {(row["report_id"], row["concept_code"], row["horizon_days"]): json.loads(row["result_json"])
                 for row in db.execute("SELECT * FROM forecast_feedback")}
    result = []
    today = database.china_date()
    for row in rows:
        report = _report(row)
        if report["result"]["window"]["startDate"] > today:
            continue
        def needs_update(concept: dict, days: int) -> bool:
            item = saved.get((row["id"], concept["code"], days))
            if item is None or item.get("returnPct") is None or item.get("dataStatus") == "unavailable":
                return True
            if any(item.get("benchmarks", {}).get(code, {}).get("returnPct") is None for code in BENCHMARKS):
                return True
            if item["status"] == "completed":
                return False
            checked = report["checkedAt"] or ""
            # No new daily close is available after a successful post-close read.
            # Missing sources continue retrying on the next scheduled batch.
            return not (item["status"] == "tracking" and checked[:10] == today and checked[11:16] >= "15:10")

        if any(needs_update(concept, days) for concept in report["result"]["concepts"] for days in HORIZONS):
            result.append(report)
        if len(result) >= limit:
            break
    return result
