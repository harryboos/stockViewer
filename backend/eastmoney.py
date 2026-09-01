from __future__ import annotations

import math
import random
import time
from typing import Any

import pandas as pd
import requests

from .config import MARKET, MarketSettings


API_PATH = "/api/qt/clist/get"
COMMON_PARAMS = {
    "po": "1",
    "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2",
    "invt": "2",
    "fid": "f12",
}

SPOT_FIELD_MAP = {
    "代码": "f12", "名称": "f14", "最新价": "f2", "涨跌幅": "f3", "涨跌额": "f4",
    "成交量": "f5", "成交额": "f6", "振幅": "f7", "换手率": "f8", "市盈率-动态": "f9",
    "量比": "f10", "最高": "f15", "最低": "f16", "今开": "f17", "昨收": "f18",
    "总市值": "f20", "流通市值": "f21", "市净率": "f23", "行情时间": "f124",
}

CONCEPT_FIELD_MAP = {
    "板块名称": "f14", "板块代码": "f12", "最新价": "f2", "涨跌额": "f4", "涨跌幅": "f3",
    "成交额": "f6", "总市值": "f20", "换手率": "f8", "上涨家数": "f104", "下跌家数": "f105",
    "领涨股票": "f128", "领涨股票-涨跌幅": "f136",
}

CONSTITUENT_FIELD_MAP = {
    "代码": "f12", "名称": "f14", "最新价": "f2", "涨跌幅": "f3", "涨跌额": "f4",
    "成交量": "f5", "成交额": "f6", "振幅": "f7", "换手率": "f8", "市盈率-动态": "f9",
    "最高": "f15", "最低": "f16", "今开": "f17", "昨收": "f18", "市净率": "f23",
}

SECTOR_FUND_FLOW_FIELD_MAP = {
    "名称": "f14", "涨跌幅": "f3", "主力净流入-净额": "f62",
    "主力净流入-净占比": "f184", "超大单净流入-净额": "f66",
    "大单净流入-净额": "f72", "主力净流入最大股": "f204",
    "主力净流入最大股代码": "f205",
}


