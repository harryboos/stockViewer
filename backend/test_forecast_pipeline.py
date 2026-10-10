from __future__ import annotations

import copy
import json
import math
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

from backend import concept_forecast as forecast
from backend import forecast_pipeline as pipeline
from backend.forecast_candidates import select_forecast_boards
from backend.forecast_evaluation import _plan
from backend.forecast_data import forecast_window
from backend.research_pool import BatchResult


DECISION = "2026-09-13T10:00:00+08:00"
QUOTE = "2026-09-11T15:10:00+08:00"
TRADE_DATE = "20260911"


def board(index: int) -> dict:
    return {"code": f"BK{index:04d}", "name": f"研究方向{index}", "kind": "concept",
            "pctChg": 20 - index, "amount": index * 1e7, "breadth": 30 + index,
            "upCount": index, "downCount": 50 - index, "mainNetInflow": None}


def history(count: int = 40, *, amount: bool = True, growth: float = 1.0) -> list[dict]:
    end = date(2026, 9, 11)
    return [{"date": (end - timedelta(days=count - index - 1)).isoformat(), "close": 100 + growth * index,
             **({"amount": 1e7 + index * 1e5} if amount else {}),
             "historySource": "测试行情", "historyUrl": "https://example.org/prices"}
            for index in range(count)]


def overview(count: int = 40) -> dict:
    return {"tradeDate": TRADE_DATE, "updatedAt": QUOTE, "quoteAsOf": QUOTE,
            "forecastDiscoveryBoards": [board(index) for index in range(1, count + 1)],
            "warnings": [], "scope": "同来源测试行情范围"}


def cache_row(code: str, rows: list[dict], *, fetched: str = QUOTE, closed: bool = True) -> dict:
    return {"key": f"concept_history:{code}:{TRADE_DATE}:v4", "value": json.dumps({
        "fetchedAt": fetched, "closed": closed, "rows": rows})}


@contextmanager
def fake_connection(meta: list[dict], research: list[dict] | None = None):
    # Exercise the actual indexed prefix query against an isolated database.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE app_meta (key TEXT PRIMARY KEY,value TEXT);"
                     "CREATE TABLE research_cache (cache_key TEXT PRIMARY KEY,payload_json TEXT,updated_at TEXT);")
    db.executemany("INSERT INTO app_meta VALUES (:key,:value)", meta)
    db.executemany("INSERT INTO research_cache VALUES (:cache_key,:payload_json,:updated_at)", research or [])
    try:
        yield db
    finally:
        db.close()


def execute_batch(items, fetch, **kwargs):
    values = BatchResult()
    for item in items:
        values.attempted.add(item)
        values[item] = fetch(item)
    return values


