"""Bounded daily OHLC and exchange calendar reads for forecast evaluation."""
from __future__ import annotations

import json
import math
import re
from datetime import date

from . import database
from .concept_data import ConceptResearchClient

CALENDAR_KEY = "forecast_trade_calendar:v1"


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
        saved = json.loads(database.get_meta(CALENDAR_KEY) or "{}")
        if saved.get("fetchedOn") == database.china_date():
            return saved["dates"]
        try:
            from akshare.stock.cons import hk_js_decode
            from py_mini_racer import MiniRacer
            with self._session(trust_env=False) as session:
                response = session.get("https://finance.sina.com.cn/realstock/company/klc_td_sh.txt", timeout=(3, 8))
                response.raise_for_status()
            encoded = response.text.split("=", 1)[1].split(";", 1)[0].strip().strip('"')
            with MiniRacer() as runtime:
                runtime.eval(hk_js_decode)
                raw = runtime.call("d", encoded)
            dates = sorted({date.fromisoformat(str(value)[:10]).isoformat() for value in raw})
            if not dates:
                raise ValueError("empty calendar")
            database.set_meta(CALENDAR_KEY, json.dumps({"dates": dates, "fetchedOn": database.china_date()}))
            return dates
        except Exception:
            if saved.get("dates"):
                return saved["dates"]
            raise RuntimeError("交易日历暂不可用，不能把缺行情误判为休市") from None

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
