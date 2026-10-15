from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import database, research_data, research_store, storage_policy
from backend.forecast_prices import FeedbackPriceClient
from backend.history_cache import archive_key, cached_history, clean_history, join_histories
from backend.history_sources import normalize_bars


def history(days, *, source="东方财富日线（不复权）", url="https://quote.eastmoney.com/bk/90.BK1152.html"):
    return {"source": source, "url": url,
            "rows": [{"date": day, "open": 100, "close": 101, "high": 102, "low": 99,
                      "amount": 1000, "pctChg": 1} for day in days]}


class HistoryCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target in (patch.object(database, "DATABASE_PATH", Path(directory.name) / "cache.sqlite3"),
                       patch.object(research_data, "last_closed_day", return_value="2026-10-15"),
                       patch.object(FeedbackPriceClient, "known_calendar", return_value=(
                           "2026-01-01", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"))):
            target.start()
            self.addCleanup(target.stop)
        database.initialize()

    def save(self, value, *, end="2026-10-13", age=0):
        key = f"evaluation:BK1152:2026-07-05:{end}"
        research_store.cache_put(key, value)
        if age:
            with database.connection() as db:
                db.execute("UPDATE research_cache SET updated_at=? WHERE cache_key=?",
                           ((datetime.now(database.CHINA_TZ) - timedelta(days=age)).isoformat(), key))
        return key

    def test_completed_window_reuses_older_key_without_requiring_later_prices(self):
        old = history(["2026-10-12", "2026-10-13"])
        old["cacheAsOf"] = "2026-10-13T17:00:00+08:00"
        self.save(old)
        with patch.object(FeedbackPriceClient, "history") as fetch:
            result = research_data.index_history("BK1152", "2026-10-12", "2026-10-13")
        fetch.assert_not_called()
        self.assertEqual(result["rows"], old["rows"])
        self.assertEqual(result["cacheAsOf"], old["cacheAsOf"])
        # Reusing an old, complete month must not mark the broader moving
        # window freshly fetched and suppress a subsequent missing-tail read.
        with patch.object(FeedbackPriceClient, "history", return_value=history([
                "2026-10-13", "2026-10-14", "2026-10-15"])) as fetch:
            updated = research_data.index_history("BK1152", "2026-10-12", "2026-10-15")
        fetch.assert_called_once_with("BK1152", "2026-10-13", "2026-10-15")
        self.assertEqual(updated["rows"][-1]["date"], "2026-10-15")

    def test_missing_tail_reads_overlap_and_preserves_amount_for_prediction(self):
        self.save(history(["2026-10-12", "2026-10-13"]))
        newer = history(["2026-10-13", "2026-10-14", "2026-10-15"])
        newer["rows"][0].pop("amount")
        with patch.object(FeedbackPriceClient, "history", return_value=newer) as fetch:
            result = research_data.index_history("BK1152", "2026-10-12", "2026-10-15")
        fetch.assert_called_once_with("BK1152", "2026-10-13", "2026-10-15")
        self.assertEqual([row["date"] for row in result["rows"]],
                         ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertEqual(result["rows"][1]["amount"], 1000)
        self.assertEqual(len(research_store.cache_get(archive_key("BK1152"))["rows"]), 4)

    def test_conflicting_duplicate_day_is_missing_in_cache_and_raw_transport(self):
        value = history(["2026-10-12", "2026-10-13", "2026-10-13", "2026-10-14"])
        value["rows"][2]["close"] = 100.5
        cleaned = clean_history(value, "2026-10-12", "2026-10-14")
        self.assertEqual([row["date"] for row in cleaned["rows"]], ["2026-10-12", "2026-10-14"])
        raw = [[row[key] for key in ("date", "open", "close", "high", "low")] for row in value["rows"]]
        self.assertEqual([row["date"] for row in normalize_bars(raw, "2026-10-12", "2026-10-14")],
                         ["2026-10-12", "2026-10-14"])
        self.save(value)
        with patch.object(FeedbackPriceClient, "history", return_value=history([
                "2026-10-12", "2026-10-13", "2026-10-14"])) as fetch:
            repaired = research_data.index_history("BK1152", "2026-10-12", "2026-10-14")
        fetch.assert_called_once_with("BK1152", "2026-10-12", "2026-10-14")
        self.assertEqual(len(repaired["rows"]), 3)

    def test_narrow_success_does_not_hide_a_wider_windows_missing_tail(self):
        self.save(history(["2026-10-12"]))
        with patch.object(FeedbackPriceClient, "history", side_effect=[
                history(["2026-10-12", "2026-10-13"]),
                history(["2026-10-13", "2026-10-14", "2026-10-15"])]) as fetch:
            narrow = research_data.index_history("BK1152", "2026-10-12", "2026-10-13")
            wider = research_data.index_history("BK1152", "2026-10-12", "2026-10-15")
        self.assertEqual(narrow["rows"][-1]["date"], "2026-10-13")
        self.assertEqual(wider["rows"][-1]["date"], "2026-10-15")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args, ("BK1152", "2026-10-13", "2026-10-15"))

    def test_short_update_preserves_older_prices_in_longer_archive(self):
        old_days = ["2026-06-01", "2026-07-06", "2026-10-12", "2026-10-13"]
        research_store.cache_put(archive_key("BK1152"), history(old_days))
        with patch.object(FeedbackPriceClient, "history", return_value=history([
                "2026-10-13", "2026-10-14", "2026-10-15"])):
            research_data.index_history("BK1152", "2026-10-12", "2026-10-15")
        archived = research_store.cache_get(archive_key("BK1152"))
        self.assertEqual([row["date"] for row in archived["rows"]], old_days + ["2026-10-14", "2026-10-15"])

    def test_narrow_different_source_does_not_replace_larger_archive(self):
        old = history(["2026-06-01", "2026-06-03", "2026-10-13"])
        research_store.cache_put(archive_key("BK1152"), old)
        new = history(["2026-06-01", "2026-06-02", "2026-06-03"], source="Tushare", url="https://tushare.pro")
        with (patch.object(FeedbackPriceClient, "known_calendar", return_value=("2026-06-01", "2026-06-02", "2026-06-03")),
              patch.object(FeedbackPriceClient, "history", return_value=new)):
            result = research_data.index_history("BK1152", "2026-06-01", "2026-06-03")
        self.assertEqual(result, new)
        self.assertEqual(research_store.cache_get(archive_key("BK1152")), old)

    def test_different_source_and_changed_index_basis_never_splice(self):
        original = history(["2026-10-12", "2026-10-13"])
        different = history(["2026-10-13", "2026-10-14"], source="Tushare", url="https://tushare.pro")
        self.assertIsNone(join_histories(original, different))
        revised = history(["2026-10-13", "2026-10-14"])
        revised["rows"][0]["open"] = 100.5
        self.assertIsNone(join_histories(original, revised))
        self.assertIsNone(join_histories(original, history(["2026-10-14"])))
        self.save(original)
        full = history(["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"],
                       source="Tushare", url="https://tushare.pro")
        with patch.object(FeedbackPriceClient, "history", side_effect=[different, full]) as fetch:
            result = research_data.index_history("BK1152", "2026-10-12", "2026-10-15")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result, full)

    def test_failed_increment_does_not_erase_saved_prices_or_claim_complete(self):
        old = history(["2026-10-12", "2026-10-13"])
        key = self.save(old)
        with patch.object(FeedbackPriceClient, "history", return_value={
                "rows": [], "error": "连接不可用", "errorKind": "unavailable"}):
            result = research_data.index_history("BK1152", "2026-10-12", "2026-10-15")
        self.assertEqual(result["rows"], old["rows"])
        self.assertEqual(result["errorKind"], "unavailable")
        self.assertEqual(research_store.cache_get(key), old)
        self.assertEqual(research_store.cache_get(archive_key("BK1152"))["rows"], old["rows"])
        # The same saved prices still legitimately complete an earlier month.
        with patch.object(FeedbackPriceClient, "history") as fetch:
            earlier = research_data.index_history("BK1152", "2026-10-12", "2026-10-13")
        fetch.assert_not_called()
        self.assertNotIn("errorKind", earlier)

    def test_cache_identity_is_exact_and_allows_only_overlapping_same_source(self):
        self.save(history(["2026-10-12", "2026-10-13"]))
        self.save(history(["2026-10-13", "2026-10-14"]), end="2026-10-14")
        research_store.cache_put("evaluation:THS:885955:archive", history(["2026-10-15"]))
        result = cached_history("BK1152", "2026-10-12", "2026-10-15")
        self.assertEqual([row["date"] for row in result["rows"]], ["2026-10-12", "2026-10-13", "2026-10-14"])
        self.assertIsNone(cached_history("BK115", "2026-10-12", "2026-10-15"))

    def test_monthly_archive_survives_short_cache_retention_but_is_bounded(self):
        now = datetime.now(database.CHINA_TZ)
        with database.connection() as db:
            for key, age in ((archive_key("BK1152"), 35), (archive_key("BK1136"), 181),
                             ("evaluation:BK1152:2026-07-05:2026-10-13", 8)):
                db.execute("INSERT INTO research_cache VALUES (?,?,?)",
                           (key, json.dumps(history(["2026-10-13"])), (now - timedelta(days=age)).isoformat()))
        research_store.cache_put("stock-news:600000", {"items": []})
        self.assertIsNotNone(research_store.cache_get(archive_key("BK1152")))
        self.assertIsNone(research_store.cache_get(archive_key("BK1136")))
        self.assertIsNone(research_store.cache_get("evaluation:BK1152:2026-07-05:2026-10-13"))
        with database.connection() as db:
            storage_policy.cleanup(db, now.date(), lambda _: now.isoformat())
        self.assertIsNotNone(research_store.cache_get(archive_key("BK1152")))
