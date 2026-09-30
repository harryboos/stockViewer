from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd
import requests

from backend.config import CHINA_TZ
from backend.probe_sources import _board_child, board_frame_summary, main, probe_board


def board_frame(**overrides):
    return pd.DataFrame([{"板块代码": "BK1152", "板块名称": "高带宽内存", "涨跌幅": 1.2,
                          "上涨家数": 8, "下跌家数": 2,
                          "行情时间": datetime(2020, 9, 30, 15, tzinfo=CHINA_TZ).timestamp(), **overrides}])


class BoardProbeTests(unittest.TestCase):
    def test_summary_counts_valid_rows_without_showing_raw_records(self):
        valid = board_frame()
        frame = pd.concat([valid, valid, board_frame(板块代码="BK0002", 涨跌幅=float("nan")),
                           board_frame(板块代码="BK0003", 上涨家数=-1)], ignore_index=True)
        result = board_frame_summary(frame)
        self.assertEqual((result["rowCount"], result["validRowCount"], result["invalidRowCount"]), (4, 1, 3))
        self.assertEqual(result["quoteDates"], ["2020-09-30"])
        self.assertEqual(result["quoteDateStatus"], "provided")
        self.assertNotIn("BK1152", json.dumps(result))

    def test_missing_timestamp_does_not_fabricate_today(self):
        result = board_frame_summary(board_frame().drop(columns="行情时间"))
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["quoteDateStatus"], "unavailable")
        self.assertEqual(result["quoteDates"], [])
        self.assertIsNone(result["quoteAsOf"])
        self.assertIn("不能", result["quoteDateNote"])

    def test_required_fields_and_empty_responses_are_distinct(self):
        result = board_frame_summary(board_frame().drop(columns="上涨家数"))
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["missingColumns"], ["上涨家数"])
        self.assertEqual(board_frame_summary(board_frame().iloc[:0])["status"], "empty")
        self.assertEqual(board_frame_summary(board_frame(板块名称=float("nan")))["status"], "invalid")

    def test_akshare_sources_are_checked_individually_and_do_not_echo_output(self):
        fake = Mock()
        def noisy_reader():
            print("https://secret:password@proxy.invalid")
            return board_frame()
        fake.stock_board_concept_name_em.side_effect = noisy_reader
        captured = io.StringIO()
        with patch.dict(sys.modules, {"akshare": fake}), contextlib.redirect_stdout(captured):
            result = _board_child("akshare", "concept")
        self.assertEqual(result["validRowCount"], 1)
        self.assertEqual(captured.getvalue(), "")
        fake.stock_board_industry_name_em.assert_not_called()

    def test_wrapped_connection_failure_is_classified_without_credentials(self):
        unsafe = "HTTPSConnectionPool https://user:secret@proxy.invalid RemoteDisconnected"
        error = RuntimeError("东方财富备用线路连接失败：" + unsafe)
        error.__cause__ = requests.ConnectionError(unsafe)
        with patch("backend.eastmoney.EastmoneyClient.industry_name_frame", side_effect=error):
            result = _board_child("eastmoney", "industry")
        self.assertEqual(result["errorKind"], "connection")
        self.assertEqual(result["reason"], "远端中断连接")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("proxy.invalid", json.dumps(result))

    def test_child_process_has_hard_timeout_and_never_echoes_timeout_output(self):
        with patch("backend.probe_sources.subprocess.run", side_effect=subprocess.TimeoutExpired("child", 5, "secret")) as run:
            result = probe_board("akshare", "concept", 5)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["timeoutLimitSeconds"], 5)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("secret", json.dumps(result))

    def test_child_crashes_and_invalid_json_are_safe(self):
        failures = [subprocess.CalledProcessError(1, "child", output="secret"),
                    Mock(stdout="secret", returncode=0)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch("backend.probe_sources.subprocess.run", **kwargs):
                    result = probe_board("eastmoney", "industry")
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("secret", json.dumps(result))

    def test_cli_boards_uses_selected_sources_and_does_not_run_history(self):
        captured = io.StringIO()
        with patch.object(sys, "argv", ["probe", "--boards", "--board-sources", "akshare", "--board-timeout", "10"]), \
                patch("backend.probe_sources.probe_board", return_value={"status": "empty"}) as board_probe, \
                patch("backend.probe_sources.probe") as history_probe, contextlib.redirect_stdout(captured):
            main()
        self.assertEqual(json.loads(captured.getvalue())["mode"], "boards")
        self.assertEqual({call.args for call in board_probe.call_args_list},
                         {("akshare", "industry", 10), ("akshare", "concept", 10)})
        history_probe.assert_not_called()

    def test_board_module_can_load_without_importing_database_or_ai(self):
        result = subprocess.run([sys.executable, "-c",
                                 "import sys; import backend.probe_sources; "
                                 "assert 'backend.database' not in sys.modules; "
                                 "assert 'backend.ai' not in sys.modules"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
