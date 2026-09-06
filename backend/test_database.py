from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = patch.object(database, "DATABASE_PATH", Path(temporary.name) / "test.sqlite3")
        target.start()
        self.addCleanup(target.stop)
        database.initialize()

    def test_restart_preserves_deleted_default_stocks_and_empty_watchlist(self) -> None:
        self.assertEqual(database.list_watch_codes(), database.DEFAULT_WATCHLIST)
        for code in database.DEFAULT_WATCHLIST:
            database.remove_watch_stock(code)
        database.initialize()
        self.assertEqual(database.list_watch_codes(), [])

    def test_seed_does_not_overwrite_updated_stock_name(self) -> None:
        stock = database.get_stock_basics(["600519.SH"])[0]
        database.upsert_stock_basics([{**stock, "name": "更新后的名称"}])
        database.initialize()
        self.assertEqual(database.get_stock_basics(["600519.SH"])[0]["name"], "更新后的名称")

    def test_old_or_daily_quotes_do_not_replace_newer_intraday_data(self) -> None:
        quote = {"tsCode": "600519.SH", "tradeDate": "20260907", "close": 10,
                 "source": "AKShare", "fetchedAt": "2026-09-07T14:00:00+08:00"}
        database.upsert_quotes([quote])
        for older in (
            {**quote, "tradeDate": "20260904", "close": 9},
            {**quote, "fetchedAt": "2026-09-07T13:00:00+08:00", "close": 8},
            {**quote, "source": "BaoStock", "fetchedAt": "2026-09-07T15:00:00+08:00", "close": 7},
        ):
            database.upsert_quotes([older])
            self.assertEqual(database.get_quotes(["600519.SH"])["600519.SH"]["close"], 10)
        database.upsert_quotes([{**quote, "tradeDate": "20260908", "source": "BaoStock", "close": 11}])
        self.assertEqual(database.get_quotes(["600519.SH"])["600519.SH"]["close"], 11)

    def test_volume_migration_runs_once(self) -> None:
        with database.connection() as db:
            db.execute("DELETE FROM app_meta WHERE key = 'quote_volume_unit'")
        database.upsert_quotes([{"tsCode": "600519.SH", "tradeDate": "20260907", "close": 10,
                                 "vol": 20, "source": "AKShare", "fetchedAt": database.now_iso()}])
        database.initialize()
        database.initialize()
        self.assertEqual(database.get_quotes(["600519.SH"])["600519.SH"]["vol"], 2000)

    def test_ai_claim_is_atomic_even_for_forced_requests(self) -> None:
        with ThreadPoolExecutor(max_workers=6) as executor:
            tokens = list(executor.map(
                lambda _: database.start_ai_run("deepseek", "model", "2026-09-07", "v1", True), range(6)
            ))
        self.assertEqual(sum(token is not None for token in tokens), 1)

    def test_ai_stale_lease_is_retryable_and_late_results_cannot_overwrite(self) -> None:
        first = database.start_ai_run("deepseek", "model", "2026-09-07", "v1")
        expired = (datetime.now(database.CHINA_TZ) - timedelta(minutes=10)).isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute("UPDATE ai_runs SET started_at = ?", (expired,))
        self.assertEqual(database.read_ai_run("deepseek", "2026-09-07")["status"], "failed")
        second = database.start_ai_run("deepseek", "model", "2026-09-07", "v1")
        self.assertIsNotNone(second)
        database.finish_ai_run("deepseek", "2026-09-07", {"title": "old"}, None, first)
        self.assertEqual(database.read_ai_run("deepseek", "2026-09-07")["status"], "running")
        database.finish_ai_run("deepseek", "2026-09-07", {"title": "new"}, None, second)
        self.assertEqual(database.read_ai_run("deepseek", "2026-09-07")["result"]["title"], "new")
        self.assertIsNone(database.start_ai_run("deepseek", "model", "2026-09-07", "v1"))
        self.assertIsNotNone(database.start_ai_run("deepseek", "new-model", "2026-09-07", "v1"))

    def test_transaction_rolls_back_on_failure(self) -> None:
        with self.assertRaises(RuntimeError):
            with database.connection() as db:
                db.execute("DELETE FROM watchlist")
                raise RuntimeError("rollback")
        self.assertEqual(database.list_watch_codes(), database.DEFAULT_WATCHLIST)

    def test_legacy_prompt_metadata_preserves_successful_ai_cache(self) -> None:
        with database.connection() as db:
            db.execute(
                "INSERT INTO ai_runs (run_date, provider, model, status, started_at) VALUES (?, ?, ?, ?, ?)",
                ("2026-09-07", "deepseek", "model", "succeeded", database.now_iso()),
            )
        database.set_meta("ai_prompt:deepseek:2026-09-07:v5", "5")
        database.initialize()
        self.assertEqual(database.read_ai_run("deepseek", "2026-09-07")["promptVersion"], "5")
        self.assertIsNone(database.start_ai_run("deepseek", "model", "2026-09-07", "5"))
