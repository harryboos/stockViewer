from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import database, forecast_evaluation, forecast_history
from backend.forecast_evaluation import REPORT_QUERY, evaluation_payload
from backend.forecast_prices import CALENDAR_KEY, FeedbackPriceClient
from backend.report_storage import FORMAT, encode_result


class ForecastEvaluationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "evaluation.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        calendar = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(100)
                    if (date(2026, 9, 1) + timedelta(days=i)).weekday() < 5]
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": calendar}))
        self.now = datetime.fromisoformat("2026-10-30T18:00:00+08:00")
        self.result = {
            "summary": "test", "window": {"startDate": "2026-09-13"}, "conceptProvider": "eastmoney",
            "tradeDate": "2026-09-11",
            "concepts": [{"code": "BK1001", "name": "原预测"}],
            "comparisonUniverse": {"asOf": "2026-09-12T17:00:00+08:00", "scope": "已保存范围",
                "boards": [{"code": "BK1001", "name": "原预测"}, {"code": "BK1002", "name": "候选赢家"},
                           {"code": "BK1003", "name": "未获取行情"}]},
            "candidateAudit": {"complete": True, "decisionAt": "2026-09-12T17:30:00+08:00",
                "candidates": [{"code": "BK1001"}, {"code": "BK1002"}]},
            "evaluationPlan": {"version": 1, "frozenAt": "2026-09-12T17:30:00+08:00",
                "candidateCodes": ["BK1001", "BK1002"], "baselines": [
                    {"id": "daily_momentum", "codes": ["BK1002"], "snapshot": [
                        {"code": "BK1002", "name": "候选赢家", "tradeDate": "2026-09-11", "pctChg": 3.5}]},
                    {"id": "candidate_equal_weight", "codes": ["BK1001", "BK1002"]}]}}

    def report(self, result=None, day="2026-09-12", published="2026-09-12T18:00:00+08:00", version="v7"):
        result = copy.deepcopy(self.result if result is None else result)
        with database.connection() as db:
            return db.execute("""INSERT INTO forecast_reports
                (run_key,run_date,published_at,model,prompt_version,result_json,overview_json)
                VALUES (?,?,?,?,?,?,?)""", (str(datetime.now().timestamp()), day, published, "glm-5.3", version,
                                           json.dumps(result), json.dumps(result))).lastrowid

    def outcome(self, report_id, code, change=12, *, updated="2026-09-28T18:00:00+08:00",
                days=15, selected=False, **overrides):
        value = {"status": "completed", "dataStatus": "ready", "returnPct": change,
                 "entryDate": "2026-09-14", "exitDate": "2026-09-25", "targetDate": "2026-09-27",
                 "maxDrawdownPct": -4, "source": "dated fixture", **overrides}
        with database.connection() as db:
            table = "forecast_feedback" if selected else "comparison_outcomes"
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?)",
                       (report_id, code, days, json.dumps(value), updated))

    def complete(self, result=None, **kwargs):
        report_id = self.report(result, **kwargs)
        self.outcome(report_id, "BK1001", -3, selected=True)
        self.outcome(report_id, "BK1002", 12)
        return report_id

    def group(self, days=15, version="v7", provider="eastmoney"):
        return next(group for group in evaluation_payload(as_of=self.now)["groups"]
                    if (group["horizonDays"], group["promptVersion"], group["conceptProvider"]) == (days, version, provider))

    def test_pairing_coverage_and_original_candidate_attribution(self):
        self.complete()
        group = self.group()
        self.assertEqual(group["averageReturnPct"], -3)
        self.assertEqual(group["averageSelectedMaxDrawdownPct"], -4)
        self.assertEqual(group["baselines"][0]["averageExcessPct"], -15)
        self.assertEqual(group["baselines"][1]["averageBaselineReturnPct"], 4.5)
        self.assertEqual(group["baselines"][1]["averageExcessPct"], -7.5)
        self.assertEqual(group["ranking"]["averageCoveragePct"], 66.6667)
        self.assertEqual(group["ranking"]["averagePercentile"], 0)
        self.assertEqual(group["ranking"]["top20Rate"], 0)
        self.assertEqual(group["missedLeader"]["inPoolCount"], 1)
        self.assertFalse(any(key.startswith("_") for key in group))

    def test_no_network_no_database_writes_and_shared_history_snapshot(self):
        self.complete()
        with database.connection() as db:
            before = list(db.iterdump())
        with (patch.object(FeedbackPriceClient, "calendar", side_effect=AssertionError("network")),
              patch.object(FeedbackPriceClient, "history", side_effect=AssertionError("network"))):
            payload = forecast_history.history_payload(as_of=self.now)
        with database.connection() as db:
            self.assertEqual(before, list(db.iterdump()))
        self.assertEqual(payload["evaluation"]["groups"][0]["verifiedReportCount"], 1)

    def test_read_projection_preserves_plain_and_packed_audits_without_large_prose_or_paths(self):
        report_id = self.complete()
        expected = evaluation_payload(as_of=self.now)
        result = copy.deepcopy(self.result)
        marker = "unneeded-prose-" * 2000
        result["summary"] = marker
        result["concepts"][0].update(thesis=marker, technicalData={"extra": marker},
            technical={"sources": [{"id": "BK1001:market", "kind": "news", "excerpt": marker}] * 8})
        result["candidateAudit"]["candidates"][0].update(news=[{"excerpt": marker}], technicalData={"extra": marker})
        packed = encode_result(result)
        self.assertEqual(json.loads(packed)["storageFormat"], FORMAT)
        for serialized in (json.dumps(result), packed):
            with self.subTest(storage="packed" if serialized == packed else "plain"):
                with database.connection() as db:
                    db.execute("UPDATE forecast_reports SET result_json=? WHERE id=?", (serialized, report_id))
                    projected = db.execute(REPORT_QUERY, (120, FORMAT)).fetchone()["result_json"]
                    for table in ("comparison_outcomes", "forecast_feedback"):
                        for row in db.execute(f"SELECT rowid,result_json FROM {table}").fetchall():
                            value = json.loads(row["result_json"])
                            value["path"] = [{"unused": "unneeded-path-" * 100} for _ in range(100)]
                            db.execute(f"UPDATE {table} SET result_json=? WHERE rowid=?", (json.dumps(value), row["rowid"]))
                self.assertNotIn("unneeded-prose", projected)
                self.assertLess(len(projected), len(serialized) / 10)
                compact = json.loads(projected)
                self.assertIs(compact["candidateAudit"]["complete"], True)
                self.assertEqual(compact["evaluationPlan"], result["evaluationPlan"])
                self.assertEqual(compact["comparisonUniverse"], result["comparisonUniverse"])
                with patch.object(forecast_evaluation, "_ready", wraps=forecast_evaluation._ready) as ready:
                    self.assertEqual(evaluation_payload(as_of=self.now), expected)
                self.assertTrue(ready.called)
                for call in ready.call_args_list:
                    for value in call.args[0]:
                        self.assertNotIn("path", json.loads(value["result_json"]))
                        self.assertNotIn("unneeded-path", value["result_json"])

    def test_missing_ai_or_baseline_never_becomes_zero_or_partial_pair(self):
        result = copy.deepcopy(self.result)
        result["concepts"].append({"code": "BK1002", "name": "未核对预测"})
        report_id = self.complete(result)
        self.assertIsNone(self.group()["averageReturnPct"])
        self.assertEqual(self.group()["baselines"][0]["pairedReportCount"], 0)
        self.outcome(report_id, "BK1002", 12, selected=True)
        self.assertEqual(self.group()["averageReturnPct"], 4.5)
        with database.connection() as db:
            db.execute("DELETE FROM forecast_feedback WHERE concept_code='BK1002'")
            db.execute("DELETE FROM comparison_outcomes")
            db.execute("UPDATE forecast_reports SET result_json=?", (json.dumps(self.result),))
        self.assertEqual(self.group()["averageReturnPct"], -3)
        self.assertEqual(self.group()["baselines"][0]["pairedReportCount"], 0)
        self.assertIsNone(self.group()["baselines"][0]["averageExcessPct"])

    def test_future_naive_premature_and_wrong_span_outcomes_are_excluded(self):
        report_id = self.complete()
        for kwargs in ({"updated": "2026-10-31T18:00:00+08:00"},
                       {"updated": "2026-09-28T18:00:00"}, {"updated": "2026-09-27T14:00:00+08:00"},
                       {"exitDate": "2026-09-24"}, {"status": "tracking"}, {"dataStatus": "unavailable"},
                       {"returnPct": float("nan")}, {"returnPct": True}):
            with self.subTest(kwargs=kwargs):
                self.outcome(report_id, "BK1001", -3, selected=True, **kwargs)
                self.assertEqual(self.group()["verifiedReportCount"], 0)
        self.outcome(report_id, "BK1001", -3, selected=True, updated=self.now.isoformat())
        self.assertEqual(self.group()["verifiedReportCount"], 1)

    def test_no_calendar_no_reconstructed_sessions(self):
        self.complete()
        database.set_meta(CALENDAR_KEY, "broken")
        payload = evaluation_payload(as_of=self.now)
        self.assertFalse(payload["calendarAvailable"])
        self.assertEqual(self.group()["verifiedReportCount"], 0)
        self.assertEqual(self.group()["matureReportCount"], 1)

    def test_legacy_and_backfilled_universes_cannot_create_baseline_or_rank(self):
        result = copy.deepcopy(self.result)
        result.pop("evaluationPlan")
        result.pop("comparisonUniverse")
        result.pop("candidateAudit")
        report_id = self.complete(result)
        with database.connection() as db:
            db.execute("INSERT INTO comparison_universes VALUES (?,?,?,?)", (
                report_id, "2026-09-28T18:00:00+08:00", "回填", json.dumps(self.result["comparisonUniverse"]["boards"])))
        group = self.group()
        self.assertEqual(group["baselineMissingPlanCount"], 1)
        self.assertEqual(group["rankingMissingUniverseCount"], 1)
        self.assertEqual(group["ranking"]["reportCount"], 0)
        self.assertEqual(group["baselines"][0]["pairedReportCount"], 0)

    def test_invalid_and_future_plans_cannot_be_evaluated(self):
        report_id = self.complete()
        for field, value in (("frozenAt", "2026-09-13T18:00:00+08:00"),
                             ("candidateCodes", ["BK1001", "THS:885955"]),
                             ("candidateCodes", ["BK1001"]), ("version", 2)):
            with self.subTest(field=field):
                result = copy.deepcopy(self.result)
                result["evaluationPlan"][field] = value
                with database.connection() as db:
                    db.execute("UPDATE forecast_reports SET result_json=? WHERE id=?", (json.dumps(result), report_id))
                self.assertEqual(self.group()["baselines"][0]["pairedReportCount"], 0)

    def test_daily_momentum_baseline_may_include_frozen_snapshot_outside_model_pool(self):
        result = copy.deepcopy(self.result)
        result["evaluationPlan"]["baselines"][0] = {"id": "daily_momentum", "codes": ["BK1003"],
            "snapshot": [{"code": "BK1003", "name": "池外当日最强", "tradeDate": "2026-09-11", "pctChg": 5.2}]}
        report_id = self.complete(result)
        self.outcome(report_id, "BK1003", 20)
        self.assertEqual(self.group()["baselines"][0]["averageBaselineReturnPct"], 20)
        self.assertEqual(self.group()["baselines"][0]["averageExcessPct"], -23)

    def test_daily_momentum_requires_matching_order_date_quotes_and_universe(self):
        report_id = self.complete()
        fixtures = [
            [{"code": "BK1002", "tradeDate": "2026-09-10", "pctChg": 3}],
            [{"code": "BK1002", "tradeDate": "2026-09-11", "pctChg": float("nan")}],
            [{"code": "BK1002", "tradeDate": "2026-09-11", "pctChg": True}],
            [{"code": "BK1999", "tradeDate": "2026-09-11", "pctChg": 3}],
            [{"code": "BK1002", "tradeDate": "2026-09-11", "pctChg": 2},
             {"code": "BK1001", "tradeDate": "2026-09-11", "pctChg": 3}],
        ]
        for snapshot in fixtures:
            with self.subTest(snapshot=snapshot):
                result = copy.deepcopy(self.result)
                result["evaluationPlan"]["baselines"][0].update(snapshot=snapshot, codes=[row["code"] for row in snapshot])
                with database.connection() as db:
                    db.execute("UPDATE forecast_reports SET result_json=? WHERE id=?", (json.dumps(result), report_id))
                self.assertEqual(self.group()["baselines"][0]["pairedReportCount"], 0)
                self.assertEqual(self.group()["baselines"][1]["pairedReportCount"], 1)

    def test_daily_first_report_and_overlapping_counts(self):
        self.complete()
        self.complete(published="2026-09-12T22:00:00+08:00", version="regenerated")
        self.complete(day="2026-09-13", published="2026-09-13T01:00:00+08:00")
        group = self.group()
        self.assertEqual(group["reportCount"], 2)
        self.assertEqual(group["verifiedReportCount"], 2)
        self.assertEqual(group["nonOverlappingReportCount"], 1)
        self.assertEqual(len(evaluation_payload(as_of=self.now)["groups"]), 2)

    def test_abstention_rate_is_mature_report_rate_and_not_cash_return(self):
        self.complete()
        result = copy.deepcopy(self.result)
        result["concepts"] = []
        self.report(result, day="2026-09-13", published="2026-09-13T01:00:00+08:00")
        group = self.group()
        self.assertEqual(group["abstentionRate"], 50)
        self.assertEqual(group["averageReturnPct"], -3)
        self.assertEqual(group["verifiedReportCount"], 1)
        self.now = datetime.fromisoformat("2026-09-27T14:00:00+08:00")
        self.assertEqual(self.group()["matureReportCount"], 0)
        self.assertIsNone(self.group()["abstentionRate"])

    def test_versions_providers_and_horizons_are_not_mixed(self):
        report_id = self.complete()
        self.outcome(report_id, "BK1001", 5, selected=True, days=30,
                     exitDate="2026-10-12", updated="2026-10-13T18:00:00+08:00")
        self.complete(day="2026-09-13", published="2026-09-13T01:00:00+08:00", version="v6")
        result = copy.deepcopy(self.result)
        result["concepts"] = [{"code": "THS:885955", "name": "同名异源"}]
        result["conceptProvider"] = "ths"
        ths = self.report(result, day="2026-09-14", published="2026-09-14T01:00:00+08:00")
        self.outcome(ths, "THS:885955", 30, selected=True)
        self.assertEqual(self.group()["averageReturnPct"], -3)
        self.assertEqual(self.group(days=30)["averageReturnPct"], 5)
        self.assertEqual(self.group(provider="ths")["averageReturnPct"], 30)
        self.assertEqual(self.group(version="v6")["verifiedReportCount"], 1)

    def test_same_return_selected_winner_is_not_missed(self):
        report_id = self.complete()
        self.outcome(report_id, "BK1001", 12, selected=True)
        group = self.group()
        self.assertEqual(group["ranking"]["averagePercentile"], 100)
        self.assertEqual(group["ranking"]["top20Rate"], 100)
        self.assertEqual(group["missedLeader"]["cases"], [])

    def test_attribution_requires_frozen_audit_not_price_explanations(self):
        report_id = self.complete()
        for audit, expected in ((None, "unknown"),
                                ({"complete": True, "decisionAt": "2026-09-12T17:30:00+08:00",
                                  "candidates": [{"code": "BK1001"}]}, "outside_pool"),
                                ({"complete": True, "decisionAt": "2026-09-13T17:30:00+08:00",
                                  "candidates": [{"code": "BK1001"}]}, "unknown")):
            result = copy.deepcopy(self.result)
            result["candidateAudit"] = audit
            with database.connection() as db:
                db.execute("UPDATE forecast_reports SET result_json=? WHERE id=?", (json.dumps(result), report_id))
            self.assertEqual(self.group()["missedLeader"]["cases"][0]["candidateAvailability"], expected)


if __name__ == "__main__":
    unittest.main()
