from __future__ import annotations

import math
import random
import time
from typing import Any

import requests


class TencentClient:
    """Tencent quote fallbacks used when Eastmoney's history routes are unavailable."""

    RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
    INDEX_DAY_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"

    @staticmethod
    def _session(trust_env: bool) -> requests.Session:
        session = requests.Session()
        session.trust_env = trust_env
        session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "Referer": "https://gu.qq.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            }
        )
        return session

    def _request_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        post_json: bool = False,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        # Server-side proxy variables are a frequent source of broken finance
        # requests. Prefer a direct connection, and only try the environment as
        # the final compatibility route.
        for trust_env, attempts in ((False, 3), (True, 1)):
            session = self._session(trust_env)
            try:
                for attempt in range(attempts):
                    try:
                        response = (
                            session.post(url, json=params, timeout=(5, 15))
                            if post_json
                            else session.get(url, params=params, timeout=(5, 15))
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise RuntimeError("腾讯行情返回了无效数据")
                        if payload.get("code") not in (None, 0):
                            raise RuntimeError(str(payload.get("msg") or "腾讯行情返回错误"))
                        return payload
                    except (requests.RequestException, ValueError, RuntimeError) as error:
                        last_error = error
                        if attempt + 1 < attempts:
                            time.sleep(0.3 * (attempt + 1) + random.uniform(0, 0.15))
            finally:
                session.close()
        raise RuntimeError(f"腾讯行情连接失败：{last_error or '未知错误'}")

    @staticmethod
    def _find_dict_rows(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
        if isinstance(value, dict):
            for key in ("rank_list", "list", "rank", "items", "data"):
                rows = TencentClient._find_dict_rows(value.get(key)) if key in value else []
                if rows:
                    return rows
            for child in value.values():
                rows = TencentClient._find_dict_rows(child)
                if rows:
                    return rows
        return []

    @staticmethod
    def _find_total(value: Any) -> int | None:
        if isinstance(value, dict):
            for key in ("total", "total_num", "totalCount", "count"):
                raw = value.get(key)
                try:
                    total = int(raw)
                except (TypeError, ValueError):
                    continue
                if total > 0:
                    return total
            for child in value.values():
                total = TencentClient._find_total(child)
                if total is not None:
                    return total
        return None

    def fetch_rank_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_size = 200
        total: int | None = None
        for offset in range(0, 10_000, page_size):
            payload = self._request_json(
                self.RANK_URL,
                {
                    "boardCode": "aStock",
                    "sortType": "turnover",
                    "direct": "down",
                    "offset": offset,
                    "count": page_size,
                },
                post_json=True,
            )
            page_rows = self._find_dict_rows(payload)
            if not page_rows:
                if offset == 0:
                    raise RuntimeError("腾讯 A 股排行没有返回有效数据")
                break
            rows.extend(page_rows)
            total = total or self._find_total(payload)
            if len(page_rows) < page_size or (total is not None and len(rows) >= total):
                break
            time.sleep(0.06)
        if not rows:
            raise RuntimeError("腾讯 A 股排行没有返回有效数据")
        return rows[:total] if total is not None else rows

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def market_fund_flow_snapshot(self, trade_date: str) -> dict[str, Any]:
        rows = self.fetch_rank_rows()
        turnover = 0.0
        main_net_total = 0.0
        valid = 0
        for row in rows:
            amount = self._number(row.get("turnover"))
            net = self._number(row.get("zljlr"))
            if net is None:
                continue
            valid += 1
            main_net_total += net
            turnover += max(amount or 0.0, 0.0)
        if not valid:
            raise RuntimeError("腾讯逐股资金流没有返回有效数据")
        # The rank endpoint reports monetary fields in 万元.
        scale = 10_000.0
        main_net = main_net_total * scale
        return {
            "date": trade_date,
            "shClose": None,
            "shPctChg": None,
            "szClose": None,
            "szPctChg": None,
            "mainNetInflow": main_net,
            "mainNetInflowRatio": main_net / (turnover * scale) * 100 if turnover else None,
            "superLargeNetInflow": None,
            "largeNetInflow": None,
            "source": "腾讯证券逐股资金汇总",
        }

    @staticmethod
    def _day_series(payload: dict[str, Any], code: str) -> list[dict[str, Any]]:
        data = payload.get("data")
        stock_data = data.get(code) if isinstance(data, dict) else None
        days = (
            stock_data.get("day") or stock_data.get("data")
            if isinstance(stock_data, dict)
            else None
        )
        if not isinstance(days, list):
            return []
        return [item for item in days if isinstance(item, dict)]

    @staticmethod
    def _cumulative_points(day: dict[str, Any]) -> list[tuple[str, float]]:
        result: list[tuple[str, float]] = []
        rows = day.get("data")
        if not isinstance(rows, list):
            return result
        for raw in rows:
            values = str(raw).split()
            if len(values) < 4 or len(values[0]) != 4:
                continue
            amount = TencentClient._number(values[3])
            if amount is not None and amount >= 0:
                result.append((f"{values[0][:2]}:{values[0][2:]}", amount))
        return result

    def _index_days(self, code: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            self.INDEX_DAY_URL,
            {"code": code, "qfq": "", "start": "", "num": "-5"},
        )
        days = self._day_series(payload, code)
        if not days:
            raise RuntimeError(f"腾讯指数 {code} 没有返回分时历史")
        return days

    def index_intraday_turnover_pair(self, trade_date: str, cutoff_time: str) -> dict[str, Any]:
        if len(trade_date) != 8 or not trade_date.isdigit():
            raise ValueError("交易日期格式不正确")
        if len(cutoff_time) != 5 or cutoff_time[2] != ":":
            raise ValueError("对比时间格式不正确")

        days_by_index = {code: self._index_days(code) for code in ("sh000001", "sz399106")}
        by_index: dict[str, dict[str, list[tuple[str, float]]]] = {}
        for code, days in days_by_index.items():
            by_index[code] = {
                str(day.get("date") or "").replace("-", "")[:8]: self._cumulative_points(day)
                for day in days
            }
        common_dates = set.intersection(*(set(days) for days in by_index.values()))
        if trade_date not in common_dates:
            raise RuntimeError("腾讯沪深指数分时历史缺少当前交易日")
        previous_date = max((item for item in common_dates if item < trade_date), default=None)
        if previous_date is None:
            raise RuntimeError("腾讯沪深指数分时历史缺少前一交易日")

        latest_times: list[str] = []
        for days in by_index.values():
            available = [minute for minute, _ in days[trade_date] if minute <= cutoff_time]
            if not available:
                raise RuntimeError("腾讯沪深指数在指定时点没有当前交易日数据")
            latest_times.append(max(available))
        effective_time = min(latest_times)

        current_turnover = 0.0
        previous_turnover = 0.0
        for days in by_index.values():
            current_values = [amount for minute, amount in days[trade_date] if minute <= effective_time]
            previous_values = [amount for minute, amount in days[previous_date] if minute <= effective_time]
            if not current_values or not previous_values:
                raise RuntimeError("腾讯沪深指数在指定时点没有完整数据")
            # Tencent's fourth field is cumulative amount, not a per-minute delta.
            current_turnover += current_values[-1]
            previous_turnover += previous_values[-1]

        return {
            "date": trade_date,
            "previousDate": previous_date,
            "comparisonTime": effective_time,
            "currentTurnover": current_turnover,
            "previousTurnover": previous_turnover,
        }
