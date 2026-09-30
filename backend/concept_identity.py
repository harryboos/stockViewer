"""Concept codes identify an index provider, not interchangeable topic names."""
from __future__ import annotations

import re

CONCEPT_CODE_PATTERN = r"^(?:BK\d+|THS:88\d{4})$"
CONCEPT_PROVIDERS = {"eastmoney": "东方财富", "ths": "同花顺"}


def concept_provider(code: str) -> str | None:
    if isinstance(code, str) and re.fullmatch(r"BK\d+", code):
        return "eastmoney"
    if isinstance(code, str) and re.fullmatch(r"THS:88\d{4}", code):
        return "ths"
    return None


def single_provider(boards: list[dict]) -> str | None:
    providers = {concept_provider(board.get("code")) for board in boards}
    return next(iter(providers)) if len(providers) == 1 and None not in providers else None
