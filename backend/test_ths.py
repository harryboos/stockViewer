from __future__ import annotations

import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import Mock, patch

import requests

from backend.ths import CHINA_TZ, ThsClient


def directory(count=1):
    items = {str(i): {"platecode": f"{885001 + i}", "cid": f"{300001 + i}",
                      "platename": f"概念{i}", "199112": i / 10} for i in range(count)}
    links = "".join(f'<a href="http://q.10jqka.com.cn/gn/detail/code/{row["cid"]}/">{row["platename"]}</a>'
                    for row in items.values())
    return links + "<input id='gnSection' value='" + json.dumps(items) + "'>"


def quote(symbol, **overrides):
    fields = {"5": symbol.split("_")[1], "name": "测试板块", "10": "100.2", "199112": "2.3",
              "19": "125000000", "38": "12", "39": "4", "3541450": "3000000000",
              "1968584": "2.1", "264648": "2.2", "updateTime": "2026-09-30 15:00",
              "time": "2026-10-01 09:00:00 北京时间", **overrides}
    return f"quotebridge_v4_realhead_{symbol}_last(" + json.dumps({"items": fields}) + ")"


def members(symbols, code="885001"):
    return f'<input id="clid" value="{code}"><table>' + "".join(
        '<tr>' + ''.join(f'<td>{value}</td>' for value in [1, symbol, "测试股票", *(["0"] * 11)]) + '</tr>'
        for symbol in symbols) + '</table>'


class ThsClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ThsClient()
        self.addCleanup(self.client._pool.shutdown, wait=True, cancel_futures=True)

    def test_concept_directory_uses_explicit_mapping_not_arithmetic(self):
        page = '<a href="http://q.10jqka.com.cn/gn/detail/code/301558/">互联网医疗</a>'
        page += '<input id="gnSection" value=\'{"x":{"platecode":"885611","cid":"301558","platename":"互联网医疗","199112":1.2}}\'>'
        rows, total = self.client._parse_directory(page, "concept")
        self.assertEqual(rows[0]["code"], "THS:885611")
        self.assertTrue(rows[0]["url"].endswith("/301558/"))
        self.assertEqual(total, 1)

    def test_industry_table_average_price_is_not_index_price(self):
        page = '<a href="https://q.10jqka.com.cn/thshy/detail/code/881142/">生物制品</a><table><tr>'
        page += ''.join(f'<td>{value}</td>' for value in [1, "生物制品", "4.63", 10, 20, 3, 5, 2, "26.93", "康希诺", 102, 20])
        rows, _ = self.client._parse_directory(page + '</tr></table>', "industry")
        self.assertEqual(rows[0]["code"], "THS:881142")
        self.assertNotIn("price", rows[0])
        self.assertEqual(rows[0]["leaderName"], "康希诺")

    def test_quote_uses_market_time_instead_of_holiday_response_time(self):
        result = self.client._parse_quote(quote("bk_885001"), "bk_885001")
        self.assertEqual(result["quotedAt"].strftime("%Y%m%d"), "20260930")
        self.assertEqual(result["amount"], 125000000)
        self.assertEqual(result["price"], 100.2)

    def test_quote_rejects_wrong_symbol_missing_time_and_invalid_values(self):
        variants = [{"5": "885002"}, {"updateTime": ""}, {"updateTime": "2026-09-30"}, {"updateTime": "2099-01-01 15:00"},
                    {"10": "nan"}, {"19": "-1"}]
        for overrides in variants:
            with self.subTest(overrides=overrides), self.assertRaises(RuntimeError):
                self.client._parse_quote(quote("bk_885001", **overrides), "bk_885001")
        with self.assertRaises(RuntimeError):
            self.client._parse_quote(quote("bk_885002"), "bk_885001")

    def test_board_reads_bounded_sample_with_verified_dates_and_reusable_cache(self):
        def load(url, _deadline):
            if url.endswith("/gn/"):
                return directory(40)
            return quote(url.split("/")[-2])
        with patch.object(self.client, "_get_text", side_effect=load) as get:
            frame = self.client.board_frame("concept")
            self.assertEqual(len(frame), 24)
            self.assertEqual(get.call_count, 25)
            self.assertEqual(frame.attrs["scope"]["directoryTotal"], 40)
            self.assertFalse(frame.attrs["scope"]["isComplete"])
            self.assertEqual(frame.iloc[0]["板块代码"], "THS:885040")
            self.assertEqual(datetime.fromtimestamp(frame.iloc[0]["行情时间"], CHINA_TZ).strftime("%Y%m%d"), "20260930")
            frame.loc[0, "板块名称"] = "changed"
            frame.attrs["scope"]["validCount"] = 0
            again = self.client.board_frame("concept")
            self.assertNotEqual(again.iloc[0]["板块名称"], "changed")
            self.assertEqual(again.attrs["scope"]["validCount"], 24)
            self.assertEqual(get.call_count, 25)

    def test_partial_quotes_are_reported_without_zero_filling_or_stale_dates(self):
        def load(url, _deadline):
            if url.endswith("/gn/"):
                return directory(3)
            symbol = url.split("/")[-2]
            if symbol == "bk_885002":
                raise RuntimeError("temporarily unavailable")
            return quote(symbol, updateTime="2026-09-29 15:00" if symbol == "bk_885001" else "2026-09-30 15:00")
        with patch.object(self.client, "_get_text", side_effect=load):
            frame = self.client.board_frame("concept")
        self.assertEqual(list(frame["板块代码"]), ["THS:885003"])
        self.assertEqual(frame.attrs["scope"]["requestedCount"], 3)
        self.assertEqual(frame.attrs["scope"]["validCount"], 1)

    def test_forced_refresh_reloads_quotes_and_failures_do_not_replace_good_cache(self):
        def load(url, _deadline):
            return directory() if url.endswith("/gn/") else quote("bk_885001")
        with patch.object(self.client, "_get_text", side_effect=load) as get:
            self.client.board_frame("concept")
            self.client.board_frame("concept", force=True)
            self.assertEqual(get.call_count, 4)
        with patch.object(self.client, "_get_text", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.board_frame("concept", force=True)
            self.assertEqual(len(self.client.board_frame("concept")), 1)

    def test_strong_members_require_same_day_positive_liquid_non_st_quotes(self):
        variations = {"600001": {}, "600002": {"name": "ST测试"}, "600003": {"199112": "-1"},
                      "600004": {"19": "99999999"}, "600005": {"updateTime": "2026-09-29 15:00"}}
        def load(url, _deadline):
            if url.endswith("/gn/"):
                return directory()
            if "/detail/" in url:
                return members(list(variations))
            symbol = url.split("/")[-2]
            return quote(symbol, **variations[symbol[3:]])
        with patch.object(self.client, "_get_text", side_effect=load) as get:
            rows = self.client.strong_stocks("THS:885001", "20260930")
            self.assertEqual([row["code"] for row in rows], ["600001"])
            self.assertIsNone(rows[0]["peDynamic"])
            self.assertEqual(rows[0]["tradeDate"], "20260930")
            self.client.strong_stocks("THS:885001", "20260930")
            self.assertEqual(get.call_count, 7)

    def test_member_index_mismatch_and_bk_namespace_are_rejected(self):
        with patch.object(self.client, "_get_text", side_effect=[directory(), members(["600001"], "885002")]):
            with self.assertRaises(RuntimeError):
                self.client.strong_stocks("THS:885001", "20260930")
        with self.assertRaises(ValueError):
            self.client.strong_stocks("BK1001", "20260930")

    def test_overlapping_quote_reads_are_coalesced(self):
        entered, release = threading.Event(), threading.Event()
        def load(_url, _deadline):
            entered.set()
            self.assertTrue(release.wait(2))
            return quote("hs_600001")
        with patch.object(self.client, "_get_text", side_effect=load) as get, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.client._quote, "hs_600001", time.monotonic() + 5)
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.client._quote, "hs_600001", time.monotonic() + 5)
            release.set()
            self.assertEqual(first.result()["price"], second.result()["price"])
            self.assertEqual(get.call_count, 1)

    def test_expired_deadline_does_not_start_network_read(self):
        with patch("backend.ths.requests.Session") as session, self.assertRaises(RuntimeError):
            self.client._get_text("https://q.10jqka.com.cn/gn/", time.monotonic() - 1)
        session.assert_not_called()

    def test_network_failure_retries_once_but_access_restriction_does_not(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        response = Mock(status_code=200, text="valid")
        session.get.side_effect = [requests.ConnectionError("disconnected"), response]
        with patch("backend.ths.requests.Session", return_value=session):
            self.assertEqual(self.client._get_text("https://q.10jqka.com.cn/gn/", time.monotonic() + 10), "valid")
        self.assertEqual(session.get.call_count, 2)
        session.get.reset_mock(side_effect=True)
        session.get.return_value = Mock(status_code=401)
        with patch("backend.ths.requests.Session", return_value=session), self.assertRaisesRegex(RuntimeError, "限制访问"):
            self.client._get_text("https://q.10jqka.com.cn/gn/", time.monotonic() + 10)
        self.assertEqual(session.get.call_count, 1)

    def test_unverified_directory_leader_is_not_attached_to_dated_snapshot(self):
        candidate = {"code": "THS:881142", "name": "生物制品", "url": "https://q.10jqka.com.cn/thshy/",
                     "leaderName": "过期领涨股", "leaderChange": 10}
        with patch.object(self.client, "_directory", return_value=([candidate], 1)), \
                patch.object(self.client, "_get_text", return_value=quote("bk_881142")):
            frame = self.client.board_frame("industry")
        self.assertEqual(frame.iloc[0]["领涨股票"], "")
        self.assertIsNone(frame.iloc[0]["领涨股票-涨跌幅"])

    def test_transient_gateway_failure_gets_one_bounded_retry(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = [Mock(status_code=502), Mock(status_code=200, text="ok")]
        with patch("backend.ths.requests.Session", return_value=session):
            self.assertEqual(self.client._get_text("https://d.10jqka.com.cn/example", time.monotonic() + 10), "ok")
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
