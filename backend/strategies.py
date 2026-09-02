from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import database
from .config import STRATEGY
from .data_sources import market_data
from .strategy_factors import (
    build_factor_row,
    percentile,
    show_factor as show,
    top_picks,
    volume_breakout_picks,
)


STRATEGY_VERSION = "5"
CORE_CANDIDATES = [
    "600519.SH", "300750.SZ", "601318.SH", "000858.SZ", "600036.SH", "688981.SH",
    "600900.SH", "601088.SH", "000333.SZ", "600276.SH", "601899.SH", "300308.SZ",
]


def _candidate_rows(force: bool = False) -> list[dict[str, Any]]:
    try:
        snapshot = market_data.market_snapshot(force=force)
    except Exception:
        codes = list(dict.fromkeys(database.list_watch_codes() + CORE_CANDIDATES))
        market_data.refresh_quotes(codes, False)
        basics = {row["tsCode"]: row for row in database.get_stock_basics(codes)}
        quotes = database.get_quotes(codes)
        snapshot = [{**basics[code], **quotes[code]} for code in codes if code in basics and code in quotes]
    eligible = [
        row for row in snapshot
        if row.get("amount") and row.get("totalMv") and "ST" not in row["name"].upper() and "退" not in row["name"]
    ]
    liquid = sorted(eligible, key=lambda row: float(row.get("amount") or 0), reverse=True)[:STRATEGY.sample_size]
    by_code = {row["tsCode"]: row for row in snapshot}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for code in database.list_watch_codes() + CORE_CANDIDATES:
        row = by_code.get(code)
        if row and code not in seen:
            selected.append(row)
            seen.add(code)
    for row in liquid:
        if row["tsCode"] not in seen:
            selected.append(row)
            seen.add(row["tsCode"])
    return selected[: STRATEGY.sample_size + 6]


def candidate_snapshot(force: bool = False) -> list[dict[str, Any]]:
    rows = _candidate_rows(force=force)
    database.upsert_stock_basics(rows)
    database.upsert_quotes(rows)
    return [
        {
            "tsCode": row["tsCode"],
            "symbol": row["symbol"],
            "name": row["name"],
            "industry": row.get("industry"),
            "market": row.get("market"),
            "quote": {
                key: row.get(key)
                for key in ("tradeDate", "open", "high", "low", "close", "preClose", "change", "pctChg", "vol", "amount", "volumeRatio", "source", "fetchedAt")
            },
        }
        for row in rows
    ]


def _hot_concept_strategy(snapshot: dict[str, Any], run_date: str, trade_date: str) -> dict[str, Any]:
    concepts = snapshot.get("concepts") or []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for concept in concepts:
        for stock in concept.get("stocks") or []:
            marker = (stock["symbol"], concept["name"])
            if marker in seen:
                continue
            seen.add(marker)
            rows.append(
                {
                    **stock,
                    "conceptName": concept["name"],
                    "conceptPctChg": concept["pctChg"],
                    "conceptBreadth": concept["breadth"],
                    "industry": concept["name"],
                }
            )

    p_concept = percentile(rows, "conceptPctChg")
    p_breadth = percentile(rows, "conceptBreadth")
    p_stock = percentile(rows, "pctChg")
    p_amount = percentile(rows, "amount")
    ranked = sorted(
        rows,
        key=lambda row: (
            p_concept(row) * 0.38
            + p_breadth(row) * 0.22
            + p_stock(row) * 0.20
            + p_amount(row) * 0.20
        ),
        reverse=True,
    )
    picks: list[dict[str, Any]] = []
    picked_symbols: set[str] = set()
    for row in ranked:
        if row["symbol"] in picked_symbols:
            continue
        score = round(
            p_concept(row) * 0.38
            + p_breadth(row) * 0.22
            + p_stock(row) * 0.20
            + p_amount(row) * 0.20
        )
        picks.append(
            {
                "code": row["symbol"],
                "name": row["name"],
                "industry": row["industry"],
                "score": score,
                "reason": (
                    f"{row['conceptName']} +{float(row['conceptPctChg']):.1f}% · "
                    f"板块上涨占比 {float(row['conceptBreadth']) * 100:.0f}% · "
                    f"个股 {float(row['pctChg']):+.1f}% · 成交额 {float(row['amount']) / 100_000_000:.1f}亿"
                ),
            }
        )
        picked_symbols.add(row["symbol"])
        if len(picks) == 4:
            break

    description = (
        f"从当日涨幅靠前且上涨家数多于下跌家数的 {len(concepts)} 个概念中，"
        "综合板块强度、上涨广度、个股涨幅与成交额排序；剔除 ST、退市整理、涨幅≥11% 和成交额不足 1 亿元的股票。"
        if concepts
        else "今日概念实时源暂不可用，保留空结果；不会把旧日期概念或模拟数据冒充今日热点。"
    )
    return {
        "id": "hot_concept",
        "name": "热门概念共振",
        "runDate": run_date,
        "tradeDate": snapshot.get("tradeDate") or trade_date,
        "description": description,
        "picks": picks,
    }


