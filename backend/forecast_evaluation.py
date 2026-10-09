"""Read-only, versioned forecast evaluation using only frozen research inputs.

Returns are equal-weighted within each report, then across reports. Overlapping
windows are not independent trials, and concept drawdowns are not a portfolio
drawdown. Missing evidence is never replaced with a zero return.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time

from . import database
from .concept_identity import CONCEPT_PROVIDERS, concept_provider, single_provider
from .report_storage import FORMAT, decode_result

BASELINES = {"daily_momentum": "当日涨幅前三", "candidate_equal_weight": "模型候选等权"}


def _code_list_sql(path: str) -> str:
    """Project fixed internal JSON paths without converting invalid rows to valid lists."""
    return f"""CASE WHEN json_type(payload, '{path}') = 'array' THEN
        (SELECT json_group_array(CASE WHEN type = 'object' THEN
            json_object('code', json_extract(value, '$.code'), 'name', json_extract(value, '$.name'))
            ELSE json('null') END) FROM json_each(payload, '{path}'))
        ELSE 'null' END"""


# Both plain and compact-storage reports have the same research payload. Source
# expansion and model prose are unnecessary for evaluation; select the immutable
# audit fields directly so large reports never cross into Python on every GET.
REPORT_QUERY = f"""WITH primary_reports AS (
    SELECT r.id, r.run_date, r.published_at, r.prompt_version, r.result_json
    FROM forecast_reports r
    WHERE r.id=(SELECT MIN(r2.id) FROM forecast_reports r2 WHERE r2.run_date=r.run_date)
    ORDER BY r.run_date DESC,r.id DESC LIMIT ?
), payloads AS (
    SELECT id, run_date, published_at, prompt_version,
        CASE WHEN json_valid(result_json) THEN NULL ELSE result_json END AS legacy_raw,
        CASE WHEN json_valid(result_json) THEN
            CASE WHEN json_extract(result_json, '$.storageFormat') = ?
                 THEN CASE WHEN json_type(result_json, '$.payload') = 'object'
                           THEN json_extract(result_json, '$.payload') ELSE '{{}}' END
                 ELSE result_json END
            ELSE '{{}}' END AS payload
    FROM primary_reports
)
SELECT id, run_date, published_at, prompt_version,
    CASE WHEN legacy_raw IS NOT NULL THEN legacy_raw ELSE json_object(
        'concepts', json({_code_list_sql('$.concepts')}),
        'window', json_extract(payload, '$.window'),
        'tradeDate', json_extract(payload, '$.tradeDate'),
        'conceptProvider', json_extract(payload, '$.conceptProvider'),
        'decisionAt', json_extract(payload, '$.decisionAt'),
        'candidateAudit', json_object(
            'complete', json(CASE json_type(payload, '$.candidateAudit.complete')
                            WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE 'null' END),
            'decisionAt', json_extract(payload, '$.candidateAudit.decisionAt'),
            'candidates', json({_code_list_sql('$.candidateAudit.candidates')})),
        'evaluationPlan', json_extract(payload, '$.evaluationPlan'),
        'comparisonUniverse', json_extract(payload, '$.comparisonUniverse')
    ) END AS result_json
