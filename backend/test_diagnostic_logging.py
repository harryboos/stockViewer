from __future__ import annotations

import logging
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from backend import concept_comparison, research_jobs
from backend.diagnostic_logging import (
    SuccessfulHealthcheckFilter, install_access_log_filter, research_failure_reason,
)


def access_record(method="GET", path="/api/system", status=200, **kwargs):
    return logging.LogRecord(
        kwargs.pop("name", "uvicorn.access"), kwargs.pop("level", logging.INFO), "test", 1,
        kwargs.pop("message", '%s - "%s %s HTTP/%s" %d'),
        ("127.0.0.1:12345", method, path, "1.1", status), kwargs.pop("exc_info", None),
        sinfo=kwargs.pop("stack_info", None), **kwargs,
    )


class HealthcheckLoggingTests(unittest.TestCase):
    def test_only_standard_successful_health_get_is_filtered(self):
        filter_ = SuccessfulHealthcheckFilter()
        for status in (200, 204, 299):
            with self.subTest(status=status):
                self.assertFalse(filter_.filter(access_record(status=status)))
        for options in (
            {"status": 301}, {"status": 401}, {"status": 500}, {"status": "200"},
            {"path": "/api/forecast/history"}, {"path": "/api/system?debug=1"},
            {"path": "/api/system/"}, {"method": "POST"}, {"method": "HEAD"},
            {"name": "backend.research_jobs"}, {"level": logging.WARNING},
            {"exc_info": (RuntimeError, RuntimeError("failed"), None)},
            {"message": 'changed %s %s %s %s %s'},
        ):
            with self.subTest(options=options):
                self.assertTrue(filter_.filter(access_record(**options)))

    def test_unknown_record_shape_is_kept_and_never_formatted(self):
        for args in (None, (), ("GET", "/api/system", 200), {"status": 200}, [None] * 5,
                     (None, "GET", "/api/system", "1.1", 200),
                     ("127.0.0.1", "GET", "/api/system", None, 200)):
            record = access_record()
            record.args = args
            with self.subTest(args=args):
                self.assertTrue(SuccessfulHealthcheckFilter().filter(record))
        self.assertTrue(SuccessfulHealthcheckFilter().filter(access_record(stack_info="trace")))

    def test_installation_is_idempotent_without_touching_other_filters(self):
        logger = logging.getLogger("uvicorn.access")
        existing = logger.filters[:]
        self.addCleanup(setattr, logger, "filters", existing)
        unrelated = lambda record: True
        logger.filters = [unrelated]
        install_access_log_filter()
        install_access_log_filter()
        self.assertEqual(len(logger.filters), 2)
        self.assertIs(logger.filters[0], unrelated)
        self.assertFalse(logger.filter(access_record()))
        self.assertTrue(logger.filter(access_record(status=503)))


class ResearchFailureLoggingTests(unittest.TestCase):
    def test_known_failures_map_to_fixed_categories(self):
        cases = (
            (TimeoutError("private timeout payload"), "timeout"),
            (ConnectionError("https://private.invalid?token=secret"), "connection_failed"),
            (sqlite3.OperationalError("sensitive database path"), "database_failed"),
            (ValueError("response with credentials"), "invalid_data"),
            (RuntimeError("交易日历未覆盖最近收盘日期"), "calendar_unavailable"),
            (RuntimeError("旧预测缺少概念范围"), "universe_unavailable"),
            (RuntimeError("行情来源连续不可用，已暂停本批后续查询；仍有 4087 项尚未取得查询结果"), "source_unavailable"),
            (RuntimeError("20 项概念日线缺失或不连续；另有 7 项等待读取"), "incomplete_history"),
            (RuntimeError("仍有 40 项尚未取得查询结果"), "history_pending"),
            (RuntimeError("本批已核对 0 项，仍有 10 项轮动行情尚未取得查询结果；不计作日线缺失，可继续核对"), "history_pending"),
            (RuntimeError("读取超时"), "timeout"),
            (RuntimeError("unknown private message"), "task_failed"),
            (Exception("unknown private message"), "unexpected_error"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(research_failure_reason(error), expected)

    def test_job_failure_logs_safe_reason_counts_and_elapsed_only(self):
        def fail(progress):
            progress(4, 4087, "Authorization: stage-secret https://private.invalid")
            raise RuntimeError("行情来源连续不可用 secret-response https://private.invalid?token=key")

        with (patch.object(research_jobs.database, "connection", return_value=MagicMock()),
              patch.object(research_jobs.time, "monotonic", side_effect=[10.0, 12.3456]),
              patch.object(concept_comparison, "refresh_comparisons", side_effect=fail),
              self.assertLogs(research_jobs.logger, level="WARNING") as captured):
            research_jobs._run("comparison", "not-for-logs")
        self.assertEqual(captured.records[0].getMessage(),
                         "research_job_failed key=comparison error_type=RuntimeError "
                         "reason=source_unavailable done=4 total=4087 elapsed_seconds=2.346")
        self.assertIsNone(captured.records[0].exc_info)
        self.assertNotIn("secret", str(captured.records[0].args))
        self.assertNotIn("https", str(captured.records[0].args))

    def test_invalid_progress_and_custom_exception_name_cannot_enter_logs(self):
        private_error = type("private_exception_name", (Exception,), {})
        def fail(progress):
            progress("private-done", True, "private-stage")
            raise private_error("private-error")

        with (patch.object(research_jobs.database, "connection", return_value=MagicMock()),
              patch.object(concept_comparison, "refresh_comparisons", side_effect=fail),
              self.assertLogs(research_jobs.logger, level="WARNING") as captured):
            research_jobs._run("comparison", "private-token")
        message = captured.records[0].getMessage()
        self.assertIn("error_type=Exception reason=unexpected_error done=None total=None", message)
        self.assertNotIn("private", message)


if __name__ == "__main__":
    unittest.main()
