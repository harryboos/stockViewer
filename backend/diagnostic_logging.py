"""Keep useful operational logs without recording upstream payloads or secrets."""
from __future__ import annotations

import logging
import sqlite3
import threading

_ACCESS_TEMPLATE = '%s - "%s %s HTTP/%s" %d'
_FILTER_NAME = "stockviewer.successful_system_healthcheck"
_install_lock = threading.Lock()


class SuccessfulHealthcheckFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__(_FILTER_NAME)

    def filter(self, record: logging.LogRecord) -> bool:
        # Both Uvicorn HTTP implementations supply these five positional args.
        # Fail open if its format changes; never suppress a warning or traceback.
        args = record.args
        if (record.name != "uvicorn.access" or record.levelno != logging.INFO
                or record.msg != _ACCESS_TEMPLATE or record.exc_info or record.stack_info
                or not isinstance(args, tuple) or len(args) != 5
                or not all(isinstance(value, str) for value in args[:4])):
            return True
        _, method, path, _, status = args
        return not (method == "GET" and path == "/api/system"
                    and type(status) is int and 200 <= status < 300)


def install_access_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    with _install_lock:
        # The stable name also survives module reloads and repeated lifespans.
        if not any(getattr(item, "name", None) == _FILTER_NAME for item in access.filters):
            access.addFilter(SuccessfulHealthcheckFilter())


def research_failure_reason(error: Exception) -> str:
    """Classify locally generated failure messages; return only fixed labels."""
    error_type = safe_error_type(error)
    if isinstance(error, TimeoutError) or error_type in {"ReadTimeout", "ConnectTimeout"}:
        return "timeout"
    if isinstance(error, ConnectionError) or error_type in {"ConnectionError", "ReadError", "ConnectError"}:
        return "connection_failed"
    if isinstance(error, sqlite3.Error):
        return "database_failed"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "invalid_data"
    if not isinstance(error, RuntimeError):
        return "unexpected_error"
    # Waiting messages explicitly say they are not missing-history failures.
    detail = str(error).replace("不计作日线缺失", "")
    if "交易日历" in detail or "观察窗口" in detail:
        return "calendar_unavailable"
    if "缺少概念范围" in detail or "暂无可用概念列表" in detail:
        return "universe_unavailable"
    if any(marker in detail for marker in ("来源连续不可用", "来源不可用", "实时行情暂不可用",
                                           "板块行情未完整更新", "均未返回有效数据")):
        return "source_unavailable"
    if any(marker in detail for marker in ("日线缺失", "日线不完整", "缺少完整个股或基准日线")):
        return "incomplete_history"
    if "尚未取得查询结果" in detail or "等待读取" in detail:
        return "history_pending"
    if "超时" in detail:
        return "timeout"
    return "task_failed"


def safe_error_type(error: Exception) -> str:
    name = type(error).__name__
    return name if name in {
        "RuntimeError", "ValueError", "TypeError", "KeyError", "TimeoutError",
        "ConnectionError", "OSError", "OperationalError", "DatabaseError",
        "HTTPError", "ReadTimeout", "ConnectTimeout", "ReadError", "ConnectError",
    } else "Exception"


def progress_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None
