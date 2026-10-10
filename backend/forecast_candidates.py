"""Bounded, provider-specific forecast recall from an already dated snapshot."""
from __future__ import annotations

from collections.abc import Callable

from .concept_identity import concept_provider
from .market_numbers import number_or_none

SELECTION_METHOD = "snapshot-rank-round-robin-v1"
METRICS = ("pctChg", "breadth", "amount")


def _metric(board: dict, field: str) -> float | None:
    value = number_or_none(board.get(field))
    if value is None:
        return None
    if field == "breadth" and not 0 <= value <= 100:
        return None
    if field == "amount" and value < 0:
        return None
    return value


def _available_boards(boards: list[dict]) -> list[dict]:
    unique, providers = {}, set()
    for board in boards:
        if not isinstance(board, dict) or board.get("kind", "concept") != "concept":
            continue
        code, name = board.get("code"), board.get("name")
        provider = concept_provider(code)
        if not provider or not isinstance(name, str) or not name.strip():
            continue
        providers.add(provider)
        if code not in unique and any(_metric(board, field) is not None for field in METRICS):
            unique[code] = board
    if len(providers) > 1:
        raise ValueError("预测候选必须来自同一行情来源，不能混用概念指数")
    return list(unique.values())


def _ranked_round_robin(boards: list[dict], channels: tuple[str, ...],
                        value_for: Callable[[dict, str], float | None], limit: int) -> tuple[list[dict], dict]:
    """Share deterministic ranking; compute each board/channel metric only once."""
    values = {row["code"]: {channel: value_for(row, channel) for channel in channels} for row in boards}
    rankings = {
        channel: sorted((row for row in boards if values[row["code"]][channel] is not None),
                        key=lambda row: (-values[row["code"]][channel], row["code"]))
        for channel in channels
    }
    ranks = {channel: {row["code"]: rank for rank, row in enumerate(rows, 1)}
             for channel, rows in rankings.items()}
    cursors, counts, seen, selected = dict.fromkeys(channels, 0), dict.fromkeys(channels, 0), set(), []
    while len(selected) < limit:
        before = len(selected)
        for channel in channels:
            rows = rankings[channel]
            while cursors[channel] < len(rows) and rows[cursors[channel]]["code"] in seen:
                cursors[channel] += 1
            if cursors[channel] == len(rows):
                continue
            row = rows[cursors[channel]]
            code = row["code"]
            cursors[channel] += 1
            counts[channel] += 1
            selected.append({**row, "candidateSignals": [
                {"metric": metric, "rank": ranks[metric][code], "value": values[code][metric],
                 "selectedBy": metric == channel}
                for metric in channels if code in ranks[metric]]})
            seen.add(code)
            if len(selected) >= limit:
                break
        if len(selected) == before:
            break
    return selected, {channel: {"availableCount": len(rankings[channel]), "selectedCount": counts[channel]}
                      for channel in channels}


def select_forecast_boards(boards: list[dict], limit: int = 12) -> list[dict]:
    """Give daily change, breadth and turnover equal turns without extra reads.

    Each turn takes the next not-yet-selected code in that ranking. At the
    default budget all three routes can contribute four distinct candidates;
    exhausted routes yield their turns to the others. Missing metrics never
    become zero, and negative daily returns remain eligible.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("预测候选数量必须是非负整数")
    return _ranked_round_robin(_available_boards(boards), METRICS, _metric, limit)[0]


def forecast_candidate_metadata(boards: list[dict], selected: list[dict], limit: int = 12) -> dict:
    return {"method": SELECTION_METHOD, "availableCount": len(_available_boards(boards)),
            "selectedCount": len(selected), "limit": limit}


def daily_change_baseline(boards: list[dict], limit: int = 3) -> list[str]:
    """Freeze a simple comparison rule using only this snapshot's daily return.

    This is a research baseline, not a claim that the selected concepts will
    subsequently outperform. Missing daily returns never become zero.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("基线概念数量必须是非负整数")
    available = _available_boards(boards)
    ranked = sorted((board for board in available if _metric(board, "pctChg") is not None),
                    key=lambda board: (-_metric(board, "pctChg"), board["code"]))
    return [board["code"] for board in ranked[:limit]]
