"""Bounded daily OHLC and exchange calendar reads for forecast evaluation."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from datetime import date

from . import database
from .concept_data import ConceptResearchClient
from .history_sources import (
    TushareHistoryClient, history_covers_window, normalize_bars, sina_index_history, validate_window,
)

CALENDAR_KEY = "forecast_trade_calendar:v1"
logger = logging.getLogger(__name__)
_calendar_lock = threading.Lock()
_known_calendar: tuple[str, ...] = ()
_DECODER = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const output = vm.runInNewContext(input.source + '; JSON.stringify(d(encoded))',
  { encoded: input.encoded }, { timeout: 5000 });
process.stdout.write(output);
"""


class CalendarUnavailableError(RuntimeError):
    """A safe user message with the original failure retained in server logs."""


def decode_calendar(encoded: str) -> list[str]:
    from akshare.stock.cons import hk_js_decode
    node = shutil.which('node')
    if not node:
        raise RuntimeError('交易日历解析需要项目已有的 Node.js 运行环境')
    # Native MiniRacer initialization can abort the entire Python process when
    # multiple background workers first initialize it. Use the project's existing
    # Node runtime in a bounded child process; an engine crash cannot kill FastAPI.
    # Provider data is passed as a value, never interpolated into executable code.
    process = subprocess.run([node, '--max-old-space-size=64', '-e', _DECODER],
                             input=json.dumps({'source': hk_js_decode, 'encoded': encoded}),
                             text=True, capture_output=True, check=True, timeout=10)
    raw = json.loads(process.stdout)
    if not isinstance(raw, list) or not raw:
        raise ValueError('empty calendar')
    return sorted({date.fromisoformat(str(value)[:10]).isoformat() for value in raw})


class FeedbackPriceClient(ConceptResearchClient):
    def calendar(self) -> list[str]:
        # All consumers share the same durable calendar and initialization lock.
        global _known_calendar
        with _calendar_lock:
            dates = self._calendar()
            _known_calendar = tuple(dates)
            return dates

    def _calendar(self) -> list[str]:
        try:
            saved = json.loads(database.get_meta(CALENDAR_KEY) or "{}")
            if not isinstance(saved, dict) or not isinstance(saved.get("dates"), list):
                saved = {}
            else:
                saved["dates"] = sorted({date.fromisoformat(value).isoformat() for value in saved["dates"]})
        except (ValueError, TypeError):
            saved = {}
        if saved.get("fetchedOn") == database.china_date() and saved.get("dates"):
            return saved["dates"]
        stage = "读取"
        try:
            with self._session(trust_env=False) as session:
                response = session.get("https://finance.sina.com.cn/realstock/company/klc_td_sh.txt", timeout=(3, 8))
                response.raise_for_status()
            stage = "解析"
            encoded = response.text.split("=", 1)[1].split(";", 1)[0].strip().strip('"')
            dates = decode_calendar(encoded)
            if not dates:
                raise ValueError("empty calendar")
            stage = "保存"
            database.set_meta(CALENDAR_KEY, json.dumps({"dates": dates, "fetchedOn": database.china_date()}))
            return dates
        except Exception as error:
            logger.exception("预测反馈交易日历%s失败", stage)
            if saved.get("dates"):
                return saved["dates"]
            raise CalendarUnavailableError(f"交易日历{stage}失败，暂时无法核对收益；请更新服务后重试，具体原因见服务日志") from error

    def eastmoney_history(self, code: str, start: str, end: str) -> dict:
        if not re.fullmatch(r"BK\d+|sh000001|sh000688", code):
            raise ValueError("不支持的复盘标的")
        validate_window(start, end)
        secid = f"90.{code}" if code.startswith("BK") else f"1.{code[2:]}"
        payload = self._json([
            "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        ], {"secid": secid, "klt": "101", "fqt": "0", "lmt": "1000",
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "beg": start.replace("-", ""), "end": end.replace("-", "")})
        data = payload.get("data") or {}
        if not isinstance(data, dict) or str(data.get("code")) != code.removeprefix("sh"):
            raise ValueError("历史行情返回的标的代码不匹配")
        bars = normalize_bars([str(row).split(",") for row in data.get("klines", [])], start, end)
        return {"rows": bars, "source": "东方财富日线（不复权）",
                "url": f"https://quote.eastmoney.com/{'bk/90.' + code if code.startswith('BK') else 'zs' + code[2:]}.html"}

    def tencent_history(self, code: str, start: str, end: str) -> dict:
        if code not in ("sh000001", "sh000688"):
            raise ValueError("腾讯备用源仅用于上证指数和科创50")
        validate_window(start, end)
        payload = self._json([
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        ], {"param": f"{code},day,{start},{end},640,"})
        outer = payload.get("data")
        data = outer.get(code) if isinstance(outer, dict) else None
        if not isinstance(data, dict):
            raise ValueError("腾讯指数日线格式异常")
        bars = normalize_bars(data.get("day") or [], start, end)
        return {"rows": bars, "source": "腾讯证券指数日线", "url": f"https://gu.qq.com/{code}/zs"}

    def history(self, code: str, start: str, end: str) -> dict:
        if not re.fullmatch(r"BK\d+|sh000001|sh000688", code):
            raise ValueError("不支持的复盘标的")
        validate_window(start, end)
        # Independent benchmark sources avoid waiting on the failing BK service.
        if code.startswith("sh"):
            providers = [("腾讯", self.tencent_history), ("新浪", sina_index_history),
                         ("东方财富", self.eastmoney_history)]
        else:
            providers = ([("Tushare", TushareHistoryClient().history)] if TushareHistoryClient.configured() else [])
            providers.append(("东方财富", self.eastmoney_history))
        failures, best = [], None
        for name, read in providers:
            try:
                result = read(code, start, end)
                rows = result["rows"]
                if rows:
                    # Retain one provider's complete price series; never splice index bases.
                    if best is None or (len(rows), rows[-1]["date"]) > (len(best["rows"]), best["rows"][-1]["date"]):
                        best = result
                    if history_covers_window(rows, start, end, _known_calendar):
                        return result
                else:
                    failures.append(f"{name}未返回该区间的有效日线")
            except (RuntimeError, ValueError, TypeError, KeyError) as error:
                failures.append(str(error) if isinstance(error, (RuntimeError, ValueError)) else f"{name}日线格式异常")
        if best:
            return best
        return {"rows": [], "source": None, "url": None, "error": "；".join(failures)}
