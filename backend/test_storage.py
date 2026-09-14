from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend import database, storage_policy
from backend import main
from backend.concept_forecast import assemble_result
from backend.report_storage import FORMAT, decode_result, encode_result
from backend.test_concept_forecast import forecast_evidence, forecast_result


def report_fixture():
    evidence = forecast_evidence()
    evidence["candidates"][0]["evidence"][-1]["excerpt"] = "可核对的完整新闻摘录，保留当时证据。" * 100
    report = assemble_result(forecast_result(), evidence)
    duplicate = copy.deepcopy(report["concepts"][0])
    duplicate["code"] = "BK1002"
    for field in ("technical", "fundamental", "news"):
        for source in duplicate[field]["sources"]:
            source["id"] = source["id"].replace("BK1001", "BK1002")
    for source in duplicate["catalysts"][0]["sources"]:
        source["id"] = source["id"].replace("BK1001", "BK1002")
    report["concepts"].append(duplicate)
    return report


class ReportStorageTests(unittest.TestCase):
    def test_deduplicated_news_and_metric_references_round_trip_without_loss(self):
        report = report_fixture()
        original = copy.deepcopy(report)
        raw = encode_result(report)
        stored = json.loads(raw)
        self.assertEqual(stored["storageFormat"], FORMAT)
        self.assertEqual(sum(row["source"].get("kind") == "news" for row in stored["sourcePool"]), 1)
        self.assertTrue(any(row["snapshot"] is not None and "excerpt" not in row["source"] for row in stored["sourcePool"]))
        self.assertLess(len(raw.encode()), len(json.dumps(report, ensure_ascii=False).encode()) * .5)
        self.assertEqual(decode_result(raw), report)
        self.assertEqual(report, original)
        self.assertEqual(encode_result(decode_result(raw)), raw)

    def test_unique_data_evidence_is_preserved_and_legacy_reports_still_load(self):
        report = report_fixture()
        report["concepts"][0]["technical"]["sources"][0]["excerpt"] = "这是一条额外的数据解释，不能当重复指标删除。"
        self.assertEqual(decode_result(encode_result(report)), report)
        self.assertEqual(decode_result(json.dumps(report)), report)
        report["concepts"][0]["news"]["sources"] = [[0, "legacy"]]
        self.assertEqual(decode_result(encode_result(report)), report)
        ordinary = {"title": "股票研究", "picks": [], "summary": "保留普通 AI 结果"}
        self.assertEqual(decode_result(encode_result(ordinary)), ordinary)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 14, 10, tzinfo=database.CHINA_TZ)


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "test.sqlite3"
        for target in (patch.object(database, "DATABASE_PATH", self.path), patch.object(database, "datetime", FixedDateTime)):
            target.start()
            self.addCleanup(target.stop)
        database.initialize()

    def test_old_schema_is_backed_up_and_migrated_without_changing_watchlist_or_reports(self):
        database.remove_watch_stock(database.DEFAULT_WATCHLIST[0])
        before = database.get_watchlist_rows()
        report = report_fixture()
        day = database.china_date()
        with database.connection() as db:
            db.execute("ALTER TABLE watchlist ADD COLUMN position_weight REAL NOT NULL DEFAULT 0")
            db.execute("DELETE FROM app_meta WHERE key IN ('storage_schema_version','storage_maintenance')")
            db.execute("INSERT INTO ai_runs (run_date,provider,model,status,result_json,started_at,prompt_version) VALUES (?, 'forecast:glm','glm-5.3','succeeded',?,?, 'v1')",
                       (day, json.dumps(report, ensure_ascii=False), database.now_iso()))
        database.initialize()
        backup = self.path.with_name("test.before-storage-v1.sqlite3")
        with sqlite3.connect(backup) as db:
            self.assertIn("position_weight", {row[1] for row in db.execute("PRAGMA table_info(watchlist)")})
            self.assertEqual(json.loads(db.execute("SELECT result_json FROM ai_runs").fetchone()[0]), report)
        self.assertEqual(database.get_watchlist_rows(), before)
        self.assertEqual(database.read_ai_run("forecast:glm", day)["result"], report)
        with database.connection() as db:
            self.assertNotIn("position_weight", {row["name"] for row in db.execute("PRAGMA table_info(watchlist)")})
            self.assertEqual(json.loads(db.execute("SELECT result_json FROM ai_runs").fetchone()[0])["storageFormat"], FORMAT)
        backup_bytes = backup.read_bytes()
        database.initialize()
        self.assertEqual(backup.read_bytes(), backup_bytes)
        self.assertEqual(database.get_meta("quote_volume_unit"), "shares")

    def test_retention_boundaries_preserve_recent_results_and_active_leases(self):
        today = date(2026, 9, 14)
        rows = [("success-old", 91, "succeeded", 0), ("success-boundary", 90, "succeeded", 0),
                ("failed-old", 15, "failed", 0), ("failed-boundary", 14, "failed", 0),
                ("running-live", 120, "running", 0), ("running-stale", 120, "running", 700),
                ("forecast:glm", 120, "running", 300)]
        with database.connection() as db:
            for provider, age, status, seconds_ago in rows:
                db.execute("INSERT INTO ai_runs (run_date, provider,model,status,started_at,run_token) VALUES (?,?,'glm-5.3',?,?,?)",
                           ((today - timedelta(days=age)).isoformat(), provider, status,
                            (FixedDateTime.now() - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds"), provider))
            for age in (90, 91):
                db.execute("INSERT INTO strategy_runs VALUES (?,?,'[]','test',?)",
                           (f"{today - timedelta(days=age)}:v6", '20260601', database.now_iso()))
        summary = database.maintain_storage(force=True)
        self.assertEqual(summary["aiRemoved"], 3)
        self.assertEqual(summary["strategyRemoved"], 1)
        with database.connection() as db:
            kept = {row["provider"] for row in db.execute("SELECT provider FROM ai_runs")}
        self.assertEqual(kept, {"success-boundary", "failed-boundary", "running-live", "forecast:glm"})
        day = (today - timedelta(days=120)).isoformat()
        database.finish_ai_run("forecast:glm", day, {"summary": "运行中任务正常完成"}, None, "forecast:glm")
        self.assertEqual(database.read_ai_run("forecast:glm", day)["status"], "succeeded")
        self.assertTrue(database.maintain_storage()["skipped"])

    def test_daily_cache_uses_observed_sessions_and_preserves_fallbacks_and_unknown_keys(self):
        sessions = ["20260901", "20260902", "20260903", "20260904", "20260907", "20260908", "20260909", "20260910", "20260911", "20260914"]
        for day in sessions:
            database.set_meta(f"market_intraday_pair:{day}:v1", '{}')
        for weekend in ("2026-09-12", "2026-09-13"):
            database.set_meta(f"hot_concepts:{weekend}:v3", '{"tradeDate":"20260911"}')
        for key in ("sector_overview:v3", "sector_overview:v4", "sector_overview:v99", "market_turnover:v2", "market_fund_flow:v1",
                    "custom:keep", "sector_previous_amount:20200101:v1", "hot_concepts:2026-09-14:v2"):
            database.set_meta(key, '{}')
        with database.connection() as db:
            db.execute("INSERT INTO app_meta VALUES ('stock_catalog_source','unused',?)", (database.now_iso(),))
        database.maintain_storage(force=True)
        for day in sessions[:3]:
            self.assertIsNone(database.get_meta(f"market_intraday_pair:{day}:v1"))
        for day in sessions[3:]:
            self.assertIsNotNone(database.get_meta(f"market_intraday_pair:{day}:v1"))
        for key in ("sector_overview:v3", "sector_previous_amount:20200101:v1", "hot_concepts:2026-09-14:v2", "stock_catalog_source"):
            self.assertIsNone(database.get_meta(key))
        for key in ("sector_overview:v4", "sector_overview:v99", "market_turnover:v2", "market_fund_flow:v1", "custom:keep",
                    "hot_concepts:2026-09-12:v3", "hot_concepts:2026-09-13:v3", "quote_volume_unit"):
            self.assertIsNotNone(database.get_meta(key), key)

    def test_history_pruning_keeps_two_full_windows_and_late_writes_cannot_evict_newer_data(self):
        snapshots = {}
        for day in ("20260910", "20260911", "20260914", "20260909"):
            value = json.dumps({"rows": [{"date": str(i), "close": i + 1} for i in range(40)], "closed": True})
            key = f"concept_history:BK1001:{day}:v1"
            snapshots[key] = value
            database.set_meta(key, value)
        for day in ("20260911", "20260914"):
            key = f"concept_history:BK1001:{day}:v1"
            self.assertEqual(database.get_meta(key), snapshots[key])
        for day in ("20260909", "20260910"):
            self.assertIsNone(database.get_meta(f"concept_history:BK1001:{day}:v1"))
        self.assertIsNone(database.get_meta("stock_catalog_source"))
        database.set_meta("stock_catalog_source", "no longer persisted")
        self.assertIsNone(database.get_meta("stock_catalog_source"))

    def test_new_reports_are_compact_on_disk_and_read_as_original_api_payload(self):
        report = report_fixture()
        day = database.china_date()
        token = database.start_ai_run("forecast:glm", "glm-5.3", day, "v1")
        database.finish_ai_run("forecast:glm", day, report, None, token)
        with database.connection() as db:
            raw = db.execute("SELECT result_json FROM ai_runs").fetchone()[0]
        self.assertEqual(json.loads(raw)["storageFormat"], FORMAT)
        self.assertEqual(database.read_ai_run("forecast:glm", day)["result"], report)

    def test_maintenance_runs_even_when_daily_ai_is_disabled(self):
        scheduler = MagicMock()
        with patch.object(main, "SCHEDULER", SimpleNamespace(enabled=False)), patch.object(main, "AsyncIOScheduler", return_value=scheduler):
            with TestClient(main.app):
                scheduler.start.assert_called_once()
                self.assertEqual([call.kwargs["id"] for call in scheduler.add_job.call_args_list], ["storage-maintenance", "forecast-feedback"])
            scheduler.shutdown.assert_called_once()

    def test_orphan_prompt_metadata_is_removed_but_unmigrated_versions_are_kept(self):
        database.set_meta("ai_prompt:missing:2026-09-14:v1", "1")
        with database.connection() as db:
            db.execute("INSERT INTO ai_runs (run_date,provider,model,status,started_at) VALUES ('2026-09-14','legacy','old','failed',?)",
                       (database.now_iso(),))
        database.set_meta("ai_prompt:legacy:2026-09-14:v1", "1")
        database.maintain_storage(force=True)
        self.assertIsNone(database.get_meta("ai_prompt:missing:2026-09-14:v1"))
        self.assertEqual(database.get_meta("ai_prompt:legacy:2026-09-14:v1"), "1")

    def test_invalid_backup_stops_migration_before_removing_legacy_column(self):
        with database.connection() as db:
            db.execute("ALTER TABLE watchlist ADD COLUMN position_weight REAL NOT NULL DEFAULT 0")
        backup = self.path.with_name("test.before-storage-v1.sqlite3")
        backup.write_bytes(b'')
        with self.assertRaises(RuntimeError):
            database.initialize()
        with database.connection() as db:
            self.assertIn("position_weight", {row["name"] for row in db.execute("PRAGMA table_info(watchlist)")})
