from __future__ import annotations

import asyncio
import copy
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from backend import concept_data, concept_forecast, database
from backend.forecast_prices import FeedbackPriceClient
from backend.test_concept_ai import evidence_fixture


CALENDAR = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]
CLOSE = "2026-09-11T15:10:00+08:00"


class ConceptFreshnessTests(unittest.TestCase):
    def test_current_day_keeps_existing_path_without_calendar_request(self):
        with (patch.object(database, "now_iso", return_value="2026-09-11T16:00:00+08:00"),
              patch.object(FeedbackPriceClient, "calendar") as calendar):
            self.assertIsNone(concept_data.concept_snapshot_warning("20260911", "2026-09-11T10:00:00+08:00"))
        calendar.assert_not_called()

    def test_weekend_uses_last_session_close_including_utc_and_legacy_timestamps(self):
        for today in ("2026-09-12", "2026-09-13"):
            for snapshot in (CLOSE, "2026-09-11T07:10:00Z", "2026-09-11T15:10:00"):
                with (self.subTest(today=today, snapshot=snapshot),
                      patch.object(database, "now_iso", return_value=f"{today}T16:00:00+08:00"),
                      patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR)):
                    note = concept_data.concept_snapshot_warning("20260911", snapshot)
                self.assertIn("今日休市", note)
                self.assertIn("2026-09-11", note)

    def test_weekday_holiday_uses_exchange_calendar_instead_of_weekday_guess(self):
        # Synthetic closure schedule: freshness must follow supplied exchange dates.
        calendar = ["2026-09-30", "2026-10-08"]
        with (patch.object(database, "now_iso", return_value="2026-10-05T16:00:00+08:00"),
              patch.object(FeedbackPriceClient, "calendar", return_value=calendar)):
            self.assertIn("2026-09-30", concept_data.concept_snapshot_warning("20260930", "2026-09-30T16:00:00+08:00"))

    def test_old_session_intraday_future_and_invalid_cache_are_still_rejected(self):
        cases = [
            ("20260910", "2026-09-11T16:00:00+08:00"),  # A newer fetch cannot repair the wrong session.
            ("20260911", "2026-09-11T14:00:00+08:00"),
            ("20260911", "2026-09-11T15:09:59+08:00"),
            ("20260911", "2026-09-14T16:00:00+08:00"),
            ("20260911", None), ("invalid", CLOSE),
        ]
        with (patch.object(database, "now_iso", return_value="2026-09-13T16:00:00+08:00"),
              patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR)):
            for session, snapshot in cases:
                with self.subTest(session=session, snapshot=snapshot), self.assertRaisesRegex(RuntimeError, "旧缓存"):
                    concept_data.concept_snapshot_warning(session, snapshot)

    def test_reopening_day_does_not_accept_previous_session_cache(self):
        with (patch.object(database, "now_iso", return_value="2026-09-14T16:00:00+08:00"),
              patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR)):
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                concept_data.concept_snapshot_warning("20260911", CLOSE, forecast=True)

    def test_missing_or_short_calendar_cannot_prove_a_holiday(self):
        responses = ([], CALENDAR[:2], ["2026-09-14"], ["invalid"], RuntimeError("upstream details"))
        for response in responses:
            with (self.subTest(response=response), patch.object(database, "now_iso", return_value="2026-09-13T16:00:00+08:00"),
                  patch.object(FeedbackPriceClient, "calendar") as calendar):
                if isinstance(response, Exception):
                    calendar.side_effect = response
                else:
                    calendar.return_value = response
                with self.assertRaisesRegex(RuntimeError, "交易日历") as raised:
                    concept_data.concept_snapshot_warning("20260911", CLOSE)
                self.assertNotIn("upstream details", str(raised.exception))

    def test_recommendation_and_forecast_collection_keep_original_dates_and_warning(self):
        board = {**evidence_fixture()["candidates"][0], "kind": "concept"}
        overview = {"tradeDate": "20260911", "updatedAt": CLOSE, "conceptBoards": [board],
                    "warnings": ["板块数据已使用最近成功缓存"]}
        original = copy.deepcopy(overview)
        with (patch.object(database, "now_iso", return_value="2026-09-13T16:00:00+08:00"),
              patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR),
              patch.object(concept_data.market_data, "sector_overview", return_value=overview),
              patch.object(concept_data.ConceptResearchClient, "daily_history", return_value=[]),
              patch.object(concept_data.ConceptResearchClient, "strong_stocks", return_value=board["stocks"])):
            for forecast in (False, True):
                with self.subTest(forecast=forecast):
                    evidence = concept_data.collect_concept_evidence(forecast=forecast)
                    self.assertEqual(evidence["tradeDate"], "20260911")
                    self.assertEqual(evidence["dataAsOf"], CLOSE)
                    self.assertEqual(evidence["candidates"][0]["stocks"], board["stocks"])
                    self.assertIn("今日休市", "；".join(evidence["warnings"]))
                    self.assertIn(overview["warnings"][0], evidence["warnings"])
        self.assertEqual(overview, original)


class SharedEvidenceFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_forecast_result_is_accepted_on_closed_day_without_duplicate_warning(self):
        evidence = {**evidence_fixture(), "dataAsOf": CLOSE}
        with (patch.object(database, "now_iso", return_value="2026-09-13T16:00:00+08:00"),
              patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR)):
            note = concept_data.concept_snapshot_warning(evidence["tradeDate"], CLOSE, forecast=True)
            evidence["warnings"] = [note]
            shared = Future()
            with patch.object(concept_forecast, "_evidence_future", shared):
                task = asyncio.create_task(concept_forecast.collect_evidence())
                await asyncio.sleep(0)
                shared.set_result(evidence)
                actual = await task
        self.assertEqual(actual["dataAsOf"], CLOSE)
        self.assertEqual(actual["warnings"], [note])
        self.assertIsNot(actual, evidence)

    async def test_slow_shared_result_is_rechecked_after_reopening_date_changes(self):
        shared = Future()
        with (patch.object(database, "now_iso", return_value="2026-09-13T16:00:00+08:00") as today,
              patch.object(FeedbackPriceClient, "calendar", return_value=CALENDAR),
              patch.object(concept_forecast, "_evidence_future", shared)):
            task = asyncio.create_task(concept_forecast.collect_evidence())
            await asyncio.sleep(0)
            today.return_value = "2026-09-14T16:00:00+08:00"
            shared.set_result({**evidence_fixture(), "dataAsOf": CLOSE})
            with self.assertRaisesRegex(RuntimeError, "旧缓存"):
                await task
