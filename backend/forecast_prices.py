"""Bounded daily OHLC and exchange calendar reads for forecast evaluation."""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import threading
from datetime import date

from . import database
from .concept_data import ConceptResearchClient

CALENDAR_KEY = "forecast_trade_calendar:v1"
logger = logging.getLogger(__name__)
_calendar_lock = threading.Lock()
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


def normalize_bars(rows: list, start: str, end: str) -> list[dict]:
    result = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row[0])).isoformat()
            values = [float(value) for value in row[1:5]]
            if len(values) != 4 or not all(math.isfinite(value) and value > 0 for value in values):
                continue
            opening, close, high, low = values
            if high < max(opening, close) or low > min(opening, close) or not start <= day <= end:
                continue
            result[day] = {"date": day, "open": opening, "close": close, "high": high, "low": low}
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(result.values(), key=lambda row: row["date"])


class FeedbackPriceClient(ConceptResearchClient):
    def calendar(self) -> list[str]:
        # All consumers share the same durable calendar and initialization lock.
        with _calendar_lock:
            return self._calendar()

    def _calendar(self) -> list[str]:
        try:
            saved = json.loads(database.get_meta(CALENDAR_KEY) or "{}")
            if not isinstance(saved, dict) or not isinstance(saved.get("dates"), list):
                saved = {}
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

    def history(self, code: str, start: str, end: str) -> dict:
        if not re.fullmatch(r"BK\d+|sh000001|sh000688", code):
            raise ValueError("不支持的复盘标的")
        secid = f"90.{code}" if code.startswith("BK") else f"1.{code[2:]}"
        try:
            payload = self._json([
                "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            ], {"secid": secid, "klt": "101", "fqt": "0", "lmt": "1000",
                "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "beg": start.replace("-", ""), "end": end.replace("-", "")})
            data = payload.get("data") or {}
            if str(data.get("code")) != code.removeprefix("sh"):
                raise ValueError("history symbol mismatch")
            bars = normalize_bars([str(row).split(",") for row in data.get("klines", [])], start, end)
            if bars:
                return {"rows": bars, "source": "东方财富日线（不复权）",
                        "url": f"https://quote.eastmoney.com/{'bk/90.' + code if code.startswith('BK') else 'zs' + code[2:]}.html"}
        except (RuntimeError, ValueError, TypeError):
            pass
        if code.startswith("sh"):
            try:
                data = self._json([
                    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                ], {"param": f"{code},day,{start},{end},640,"}).get("data", {}).get(code, {})
                bars = normalize_bars(data.get("day") or [], start, end)
                if bars:
                    return {"rows": bars, "source": "腾讯证券指数日线", "url": f"https://gu.qq.com/{code}/zs"}
            except (RuntimeError, ValueError, TypeError):
                pass
        return {"rows": [], "source": None, "url": None}