def calculate_public_strategies(force: bool = False) -> dict[str, Any]:
    run_date = database.china_date()
    cache_key = f"{run_date}:v{STRATEGY_VERSION}"
    cached = database.get_strategy_run(cache_key)
    if cached and not force:
        return {"status": "succeeded", "strategies": cached}

    candidates = _candidate_rows(force=force)
    database.upsert_stock_basics(candidates)
    database.upsert_quotes(candidates)
    histories = market_data.history_batch([row["symbol"] for row in candidates])
    factors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="stock-factor") as executor:
        tasks = [
            executor.submit(build_factor_row, row, histories.get(row["symbol"], []))
            for row in candidates
        ]
        for task in as_completed(tasks):
            try:
                row = task.result()
                if row:
                    factors.append(row)
            except Exception:
                continue
    if len(factors) < 4:
        raise RuntimeError("免费数据源返回的有效历史样本不足，请稍后重试")

    p_dividend = percentile(factors, "dividendYield")
    p_pe = percentile(factors, "peTtm", higher_is_better=False)
    p_pb = percentile(factors, "pb", higher_is_better=False)
    p_m6 = percentile(factors, "momentum6m")
    p_m12 = percentile(factors, "momentum12m")
    p_m5 = percentile(factors, "change5d")
    p_m20 = percentile(factors, "change20d")
    p_m60 = percentile(factors, "change60d")
    p_ma_gap = percentile(factors, "ma20Gap")
    p_ma_spread = percentile(factors, "maSpread10_30")
    p_volume_trend = percentile(factors, "volumeTrend5To20")
    p_prior_rsi = percentile(factors, "rsi14ThreeDaysAgo", higher_is_better=False)
    p_rsi_recovery = percentile(factors, "rsiRecovery")
    p_volatility = percentile(factors, "volatility", higher_is_better=False)
    p_size = percentile(factors, "totalMv", higher_is_better=False)
    p_liquidity = percentile(factors, "averageAmount")
    trade_date = max((row["tradeDate"] for row in candidates if row.get("tradeDate")), default=run_date.replace("-", ""))
    has_dividend_data = any(row.get("dividendYield") is not None for row in factors)
    qlib_factors = [
        row for row in factors
        if all(row.get(key) is not None for key in ("change5d", "change20d", "change60d", "ma20Gap", "volumeTrend5To20"))
        and (row.get("todayPctChg") is None or float(row["todayPctChg"]) < 11)
    ]
    sma_factors = [
        row for row in factors
        if all(row.get(key) is not None for key in ("ma10", "ma30", "change20d", "maSpread10_30"))
        and float(row["latestClose"]) > float(row["ma10"]) > float(row["ma30"])
        and float(row["change20d"]) > 0
        and (row.get("todayPctChg") is None or float(row["todayPctChg"]) < 11)
    ]
    rsi_factors = [
        row for row in factors
        if all(row.get(key) is not None for key in ("rsi14", "rsi14ThreeDaysAgo", "rsiRecovery", "change5d", "ma5"))
        and float(row["rsi14ThreeDaysAgo"]) < 45
        and 0 < float(row["rsiRecovery"])
        and float(row["rsi14"]) < 60
        and float(row["change5d"]) > 0
        and float(row["latestClose"]) > float(row["ma5"])
    ]
    trend_factors = [
        row for row in factors
        if row.get("ma20") is not None
        and row.get("ma60") is not None
        and row.get("change20d") is not None
        and row.get("change60d") is not None
        and float(row["latestClose"]) > float(row["ma20"]) > float(row["ma60"])
        and float(row["change20d"]) > 0
        and float(row["change60d"]) > 0
        and (row.get("todayPctChg") is None or float(row["todayPctChg"]) < 11)
    ]
    value_momentum_factors = [
        row for row in factors
        if row.get("peTtm") is not None
        and row.get("pb") is not None
        and row.get("change60d") is not None
        and float(row["peTtm"]) > 0
        and float(row["pb"]) > 0
        and float(row["change60d"]) > 0
    ]
    concept_snapshot = market_data.hot_concept_snapshot(force=force)

    strategies = [
        {
            "id": "dividend",
            "name": "红利低波" if has_dividend_data else "价值低波",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": (
                "借鉴公开红利指数思路，综合股息率、估值与近 120 日波动排序。"
                if has_dividend_data
                else "当前降级源缺少稳定股息率，暂以正市盈率、市净率和近 120 日波动排序。"
            ),
            "picks": top_picks(
                factors,
                (
                    (lambda row: p_dividend(row) * 0.42 + p_pe(row) * 0.18 + p_pb(row) * 0.12 + p_volatility(row) * 0.28)
                    if has_dividend_data
                    else (lambda row: p_pe(row) * 0.34 + p_pb(row) * 0.24 + p_volatility(row) * 0.42)
                ),
                (
                    (lambda row: f"股息率 {show(row.get('dividendYield'))}，120日波动 {show(row.get('volatility'))}")
                    if has_dividend_data
                    else (lambda row: f"市盈率 {show(row.get('peTtm'), '')}，市净率 {show(row.get('pb'), '')}，波动 {show(row.get('volatility'))}")
                ),
            ),
        },
        {
            "id": "momentum",
            "name": "价格动量",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "借鉴公开动量指数思路，结合近半年、近一年价格动量和成交流动性。",
            "picks": top_picks(
                factors,
                lambda row: p_m6(row) * 0.45 + p_m12(row) * 0.35 + p_liquidity(row) * 0.20,
                lambda row: f"近半年 {show(row.get('momentum6m'))}，近一年 {show(row.get('momentum12m'))}",
            ),
        },
        {
            "id": "lowvol",
            "name": "小盘低波",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "借鉴公开低波指数思路，在流动性约束下偏向较低波动和较小市值。",
            "picks": top_picks(
                factors,
                lambda row: p_volatility(row) * 0.58 + p_size(row) * 0.27 + p_liquidity(row) * 0.15,
                lambda row: f"120日波动 {show(row.get('volatility'))}，流动性已纳入约束",
            ),
        },
        {
            "id": "qlib_alpha158",
            "name": "Qlib 精简多因子",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "适配微软 Qlib Alpha158 的 ROC、MA、STD 与成交量滚动因子族，综合 5/20/60 日收益、均线偏离、量能趋势、低波动与流动性；不是直接运行 Qlib 训练模型。",
            "picks": top_picks(
                qlib_factors,
                lambda row: (
                    p_m5(row) * 0.14 + p_m20(row) * 0.22 + p_m60(row) * 0.16
                    + p_ma_gap(row) * 0.14 + p_volume_trend(row) * 0.12
                    + p_volatility(row) * 0.12 + p_liquidity(row) * 0.10
                ),
                lambda row: (
                    f"5/20/60日 {show(row.get('change5d'))} / {show(row.get('change20d'))} / {show(row.get('change60d'))}，"
                    f"MA20偏离 {show(row.get('ma20Gap'))}，5/20日量能 {show(row.get('volumeTrend5To20'), '×')}"
                ),
            ),
        },
        {
            "id": "sma_cross",
            "name": "双均线趋势",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "适配 backtesting.py 的 SmaCross 示例，筛选现价＞MA10＞MA30 且 20 日收益为正的股票，再按均线强度、动量、低波动与流动性排序。",
            "picks": top_picks(
                sma_factors,
                lambda row: p_ma_spread(row) * 0.35 + p_m20(row) * 0.25 + p_m60(row) * 0.15 + p_volatility(row) * 0.10 + p_liquidity(row) * 0.15,
                lambda row: (
                    f"现价 {show(row.get('latestClose'), '')} > MA10 {show(row.get('ma10'), '')} > MA30 {show(row.get('ma30'), '')}，"
                    f"20日 {show(row.get('change20d'))}，"
                    + (f"{int(row['recentBullishCrossDays'])}日内金叉" if row.get("recentBullishCrossDays") is not None else "均线多头保持")
                ),
            ),
        },
        {
            "id": "rsi_rebound",
            "name": "RSI 超跌回升",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "适配 QuantConnect LEAN 的 14 日 Wilder RSI：三日前 RSI＜45、当前 RSI 回升但仍＜60，同时要求 5 日收益为正且现价高于 MA5；严格条件不补位。",
            "picks": top_picks(
                rsi_factors,
                lambda row: p_prior_rsi(row) * 0.34 + p_rsi_recovery(row) * 0.28 + p_m5(row) * 0.20 + p_liquidity(row) * 0.18,
                lambda row: (
                    f"RSI14 {show(row.get('rsi14ThreeDaysAgo'), '')} → {show(row.get('rsi14'), '')}，"
                    f"5日 {show(row.get('change5d'))}，现价高于 MA5"
                ),
            ),
        },
        {
            "id": "trend_confirmation",
            "name": "趋势确认",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "筛选收盘价高于 20 日均线、20 日均线高于 60 日均线且 20/60 日涨幅均为正的股票，再按趋势强度、低波动和流动性排序。",
            "picks": top_picks(
                trend_factors,
                lambda row: p_m20(row) * 0.34 + p_m60(row) * 0.31 + p_volatility(row) * 0.17 + p_liquidity(row) * 0.18,
                lambda row: (
                    f"20日 {show(row.get('change20d'))}，60日 {show(row.get('change60d'))}，"
                    f"现价 {show(row.get('latestClose'), '')} > MA20 {show(row.get('ma20'), '')} > MA60 {show(row.get('ma60'), '')}"
                ),
            ),
        },
        {
            "id": "value_momentum",
            "name": "价值动量",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "借鉴公开多因子方法，在正市盈率、正市净率与 60 日动量为正的样本中，综合估值、价格动量和流动性排序。",
            "picks": top_picks(
                value_momentum_factors,
                lambda row: p_pe(row) * 0.24 + p_pb(row) * 0.20 + p_m60(row) * 0.25 + p_m6(row) * 0.21 + p_liquidity(row) * 0.10,
                lambda row: (
                    f"PE {show(row.get('peTtm'), '')}，PB {show(row.get('pb'), '')}，"
                    f"60日 {show(row.get('change60d'))}，半年 {show(row.get('momentum6m'))}"
                ),
            ),
        },
        {
            "id": "volume_breakout",
            "name": "强势缩量筛选",
            "runDate": run_date,
            "tradeDate": trade_date,
            "description": "在当日高流动性候选池中严格筛选：昨额＞10亿、10日涨幅＞30%、20日均额＞12亿、量比＜1.5、今日涨幅＜11%。",
            "picks": volume_breakout_picks(factors),
        },
        _hot_concept_strategy(concept_snapshot, run_date, trade_date),
    ]
    database.save_strategy_run(cache_key, trade_date, strategies, "AKShare + BaoStock + 东方财富概念板块")
    return {"status": "succeeded", "strategies": strategies}
