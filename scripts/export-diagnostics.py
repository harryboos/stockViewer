#!/usr/bin/env python3
"""Export a running Docker app's SQLite snapshot and sanitized diagnostics.

Standard library only. Raw backup/logs stay in raw-server-only/; only the newly
built sanitized database, sanitized logs and manifest enter the share archive.
This is a diagnostic export, not an anonymization guarantee or a restore tool.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus


TABLES = {
    "watchlist", "stock_basics", "quote_snapshots", "ai_runs", "strategy_runs",
    "app_meta", "forecast_reports", "forecast_feedback", "selection_reports",
    "selection_outcomes", "research_jobs", "research_cache", "comparison_outcomes",
    "comparison_universes", "concept_universes", "ai_attempts",
}
META_PREFIXES = {
    "ai_prompt", "hot_concepts", "sector_overview", "concept_history",
    "sector_previous_amount", "market_intraday_pair", "market_intraday_index",
    "market_turnover", "market_overview", "forecast_feedback_refresh",
    "forecast_trade_calendar", "market_data_source", "market_data_transport",
    "market_data_updated_at", "market_data_error", "market_overview_error",
    "stock_catalog_synced_at", "storage_schema_version", "storage_maintenance",
    "quote_volume_unit",
}
CACHE_PREFIXES = {"latest-market", "latest-sectors", "rotation", "rotation-snapshot", "evaluation", "stock-history"}
IDENTIFIERS = {"token", "runtoken", "runkey"}
SECRET_FIELDS = {
    "apikey", "accesskey", "accesskeyid", "secretaccesskey", "accesstoken",
    "refreshtoken", "password", "passwd", "secret", "clientsecret",
    "sessionsecret", "sessionid", "privatekey", "authorization",
    "proxyauthorization", "cookie", "setcookie", "credentials", "headers",
    "email", "phone", "mobile", "ip", "clientip", "remoteaddr",
}

# Runs in the container without importing the application or initializing its DB.
# Secret values are returned only to the parent process's memory, never printed
# by the parent, written to metadata, or supplied in command-line arguments.
CONTAINER_CODE = r'''
import hashlib, json, os, pathlib, platform, re, shutil, sqlite3, sys, tempfile, time
mode = sys.argv[1]
if mode == "backup":
    path = pathlib.Path(os.environ.get("STOCK_VIEWER_DB", "/app/data/stockviewer.sqlite3")).resolve()
    if not path.is_file():
        raise RuntimeError("database does not exist")
    with tempfile.TemporaryDirectory(prefix="stock-diagnostic-") as directory:
        target_path = pathlib.Path(directory) / "snapshot.sqlite3"
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=20)
        target = sqlite3.connect(target_path)
        deadline = time.monotonic() + 180
        def progress(*args):
            if time.monotonic() > deadline:
                raise TimeoutError("backup deadline exceeded")
        try:
            source.backup(target, pages=256, progress=progress, sleep=0.1)
            if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("snapshot integrity check failed")
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()
        with target_path.open("rb") as snapshot:
            shutil.copyfileobj(snapshot, sys.stdout.buffer)
elif mode == "secrets":
    pattern = re.compile(r"(?:^|_)(?:API_KEY|ACCESS_KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|CREDENTIALS?|PRIVATE_KEY)(?:_|$)", re.I)
    print(json.dumps([value for key, value in os.environ.items() if value and pattern.search(key)]))
elif mode == "version":
    files = ["package.json", "package-lock.json", "backend/research_prompt.py",
             "backend/forecast_pipeline.py", "backend/concept_forecast.py",
             "backend/forecast_candidates.py", "backend/forecast_discovery.py",
             "backend/concept_comparison.py", "backend/forecast_feedback.py",
             "backend/data_sources.py"]
    hashes = {}
    for name in files:
        path = pathlib.Path("/app") / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    print(json.dumps({"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "sourceSha256": hashes}))
else:
    raise RuntimeError("unknown export mode")
'''


class ExportError(Exception):
    pass


def normalized(key):
    return re.sub(r"[^a-z0-9]", "", key.lower())


class Redactor:
    def __init__(self, secrets=()):
        self.salt = os.urandom(32)
        variants = set()
        for secret in secrets:
            if secret:
                variants.update((secret, quote(secret, safe=""), quote_plus(secret),
                                 json.dumps(secret, ensure_ascii=True)[1:-1],
                                 base64.b64encode(secret.encode()).decode()))
        self.secrets = sorted(variants, key=len, reverse=True)

    def identity(self, value):
        return "diag-" + hmac.new(self.salt, str(value).encode(), hashlib.sha256).hexdigest()[:32]

    def text(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[PRIVATE KEY REDACTED]", value, flags=re.S)
        value = re.sub(r'''(?im)\b(authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=][^\r\n]*''', r"\1: [REDACTED]", value)
        value = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/_=.~:-]+", r"\1 [REDACTED]", value)
        value = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r'''(?i)(\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|session[_-]?id|signature)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&}\]]+)''', r"\1[REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
        value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT REDACTED]", value)
        value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL REDACTED]", value)
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE REDACTED]", value)
        value = re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", "[IP REDACTED]", value)
        value = re.sub(r"\[[0-9a-fA-F:]*:[0-9a-fA-F:]+\]", "[IP REDACTED]", value)
        return value

    def value(self, value, key="", depth=0):
        if depth > 80:
            raise ExportError("数据嵌套过深，已停止打包，请单独检查该记录。")
        name = normalized(key)
        if value is not None and name in IDENTIFIERS:
            return self.identity(value)
        if name in SECRET_FIELDS or name.endswith(("apikey", "password", "secret", "credential", "token", "privatekey", "accesskey", "sessionid", "cookie")):
            return "[REDACTED]" if value is not None else None
        if isinstance(value, bytes):
            return b""  # Unknown binary payloads cannot be safely scanned as text.
        if isinstance(value, dict):
            return {self.text(k): self.value(v, k, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, depth=depth + 1) for v in value]
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    pass
                else:
                    return json.dumps(self.value(parsed, depth=depth + 1), ensure_ascii=False, separators=(",", ":"))
            return self.text(value)
        return value


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def sanitized_database(source_path, destination, redactor):
    # Build from clean rows, never copy then UPDATE: deleted/private bytes must
    # not survive in free pages, a journal or a WAL belonging to the shared DB.
    counts = {}
    with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        with closing(sqlite3.connect(destination)) as target:
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("PRAGMA trusted_schema=OFF")
            tables = source.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            for table in tables:
                name = table["name"]
                if name not in TABLES:
                    continue
                target.execute(redactor.text(table["sql"]))
                columns = [row[1] for row in source.execute(f"PRAGMA table_info({identifier(name)})")]
                query = f"INSERT INTO {identifier(name)} ({','.join(map(identifier, columns))}) VALUES ({','.join('?' for _ in columns)})"
                kept = skipped = 0
                for row in source.execute(f"SELECT * FROM {identifier(name)}"):
                    if (name == "watchlist"
                        or name == "app_meta" and row["key"].split(":")[0] not in META_PREFIXES
                        or name == "research_cache" and row["cache_key"].split(":")[0] not in CACHE_PREFIXES):
                        skipped += 1
                        continue
                    target.execute(query, [redactor.value(row[column], column) for column in columns])
                    kept += 1
                counts[name] = {"included": kept, "excluded": skipped}
            target.commit()
            if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ExportError("脱敏数据库完整性检查失败，未打包。")
            if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ExportError("脱敏数据库关联检查失败，未打包。")
    return {"tables": counts, "unknownTablesExcluded": sum(t["name"] not in TABLES for t in tables)}


def sanitized_log(source, destination, redactor):
    lines = 0
    private_key = False
    with source.open(encoding="utf-8", errors="replace") as src, destination.open("x", encoding="utf-8") as dst:
        for line in src:
            lines += 1
            if re.search(r"-----BEGIN [^-]*PRIVATE KEY-----", line):
                private_key = True
                dst.write("[PRIVATE KEY BLOCK REDACTED]\n")
            if private_key:
                if re.search(r"-----END [^-]*PRIVATE KEY-----", line):
                    private_key = False
                continue
            dst.write(redactor.value(line.rstrip("\n")) + "\n")
    return lines


def docker(args, *, output=None, timeout=60):
    result = subprocess.run(["docker", *args], stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Docker/SQLite stderr may contain URLs or other sensitive values.
        raise ExportError("Docker 操作失败，未生成共享包；请检查容器名称、运行状态与磁盘空间。")
    return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_known_secrets(paths, redactor):
    patterns = [value.encode() for value in redactor.secrets]
    if not patterns:
        return
    overlap = max(map(len, patterns)) - 1
    for path in paths:
        with path.open("rb") as stream:
            previous = b""
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                combined = previous + chunk
                if any(pattern in combined for pattern in patterns):
                    raise ExportError("共享文件仍匹配已知密钥，已停止打包；请不要分享输出目录。")
                previous = combined[-overlap:] if overlap else b""


def export(args):
    os.umask(0o077)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    raw = root / "raw-server-only"
    share = root / "share"
    raw.mkdir(mode=0o700)
    share.mkdir(mode=0o700)
    inside = ["exec", args.container, "python3", "-c", CONTAINER_CODE]
    print("1/4 正在制作 SQLite 一致性备份…", flush=True)
    with (raw / "stockviewer.sqlite3").open("xb") as output:
        docker([*inside, "backup"], output=output, timeout=240)
    secrets = json.loads(docker([*inside, "secrets"]))
    redactor = Redactor(secrets)
    print("2/4 正在收集近期日志并清洗副本…", flush=True)
    with (raw / "application.log").open("xb") as output:
        log_result = subprocess.run(["docker", "logs", "--since", args.since, "--tail", str(args.tail),
                                     "--timestamps", args.container], stdout=output, stderr=subprocess.STDOUT, timeout=120)
    if log_result.returncode:
        raise ExportError("日志读取失败，原始数据库备份已保留；未生成共享包。")
    stats = sanitized_database(raw / "stockviewer.sqlite3", share / "stockviewer.sanitized.sqlite3", redactor)
    lines = sanitized_log(raw / "application.log", share / "application.sanitized.log", redactor)
    version = json.loads(docker([*inside, "version"]))
    image = docker(["inspect", "--format", "{{.Image}}", args.container]).decode().strip()
    manifest = {
        "createdAtUtc": datetime.now(timezone.utc).isoformat(), "formatVersion": 1,
        "containerImageId": image if re.fullmatch(r"sha256:[a-f0-9]{64}", image) else "unknown",
        "version": version, "database": stats,
        "logs": {"requestedSince": args.since, "tailLimit": args.tail, "exportedLines": lines},
        "notes": ["原始数据库和日志仅在服务器 raw-server-only 目录，不在共享包内。",
                  "自选表为空；未知表与未知缓存键不导出；任务关联 ID 作一致匿名映射。",
                  "递归清洗已知密钥、常见认证字段、邮箱、手机号和常见 IP 格式。",
                  "保留全部已有研究档案、预测依据及已缓存行情；不会补抓缺失行情或调用 AI。",
                  "日志仅含当前容器尚未轮转丢弃的记录，最多所设行数；重建前的日志可能已不存在。",
                  "自由文本不能保证完全自动脱敏。分享前请检查文件；脱敏库仅用于诊断，不用于生产恢复。"],
    }
    (share / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("3/4 正在校验并打包脱敏文件…", flush=True)
    # Explicit file allowlist: never tar the root or raw-server-only directory.
    files = ("stockviewer.sanitized.sqlite3", "application.sanitized.log", "manifest.json")
    check_known_secrets([share / name for name in files], redactor)
    archive = root / "stock-diagnostics.tar.gz"
    with tarfile.open(archive, "x:gz") as bundle:
        for name in files:
            bundle.add(share / name, arcname="stock-diagnostics/" + name, recursive=False,
                       filter=scrub_tar_metadata)
    (root / "stock-diagnostics.tar.gz.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    print(f"4/4 完成。只下载这两个文件：\n{archive}\n{archive}.sha256")
    print(f"原始备份留在服务器：{raw}（不要上传）")


def scrub_tar_metadata(item):
    item.uid = item.gid = 0
    item.uname = item.gname = ""
    item.mode = 0o600
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="private-apps-stock-viewer-1")
    parser.add_argument("--output", type=Path, required=True, help="新的专用输出目录；已有目录会拒绝覆盖")
    parser.add_argument("--since", default="168h", help="最近日志范围，默认 168h（7 天）")
    parser.add_argument("--tail", type=int, default=20000, help="最多导出日志行数，默认 20000")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.container) or args.tail < 1:
        parser.error("容器名无效或日志行数小于 1")
    if not re.fullmatch(r"[1-9]\d*[hms]", args.since):
        parser.error("--since 请使用 168h、30m 等时长")
    try:
        export(args)
    except ExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"导出未完成（{type(exc).__name__}）。不要分享不完整目录；未修改线上数据库。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
