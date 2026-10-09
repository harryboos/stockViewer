"""Bounded, provider-specific forecast recall from an already dated snapshot."""
from __future__ import annotations

import math

from .concept_identity import concept_provider

SELECTION_METHOD = "snapshot-rank-round-robin-v1"
METRICS = ("pctChg", "breadth", "amount")


def _metric(board: dict, field: str) -> float | None:
    raw = board.get(field)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
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


def select_forecast_boards(boards: list[dict], limit: int = 12) -> list[dict]:
    """Give daily change, breadth and turnover equal turns without extra reads.

    Each turn takes the next not-yet-selected code in that ranking. At the
    default budget all three routes can contribute four distinct candidates;
    exhausted routes yield their turns to the others. Missing metrics never
    become zero, and negative daily returns remain eligible.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("预测候选数量必须是非负整数")
    available = _available_boards(boards)
    rankings = {
        field: sorted((board for board in available if _metric(board, field) is not None),
                      key=lambda board: (-_metric(board, field), board["code"]))
        for field in METRICS
    }
    ranks = {field: {board["code"]: rank for rank, board in enumerate(rows, 1)}
             for field, rows in rankings.items()}
    cursors = dict.fromkeys(METRICS, 0)
    selected, seen = [], set()
    while len(selected) < limit:
        added = False
        for field in METRICS:
            rows = rankings[field]
            while cursors[field] < len(rows) and rows[cursors[field]]["code"] in seen:
                cursors[field] += 1
            if cursors[field] == len(rows):
                continue
            board = rows[cursors[field]]
            cursors[field] += 1
            code = board["code"]
            selected.append({**board, "candidateSignals": [
                {"metric": metric, "rank": ranks[metric][code], "value": _metric(board, metric),
                 "selectedBy": metric == field}
                for metric in METRICS if code in ranks[metric]]})
            seen.add(code)
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
    return selected


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
