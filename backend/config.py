from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class MarketSettings:
    spot_cache_seconds: int
    primary_failure_backoff_seconds: int
    eastmoney_delay_enabled: bool
    eastmoney_page_delay_seconds: float
    eastmoney_delay_hosts: tuple[str, ...]
    hot_concept_limit: int


@dataclass(frozen=True)
class StrategySettings:
    sample_size: int


@dataclass(frozen=True)
class SchedulerSettings:
    enabled: bool
    hour: int
    minute: int


MARKET = MarketSettings(
    spot_cache_seconds=_int_env("SPOT_CACHE_SECONDS", 900, 60, 86_400),
    primary_failure_backoff_seconds=_int_env("PRIMARY_FAILURE_BACKOFF_SECONDS", 1_800, 60, 86_400),
    eastmoney_delay_enabled=_bool_env("EASTMONEY_DELAY_ENABLED", True),
    eastmoney_page_delay_seconds=_float_env("EASTMONEY_PAGE_DELAY_SECONDS", 0.25, 0.0, 2.0),
    eastmoney_delay_hosts=tuple(
        host.strip()
        for host in os.getenv("EASTMONEY_DELAY_HOSTS", "").split(",")
        if host.strip()
    ),
    hot_concept_limit=_int_env("HOT_CONCEPT_LIMIT", 5, 3, 8),
)

STRATEGY = StrategySettings(
    sample_size=_int_env("STRATEGY_SAMPLE_SIZE", 18, 12, 40),
)

SCHEDULER = SchedulerSettings(
    enabled=_bool_env("ENABLE_DAILY_SCHEDULER", True),
    hour=_int_env("DAILY_RUN_HOUR", 18, 0, 23),
    minute=_int_env("DAILY_RUN_MINUTE", 10, 0, 59),
)

DATABASE_PATH = Path(os.getenv("STOCK_VIEWER_DB", PROJECT_ROOT / "data" / "stockviewer.sqlite3"))
