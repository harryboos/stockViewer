"""Read-only live source diagnostics. No database writes and no AI requests."""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .config import CHINA_TZ
from .source_errors import source_failure_reason


BOARD_COLUMNS = ("板块代码", "板块名称", "涨跌幅", "上涨家数", "下跌家数")
BOARD_ERROR_MESSAGES = {
    "timeout": "来源请求超过本次检查的时间预算",
    "connection": "来源连接中断或无法建立连接",
    "http": "来源返回非成功 HTTP 状态",
    "invalid_response": "来源返回的数据格式或分页无效",
    "dependency": "本地行情依赖不可用，请检查安装",
    "worker": "独立检查进程未正常完成",
    "unknown": "来源检查失败，未输出可能含凭据的原始异常",
}


def board_error_kind(error: Exception) -> str:
    """Classify wrapped failures without returning URLs, proxy credentials or bodies."""
    import requests
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (requests.Timeout, TimeoutError, subprocess.TimeoutExpired)):
            return "timeout"
        if isinstance(current, requests.ConnectionError):
            return "connection"
        if isinstance(current, requests.HTTPError):
            return "http"
        if isinstance(current, ImportError):
            return "dependency"
        current = current.__cause__ or current.__context__
    if isinstance(error, RuntimeError):
        message = str(error).lower()
        if "超时" in message or "timed out" in message or "timeout" in message:
            return "timeout"
        if any(marker in message for marker in ("连接失败", "连接中断", "connection", "remotedisconnected")):
            return "connection"
        if any(marker in message for marker in ("数据", "分页", "行情", "格式")):
            return "invalid_response"
    return "invalid_response" if isinstance(error, (ValueError, TypeError, KeyError)) else "unknown"


