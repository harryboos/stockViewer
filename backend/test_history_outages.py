from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from backend import concept_comparison, database, forecast_feedback, forecast_history, research_jobs
from backend.concept_data import ConceptResearchClient
from backend.forecast_prices import CALENDAR_KEY, FeedbackPriceClient
from backend.test_forecast_feedback import calendar_fixture, instant, report_fixture


class HistoryTransportTests(unittest.TestCase):
    def test_all_hosts_are_tried_directly_before_environment_proxy(self):
        client = ConceptResearchClient()
        first, second = Mock(), Mock()
        for session in (first, second):
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)
        first.get.side_effect = requests.ConnectionError("disconnected")
        second.get.return_value.text = '{"data":{"code":"BK1152"}}'
        with (patch.object(client, "_session", side_effect=[first, second]) as session,
              patch("backend.concept_data.requests.utils.get_environ_proxies", return_value={"https": "proxy"})):
            result = client._json(["https://first.test", "https://second.test"], {})
        self.assertEqual(result["data"]["code"], "BK1152")
        self.assertEqual([call.kwargs["trust_env"] for call in session.call_args_list], [False, False])

    def test_connection_failure_is_not_reported_as_empty_success(self):
        client = FeedbackPriceClient()
        message = "概念研究数据源暂不可用：连接中断或网络不可达"
        with patch.object(client, "_json", side_effect=RuntimeError(message)):
            result = client.history("BK1152", "2026-09-18", "2026-09-21")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["error"], message)


class HistoryOutageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(directory.name) / "history.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        self.now = instant("2026-09-22T08:00:00")
        report = report_fixture()["result"]
        report["window"].update(generatedOn="2026-09-17", startDate="2026-09-18", endDate="2026-10-02")
        report["concepts"] = [{**report["concepts"][0], "code": code, "name": name}
                              for code, name in (("BK1152", "高带宽内存"), ("BK1136", "光通信模块"), ("BK0917", "半导体概念"))]
        report["comparisonUniverse"] = {"asOf": "2026-09-17T00:00:00+08:00", "scope": "测试概念范围",
                                        "boards": [{"code": c["code"], "name": c["name"]} for c in report["concepts"]]}
        token = database.start_ai_run("forecast:glm", "glm-5.3", "2026-09-17", "test", True)
        with patch.object(database, "now_iso", return_value="2026-09-17T00:38:00+08:00"):
            database.finish_ai_run("forecast:glm", "2026-09-17", report, None, token)
        database.set_meta(CALENDAR_KEY, json.dumps({"dates": calendar_fixture(), "fetchedOn": database.china_date()}))

    @staticmethod
    def outage(*args):
        return {"rows": [], "error": "概念研究数据源暂不可用：连接中断或网络不可达"}

    @staticmethod
    def healthy(*args):
        return {"rows": [{"date": "2026-09-18", "open": 100, "close": 102, "high": 103, "low": 99},
                         {"date": "2026-09-21", "open": 102, "close": 108, "high": 109, "low": 101}],
                "source": "test", "url": "https://example.org"}

    def clock(self, clock):
        clock.now.return_value = self.now
        clock.combine, clock.fromisoformat = datetime.combine, datetime.fromisoformat

    def test_empty_feedback_marks_refresh_failed_with_actual_missing_dates(self):
        token = forecast_feedback.claim_refresh()
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch("backend.research_data.index_history", side_effect=self.outage),
              patch.object(forecast_feedback, "datetime") as clock):
            self.clock(clock)
            forecast_feedback.refresh_feedback(token)
        data = forecast_history.history_payload(as_of=self.now)
        self.assertEqual(data["refresh"]["status"], "failed")
        self.assertEqual(data["refresh"]["outcomesMissing"], 6)
        outcome = data["reports"][0]["concepts"][0]["outcomes"]["15"]
        self.assertIsNone(outcome["returnPct"])
        self.assertEqual(outcome["status"], "tracking")
        self.assertEqual(outcome["missingDates"], ["2026-09-18", "2026-09-21"])
        self.assertIn("连接中断", outcome["note"])

    def test_comparison_failure_is_visible_and_recovery_can_rank_the_same_window(self):
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(concept_comparison, "index_history", side_effect=self.outage),
              patch.object(concept_comparison, "datetime") as clock):
            self.clock(clock)
            token = research_jobs._claim("comparison")
            research_jobs._run("comparison", token)
        self.assertEqual(research_jobs.job_status("comparison")["status"], "failed")
        payload = concept_comparison.comparison_payload(1, 15, self.now)
        self.assertEqual(payload["coveredCount"], 0)
        self.assertEqual(payload["missingCount"], 3)
        self.assertIn("连接中断", payload["message"])
        self.assertIsNone(payload["strongest"])
        self.assertIsNotNone(payload["lastCheckedAt"])
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(concept_comparison, "index_history", side_effect=self.healthy),
              patch.object(concept_comparison, "datetime") as clock):
            self.clock(clock)
            concept_comparison.refresh_comparisons(Mock())
        payload = concept_comparison.comparison_payload(1, 15, self.now)
        self.assertEqual((payload["entryDate"], payload["exitDate"]), ("2026-09-18", "2026-09-21"))
        self.assertEqual(payload["coveredCount"], 3)
        self.assertEqual(payload["strongest"]["returnPct"], 8)
        self.assertIsNone(payload["message"])

    def test_partial_comparison_keeps_verified_values_while_reporting_failure(self):
        def history(code, *args):
            return self.healthy() if code == "BK1152" else self.outage()
        with (patch.object(FeedbackPriceClient, "calendar", return_value=calendar_fixture()),
              patch.object(concept_comparison, "index_history", side_effect=history),
              patch.object(concept_comparison, "datetime") as clock):
            self.clock(clock)
            with self.assertRaisesRegex(RuntimeError, "已核对 2/6"):
                concept_comparison.refresh_comparisons(Mock())
        payload = concept_comparison.comparison_payload(1, 15, self.now)
        self.assertEqual(payload["coveredCount"], 1)
        self.assertEqual(payload["missingCount"], 2)
        self.assertEqual(payload["strongest"]["code"], "BK1152")
        self.assertFalse(payload["fullCoverage"])