class EastmoneyClient:
    """Small, testable client for Eastmoney's AKShare-compatible delayed route."""

    def __init__(self, settings: MarketSettings = MARKET) -> None:
        self.settings = settings

    def _hosts(self, node: str) -> list[str]:
        defaults = (f"{node}.push2delay.eastmoney.com", "push2delay.eastmoney.com")
        return list(dict.fromkeys((*self.settings.eastmoney_delay_hosts, *defaults)))

    @staticmethod
    def _session(trust_env: bool) -> requests.Session:
        session = requests.Session()
        session.trust_env = trust_env
        session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "Referer": "https://quote.eastmoney.com/center/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            }
        )
        return session

    @staticmethod
    def _frame(rows: list[dict[str, Any]], mapping: dict[str, str]) -> pd.DataFrame:
        return pd.DataFrame(
            [{column: row.get(field) for column, field in mapping.items()} for row in rows]
        )

    @staticmethod
    def _request_page(
        url: str,
        session: requests.Session,
        params: dict[str, str],
        page: int,
    ) -> dict[str, Any]:
        response = session.get(
            url,
            params={**params, "pn": str(page), "pz": "100"},
            timeout=(6, 18),
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
            raise RuntimeError("东方财富备用线路返回了无效数据")
        return data

    def fetch_pages(self, node: str, params: dict[str, str]) -> list[dict[str, Any]]:
        routes = [
            (f"https://{host}{API_PATH}", trust_env)
            for trust_env in (False, True)
            for host in self._hosts(node)
        ]
        last_error: Exception | None = None
        active: tuple[str, requests.Session] | None = None
        first_payload: dict[str, Any] | None = None

        for url, trust_env in routes:
            session = self._session(trust_env)
            try:
                first_payload = self._request_page(url, session, params, 1)
                active = (url, session)
                break
            except (requests.RequestException, ValueError, RuntimeError) as error:
                last_error = error
                session.close()

        if active is None or first_payload is None:
            raise RuntimeError(f"东方财富备用线路连接失败：{last_error or '未知错误'}")

        url, session = active
        try:
            rows = list(first_payload["diff"])
            total = int(first_payload.get("total") or len(rows))
            page_count = max(1, math.ceil(total / max(len(rows), 1)))
            for page in range(2, page_count + 1):
                if self.settings.eastmoney_page_delay_seconds:
                    jitter = random.uniform(
                        0,
                        min(self.settings.eastmoney_page_delay_seconds / 2, 0.2),
                    )
                    time.sleep(self.settings.eastmoney_page_delay_seconds + jitter)
                page_error: Exception | None = None
                for attempt in range(3):
                    try:
                        payload = self._request_page(url, session, params, page)
                        rows.extend(payload["diff"])
                        page_error = None
                        break
                    except (requests.RequestException, ValueError, RuntimeError) as error:
                        page_error = error
                        time.sleep(0.35 * (attempt + 1))
                if page_error is not None:
                    raise RuntimeError(
                        f"东方财富备用线路第 {page} 页获取失败：{page_error}"
                    ) from page_error
            if not rows:
                raise RuntimeError("东方财富备用线路没有返回行情")
            return rows
        finally:
            session.close()

    def spot_frame(self) -> pd.DataFrame:
        params = {
            **COMMON_PARAMS,
            "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
            "fields": ",".join(SPOT_FIELD_MAP.values()),
        }
        return self._frame(self.fetch_pages("82", params), SPOT_FIELD_MAP)

    def concept_name_frame(self) -> pd.DataFrame:
        params = {
            **COMMON_PARAMS,
            "fs": "m:90 t:3 f:!50",
            "fields": ",".join(CONCEPT_FIELD_MAP.values()),
        }
        return self._frame(self.fetch_pages("79", params), CONCEPT_FIELD_MAP)

    def industry_name_frame(self) -> pd.DataFrame:
        params = {
            **COMMON_PARAMS,
            "fs": "m:90 t:2 f:!50",
            "fields": ",".join(CONCEPT_FIELD_MAP.values()),
        }
        return self._frame(self.fetch_pages("79", params), CONCEPT_FIELD_MAP)

    def sector_fund_flow_frame(self, sector_type: str) -> pd.DataFrame:
        if sector_type not in {"industry", "concept"}:
            raise ValueError("sector_type 必须是 industry 或 concept")
        params = {
            **COMMON_PARAMS,
            "fid": "f62",
            "fid0": "f62",
            "stat": "1",
            "fs": f"m:90 t:{'2' if sector_type == 'industry' else '3'}",
            "fields": ",".join(SECTOR_FUND_FLOW_FIELD_MAP.values()),
        }
        return self._frame(self.fetch_pages("79", params), SECTOR_FUND_FLOW_FIELD_MAP)

    def _trend_rows(self, secid: str) -> list[str]:
        trend_params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ndays": "2",
            "iscr": "0",
            "iscca": "0",
        }
        hosts = (
            "push2his.eastmoney.com",
            "91.push2his.eastmoney.com",
            "7.push2his.eastmoney.com",
        )
        last_error: Exception | None = None
        for trust_env, attempts in ((False, 3), (True, 1)):
            for host in hosts:
                session = self._session(trust_env)
                try:
                    for attempt in range(attempts):
                        try:
                            response = session.get(
                                f"https://{host}/api/qt/stock/trends2/get",
                                params=trend_params,
                                timeout=(4, 10),
                            )
                            response.raise_for_status()
                            payload = response.json()
                            data = payload.get("data") if isinstance(payload, dict) else None
                            trends = data.get("trends") if isinstance(data, dict) else None
                            if not isinstance(trends, list) or not trends:
                                raise RuntimeError("东方财富分时历史没有返回有效数据")
                            return [str(item) for item in trends]
                        except (requests.RequestException, ValueError, RuntimeError) as error:
                            last_error = error
                            if attempt + 1 < attempts:
                                time.sleep(0.3 * (attempt + 1) + random.uniform(0, 0.15))
                finally:
                    session.close()

        raise RuntimeError(f"东方财富分时历史连接失败：{last_error or '未知错误'}")

    @staticmethod
    def _turnover_points(rows: list[str]) -> list[tuple[str, str, float]]:
        points: list[tuple[str, str, float]] = []
        for item in rows:
            values = item.split(",")
            if len(values) < 7:
                continue
            timestamp = values[0]
            if len(timestamp) < 16:
                continue
            try:
                amount = float(values[6])
            except (TypeError, ValueError):
                continue
            if math.isfinite(amount) and amount >= 0:
                points.append((timestamp[:10], timestamp[11:16], amount))
        return points

    def sector_history_frame(self, code: str) -> pd.DataFrame:
        normalized = str(code).strip().upper()
        if not normalized.startswith("BK") or not normalized[2:].isdigit():
            raise ValueError("板块代码格式不正确")
        points = self._turnover_points(self._trend_rows(f"90.{normalized}"))
        amount_by_date: dict[str, float] = {}
        for trade_date, _, amount in points:
            amount_by_date[trade_date] = amount_by_date.get(trade_date, 0.0) + amount
        if len(amount_by_date) < 2:
            raise RuntimeError("东方财富板块分时历史缺少前一交易日")
        return pd.DataFrame(
            [{"日期": date_key, "成交额": amount_by_date[date_key]} for date_key in sorted(amount_by_date)]
        )

    def index_intraday_turnover_points(self, secid: str) -> list[tuple[str, str, float]]:
        if secid not in {"1.000001", "0.399106"}:
            raise ValueError("指数代码不支持")
        points = self._turnover_points(self._trend_rows(secid))
        if not points:
            raise RuntimeError("东方财富指数分时历史没有有效成交额")
        return points

    @staticmethod
    def market_intraday_pair_from_points(
        points_by_index: dict[str, list[tuple[str, str, float]]],
        trade_date: str,
        cutoff_time: str,
    ) -> dict[str, Any]:
        normalized_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        if len(points_by_index) < 2:
            raise RuntimeError("沪深指数分时历史不完整")
        common_dates = set.intersection(*(
            {point[0] for point in points}
            for points in points_by_index.values()
        ))
        if normalized_date not in common_dates:
            raise RuntimeError("沪深指数分时历史缺少当前交易日")
        previous_date = max(
            (date_key for date_key in common_dates if date_key < normalized_date),
            default=None,
        )
        if previous_date is None:
            raise RuntimeError("沪深指数分时历史缺少前一交易日")

        latest_times: list[str] = []
        for points in points_by_index.values():
            available = [
                minute for date_key, minute, _ in points
                if date_key == normalized_date and minute <= cutoff_time
            ]
            if not available:
                raise RuntimeError("沪深指数分时历史在指定时点没有当前交易日数据")
            latest_times.append(max(available))
        effective_time = min(latest_times)

        current_turnover = 0.0
        previous_turnover = 0.0
        for points in points_by_index.values():
            current_points = [
                amount for date_key, minute, amount in points
                if date_key == normalized_date and minute <= effective_time
            ]
            previous_points = [
                amount for date_key, minute, amount in points
                if date_key == previous_date and minute <= effective_time
            ]
            if not current_points or not previous_points:
                raise RuntimeError("沪深指数分时历史在指定时点没有完整数据")
            current_turnover += sum(current_points)
            previous_turnover += sum(previous_points)

        return {
            "date": trade_date,
            "previousDate": previous_date.replace("-", ""),
            "comparisonTime": effective_time,
            "currentTurnover": current_turnover,
            "previousTurnover": previous_turnover,
        }

    def market_intraday_turnover_pair(self, trade_date: str, cutoff_time: str) -> dict[str, Any]:
        if len(trade_date) != 8 or not trade_date.isdigit():
            raise ValueError("交易日期格式不正确")
        if len(cutoff_time) != 5 or cutoff_time[2] != ":":
            raise ValueError("对比时间格式不正确")

        points_by_index = {
            secid: self.index_intraday_turnover_points(secid)
            for secid in ("1.000001", "0.399106")
        }
        return self.market_intraday_pair_from_points(points_by_index, trade_date, cutoff_time)

    def market_fund_flow_frame(self) -> pd.DataFrame:
        params = {
            "lmt": "0",
            "klt": "101",
            "secid": "1.000001",
            "secid2": "0.399001",
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
        }
        hosts = (
            "push2his.eastmoney.com",
            "91.push2his.eastmoney.com",
            "7.push2his.eastmoney.com",
        )
        last_error: Exception | None = None
        klines: list[str] | None = None
        for trust_env, attempts in ((False, 3), (True, 1)):
            for host in hosts:
                session = self._session(trust_env)
                try:
                    for attempt in range(attempts):
                        try:
                            response = session.get(
                                f"https://{host}/api/qt/stock/fflow/daykline/get",
                                params=params,
                                timeout=(5, 15),
                            )
                            response.raise_for_status()
                            payload = response.json()
                            data = payload.get("data") if isinstance(payload, dict) else None
                            raw = data.get("klines") if isinstance(data, dict) else None
                            if not isinstance(raw, list) or not raw:
                                raise RuntimeError("东方财富大盘资金流没有返回有效数据")
                            klines = [str(item) for item in raw]
                            break
                        except (requests.RequestException, ValueError, RuntimeError) as error:
                            last_error = error
                            if attempt + 1 < attempts:
                                time.sleep(0.3 * (attempt + 1) + random.uniform(0, 0.15))
                    if klines:
                        break
                finally:
                    session.close()
            if klines:
                break
        if not klines:
            raise RuntimeError(f"东方财富大盘资金流连接失败：{last_error or '未知错误'}")

        columns = [
            "日期", "主力净流入-净额", "小单净流入-净额", "中单净流入-净额",
            "大单净流入-净额", "超大单净流入-净额", "主力净流入-净占比",
            "小单净流入-净占比", "中单净流入-净占比", "大单净流入-净占比",
            "超大单净流入-净占比", "上证-收盘价", "上证-涨跌幅", "深证-收盘价",
            "深证-涨跌幅",
        ]
        parsed = [item.split(",") for item in klines]
        if any(len(item) != len(columns) for item in parsed):
            raise RuntimeError("东方财富大盘资金流字段结构已变化")
        return pd.DataFrame(parsed, columns=columns)

    def concept_constituent_frame(self, code: str) -> pd.DataFrame:
        params = {
            **COMMON_PARAMS,
            "fs": f"b:{code} f:!50",
            "fields": ",".join(CONSTITUENT_FIELD_MAP.values()),
        }
        return self._frame(self.fetch_pages("29", params), CONSTITUENT_FIELD_MAP)
