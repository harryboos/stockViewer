from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from backend import concept_data, data_sources, database
from backend.data_sources import MARKET_FUND_FLOW_TENCENT_CACHE_KEY, MarketDataService
from backend.forecast_prices import FeedbackPriceClient


CALENDAR = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=database.CHINA_TZ)


class ConceptQuoteSessionReviewTests(unittest.TestCase):
    def test_refetched_older_quotes_are_rejected_after_market_open(self):
        now = instant("2026-09-14T10:00:00")
        with patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR):
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                concept_data.concept_snapshot_warning("20260911", now.isoformat(), now=now)

    def test_same_day_fetch_on_weekend_still_requires_latest_exchange_session(self):
        now = instant("2026-09-13T10:00:00")
        with patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR):
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                concept_data.concept_snapshot_warning("20260910", now.isoformat(), now=now)
            note = concept_data.concept_snapshot_warning("20260911", now.isoformat(), now=now)
        self.assertIn("今日休市", note)
        self.assertIn("2026-09-11", note)

    def test_prior_close_is_valid_before_open_but_expires_at_open(self):
        snapshot = "2026-09-11T15:10:00+08:00"
        with patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR):
            note = concept_data.concept_snapshot_warning("20260911", snapshot, now=instant("2026-09-14T09:29:59"))
            self.assertIn("开盘前", note)
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                concept_data.concept_snapshot_warning("20260911", snapshot, now=instant("2026-09-14T09:30:00"))

    def test_future_quote_future_timestamp_and_noncanonical_date_are_rejected(self):
        now = instant("2026-09-14T10:00:00")
        cases = [("20260914", "2026-09-14T10:00:01+08:00"),
                 ("20260915", now.isoformat()), ("2026914", now.isoformat())]
        with patch.object(FeedbackPriceClient, "calendar") as calendar:
            for session, snapshot in cases:
                with self.subTest(session=session, snapshot=snapshot), self.assertRaisesRegex(RuntimeError, "旧缓存"):
                    concept_data.concept_snapshot_warning(session, snapshot, now=now)
        calendar.assert_not_called()

    def test_same_day_legacy_and_utc_timestamps_retain_fast_path(self):
        now = instant("2026-09-14T10:00:00")
        with patch.object(FeedbackPriceClient, "calendar") as calendar:
            for snapshot in ("2026-09-14T09:50:00", "2026-09-14T01:50:00Z"):
                self.assertIsNone(concept_data.concept_snapshot_warning("20260914", snapshot, now=now))
        calendar.assert_not_called()


class FundFlowCacheReviewTests(unittest.TestCase):
    def setUp(self):
        self.now = instant("2026-09-14T14:00:00")
        self.old = {"date": "20260914", "mainNetInflow": 10, "source": "腾讯证券逐股资金汇总"}
        self.new = {**self.old, "mainNetInflow": 20}
        self.client = Mock()
        self.client.market_fund_flow_snapshot.return_value = self.new
        self.service = MarketDataService(tencent_client=self.client)
        self.cached_at = (self.now - timedelta(hours=3)).isoformat()
        self.patches = [
            patch.object(self.service, "_eastmoney_cooling_down", return_value=True),
            patch.object(self.service, "_cached_json_any", return_value=None),
            patch.object(self.service, "_cached_json", side_effect=lambda key:
                         {"cachedAt": self.cached_at, "rows": [copy.deepcopy(self.old)]}
                         if key == MARKET_FUND_FLOW_TENCENT_CACHE_KEY else None),
            patch.object(data_sources, "datetime", wraps=datetime),
            patch.object(database, "set_meta"),
            patch.object(database, "now_iso", return_value=self.now.isoformat()),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.mocks[3].now.return_value = self.now
        for item in self.patches:
            self.addCleanup(item.stop)

    def test_morning_snapshot_refreshes_without_force_in_afternoon(self):
        rows, _, status = self.service._fund_flow_history("20260914")
        self.assertEqual(rows[-1]["mainNetInflow"], 20)
        self.assertEqual(status["state"], "fallback")
        self.client.market_fund_flow_snapshot.assert_called_once_with("20260914")
        self.mocks[4].assert_called_once()

    def test_fresh_snapshot_reuses_cache_including_legacy_naive_timestamp(self):
        for offset in ("+08:00", ""):
            self.cached_at = f"2026-09-14T13:59:00{offset}"
            rows, _, status = self.service._fund_flow_history("20260914")
            self.assertEqual(rows[-1]["mainNetInflow"], 10)
            self.assertEqual(status["state"], "cached")
        self.client.market_fund_flow_snapshot.assert_not_called()
        self.mocks[4].assert_not_called()

    def test_failed_refresh_preserves_original_values_and_timestamp(self):
        self.client.market_fund_flow_snapshot.side_effect = RuntimeError("offline")
        rows, _, status = self.service._fund_flow_history("20260914")
        self.assertEqual(rows[-1]["mainNetInflow"], 10)
        self.assertEqual(status["state"], "cached")
        self.assertEqual(status["updatedAt"], self.cached_at)
        self.mocks[4].assert_not_called()

    def test_invalid_future_and_expired_at_boundary_timestamps_do_not_pin_cache(self):
        expired = self.now - timedelta(seconds=data_sources.MARKET.spot_cache_seconds)
        for stamp in ("invalid", (self.now + timedelta(seconds=1)).isoformat(), expired.isoformat()):
            with self.subTest(stamp=stamp):
                self.cached_at = stamp
                rows, _, _ = self.service._fund_flow_history("20260914")
                self.assertEqual(rows[-1]["mainNetInflow"], 20)
        self.assertEqual(self.client.market_fund_flow_snapshot.call_count, 3)
