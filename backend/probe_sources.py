"""Read-only live source diagnostics. No database writes and no AI requests."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .config import CHINA_TZ
from .forecast_prices import FeedbackPriceClient
from .history_sources import TushareHistoryClient, sina_index_history, ths_concept_history, validate_window


def probe(source: str, code: str, start: str, end: str) -> dict:
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
    args = parser.parse_args()
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