class ForecastPipelineTests(unittest.TestCase):
    def collect(self, *, snapshot=None, events=None, cached=None, batch=execute_batch):
        snapshot = snapshot or overview()
        client = Mock()
        client.daily_history.return_value = []
        client.strong_stocks.return_value = []
        news = AsyncMock(return_value=(events or {}, {"status": "available", "matchedCount": len(events or {})}))
        with (patch.object(pipeline, "ConceptResearchClient", return_value=client),
              patch.object(pipeline, "discover_world_news", news),
              patch.object(pipeline, "cached_histories", return_value=copy.deepcopy(cached or {})),
              patch.object(pipeline, "fetch_batch", side_effect=batch),
              patch("backend.research_data.index_history", return_value={"rows": []}),
              patch.object(pipeline.database, "now_iso", return_value=DECISION)):
            result = pipeline.collect_forecast_evidence(snapshot, None, "test-only-mocked")
        result["decisionAt"] = DECISION
        result["window"] = forecast_window("2026-09-13")
        return result, news, client

    def test_preselection_event_search_recalls_a_concept_outside_old_twelve(self):
        snapshot = overview()
        code = "BK0020"
        self.assertNotIn(code, {row["code"] for row in select_forecast_boards(snapshot["forecastDiscoveryBoards"])})
        source = {"id": f"{code}:web:event", "kind": "news", "title": "新产业事件",
                  "excerpt": "待核验的供需变化", "url": "https://example.org/event",
                  "source": "测试公告", "publishedAt": "2026-09-12T00:00:00+08:00", "publishedAtPrecision": "date"}
        result, search, client = self.collect(snapshot=snapshot, events={code: [source]})
        self.assertEqual(len(search.call_args.args[0]), 40)
        self.assertEqual(len(result["candidates"]), 12)
        candidate = next(row for row in result["candidates"] if row["code"] == code)
        self.assertEqual(candidate["eventClues"][0]["publishedAtPrecision"], "date")
        self.assertTrue(any(signal["metric"] == "eventClue" and signal["selectedBy"] for signal in candidate["candidateSignals"]))
        self.assertEqual(client.daily_history.call_args_list[0].args[0], code)
        self.assertTrue(any(item["id"] == source["id"] for item in candidate["evidence"]))

    def test_same_end_date_cache_prefers_turnover_coverage_over_extra_price_rows(self):
        meta = [cache_row("BK0001", history(40, amount=True))]
        research = [{"cache_key": f"evaluation:BK0001:2026-07-01:2026-09-11", "updated_at": QUOTE,
                     "payload_json": json.dumps({"rows": history(45, amount=False)})}]
        with patch.object(pipeline.database, "connection", side_effect=lambda: fake_connection(meta, research)):
            rows = pipeline.cached_histories({"BK0001"}, TRADE_DATE, DECISION)["BK0001"]
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(row.get("amount", 0) > 0 for row in rows[-11:]))

    def test_future_observed_stale_and_intraday_caches_are_not_reused(self):
        meta = [cache_row("BK0001", history(), fetched="2026-09-14T09:00:00+08:00"),
                cache_row("BK0002", history()[:-1]), cache_row("BK0003", history(), closed=False),
                cache_row("BK0004", history())]
        with patch.object(pipeline.database, "connection", side_effect=lambda: fake_connection(meta)):
            found = pipeline.cached_histories({f"BK{index:04d}" for index in range(1, 5)}, TRADE_DATE, DECISION)
        self.assertEqual(set(found), {"BK0004"})
        self.assertEqual(found["BK0004"][-1]["date"], TRADE_DATE)

    def test_cache_query_filters_exact_codes_and_source_before_reading_json(self):
        meta = [cache_row("BK0001", history()), cache_row("BK00010", history()),
                cache_row("THS:885955", history()), {"key": "unrelated:BK0001", "value": "not JSON"}]
        research = [{"cache_key": "evaluation:sh000001:2026-08-01:2026-09-11", "updated_at": QUOTE,
                     "payload_json": json.dumps({"rows": history()})},
                    {"cache_key": "evaluation:600000:2026-08-01:2026-09-11", "updated_at": QUOTE,
                     "payload_json": "not JSON"}]
        codes = {"BK0001", "sh000001"} | {f"BK{1000 + index}" for index in range(250)}
        with fake_connection(meta, research) as db:
            snapshots = list(pipeline._cache_snapshots(db, codes))
        self.assertEqual({row["key"] for row in snapshots},
                         {meta[0]["key"], research[0]["cache_key"]})

    def test_empty_codes_or_invalid_cutoff_do_not_open_database(self):
        with patch.object(pipeline.database, "connection") as connect:
            self.assertEqual(pipeline.cached_histories(set(), TRADE_DATE, DECISION), {})
            self.assertEqual(pipeline.cached_histories({"BK0001"}, TRADE_DATE, "invalid"), {})
        connect.assert_not_called()

    def test_invalid_or_conflicting_prices_reject_series_without_filling_the_gap(self):
        valid = history()
        for bad in (0, -1, True, "100", math.inf, math.nan, None):
            rows = copy.deepcopy(valid)
            rows[10]["close"] = bad
            with self.subTest(close=bad):
                self.assertEqual(pipeline.normalized_history(rows, TRADE_DATE), [])
        conflicting = valid + [{**valid[-1], "close": valid[-1]["close"] + 1}]
        self.assertEqual(pipeline.normalized_history(conflicting, TRADE_DATE), [])
        self.assertEqual(len(pipeline.normalized_history(valid + [valid[-1]], TRADE_DATE)), len(valid))
        trimmed = pipeline.normalized_history(valid + [{"date": "2026-09-12", "close": 1e9}], TRADE_DATE)
        self.assertEqual(trimmed[-1]["close"], valid[-1]["close"])

    def test_read_budget_failure_records_unread_scope_not_nonexistent_history(self):
        def unavailable(items, fetch, **kwargs):
            result = BatchResult()
            if items:
                result.attempted.add(items[0])
            result.errors = {code: "timeout" if index == 0 else "not_started" for index, code in enumerate(items)}
            return result
        result, _, client = self.collect(batch=unavailable)
        selection = result["candidateSelection"]
        self.assertEqual(selection["historyCheckedCount"], 0)
        self.assertEqual(selection["historyUnseenCount"], 40)
        self.assertEqual(selection["historyMissingOrStaleCount"], 0)
        self.assertGreater(selection["historyRequests"]["deferred"], 0)
        self.assertIn("不等于不存在历史行情", selection["historyRequests"]["note"])
        self.assertTrue(result["candidates"])
        self.assertTrue(all(row["technicalData"]["ma20"] is None for row in result["candidates"]))
        client.daily_history.assert_not_called()

    def test_prompt_omits_raw_history_benchmarks_and_frozen_baselines(self):
        evidence, _, _ = self.collect(snapshot=overview(3))
        evidence["evaluationPlan"] = forecast.evaluation_plan(evidence)
        before = copy.deepcopy(evidence)
        prompt = forecast.build_prompt(evidence)
        data = json.loads(prompt.split("以下仅为数据材料：", 1)[1])
        for field in ("_histories", "_benchmarks", "evaluationPlan", "comparisonUniverse"):
            self.assertNotIn(field, data)
        self.assertNotIn("baseline", data["candidateSelection"])
        self.assertIn("baseline", evidence["candidateSelection"])
        self.assertEqual(evidence, before)

    def test_relative_evidence_remains_citable_in_assembled_forecast(self):
        cached = {"BK0001": pipeline.normalized_history(history(growth=2), TRADE_DATE),
                  "sh000001": pipeline.normalized_history(history(growth=0.5), TRADE_DATE),
                  "sh000688": pipeline.normalized_history(history(growth=1), TRADE_DATE)}
        evidence, _, _ = self.collect(snapshot=overview(1), cached=cached)
        raw = {"summary": "当前同区间相对强度提供观察线索，但现实催化尚缺依据。", "concepts": [{
            "code": "BK0001", "thesis": "相对两个基准走强，需要新增经营证据确认延续。", "conviction": "low",
            "technical": {"summary": "已提供日线显示同区间相对两个基准走强。", "status": "supported", "evidenceIds": ["BK0001:relative"]},
            "fundamental": {"summary": "订单及盈利变化尚无直接证据。", "status": "hypothesis", "evidenceIds": []},
            "news": {"summary": "未取得可核对的现实事件资料。", "status": "hypothesis", "evidenceIds": []},
            "catalysts": [], "stocks": [], "confirmation": "后续订单信息与相对强度共同改善。", "invalidation": "相对两个基准明显转弱。"}]}
        result = forecast.assemble_result(raw, evidence)
        self.assertEqual(result["concepts"][0]["technical"]["sources"][0]["id"], "BK0001:relative")
        self.assertEqual(result["concepts"][0]["conviction"], "low")
        self.assertTrue(result["candidateAudit"]["candidates"][0]["relativeStrength"])

    def test_frozen_daily_baseline_outside_model_pool_survives_evaluation_validation(self):
        reversal = history()
        closes = [120, 118, 116, 114, 112, 110, 110, 110, 112, 116, 124]
        for row, close, amount in zip(reversal[-11:], closes, [10] * 5 + [20] * 5 + [30]):
            row.update(close=close, amount=amount)
        cached = {f"BK{index:04d}": pipeline.normalized_history(reversal, TRADE_DATE) for index in range(10, 25)}
        cached.update({code: pipeline.normalized_history(history(growth=0), TRADE_DATE)
                       for code in ("sh000001", "sh000688")})
        event = {"id": "BK0031:web:event", "kind": "news", "title": "公开产业事件",
                 "excerpt": "供需变化仍需核验", "url": "https://example.org/event", "source": "测试公告",
                 "publishedAt": "2026-09-12T09:00:00+08:00"}
        # Multiple real discovery channels consume the twelve slots; the third
        # daily leader legitimately remains only in the frozen baseline universe.
        evidence, _, _ = self.collect(cached=cached, events={"BK0031": [event]})
        result = forecast.assemble_result({"summary": "缺少足够的独立证据，本次保持观察并记录基线。", "concepts": []}, evidence)
        audit = {row["code"] for row in result["candidateAudit"]["candidates"]}
        self.assertNotIn("BK0003", audit)
        daily = next(row for row in result["evaluationPlan"]["baselines"] if row["id"] == "daily_momentum")
        self.assertIn("BK0003", daily["codes"])
        accepted = _plan(result, "eastmoney", datetime.fromisoformat("2026-09-13T10:01:00+08:00"))
        self.assertIn("BK0003", accepted["daily_momentum"])
        self.assertEqual(set(accepted["candidate_equal_weight"]), audit)
        frozen = copy.deepcopy(result["evaluationPlan"])
        evidence["candidateSelection"]["baseline"]["dailyTop3Snapshot"][0]["pctChg"] = 999
        self.assertEqual(result["evaluationPlan"], frozen)


if __name__ == "__main__":
    unittest.main()