def _finite(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def board_frame_summary(frame) -> dict:
    """Check source data only; this does not run application aggregation or filters."""
    if not hasattr(frame, "columns") or not hasattr(frame, "to_dict"):
        raise ValueError("invalid frame")
    missing = [column for column in BOARD_COLUMNS if column not in frame.columns]
    if missing:
        return {"status": "invalid", "rowCount": len(frame), "validRowCount": 0,
                "missingColumns": missing, "errorKind": "invalid_response",
                "message": BOARD_ERROR_MESSAGES["invalid_response"]}
    valid = []
    seen = set()
    for row in frame.to_dict(orient="records"):
        code, name = str(row.get("板块代码", "")).strip(), str(row.get("板块名称", "")).strip()
        pct, up, down = (_finite(row.get(key)) for key in ("涨跌幅", "上涨家数", "下跌家数"))
        if (not re.fullmatch(r"BK\d+", code) or not name or name.lower() in {"nan", "none", "<na>"}
                or code in seen or pct is None or up is None or down is None or up < 0 or down < 0):
            continue
        seen.add(code)
        valid.append(row)
    quoted = []
    now = datetime.now(CHINA_TZ)
    for row in valid:
        stamp = _finite(row.get("行情时间"))
        if stamp is None or stamp < 946684800:
            continue
        try:
            at = datetime.fromtimestamp(stamp, CHINA_TZ)
        except (ValueError, OverflowError, OSError):
            continue
        if at <= now:
            quoted.append(at)
    dates = sorted({at.date().isoformat() for at in quoted})
    date_status = "unavailable" if not quoted else "partial" if len(quoted) != len(valid) else "mixed" if len(dates) > 1 else "provided"
    return {"status": "available" if valid else "empty" if not len(frame) else "invalid",
            "rowCount": len(frame), "validRowCount": len(valid), "invalidRowCount": len(frame) - len(valid),
            "quoteDates": dates, "quoteAsOf": max(quoted).isoformat() if quoted else None,
            "quoteDateStatus": date_status,
            "quoteDateNote": "来源未完整提供有效报价时间，不能据此验证行情日期" if date_status in {"unavailable", "partial"}
                             else "仅记录来源报价时间，未验证是否为最近交易日"}


def _board_child(source: str, kind: str) -> dict:
    try:
        # AKShare may print progress or upstream details. Only our fixed schema
        # is allowed on stdout; the parent deliberately discards stderr too.
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            if source == "eastmoney":
                from .eastmoney import EastmoneyClient
                client = EastmoneyClient()
                frame = client.industry_name_frame() if kind == "industry" else client.concept_name_frame()
            else:
                import akshare as ak
                frame = ak.stock_board_industry_name_em() if kind == "industry" else ak.stock_board_concept_name_em()
            return board_frame_summary(frame)
    except Exception as error:
        category = board_error_kind(error)
        return {"status": "unavailable", "errorKind": category, "message": BOARD_ERROR_MESSAGES[category],
                "reason": source_failure_reason(error)}


def probe_board(source: str, kind: str, timeout_seconds: float = 75) -> dict:
    if source not in {"eastmoney", "akshare"} or kind not in {"industry", "concept"}:
        raise ValueError("unknown board source or kind")
    if not math.isfinite(timeout_seconds) or not 5 <= timeout_seconds <= 90:
        raise ValueError("board timeout must be between 5 and 90 seconds")
    clock = time.monotonic()
    result = {"source": source, "kind": kind, "timeoutLimitSeconds": timeout_seconds}
    try:
        # Threads cannot stop a stuck third-party request. A child process gives
        # every list request a hard limit, including imports and pagination.
        process = subprocess.run([sys.executable, "-m", "backend.probe_sources", "--board-child", source, kind],
                                 cwd=os.path.dirname(os.path.dirname(__file__)), text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout_seconds, check=True)
        payload = json.loads(process.stdout)
        if not isinstance(payload, dict) or "status" not in payload:
            raise ValueError("invalid child response")
        result.update(payload)
    except subprocess.TimeoutExpired:
        result.update(status="timeout", errorKind="timeout", message=BOARD_ERROR_MESSAGES["timeout"])
    except (OSError, subprocess.CalledProcessError):
        result.update(status="unavailable", errorKind="worker", message=BOARD_ERROR_MESSAGES["worker"])
    except (ValueError, TypeError):
        result.update(status="unavailable", errorKind="invalid_response", message=BOARD_ERROR_MESSAGES["invalid_response"])
    result["elapsedSeconds"] = round(time.monotonic() - clock, 3)
    return result


def probe(source: str, code: str, start: str, end: str) -> dict:
    from .forecast_prices import FeedbackPriceClient
    from .history_sources import TushareHistoryClient, sina_index_history, ths_concept_history

    clock = time.monotonic()
    result = {"source": source, "code": code, "requestedStart": start, "requestedEnd": end}
    readers = {"eastmoney": FeedbackPriceClient().eastmoney_history,
               "tencent": FeedbackPriceClient().tencent_history, "sina": sina_index_history,
               "tushare": TushareHistoryClient().history, "ths": ths_concept_history,
               "auto": FeedbackPriceClient().history}
    if source == "tushare" and not TushareHistoryClient.configured():
        return {**result, "status": "not_configured", "message": "未配置 TUSHARE_TOKEN，未发起凭证请求"}
    try:
        data = readers[source](code, start, end)
        rows = data.get("rows", [])
        result.update(status="available" if rows else "unavailable" if data.get("error") else "empty", rowCount=len(rows),
                      dates=[row["date"] for row in rows], actualSource=data.get("source"),
                      first=rows[0] if rows else None, last=rows[-1] if rows else None)
        if data.get("error"):
            result["message"] = data["error"]
    except (RuntimeError, ValueError, TypeError, KeyError) as error:
        result.update(status="unavailable", message=str(error) if isinstance(error, RuntimeError) else type(error).__name__)
    result["elapsedSeconds"] = round(time.monotonic() - clock, 3)
    return result


def main() -> None:
    today = datetime.now(CHINA_TZ).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=(today - timedelta(days=7)).isoformat())
    parser.add_argument("--end", default=(today - timedelta(days=1)).isoformat())
    parser.add_argument("--sources", nargs="+", choices=("eastmoney", "tencent", "sina", "tushare", "ths", "auto"),
                        default=["eastmoney", "tencent", "sina", "tushare", "ths"])
    parser.add_argument("--boards", action="store_true", help="独立检查板块列表，替代默认的历史日线检查")
    parser.add_argument("--board-sources", nargs="+", choices=("eastmoney", "akshare"), default=["eastmoney", "akshare"])
    parser.add_argument("--board-timeout", type=float, default=75, help="每项板块检查的硬超时秒数（5–90，默认75）")
    parser.add_argument("--board-child", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.board_child:
        source, kind = args.board_child
        if source not in {"eastmoney", "akshare"} or kind not in {"industry", "concept"}:
            parser.error("unknown board source or kind")
        print(json.dumps(_board_child(source, kind), ensure_ascii=False))
        return
    if args.boards:
        if not math.isfinite(args.board_timeout) or not 5 <= args.board_timeout <= 90:
            parser.error("--board-timeout must be between 5 and 90 seconds")
        tasks = [(source, kind) for source in dict.fromkeys(args.board_sources) for kind in ("industry", "concept")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda task: probe_board(*task, args.board_timeout), tasks))
        print(json.dumps({"testedAt": datetime.now(CHINA_TZ).isoformat(), "mode": "boards",
                          "note": "仅检查独立板块列表，不代表应用完整聚合结果。两条线路均依赖东方财富；缺报价时间时无法验证日期。超时包括导入与分页，未写数据库或调用 AI。",
                          "results": results}, ensure_ascii=False, indent=2))
        return
    from .history_sources import validate_window
    validate_window(args.start, args.end)
    concepts, indices = ["BK1152", "BK1136", "BK0917"], ["sh000001", "sh000688"]
    samples = {"eastmoney": concepts, "tushare": concepts, "auto": concepts + indices,
               "tencent": indices, "sina": indices, "ths": ["THS:886042", "THS:886033", "THS:885756"]}
    tasks = [(source, code) for source in dict.fromkeys(args.sources) for code in samples[source]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda task: probe(*task, args.start, args.end), tasks))
    print(json.dumps({"testedAt": datetime.now(CHINA_TZ).isoformat(),
                      "note": "THS 为同花顺独立概念口径，不可替代原 BK 预测收益。available 仅表示返回有效日线，日期完整性须按交易日历核对。",
                      "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
