from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import concept_ai, concept_forecast, database, forecast_history, research_jobs
from backend.report_storage import decode_result


class ResearchNamespaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(folder.name) / "namespaces.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()
        self.day = database.china_date()

    def report(self):
        start = datetime.fromisoformat(self.day).date() + timedelta(days=1)
        return {"summary": "保留原预测和实际模型，不因更换供应商重写。",
                "window": {"startDate": start.isoformat(), "endDate": (start + timedelta(days=14)).isoformat()},
                "concepts": [{"code": "BK1001", "name": "测试概念"}]}

    def test_both_models_share_each_research_module_lease_even_across_midnight(self):
        yesterday = (datetime.fromisoformat(self.day).date() - timedelta(days=1)).isoformat()
        for module in ("concept", "forecast"):
            with self.subTest(module=module):
                old = database.start_ai_run(f"{module}:glm", "glm-5.3", yesterday, "old")
                self.assertIsNotNone(old)
                self.assertIsNone(database.start_ai_run(f"{module}:deepseek", "deepseek-flash", self.day, "new", True))
                active = database.read_ai_run(f"{module}:deepseek", self.day, include_result=False)
                self.assertEqual((active["status"], active["model"], active["provider"], active["runDate"]),
                                 ("running", "glm-5.3", f"{module}:glm", yesterday))
                database.finish_ai_run(f"{module}:glm", yesterday, None, "停止旧任务", old)
                new = database.start_ai_run(f"{module}:deepseek", "deepseek-flash", self.day, "new")
                self.assertIsNotNone(new)
                database.finish_ai_run(f"{module}:deepseek", self.day, None, "测试结束", new)

    def test_concurrent_cross_model_claims_have_one_winner_but_separate_modules_can_run(self):
        providers = ["forecast:glm", "forecast:deepseek"] * 4
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda provider: database.start_ai_run(provider, "model", self.day, "v1", True), providers))
        self.assertEqual(sum(token is not None for token in claims), 1)
        self.assertIsNotNone(database.start_ai_run("concept:deepseek", "deepseek-flash", self.day, "v1"))
        self.assertIsNotNone(database.start_ai_run("deepseek", "deepseek-flash", self.day, "v1"))

    def test_expired_old_model_cannot_publish_after_new_model_claims(self):
        old = database.start_ai_run("forecast:glm", "glm-5.3", self.day, "v1")
        expired = (datetime.now(database.CHINA_TZ) - timedelta(seconds=database.FORECAST_RUN_LEASE_SECONDS + 1)).isoformat()
        with database.connection() as db:
            db.execute("UPDATE ai_runs SET started_at=? WHERE run_token=?", (expired, old))
        new = database.start_ai_run("forecast:deepseek", "deepseek-flash", self.day, "v2")
        self.assertIsNotNone(new)
        database.finish_ai_run("forecast:glm", self.day, self.report(), None, old)
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM ai_attempts WHERE token=?", (old,)).fetchone()[0], "failed")
            self.assertEqual(db.execute("SELECT status FROM ai_runs WHERE run_token=?", (old,)).fetchone()[0], "failed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM forecast_reports").fetchone()[0], 0)
        database.finish_ai_run("forecast:deepseek", self.day, self.report(), None, new)
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT model FROM forecast_reports").fetchone()[0], "deepseek-flash")

    def test_new_success_preserves_old_archive_and_daily_first_version(self):
        old = database.start_ai_run("forecast:glm", "glm-5.3", self.day, "old")
        database.finish_ai_run("forecast:glm", self.day, self.report(), None, old)
        with database.connection() as db:
            before = tuple(db.execute("SELECT * FROM forecast_reports WHERE run_key=?", (old,)).fetchone())
        self.assertIsNone(database.read_ai_run("forecast:deepseek", self.day))
        new = database.start_ai_run("forecast:deepseek", "deepseek-flash", self.day, "new")
        database.finish_ai_run("forecast:deepseek", self.day, self.report(), None, new)
        database.initialize()
        with database.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM forecast_reports WHERE run_key=?", (old,)).fetchone()), before)
            reports = db.execute("SELECT id,model,result_json FROM forecast_reports ORDER BY id").fetchall()
            self.assertEqual([row["model"] for row in reports], ["glm-5.3", "deepseek-flash"])
            first = db.execute("SELECT MIN(id) FROM forecast_reports WHERE run_date=?", (self.day,)).fetchone()[0]
            self.assertEqual(first, reports[0]["id"])
            self.assertEqual(decode_result(reports[1]["result_json"]), self.report())

    def test_initialization_imports_both_namespaces_in_publication_order_once(self):
        with database.connection() as db:
            for namespace, model, hour in (("forecast:deepseek", "deepseek-flash", 12), ("forecast:glm", "glm-5.3", 11)):
                stamp = f"{self.day}T{hour}:00:00+08:00"
                db.execute("""INSERT INTO ai_runs
                    (run_date,provider,model,status,result_json,started_at,finished_at,prompt_version,run_token)
                    VALUES (?,?,?,'succeeded',?,?,?,?,?)""",
                           (self.day, namespace, model, json.dumps(self.report()), stamp, stamp, "v1", namespace))
            forecast_history.initialize_archive(db)
            forecast_history.initialize_archive(db)
            self.assertEqual([row[0] for row in db.execute("SELECT model FROM forecast_reports ORDER BY id")],
                             ["glm-5.3", "deepseek-flash"])

    def test_retention_uses_long_lease_for_new_namespaces(self):
        old_day = (datetime.fromisoformat(self.day).date() - timedelta(days=120)).isoformat()
        started = (datetime.now(database.CHINA_TZ) - timedelta(seconds=300)).isoformat()
        with database.connection() as db:
            for namespace in ("concept:deepseek", "forecast:deepseek"):
                db.execute("""INSERT INTO ai_runs (run_date,provider,model,status,started_at)
                    VALUES (?,?,'deepseek-flash','running',?)""", (old_day, namespace, started))
        database.maintain_storage(force=True)
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_runs WHERE status='running'").fetchone()[0], 2)
        self.assertEqual(database._ai_lease_cutoff("concept:deepseek"), database._ai_lease_cutoff("concept:glm"))
        self.assertEqual(database._ai_lease_cutoff("forecast:deepseek"), database._ai_lease_cutoff("forecast:glm"))

    def test_task_center_uses_current_module_namespaces_and_preserves_actual_running_model(self):
        from backend import ai
        from backend.data_sources import market_data
        concept = {"provider": "deepseek", "model": "deepseek-flash", "status": "not_configured"}
        # A previous model can remain active during a deployment, while the
        # actionable module route points to the newly configured provider.
        forecast = {"provider": "glm", "model": "glm-5.3", "status": "running"}
        with (patch.object(ai, "get_daily_ai_runs", return_value={"runs": []}),
              patch.object(concept_ai, "PROVIDER", "deepseek"),
              patch.object(concept_ai, "get_concept_run", return_value=concept),
              patch.object(concept_forecast, "RUN_NAMESPACE", "forecast:deepseek"),
              patch.object(concept_forecast, "get_forecast_run", return_value=forecast),
              patch.object(market_data, "status", return_value={}),
              patch.object(research_jobs, "cache_get", return_value=None)):
            runs = research_jobs.operations_payload()["aiRuns"]
        self.assertEqual([(row["provider"], row["model"], row["status"]) for row in runs],
                         [("concept:deepseek", "deepseek-flash", "not_configured"),
                          ("forecast:deepseek", "glm-5.3", "running")])


if __name__ == "__main__":
    unittest.main()
