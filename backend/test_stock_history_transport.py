"""Provider failures must not become empty or partially successful price series."""
from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import date
from unittest.mock import Mock, patch

from baostock.data.resultset import ResultData

from backend.data_sources import MarketDataService
from backend.research_pool import fetch_batch, history_result


class StockHistoryTransportTests(unittest.TestCase):
    start, end = date(2026, 9, 1), date(2026, 9, 30)

    @staticmethod
    def query(rows=None, error_code="0"):
        result = ResultData()
        result.error_code = error_code
        result.data = [] if rows is None else rows
        return result

    @staticmethod
    def bar(day="2026-09-01"):
        return [day, "10", "11", "9", "10.5", "100000", "1050000", "5", "1", "20", "2", "1"]

    def read(self, query):
        client = Mock()
        client.query_history_k_data_plus.return_value = query
        return MarketDataService._query_baostock_history(client, "600519", self.start, self.end)

    def test_nonzero_query_status_is_not_an_empty_success(self):
        query = self.query(error_code="10002007")
        with self.assertRaisesRegex(RuntimeError, "日线查询失败"):
            self.read(query)

    def test_error_after_reading_a_bar_discards_the_partial_history(self):
        query = Mock(error_code="0")
        query.get_row_data.return_value = self.bar()

        def advance():
            if query.get_row_data.call_count:
                query.error_code = "10002007"
                return False
            return True

        query.next.side_effect = advance
        with self.assertRaisesRegex(RuntimeError, "日线查询失败"):
            self.read(query)

    def test_silent_page_disconnect_is_not_a_successful_truncated_series(self):
        query = self.query([self.bar()])
        query.cur_page_num, query.per_page_count = "1", 1
        query.msg_type = "1"
        query.msg_body = "query_history_k_data_plus\x01anonymous\x011"
        with (patch("baostock.data.resultset.cons.BAOSTOCK_PER_PAGE_COUNT", 1),
              patch("baostock.data.resultset.sock.send_msg", return_value=None) as send):
            with self.assertRaisesRegex(RuntimeError, "日线分页中断"):
                self.read(query)
        send.assert_called_once()
        self.assertEqual(query.error_code, "0")  # The SDK failed without changing its success status.

    def test_successful_empty_and_complete_short_page_keep_their_meaning(self):
        self.assertEqual(self.read(self.query()), [])
        rows = self.read(self.query([self.bar(), self.bar("2026-09-02")]))
        self.assertEqual([row["date"] for row in rows], ["20260901", "20260902"])
        self.assertEqual(rows[-1]["close"], 10.5)

    def test_successful_empty_terminal_page_is_not_mistaken_for_disconnect(self):
        query = self.query([self.bar()])
        query.per_page_count = 1
        original_next = query.next

        def advance():
            if query.cur_row_num == 1:
                query.data = []
                query.cur_page_num = "2"
                return False
            return original_next()

        with patch.object(query, "next", side_effect=advance):
            self.assertEqual(len(self.read(query)), 1)

    def test_akshare_and_baostock_failure_reaches_research_as_waiting_not_missing(self):
        service = MarketDataService()
        akshare, baostock = Mock(), Mock()
        akshare.stock_zh_a_hist.side_effect = RuntimeError("private upstream response")
        baostock.query_history_k_data_plus.return_value = self.query(error_code="10002007")
        with (patch.object(service, "_akshare", return_value=akshare),
              patch.object(service, "_baostock_session", return_value=nullcontext(baostock))):
            result = fetch_batch(["600519"], service.history)
        self.assertNotIn("600519", result)
        self.assertEqual(result.errors, {"600519": "failed"})
        self.assertNotIn("private", history_result(result, "600519")["error"])

    def test_login_and_midstream_exceptions_are_sanitized_not_swallowed(self):
        for failure_at in ("login", "stream"):
            with self.subTest(failure_at=failure_at):
                service = MarketDataService()
                client = Mock()
                query = Mock(error_code="0")
                query.next.side_effect = [True, BrokenPipeError("private connection detail")]
                query.get_row_data.return_value = self.bar()
                client.query_history_k_data_plus.return_value = query
                session = (patch.object(service, "_baostock_session", side_effect=RuntimeError("private login detail"))
                           if failure_at == "login" else
                           patch.object(service, "_baostock_session", return_value=nullcontext(client)))
                with session, self.assertRaisesRegex(RuntimeError, "日线来源暂不可用") as caught:
                    service._baostock_history("600519", self.start, self.end)
                self.assertNotIn("private", str(caught.exception))

    def test_batch_keeps_healthy_symbols_and_retries_only_failed_ones(self):
        service = MarketDataService()
        client = Mock()
        client.query_history_k_data_plus.side_effect = [self.query([self.bar()]), self.query(error_code="10002007")]
        recovered = [{"date": "20260902", "close": 12}]
        with (patch.object(service, "_baostock_session", return_value=nullcontext(client)),
              patch.object(service, "history", return_value=recovered) as fallback):
            result = service.history_batch(["600519", "000001"])
        self.assertEqual(result["600519"][0]["close"], 10.5)
        self.assertEqual(result["000001"], recovered)
        fallback.assert_called_once_with("000001", 420)

    def test_batch_still_excludes_failed_symbols_without_aborting_other_stocks(self):
        service = MarketDataService()
        client = Mock()
        client.query_history_k_data_plus.side_effect = [self.query([self.bar()]), self.query(error_code="10002007")]
        with (patch.object(service, "_baostock_session", return_value=nullcontext(client)),
              patch.object(service, "history", side_effect=RuntimeError("来源暂不可用"))):
            result = service.history_batch(["600519", "000001"])
        self.assertEqual(result["600519"][0]["close"], 10.5)
        self.assertEqual(result["000001"], [])
