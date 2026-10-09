from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import database, forecast_history
from backend.forecast_comparison_feedback import missed_leader_cases
from backend.forecast_prices import CALENDAR_KEY, FeedbackPriceClient


def moment(value):
    return datetime.fromisoformat(value + "+08:00")


class MissedLeaderFeedbackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "feedback.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        calendar = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(75)
                    if (date(2026, 9, 1) + timedelta(days=i)).weekday() < 5]
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": calendar}))
        self.result = {"summary": "test", "window": {"startDate": "2026-09-13"},
                       "concepts": [{"code": "BK1001", "name": "原预测"}],
                       "comparisonUniverse": {"asOf": "2026-09-12T18:00:00+08:00", "scope": "有限已保存范围",
                                              "boards": [{"code": "BK1001", "name": "原预测"},
                                                         {"code": "BK1002", "name": "漏选方向"},
                                                         {"code": "BK1003", "name": "行情未齐"}]}}
        self.now = moment("2026-10-10T18:00:00")

    def report(self, result=None, day="2026-09-12", published="2026-09-12T18:00:00+08:00"):
        result = result or self.result
        with database.connection() as db:
            cursor = db.execute("""INSERT INTO forecast_reports
                (run_key,run_date,published_at,model,prompt_version,result_json,overview_json)
                VALUES (?,?,?,?,?,?,?)""", (str(datetime.now().timestamp()), day, published, "glm-5.3", "test",
                                           json.dumps(result), json.dumps(result)))
            return cursor.lastrowid

    def outcome(self, report_id, code, change=12, *, updated="2026-09-28T18:00:00+08:00",
                days=15, selected=False, **overrides):
        value = {"status": "completed", "dataStatus": "ready", "returnPct": change,
                 "entryDate": "2026-09-14", "exitDate": "2026-09-25", "targetDate": "2026-09-27",
                 "source": "dated fixture", **overrides}
        table = "forecast_feedback" if selected else "comparison_outcomes"
        with database.connection() as db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?)",
                       (report_id, code, days, json.dumps(value), updated))

    def complete_case(self, result=None, **report_args):
        report_id = self.report(result, **report_args)
        self.outcome(report_id, "BK1001", -3)
        self.outcome(report_id, "BK1002", 12)
        self.outcome(report_id, "BK1001", -3, selected=True)
        return report_id

    def test_partial_same_window_feedback_uses_original_audit_and_reads_no_network(self):
        result = copy.deepcopy(self.result)
        result["candidateAudit"] = {"complete": True, "candidates": [
            {"code": "BK1002", "name": "漏选方向", "selected": False,
             "technicalData": {"return5d": 1.4, "aboveMa20": True}}]}
        self.complete_case(result)
        with (patch.object(FeedbackPriceClient, "calendar", side_effect=AssertionError("network")),
              patch.object(FeedbackPriceClient, "history", side_effect=AssertionError("network"))):
            case = forecast_history.feedback_context(self.now)["missedLeaderCases"][0]
        self.assertEqual(case["candidateAvailability"], "in_pool")
        self.assertEqual(case["prePredictionTechnicalData"]["return5d"], 1.4)
        self.assertEqual(case["coverage"]["coveredCount"], 2)
        self.assertEqual(case["coverage"]["totalCount"], 3)
        self.assertFalse(case["coverage"]["fullCoverage"])
        self.assertEqual(case["gapPct"], -15)
        self.assertEqual(case["verifiedSelectedCount"], 1)
        self.assertFalse(case["independentSample"])

    def test_old_or_partial_audit_cannot_be_diagnosed_as_outside_pool(self):
        for audit, expected in ((None, "unknown"), ({"candidates": []}, "unknown"),
                                ({"complete": True, "candidates": []}, "outside_pool")):
            with self.subTest(audit=audit):
                result = copy.deepcopy(self.result)
                result["candidateAudit"] = audit
                report_id = self.complete_case(result)
                self.assertEqual(missed_leader_cases(self.now)[0]["candidateAvailability"], expected)
                with database.connection() as db:
                    db.execute("DELETE FROM comparison_outcomes WHERE report_id=?", (report_id,))
                    db.execute("DELETE FROM forecast_feedback WHERE report_id=?", (report_id,))
                    db.execute("DELETE FROM forecast_reports WHERE id=?", (report_id,))

    def test_future_check_naive_check_and_pre_maturity_check_cannot_leak(self):
        report_id = self.complete_case()
        for checked in ("2026-10-11T18:00:00+08:00", "2026-09-28T18:00:00", "2026-09-27T14:00:00+08:00"):
            self.outcome(report_id, "BK1002", updated=checked)
            self.assertEqual(missed_leader_cases(self.now), [])
        self.outcome(report_id, "BK1002", updated=self.now.isoformat())
        self.assertEqual(len(missed_leader_cases(self.now)), 1)

    def test_intraday_expiry_and_future_publication_are_excluded(self):
        self.complete_case()
        self.assertEqual(missed_leader_cases(moment("2026-09-27T14:00:00")), [])
        with database.connection() as db:
            db.execute("UPDATE forecast_reports SET published_at='2026-10-11T18:00:00+08:00'")
        self.assertEqual(missed_leader_cases(self.now), [])

    def test_incomplete_mismatched_and_different_provider_outcomes_cannot_win(self):
        result = copy.deepcopy(self.result)
        result["comparisonUniverse"]["boards"].append({"code": "THS:885955", "name": "同名异源"})
        report_id = self.complete_case(result)
        self.outcome(report_id, "THS:885955", 200)
        for fields in ({"exitDate": "2026-09-24"}, {"entryDate": "2026-09-15"},
                       {"status": "tracking"}, {"returnPct": float("nan")}, {"dataStatus": "unavailable"}):
            self.outcome(report_id, "BK1002", **fields)
            self.assertEqual(missed_leader_cases(self.now), [])

    def test_selected_returns_are_excluded_if_different_range_or_later_check(self):
        report_id = self.complete_case()
        self.outcome(report_id, "BK1001", -3, selected=True, exitDate="2026-09-24")
        case = missed_leader_cases(self.now)[0]
        self.assertIsNone(case["averageSelectedReturn"])
        self.assertIsNone(case["gapPct"])
        self.assertEqual(case["selectedCount"], 1)
        self.assertEqual(case["verifiedSelectedCount"], 0)

    def test_regenerated_and_overlapping_winner_cases_are_not_independent_samples(self):
        self.complete_case()
        self.complete_case(day="2026-09-12", published="2026-09-12T22:00:00+08:00")
        second = self.complete_case(day="2026-09-13", published="2026-09-13T12:00:00+08:00")
        cases = missed_leader_cases(self.now)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["reportId"], second)
        self.assertEqual(cases[0]["relatedObservationCount"], 2)

    def test_unavailable_calendar_and_future_backfill_cannot_reconstruct_a_case(self):
        result = copy.deepcopy(self.result)
        universe = result.pop("comparisonUniverse")
        report_id = self.complete_case(result)
        with database.connection() as db:
            db.execute("INSERT INTO comparison_universes VALUES (?,?,?,?)", (
                report_id, "2026-10-11T18:00:00+08:00", "事后回填范围", json.dumps(universe["boards"])))
        self.assertEqual(missed_leader_cases(self.now), [])
        with database.connection() as db:
            db.execute("UPDATE comparison_universes SET as_of='2026-09-28T18:00:00+08:00'")
        case = missed_leader_cases(self.now)[0]
        self.assertEqual(case["coverage"]["universeOrigin"], "backfilled")
        self.assertEqual(case["candidateAvailability"], "unknown")
        database.set_meta(CALENDAR_KEY, "broken")
        self.assertEqual(missed_leader_cases(self.now), [])

    def test_selected_winner_is_not_missed(self):
        report_id = self.complete_case()
        self.outcome(report_id, "BK1001", 15)
        self.assertEqual(missed_leader_cases(self.now), [])

    def test_tied_selected_winner_is_not_missed_due_to_code_order(self):
        result = copy.deepcopy(self.result)
        result["concepts"] = [{"code": "BK1003", "name": "同收益入选方向"}]
        report_id = self.complete_case(result)
        self.outcome(report_id, "BK1003", 12)
        self.assertEqual(missed_leader_cases(self.now), [])

    def test_stronger_or_tied_selected_feedback_overrides_missing_or_stale_comparison(self):
        report_id = self.complete_case()
        for change in (12, 20):
            with self.subTest(selected_return=change):
                self.outcome(report_id, "BK1001", change, selected=True)
                with database.connection() as db:
                    db.execute("DELETE FROM comparison_outcomes WHERE report_id=? AND code='BK1001'", (report_id,))
                self.assertEqual(missed_leader_cases(self.now), [])
                self.outcome(report_id, "BK1001", -3, exitDate="2026-09-24")
                self.assertEqual(missed_leader_cases(self.now), [])
        # Future audit evidence still cannot suppress a valid historical case.
        self.outcome(report_id, "BK1001", 20, selected=True, updated="2026-10-11T18:00:00+08:00")
        self.assertEqual(len(missed_leader_cases(self.now)), 1)

    def test_stronger_selected_feedback_neither_inflates_overlaps_nor_uses_case_slots(self):
        older = self.complete_case()
        self.outcome(older, "BK1001", 20, selected=True)
        newer = self.complete_case(day="2026-09-13", published="2026-09-13T12:00:00+08:00")
        cases = missed_leader_cases(self.now)
        self.assertEqual(cases[0]["reportId"], newer)
        self.assertEqual(cases[0]["relatedObservationCount"], 1)
        # Once the newest report is a false miss, a one-case budget remains
        # available for the older valid case.
        self.outcome(older, "BK1001", -3, selected=True)
        self.outcome(newer, "BK1001", 20, selected=True)
        cases = missed_leader_cases(self.now, limit=1)
        self.assertEqual([case["reportId"] for case in cases], [older])

    def test_at_most_four_distinct_missed_winners_enter_prompt(self):
        for number in range(6):
            result = copy.deepcopy(self.result)
            code = f"BK20{number:02}"
            result["comparisonUniverse"]["boards"].append({"code": code, "name": f"独立方向{number}"})
            report_id = self.report(result, day=f"2026-09-{12 + number}")
            self.outcome(report_id, code, 20)
        self.assertEqual(len(missed_leader_cases(self.now, limit=20)), 4)


if __name__ == "__main__":
    unittest.main()
