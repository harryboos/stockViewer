"""Dated THS samples, isolated from existing Eastmoney snapshots and universes."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime

from . import database
from .config import MARKET
from .source_errors import source_failure_reason
from .storage_policy import SECTOR_OVERVIEW_CACHE_VERSION

_BOARD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ths-overview")
_LEADER_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ths-leaders")


def sector_overview(service, force: bool = False) -> dict:
    from .data_sources import SECTOR_DISPLAY_LIMIT, SECTOR_TURNOVER_LIMIT, number_or_none
    from .research_store import cache_put, save_universe
    from .ths import ths_client

    requested_at = datetime.now(database.CHINA_TZ)
    with service._sector_lock:
        if (service._sector_cache and service._sector_fetched_at
                and (not force or service._sector_fetched_at >= requested_at)
                and (requested_at - service._sector_fetched_at).total_seconds() < MARKET.spot_cache_seconds):
            return service._sector_cache
        cache_key = f"sector_overview:ths:v{SECTOR_OVERVIEW_CACHE_VERSION}"
        cached = service._cached_json(cache_key)
        frames, warnings, errors, coverage = {}, [], [], {}
        futures = {kind: _BOARD_POOL.submit(ths_client.board_frame, kind, force=force)
                   for kind in ("concept", "industry")}
        done, pending = wait(futures.values(), timeout=26)
        for task in pending:
            task.cancel()
        for kind, label in (("concept", "概念"), ("industry", "行业")):
            try:
                if futures[kind] not in done:
                    raise TimeoutError("同花顺板块请求超时")
                frame = futures[kind].result()
                quoted = []
                for index, value in frame.get("行情时间", {}).items():
                    stamp = number_or_none(value)
                    if stamp is None:
                        continue
                    try:
                        instant = datetime.fromtimestamp(stamp, database.CHINA_TZ)
                    except (ValueError, OverflowError, OSError):
                        continue
                    if 2000 <= instant.year and instant <= datetime.now(database.CHINA_TZ):
                        quoted.append((index, instant))
                if not quoted:
                    raise RuntimeError("返回无有效报价日期的数据")
                day = max(instant.date() for _, instant in quoted)
                indices = [index for index, instant in quoted if instant.date() == day]
                frames[kind] = (frame.loc[indices], min(instant for _, instant in quoted if instant.date() == day))
                coverage[kind] = frame.attrs.get("coverage", {})
                warnings.extend(frame.attrs.get("warnings", []))
                if len(indices) < len(frame):
                    warnings.append(f"部分同花顺{label}报价日期落后，已排除")
            except Exception as error:
                errors.append(f"{label}：{source_failure_reason(error)}")
        target = frames.get("concept") or frames.get("industry")
        rows = {}
        for kind in ("concept", "industry"):
            value = frames.get(kind)
            rows[kind] = service._board_rows(kind, value[0], None, []) if value and target and value[1].date() == target[1].date() else []
        if not rows["concept"]:
            message = "同花顺概念样本暂未取得有效行情" + ("；" + "；".join(errors) if errors else "")
            if isinstance(cached, dict) and cached.get("conceptBoards"):
                return {**cached, "usingCachedSnapshot": True, "refreshStatus": "failed",
                        "refreshError": message + "；已保留同花顺最近成功快照", "refreshAttemptedAt": database.now_iso()}
            raise RuntimeError(message)
        quoted_at = frames["concept"][1]
        # Only a small set is enriched here; research reuses the client's quote
        # cache. Undated directory leader names are never used as today's data.
        leader_tasks = {_LEADER_POOL.submit(ths_client.strong_stocks, row["code"], quoted_at.strftime("%Y%m%d")): row
                        for row in rows["concept"][:3]}
        done, pending = wait(leader_tasks, timeout=20)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                stocks = task.result()
                leader_tasks[task]["leaders"] = [
                    {"role": "样本强势股", **{key: stock[key] for key in ("code", "name", "price", "pctChg", "amount")}}
                    for stock in stocks[:3] if stock.get("tradeDate") == quoted_at.strftime("%Y%m%d")]
            except (RuntimeError, ValueError, TimeoutError, KeyError):
                pass
        boards = [*rows["industry"], *rows["concept"]]
        turnover = sorted((row for row in boards if row.get("amount") is not None),
                          key=lambda row: row["amount"], reverse=True)[:SECTOR_TURNOVER_LIMIT]
        warnings.extend(errors)
        warnings.append("同花顺公开榜单活跃样本，非全市场；未取得的资金流和昨日成交额显示为空")
        scope = f"同花顺公开榜单活跃概念样本（本次核验 {len(rows['concept'])} 个，非全市场）"
        now = database.now_iso()
        result = {
            "tradeDate": quoted_at.strftime("%Y%m%d"), "quoteAsOf": quoted_at.isoformat(timespec="seconds"),
            "updatedAt": now, "refreshAttemptedAt": now,
            "refreshStatus": "succeeded" if rows["industry"] else "partial",
            "refreshError": None if rows["industry"] else "同花顺行业样本暂不可用；概念行情已更新",
            "source": "同花顺公开板块行情（活跃样本）", "sourceProvider": "ths", "scope": scope,
            "coverage": coverage,
            "summary": {"industryCount": len(rows["industry"]), "conceptCount": len(rows["concept"]),
                        "risingIndustryCount": sum(row["pctChg"] > 0 for row in rows["industry"]),
                        "risingConceptCount": sum(row["pctChg"] > 0 for row in rows["concept"]),
                        "topBoard": max(boards, key=lambda row: row["pctChg"], default=None), "topFundBoard": None},
            "industryBoards": rows["industry"][:SECTOR_DISPLAY_LIMIT],
            "conceptBoards": rows["concept"][:SECTOR_DISPLAY_LIMIT],
            "researchConcepts": list({row["code"]: row for row in [*rows["concept"][:9],
                *sorted(rows["concept"], key=lambda row: row.get("amount") or 0, reverse=True)[:3]]}.values()),
            "conceptUniverse": [{"code": row["code"], "name": row["name"]} for row in rows["concept"]],
            "turnoverBoards": turnover, "warnings": list(dict.fromkeys(warnings)),
        }
        save_universe(result["conceptUniverse"], now, scope=scope)
        cache_put("latest-sectors", result)
        cache_put("rotation-snapshot", {"asOf": now, "sourceProvider": "ths", "scope": scope,
                  "boards": [{key: row[key] for key in ("code", "name", "pctChg", "breadth", "amount")} for row in rows["concept"]]})
        database.set_meta(cache_key, json.dumps(result, ensure_ascii=False))
        service._sector_cache = result
        service._sector_fetched_at = datetime.now(database.CHINA_TZ)
        return result
