"""Small durable records for watch notes, strategy evaluation and research tasks."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .concept_identity import CONCEPT_PROVIDERS, concept_provider, single_provider


def initialize_universes(db) -> None:
    """Keep one independently identified population for each index provider."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(concept_universes)")}
    if columns and "provider" not in columns:
        db.execute("ALTER TABLE concept_universes RENAME TO concept_universes_legacy")
    db.execute("""CREATE TABLE IF NOT EXISTS concept_universes (
        provider TEXT NOT NULL, as_of TEXT NOT NULL, boards_json TEXT NOT NULL, scope TEXT NOT NULL,
        PRIMARY KEY(provider, as_of))""")
    if "provider" in columns and "scope" not in columns:
        db.execute("ALTER TABLE concept_universes ADD COLUMN scope TEXT NOT NULL DEFAULT '来源已保存的概念范围'")
    if columns and "provider" not in columns:
        for row in db.execute("SELECT * FROM concept_universes_legacy ORDER BY as_of"):
            boards = json.loads(row["boards_json"])
            for provider in CONCEPT_PROVIDERS:
                selected = [board for board in boards if concept_provider(board.get("code")) == provider]
                if selected:
                    db.execute("INSERT OR REPLACE INTO concept_universes VALUES (?,?,?,?)",
                               (provider, row["as_of"], json.dumps(selected, ensure_ascii=False), "此前已保存的概念范围"))
        db.execute("DROP TABLE concept_universes_legacy")
    db.execute("""DELETE FROM concept_universes WHERE (provider, as_of) NOT IN
        (SELECT provider, MAX(as_of) FROM concept_universes GROUP BY provider)""")


def initialize(db) -> None:
    columns = {row["name"] for row in db.execute("PRAGMA table_info(watchlist)")}
    for name, definition in {"group_name": "TEXT NOT NULL DEFAULT '未分组'", "note": "TEXT NOT NULL DEFAULT ''",
                             "reason": "TEXT NOT NULL DEFAULT ''", "reference_price": "REAL",
                             "reference_date": "TEXT"}.items():
        if name not in columns:
            db.execute(f"ALTER TABLE watchlist ADD COLUMN {name} {definition}")
    schema = """
        CREATE TABLE IF NOT EXISTS selection_reports (
            id INTEGER PRIMARY KEY, run_date TEXT NOT NULL, strategy_key TEXT NOT NULL,
            name TEXT NOT NULL, published_at TEXT NOT NULL, picks_json TEXT NOT NULL,
            UNIQUE(run_date, strategy_key));
        CREATE TABLE IF NOT EXISTS selection_outcomes (
            report_id INTEGER NOT NULL REFERENCES selection_reports(id), code TEXT NOT NULL,
            sessions INTEGER NOT NULL, result_json TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(report_id, code, sessions));
        CREATE TABLE IF NOT EXISTS research_jobs (
            job_key TEXT PRIMARY KEY, token TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, progress_json TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS research_cache (
            cache_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS comparison_outcomes (
            report_id INTEGER NOT NULL REFERENCES forecast_reports(id), code TEXT NOT NULL,
            horizon_days INTEGER NOT NULL, result_json TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(report_id, code, horizon_days));
        CREATE TABLE IF NOT EXISTS comparison_universes (
            report_id INTEGER PRIMARY KEY REFERENCES forecast_reports(id), as_of TEXT NOT NULL,
            scope TEXT NOT NULL, boards_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_attempts (
            token TEXT PRIMARY KEY, provider TEXT NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT NOT NULL, stage TEXT, calls INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_selection_reports_published ON selection_reports(published_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_selection_outcomes_sessions ON selection_outcomes(sessions,report_id,code);
        CREATE INDEX IF NOT EXISTS idx_research_cache_updated ON research_cache(updated_at);
        CREATE INDEX IF NOT EXISTS idx_ai_attempts_started ON ai_attempts(started_at);
    """
    for statement in schema.split(";"):
        if statement.strip():
            db.execute(statement)
    initialize_universes(db)
    if 'origin' not in {row['name'] for row in db.execute('PRAGMA table_info(selection_reports)')}:
        db.execute("ALTER TABLE selection_reports ADD COLUMN origin TEXT NOT NULL DEFAULT 'retained_cache'")
    db.execute('PRAGMA optimize')
    # Preserve actual cached selections before routine daily-cache retention removes them.
    for row in db.execute("SELECT * FROM strategy_runs").fetchall():
        try:
            archive_rules(db, json.loads(row["result_json"]), row["created_at"], origin='retained_cache')
        except (ValueError, TypeError, KeyError):
            continue
    from .report_storage import decode_result
    for row in db.execute("SELECT * FROM ai_runs WHERE status='succeeded' AND provider IN ('glm','deepseek','qwen')").fetchall():
        try:
            result = decode_result(row["result_json"])
            archive_selection(db, row["run_date"], f"ai:{row['provider']}", row["model"],
                              row["finished_at"], result["picks"], origin='retained_cache')
        except (ValueError, TypeError, KeyError):
            continue