FROM payloads ORDER BY run_date DESC,id DESC"""


def _stamp(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(database.CHINA_TZ) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def _number(value) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _mean(values) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _load(value) -> dict:
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _codes(value, provider: str) -> list[str] | None:
    if (not isinstance(value, list) or any(concept_provider(code) != provider for code in value)
            or len(set(value)) != len(value)):
        return None
    return value


def _frozen_pool(result: dict, provider: str, published: datetime) -> dict:
    universe = result.get("comparisonUniverse")
    if not isinstance(universe, dict):
        return {}
    stamp = _stamp(universe.get("asOf"))
    if stamp is None or stamp > published or not isinstance(universe.get("boards"), list):
        return {}
    return {row["code"]: row.get("name", row["code"]) for row in universe["boards"]
            if isinstance(row, dict) and concept_provider(row.get("code")) == provider}


def _audit_codes(result: dict, provider: str, published: datetime) -> tuple[set[str], bool]:
    audit = result.get("candidateAudit")
    if not isinstance(audit, dict) or not isinstance(audit.get("candidates"), list):
        return set(), False
    stamp = _stamp(audit.get("decisionAt"))
    if stamp is None or stamp > published:
        return set(), False
    codes = [item.get("code") for item in audit["candidates"] if isinstance(item, dict)]
    valid = _codes(codes, provider)
    if valid is None or len(codes) != len(audit["candidates"]):
        return set(), False
    return set(valid), audit.get("complete") is True


def _plan(result: dict, provider: str, published: datetime) -> dict[str, list[str]]:
    plan = result.get("evaluationPlan")
    if not isinstance(plan, dict) or plan.get("version") != 1:
        return {}
    stamp = _stamp(plan.get("frozenAt"))
    candidates = _codes(plan.get("candidateCodes"), provider)
    audit_codes, complete = _audit_codes(result, provider, published)
    if (stamp is None or stamp > published or candidates is None or not complete
            or set(candidates) != audit_codes or not isinstance(plan.get("baselines"), list)):
        return {}
    if any(item.get("code") not in audit_codes for item in result.get("concepts", [])):
        return {}
    pool = _frozen_pool(result, provider, published)
    baselines = {}
    for item in plan["baselines"]:
        if not isinstance(item, dict) or item.get("id") not in BASELINES:
            continue
        codes = _codes(item.get("codes"), provider)
        if codes is None or not codes or item["id"] in baselines:
            continue
        if item["id"] == "candidate_equal_weight" and set(codes) != audit_codes:
            continue
        if item["id"] == "daily_momentum":
            snapshot = item.get("snapshot")
            if (len(codes) > 3 or not set(codes).issubset(pool) or not isinstance(snapshot, list)
                    or len(snapshot) != len(codes) or any(not isinstance(value, dict) for value in snapshot)
                    or [value.get("code") for value in snapshot] != codes
                    or any(not _number(value.get("pctChg")) for value in snapshot)):
                continue
            try:
                trade_day = date.fromisoformat(result["tradeDate"])
                if (trade_day > stamp.date()
                        or any(date.fromisoformat(value.get("tradeDate")) != trade_day for value in snapshot)
                        or any(a["pctChg"] < b["pctChg"] for a, b in zip(snapshot, snapshot[1:]))):
                    continue
            except (TypeError, KeyError, ValueError):
                continue
        baselines[item["id"]] = codes
    return baselines


def _ready(rows: list[dict], provider: str, span: tuple[str, str], mature: datetime, now: datetime) -> dict:
    ready = {}
    for row in rows:
        value, stamp = _load(row["result_json"]), _stamp(row["updated_at"])
        code = row.get("code", row.get("concept_code"))
        if (concept_provider(code) == provider and stamp is not None and mature <= stamp <= now
                and value.get("status") == "completed" and value.get("dataStatus", "ready") == "ready"
                and _number(value.get("returnPct"))
                and (value.get("entryDate"), value.get("exitDate")) == span):
            ready[code] = value
    return ready


def _new_group(version: str, provider: str, days: int) -> dict:
    return {"promptVersion": version, "conceptProvider": provider, "horizonDays": days,
            "reportCount": 0, "matureReportCount": 0, "abstentionCount": 0,
            "verifiedReportCount": 0, "baselineMissingPlanCount": 0, "calendarWindowMissingCount": 0,
            "rankingMissingUniverseCount": 0, "latestPublishedAt": "",
            "_returns": [], "_drawdowns": [], "_spans": [],
            "_baselines": {key: [] for key in BASELINES},
            "_ranking": [], "_percentiles": [], "_top20": [],
            "missedLeader": {"inPoolCount": 0, "outsidePoolCount": 0, "unknownCount": 0, "cases": []}}


def _comparison(group: dict, row, result: dict, selected: dict, prices: dict,
                pool: dict, published: datetime, span: tuple[str, str]) -> None:
    """Use the same complete observation span; selected audits supersede old ranks."""
    if not pool:
        group["rankingMissingUniverseCount"] += 1
        return
    ranked = {code: value for code, value in prices.items() if code in pool}
    ranked.update({code: value for code, value in selected.items() if code in pool})
    if len(ranked) < 2 or not selected or any(code not in ranked for code in selected):
        return
    group["_ranking"].append(100 * len(ranked) / len(pool))
    values = [item["returnPct"] for item in ranked.values()]
    for item in selected.values():
        rank = 1 + sum(value > item["returnPct"] for value in values)
        group["_percentiles"].append(100 * (len(values) - rank) / max(1, len(values) - 1))
        group["_top20"].append(100.0 if rank <= max(1, math.ceil(len(values) * .2)) else 0.0)
    leader_code = max(ranked, key=lambda code: (ranked[code]["returnPct"], code))
    leader_return = ranked[leader_code]["returnPct"]
    if any(item["returnPct"] >= leader_return for item in selected.values()):
        return
    audit_codes, complete = _audit_codes(result, group["conceptProvider"], published)
    availability = "in_pool" if leader_code in audit_codes else "outside_pool" if complete else "unknown"
    key = {"in_pool": "inPoolCount", "outside_pool": "outsidePoolCount", "unknown": "unknownCount"}[availability]
    group["missedLeader"][key] += 1
    if len(group["missedLeader"]["cases"]) < 4:
        group["missedLeader"]["cases"].append({
            "reportId": row["id"], "predictedOn": row["run_date"], "entryDate": span[0], "exitDate": span[1],
            "code": leader_code, "name": pool[leader_code], "returnPct": leader_return,
            "candidateAvailability": availability, "coveredCount": len(ranked), "totalCount": len(pool),
            "independentSample": False})


def _finish(group: dict) -> dict:
    spans = sorted(group.pop("_spans"), key=lambda value: (value[1], value[0]))
    previous, independent = "", 0
    for start, end in spans:
        if start > previous:
            independent += 1
            previous = end
    ranking, percentiles, top20 = (group.pop(key) for key in ("_ranking", "_percentiles", "_top20"))
    pairs = group.pop("_baselines")
    returns, drawdowns = group.pop("_returns"), group.pop("_drawdowns")
    return {**group, "averageReturnPct": _mean(returns),
            "averageSelectedMaxDrawdownPct": _mean(drawdowns),
            "drawdownReportCount": len(drawdowns),
            "unverifiedReportCount": group["matureReportCount"] - group["abstentionCount"] - group["verifiedReportCount"],
            "abstentionRate": round(100 * group["abstentionCount"] / group["matureReportCount"], 4)
                if group["matureReportCount"] else None,
            "nonOverlappingReportCount": independent,
            "baselines": [{"id": key, "name": name, "pairedReportCount": len(pairs[key]),
                           "averageAiReturnPct": _mean([item[0] for item in pairs[key]]),
                           "averageBaselineReturnPct": _mean([item[1] for item in pairs[key]]),
                           "averageExcessPct": _mean([item[0] - item[1] for item in pairs[key]])}
                          for key, name in BASELINES.items()],
            "ranking": {"reportCount": len(ranking), "conceptCount": len(percentiles),
                        "averagePercentile": _mean(percentiles), "top20Rate": _mean(top20),
                        "averageCoveragePct": _mean(ranking)}}


def _evaluate(db, now: datetime, limit: int) -> dict:
    from .forecast_feedback import bounds, observation_window
    from .forecast_prices import CALENDAR_KEY

    raw = db.execute("SELECT value FROM app_meta WHERE key=?", (CALENDAR_KEY,)).fetchone()
    try:
        calendar = sorted({date.fromisoformat(day).isoformat() for day in _load(raw[0]).get("dates", [])}) if raw else []
    except (ValueError, TypeError):
        calendar = []
    reports = db.execute(REPORT_QUERY, (limit, FORMAT)).fetchall()
    outcomes, feedback = {}, {}
    if reports:
        ids = [row["id"] for row in reports]
        slots = ",".join("?" for _ in ids)
        for table, target in (("comparison_outcomes", outcomes), ("forecast_feedback", feedback)):
            code_column = "code" if table == "comparison_outcomes" else "concept_code"
            for row in db.execute(f"""SELECT report_id, {code_column}, horizon_days, updated_at,
                    CASE WHEN json_valid(result_json) THEN json_remove(result_json, '$.path')
                         ELSE '{{}}' END AS result_json
                    FROM {table} WHERE report_id IN ({slots})""", ids):
                target.setdefault((row["report_id"], row["horizon_days"]), []).append(dict(row))
    groups, skipped = {}, 0
    for row in reports:
        published = _stamp(row["published_at"])
        if published is None or published > now:
            skipped += 1
            continue
        try:
            result = decode_result(row["result_json"])
            concepts = result["concepts"]
            if not isinstance(concepts, list) or any(not isinstance(item, dict) for item in concepts):
                raise ValueError("invalid concept list")
            provider = single_provider(concepts) or (result.get("conceptProvider") if not concepts else None)
            selected_codes = _codes([item.get("code") for item in concepts], provider)
            if provider not in CONCEPT_PROVIDERS or selected_codes is None:
                raise ValueError("unknown provider")
            report = {"publishedAt": row["published_at"], "result": result}
            # Validate both windows before modifying any counters.
            maturity = {days: datetime.combine(bounds(report, days)[1], time(15, 10), database.CHINA_TZ)
                        for days in (15, 30)}
            pool, plan = _frozen_pool(result, provider, published), _plan(result, provider, published)
            for days, mature in maturity.items():
                key = (row["prompt_version"] or "legacy", provider, days)
                group = groups.setdefault(key, _new_group(*key))
                group["reportCount"] += 1
                group["latestPublishedAt"] = max(group["latestPublishedAt"], published.isoformat())
                if now < mature:
                    continue
                group["matureReportCount"] += 1
                group["abstentionCount"] += int(not selected_codes)
                group["baselineMissingPlanCount"] += int(not plan)
                if not selected_codes:
                    continue  # Waiting is an abstention, not a fabricated cash return.
                window, sessions = observation_window(report, days, calendar, now)
                if not sessions:
                    group["calendarWindowMissingCount"] += int(window.get("dataStatus") == "unavailable")
                    continue
                span = (sessions[0], sessions[-1])
                selected = _ready(feedback.get((row["id"], days), []), provider, span, mature, now)
                if any(code not in selected for code in selected_codes):
                    continue
                selected = {code: selected[code] for code in selected_codes}
                ai_return = _mean([item["returnPct"] for item in selected.values()])
                group["verifiedReportCount"] += 1
                group["_returns"].append(ai_return)
                group["_spans"].append(span)
                if all(_number(item.get("maxDrawdownPct")) for item in selected.values()):
                    group["_drawdowns"].append(_mean([item["maxDrawdownPct"] for item in selected.values()]))
                prices = _ready(outcomes.get((row["id"], days), []), provider, span, mature, now)
                prices.update(selected)
                for baseline, codes in plan.items():
                    if all(code in prices for code in codes):
                        group["_baselines"][baseline].append((ai_return, _mean([prices[code]["returnPct"] for code in codes])))
                _comparison(group, row, result, selected, prices, pool, published, span)
        except (AttributeError, TypeError, ValueError, KeyError, IndexError):
            skipped += 1
    finished = [_finish(group) for group in groups.values()]
    # Avoid lexicographic version sorting (v10 vs v9); latest publication wins.
    finished.sort(key=lambda group: (group["latestPublishedAt"], -group["horizonDays"]), reverse=True)
    return {"asOf": now.isoformat(timespec="seconds"), "reportLimit": limit,
            "readReportCount": len(reports), "skippedReportCount": skipped, "groups": finished,
            "calendarAvailable": bool(calendar),
            "sampleUnit": "每日首次成功预测；每份报告内概念等权，再对报告等权",
            "limits": "仅已到期且同源、同区间、截至评估时已核对的数据。缺失不按零收益；观望不计算收益。基线只使用预测时冻结名单，旧记录不补造。排名与漏选仅指冻结且已覆盖范围；回撤为概念回撤均值，并非组合回撤。重叠窗口非独立样本，非重叠报告数也不证明统计独立；不同版本的日期与行情不同，不能据此断言效果提升。30日为原15日预测的延伸观察。"}


def evaluation_payload(db=None, *, as_of: datetime | None = None, limit: int = 120) -> dict:
    """Reuse the history read transaction when supplied; never fetch or write."""
    now = as_of or datetime.now(database.CHINA_TZ)
    if now.tzinfo is None:
        raise ValueError("Evaluation requires a timezone-aware timestamp")
    now = now.astimezone(database.CHINA_TZ)
    limit = max(1, min(120, limit))
    if db is not None:
        return _evaluate(db, now, limit)
    with database.connection() as connection:
        connection.execute("BEGIN")
        return _evaluate(connection, now, limit)
