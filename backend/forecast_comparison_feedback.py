"""Read-only, point-in-time feedback for leaders omitted from a forecast.

Comparison rankings are hindsight evidence. They cannot establish why a theme
rose or whether the forecaster could see it before publication.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time

from . import database
from .concept_identity import CONCEPT_PROVIDERS, concept_provider, single_provider
from .report_storage import decode_result


def _timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(database.CHINA_TZ) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def _number(value) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _ready(value: dict, checked: str, span: tuple[str, str], mature: datetime, now: datetime) -> bool:
    stamp = _timestamp(checked)
    return (stamp is not None and mature <= stamp <= now and value.get("status") == "completed"
            and value.get("dataStatus", "ready") == "ready" and _number(value.get("returnPct"))
            and (value.get("entryDate"), value.get("exitDate")) == span)


def _candidate_audit(result: dict, code: str) -> dict:
    audit = result.get("candidateAudit")
    rows = audit.get("candidates") if isinstance(audit, dict) else None
    if not isinstance(rows, list):
        return {"candidateAvailability": "unknown", "prePredictionTechnicalData": None}
    match = next((row for row in rows if isinstance(row, dict) and row.get("code") == code), None)
    if match is None:
        return {"candidateAvailability": "outside_pool" if audit.get("complete") is True else "unknown",
                "prePredictionTechnicalData": None}
    # Only original compact indicators enter feedback, never today's prices or
    # a reconstructed history advertised as the original candidate snapshot.
    technical = match.get("technicalData")
    return {"candidateAvailability": "in_pool",
            "prePredictionTechnicalData": technical if isinstance(technical, dict) else None}


def missed_leader_cases(as_of: datetime, *, limit: int = 4) -> list[dict]:
    """Return at most four comparable, mature cases without fetching any data."""
    from .forecast_feedback import bounds, observation_window
    from .forecast_prices import CALENDAR_KEY

    now = _timestamp(as_of.isoformat())
    if now is None or limit <= 0:
        return []
    # A snapshot transaction prevents mixing a freshly updated universe with
    # earlier outcomes while a feedback worker writes in another connection.
    with database.connection() as db:
        db.execute("BEGIN")
        raw = db.execute("SELECT value FROM app_meta WHERE key=?", (CALENDAR_KEY,)).fetchone()
        try:
            calendar = sorted({date.fromisoformat(day).isoformat() for day in json.loads(raw[0])["dates"]}) if raw else []
        except (ValueError, TypeError, KeyError):
            calendar = []
        if not calendar:
            return []
        rows = db.execute("""SELECT r.* FROM forecast_reports r
            WHERE r.id = (SELECT MIN(r2.id) FROM forecast_reports r2 WHERE r2.run_date=r.run_date)
              AND EXISTS (SELECT 1 FROM comparison_outcomes c WHERE c.report_id=r.id)
            ORDER BY r.run_date DESC,r.id DESC LIMIT 120""").fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return []
        slots = ",".join("?" for _ in ids)
        outcomes, selected_outcomes, universes = {}, {}, {}
        for row in db.execute(f"SELECT * FROM comparison_outcomes WHERE report_id IN ({slots})", ids):
            outcomes.setdefault((row["report_id"], row["horizon_days"]), []).append(dict(row))
        for row in db.execute(f"""SELECT report_id,concept_code,horizon_days,updated_at,
            json_remove(result_json,'$.path') AS result_json FROM forecast_feedback WHERE report_id IN ({slots})""", ids):
            selected_outcomes.setdefault((row["report_id"], row["horizon_days"]), []).append(dict(row))
        for row in db.execute(f"SELECT * FROM comparison_universes WHERE report_id IN ({slots})", ids):
            universes[row["report_id"]] = dict(row)

    cases = []
    for row in rows:
        published = _timestamp(row["published_at"])
        if published is None or published > now:
            continue
        try:
            result = decode_result(row["result_json"])
            concepts = result.get("concepts", [])
            provider = single_provider(concepts) or (result.get("conceptProvider") if not concepts else None)
            if provider not in CONCEPT_PROVIDERS:
                continue
            frozen = result.get("comparisonUniverse")
            if isinstance(frozen, dict) and frozen.get("boards"):
                universe, origin = frozen, "frozen_at_prediction"
            else:
                saved = universes.get(row["id"])
                if not saved:
                    continue
                universe = {"boards": json.loads(saved["boards_json"]), "scope": saved["scope"], "asOf": saved["as_of"]}
                origin = "backfilled"
            # A later backfill is not part of what was known as of the new run.
            universe_stamp = _timestamp(universe.get("asOf"))
            if universe_stamp is None or universe_stamp > now:
                continue
            pool = {board["code"]: board.get("name", board["code"]) for board in universe["boards"]
                    if isinstance(board, dict) and concept_provider(board.get("code")) == provider}
            if not pool:
                continue
            report = {"publishedAt": row["published_at"], "result": result}
            selected = {concept["code"]: concept.get("name", concept["code"]) for concept in concepts}
            for days in (15, 30):
                mature = datetime.combine(bounds(report, days)[1], time(15, 10), database.CHINA_TZ)
                if now < mature:
                    continue
                _, sessions = observation_window(report, days, calendar, now)
                if not sessions:
                    continue
                span = (sessions[0], sessions[-1])
                ranked = []
                for candidate in outcomes.get((row["id"], days), []):
                    if candidate["code"] not in pool:
                        continue
                    value = json.loads(candidate["result_json"])
                    if _ready(value, candidate["updated_at"], span, mature, now):
                        ranked.append({"code": candidate["code"], "name": pool[candidate["code"]],
                                       "returnPct": value["returnPct"], "checkedAt": candidate["updated_at"],
                                       "priceSource": value.get("source")})
                ranked.sort(key=lambda item: (-item["returnPct"], item["code"]))
                if not ranked or any(item["code"] in selected and item["returnPct"] == ranked[0]["returnPct"]
                                     for item in ranked):
                    continue
                leader = ranked[0]
                returns = []
                for item in selected_outcomes.get((row["id"], days), []):
                    if item["concept_code"] not in selected:
                        continue
                    value = json.loads(item["result_json"])
                    if _ready(value, item["updated_at"], span, mature, now):
                        returns.append({"code": item["concept_code"], "name": selected[item["concept_code"]],
                                        "returnPct": value["returnPct"]})
                # Selected outcomes can be audited before the broad comparison
                # worker catches up. A stronger/tied selected result means the
                # apparent comparison leader is not a missed winner, and must
                # neither consume a case slot nor inflate an overlap count.
                if any(item["returnPct"] >= leader["returnPct"] for item in returns):
                    continue
                duplicates = next((item for item in cases if item["conceptProvider"] == provider
                    and item["leader"]["code"] == leader["code"] and item["entryDate"] <= span[1]
                    and span[0] <= item["exitDate"]), None)
                if duplicates:
                    duplicates["relatedObservationCount"] += 1
                    continue
                if len(cases) >= min(limit, 4):
                    continue
                average = round(sum(item["returnPct"] for item in returns) / len(returns), 4) if returns else None
                cases.append({"reportId": row["id"], "predictedOn": row["run_date"], "horizonDays": days,
                              "conceptProvider": provider, "entryDate": span[0], "exitDate": span[1],
                              "leader": leader, "selectedReturns": returns,
                              "selectedCount": len(selected), "verifiedSelectedCount": len(returns),
                              "averageSelectedReturn": average,
                              "gapPct": round(average - leader["returnPct"], 4) if average is not None else None,
                              "coverage": {"coveredCount": len(ranked), "totalCount": len(pool),
                                           "fullCoverage": len(ranked) == len(pool), "scope": universe.get("scope"),
                                           "universeOrigin": origin, "universeAsOf": universe.get("asOf")},
                              "leaderMeaning": "已核对同区间范围内最强；未覆盖概念不能排名，收益不证明原因或事前可预测性",
                              "relatedObservationCount": 1, "independentSample": False,
                              **_candidate_audit(result, leader["code"])})
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            # Corrupt/legacy records provide no trustworthy learning signal.
            continue
    return cases