def archive_selection(db, day: str, key: str, name: str, published: str, picks: list, *, origin='first_success') -> None:
    if not published or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return
    if datetime.fromisoformat(published).tzinfo is None:
        return
    db.execute("""INSERT OR IGNORE INTO selection_reports
        (run_date,strategy_key,name,published_at,picks_json,origin) VALUES (?,?,?,?,?,?)""",
               (day, key, name, published, json.dumps(picks, ensure_ascii=False, separators=(",", ":")), origin))


def archive_rules(db, results: list, published: str, *, origin='first_success') -> None:
    for result in results:
        if isinstance(result, dict) and all(key in result for key in ("runDate", "id", "name", "picks")):
            archive_selection(db, result["runDate"], f"rule:{result['id']}", result["name"], published, result["picks"], origin=origin)


def cache_get(key: str, max_age: float | None = None):
    from . import database
    with database.connection() as db:
        row = db.execute("SELECT * FROM research_cache WHERE cache_key=?", (key,)).fetchone()
    if not row:
        return None
    if max_age is not None and (datetime.now(database.CHINA_TZ) - datetime.fromisoformat(row["updated_at"])).total_seconds() > max_age:
        return None
    return json.loads(row["payload_json"])


def cache_put(key: str, value) -> None:
    from . import database
    with database._write_lock, database.connection() as db:
        db.execute("INSERT OR REPLACE INTO research_cache VALUES (?,?,?)",
                   (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), database.now_iso()))
        cutoff = (datetime.now(database.CHINA_TZ) - timedelta(days=7)).isoformat()
        db.execute("""DELETE FROM research_cache WHERE updated_at < ?
            AND cache_key NOT IN ('latest-market','latest-sectors','rotation','rotation-snapshot')""", (cutoff,))


def save_universe(boards: list[dict], as_of: str, scope: str | None = None) -> dict:
    from . import database
    compact = [{"code": row["code"], "name": row["name"]} for row in boards]
    provider = single_provider(compact)
    if not provider:
        raise ValueError("概念范围必须包含同一来源的有效概念代码")
    scope = scope or ("同花顺已采集公开概念样本（非全市场）" if provider == "ths" else "预测当时可获取的全部有效概念")
    with database._write_lock, database.connection() as db:
        db.execute("INSERT OR REPLACE INTO concept_universes VALUES (?,?,?,?)",
                   (provider, as_of, json.dumps(compact, ensure_ascii=False, separators=(",", ":")), scope))
        db.execute("DELETE FROM concept_universes WHERE provider=? AND as_of <> ?", (provider, as_of))
    return {"asOf": as_of, "provider": provider, "boards": compact, "scope": scope}


def update_watch(ts_code: str, group: str, reason: str, note: str) -> None:
    from . import database
    with database._write_lock, database.connection() as db:
        changed = db.execute("UPDATE watchlist SET group_name=?, reason=?, note=? WHERE ts_code=?",
                             (group.strip() or "未分组", reason.strip(), note.strip(), ts_code)).rowcount
        if not changed:
            raise ValueError("该股票已不在自选中")


def capture_reference(ts_code: str) -> None:
    from . import database
    with database._write_lock, database.connection() as db:
        # Only the original add operation may capture a reference, never a later refresh/migration.
        row = db.execute("SELECT close,trade_date FROM quote_snapshots WHERE ts_code=?", (ts_code,)).fetchone()
        if row:
            db.execute("""UPDATE watchlist SET reference_price=?,reference_date=?
                WHERE ts_code=? AND reference_price IS NULL AND substr(added_at,1,10)=?""",
                       (row["close"], row["trade_date"], ts_code, database.china_date()))
