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
    "总市值": "f20", "换手率": "f8", "上涨家数": "f104", "下跌家数": "f105",
    "领涨股票": "f128", "领涨股票-涨跌幅": "f136",
}

CONSTITUENT_FIELD_MAP = {
    "代码": "f12", "名称": "f14", "最新价": "f2", "涨跌幅": "f3", "涨跌额": "f4",
    "成交量": "f5", "成交额": "f6", "振幅": "f7", "换手率": "f8", "市盈率-动态": "f9",
    "最高": "f15", "最低": "f16", "今开": "f17", "昨收": "f18", "市净率": "f23",
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

    def concept_constituent_frame(self, code: str) -> pd.DataFrame:
        params = {
            **COMMON_PARAMS,
            "fs": f"b:{code} f:!50",
            "fields": ",".join(CONSTITUENT_FIELD_MAP.values()),
        }
        return self._frame(self.fetch_pages("29", params), CONSTITUENT_FIELD_MAP)
