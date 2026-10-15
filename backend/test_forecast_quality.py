from __future__ import annotations

import unittest
from datetime import date, timedelta

from backend.forecast_quality import verified_history


class ForecastHistoryQualityTests(unittest.TestCase):
    def setUp(self):
        first = date(2026, 7, 1)
        self.calendar = [(first + timedelta(days=i)).isoformat() for i in range(90)
                         if (first + timedelta(days=i)).weekday() < 5]
        self.days = self.calendar[:40]
        # A declining concept with complete history remains a reversal candidate.
        self.rows = [{"date": day, "close": 200 - i, "amount": None}
                     for i, day in enumerate(self.days)]
        self.end = self.days[-1].replace("-", "")

    def test_complete_declining_prices_qualify_without_inventing_turnover(self):
        rows, quality = verified_history(self.rows, self.end, self.calendar)
        self.assertEqual(quality["status"], "ready")
        self.assertEqual(len(rows), 40)
        self.assertIsNone(rows[-1]["amount"])

    def test_twenty_prices_are_not_enough_for_a_twenty_session_return(self):
        rows, quality = verified_history(self.rows[-20:], self.end, self.calendar)
        self.assertEqual(rows, [])
        self.assertEqual(quality["missingDates"], [self.days[-21].replace("-", "")])

    def test_recent_interior_gap_or_stale_endpoint_cannot_pass_on_row_count(self):
        for missing in (-10, -1):
            damaged = self.rows.copy()
            del damaged[missing]
            rows, quality = verified_history(damaged, self.end, self.calendar)
            self.assertEqual(rows, [])
            self.assertEqual(quality["status"], "incomplete")
            self.assertEqual(len(quality["missingDates"]), 1)

    def test_older_gap_shortens_history_to_a_continuous_suffix(self):
        damaged = self.rows[:10] + self.rows[11:]
        rows, quality = verified_history(damaged, self.end, self.calendar)
        self.assertEqual(quality["status"], "ready")
        self.assertEqual(len(rows), 29)
        self.assertEqual(rows[0]["date"], self.days[11].replace("-", ""))

    def test_unknown_calendar_does_not_assert_twenty_one_missing_prices(self):
        for calendar in ([], self.calendar[:10], ["invalid"]):
            rows, quality = verified_history(self.rows, self.end, calendar)
            self.assertEqual(rows, [])
            self.assertEqual(quality["status"], "calendar_unavailable")
            self.assertEqual(quality["missingDates"], [])

    def test_conflicting_price_does_not_hide_a_gap(self):
        changed = {**self.rows[-4], "close": self.rows[-4]["close"] + 1}
        self.assertEqual(verified_history(self.rows + [changed], self.end, self.calendar)[0], [])

    def test_known_holidays_do_not_count_as_missing_bars(self):
        holiday = self.calendar[22]
        calendar = [day for day in self.calendar if day != holiday]
        rows = [row for row in self.rows if row["date"] != holiday]
        self.assertEqual(verified_history(rows, self.end, calendar)[1]["status"], "ready")
