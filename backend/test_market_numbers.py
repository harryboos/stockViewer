from __future__ import annotations

import math
import unittest

from backend.data_sources import number_or_none, volume_in_shares
from backend.market_numbers import number_or_none as parse_number
from backend.history_sources import normalize_bars
from backend.tencent import TencentClient
from backend.ths import _number as ths_number


class MarketNumberTests(unittest.TestCase):
    def test_provider_parsers_preserve_missing_and_invalid_quotes(self):
        for parse in (parse_number, number_or_none, TencentClient._number, ths_number):
            for raw in (None, "", "-", "nan", math.nan, math.inf, -math.inf,
                        True, False, 10 ** 400, {}, []):
                with self.subTest(parser=parse, raw_type=type(raw).__name__):
                    self.assertIsNone(parse(raw))

    def test_numeric_strings_zero_and_negative_values_remain_valid(self):
        for raw, expected in ((0, 0.0), ("0", 0.0), (" 12.5 ", 12.5), (-3, -3.0), ("-1.25e2", -125.0)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_number(raw), expected)
        self.assertEqual(volume_in_shares("12.5"), 1250)
        self.assertIsNone(volume_in_shares(True))

    def test_corrupt_history_values_remain_missing_without_aborting_valid_rows(self):
        rows = [["2026-09-10", 1, True, 2, 0.5],
                ["2026-09-11", 1, 10 ** 400, 2, 0.5],
                ["2026-09-14", "1", "1.5", "2", "0.5"]]
        self.assertEqual(normalize_bars(rows, "2026-09-10", "2026-09-14"),
                         [{"date": "2026-09-14", "open": 1.0, "close": 1.5, "high": 2.0, "low": 0.5}])


if __name__ == "__main__":
    unittest.main()
