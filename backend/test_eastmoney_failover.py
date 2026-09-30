from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from backend.eastmoney import EastmoneyClient


class EastmoneySnapshotFailoverTests(unittest.TestCase):
    def setUp(self):
        self.client = EastmoneyClient()
        self.sessions = [Mock(), Mock()]
        for mocked in (
            patch.object(self.client, "_hosts", return_value=["first.eastmoney.com", "second.eastmoney.com"]),
            patch.object(self.client, "_session", side_effect=self.sessions),
            patch("backend.eastmoney.requests.utils.get_environ_proxies", return_value={}),
            patch("backend.eastmoney.time.sleep"),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_empty_first_page_tries_another_route(self):
        with patch.object(self.client, "_request_page", side_effect=[
            {"total": 400, "diff": []},
            {"total": 1, "diff": [{"f12": "BK1152", "f2": 101}]},
        ]) as fetch:
            rows = self.client.fetch_pages("79", {"fields": "f12,f2"})
        self.assertEqual(rows, [{"f12": "BK1152", "f2": 101}])
        self.assertEqual([call.args[3] for call in fetch.call_args_list], [1, 1])
        for session in self.sessions:
            session.close.assert_called_once()

    def test_later_page_failure_restarts_snapshot_without_mixing_routes(self):
        with patch.object(self.client, "_request_page", side_effect=[
            {"total": 2, "diff": [{"f12": "BK1152", "f2": 90}]},
            requests.ConnectionError("disconnected"),
            {"total": 2, "diff": [{"f12": "BK1152", "f2": 101}]},
            {"total": 2, "diff": [{"f12": "BK1136", "f2": 102}]},
        ]) as fetch:
            rows = self.client.fetch_pages("79", {"fields": "f12,f2"})
        self.assertEqual([row["f2"] for row in rows], [101, 102])
        self.assertEqual([call.args[3] for call in fetch.call_args_list], [1, 2, 1, 2])
        self.assertIn("second.eastmoney.com", fetch.call_args_list[2].args[0])
        for session in self.sessions:
            session.close.assert_called_once()

    def test_inconsistent_snapshot_tries_another_route(self):
        for invalid in (
            {"total": 3, "diff": [{"f12": "BK1136"}]},
            {"total": 2, "diff": [{"f12": "BK1152"}]},
        ):
            with self.subTest(invalid=invalid):
                # Each subtest has two independent route sessions.
                with patch.object(self.client, "_session", side_effect=[Mock(), Mock()]):
                    with patch.object(self.client, "_request_page", side_effect=[
                        {"total": 2, "diff": [{"f12": "BK1152"}]}, invalid,
                        {"total": 1, "diff": [{"f12": "BK1152", "f2": 101}]},
                    ]):
                        rows = self.client.fetch_pages("79", {"fields": "f12,f2"})
                self.assertEqual(rows, [{"f12": "BK1152", "f2": 101}])

    def test_global_budget_stops_before_next_route(self):
        with (patch("backend.eastmoney.time.monotonic", side_effect=[0, 0, 61]),
              patch.object(self.client, "_request_page", side_effect=requests.Timeout("timeout")) as fetch):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                self.client.fetch_pages("79", {"fields": "f12"})
        fetch.assert_called_once()
        self.sessions[0].close.assert_called_once()
        self.sessions[1].close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
