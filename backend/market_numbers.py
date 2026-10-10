"""Shared scalar parsing for quote data, without importing provider clients."""
from __future__ import annotations

import math


def number_or_none(value: object) -> float | None:
    """Keep missing, boolean, non-finite and unrepresentable quotes unknown."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
