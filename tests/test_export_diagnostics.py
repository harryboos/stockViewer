"""Synthetic fixtures only: never opens the real database or environment files."""
import base64
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export-diagnostics.py"
spec = importlib.util.spec_from_file_location("diagnostic_export", SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class DiagnosticExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.secret = "fixture-key-ONLY+/=123456789"
        self.redactor = exporter.Redactor([self.secret])

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        path = self.root / "source.sqlite3"
        db = sqlite3.connect(path)
        db.executescript('''
            CREATE TABLE watchlist(id INTEGER PRIMARY KEY, note TEXT);
            CREATE TABLE forecast_reports(id INTEGER PRIMARY KEY, run_key TEXT NOT NULL UNIQUE, result_json TEXT NOT NULL);
            CREATE TABLE forecast_feedback(report_id INTEGER REFERENCES forecast_reports(id), result_json TEXT NOT NULL);
            CREATE TABLE ai_attempts(token TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE research_jobs(job_key TEXT PRIMARY KEY, token TEXT, error TEXT);
            CREATE TABLE app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE research_cache(cache_key TEXT PRIMARY KEY, payload_json TEXT);
            CREATE TABLE private_accounts(password TEXT);
        ''')
        db.execute("INSERT INTO watchlist VALUES (1, 'PRIVATE WATCH NOTE')")
        payload = {"storageFormat": "concept-report-v1", "payload": {
            "candidateAudit": [{"name": "重组蛋白", "rank": 12, "returnPct": 16.7}],
            "apiKey": self.secret, "headers": {"Cookie": "session=fixture-cookie"},
            "runToken": "fixture-run-one"}, "sourcePool": [{"url": "https://example.com/news?id=20260915", "excerpt": "公开新闻"}]}
        db.execute("INSERT INTO forecast_reports VALUES (1, ?, ?)", ("fixture-run-one", json.dumps(payload)))
        db.execute("INSERT INTO forecast_feedback VALUES (1, ?)", (json.dumps({"returnPct": 16.7, "entryDate": "2026-09-15"}),))
        db.executemany("INSERT INTO ai_attempts VALUES (?, 'succeeded')", [("fixture-run-one",), ("fixture-run-two",)])
        db.execute("INSERT INTO research_jobs VALUES ('comparison', ?, ?)", ("fixture-run-one", "upstream token=" + self.secret))
        db.executemany("INSERT INTO app_meta VALUES (?, ?)", [("concept_history:BK1234:20260915:v1", '{"rows":[{"close":10.5}]}'), ("private_setting", self.secret)])
        db.executemany("INSERT INTO research_cache VALUES (?, ?)", [("evaluation:sh000001:2026-09-15:2026-09-30", '{"rows":[{"close":100}]}'), ("user-profile", '{"name":"PRIVATE PERSON"}')])
        db.execute("INSERT INTO private_accounts VALUES (?)", (self.secret,))
        db.commit()
        db.close()
        return path

    def test_recursive_redaction_preserves_research_and_public_source_urls(self):
        payload = {"candidateAudit": {"returnPct": -8.21, "name": "重组蛋白"},
                   "sourcePool": [{"url": "https://example.com/news?id=12", "apiKey": "legacy-unknown-key"}],
                   "nested": json.dumps({"authorization": "Bearer legacy-key", "accessToken": "legacy-token"}),
                   "tushare_token": "historical-tushare-credential", "proxyPrivateKey": "historical-proxy-private-key",
                   "text": " ".join([self.secret, quote(self.secret, safe=""), base64.b64encode(self.secret.encode()).decode()]),
                   "email": "person@example.com"}
        result = json.loads(self.redactor.value(json.dumps(payload)))
        self.assertEqual(result["candidateAudit"], payload["candidateAudit"])
        self.assertEqual(result["sourcePool"][0]["url"], payload["sourcePool"][0]["url"])
        self.assertEqual(json.loads(result["nested"])["authorization"], "[REDACTED]")
        for private in [self.secret, "legacy-unknown-key", "legacy-key", "legacy-token", "person@example.com", "historical-tushare-credential", "historical-proxy-private-key"]:
            self.assertNotIn(private, json.dumps(result))

    def test_fresh_database_excludes_private_rows_and_preserves_relations(self):
        source = self.fixture()
        before = exporter.sha256(source)
        destination = self.root / "clean.sqlite3"
        stats = exporter.sanitized_database(source, destination, self.redactor)
        self.assertEqual(exporter.sha256(source), before)
        self.assertEqual(stats["tables"]["watchlist"]["included"], 0)
        self.assertEqual(stats["unknownTablesExcluded"], 1)
        with closing(sqlite3.connect(destination)) as db:
            run_key, payload = db.execute("SELECT run_key,result_json FROM forecast_reports").fetchone()
            self.assertEqual(run_key, db.execute("SELECT token FROM research_jobs").fetchone()[0])
            self.assertEqual(run_key, json.loads(payload)["payload"]["runToken"])
            self.assertEqual(db.execute("SELECT COUNT(DISTINCT token) FROM ai_attempts").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM app_meta").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM research_cache").fetchone()[0], 1)
        for private in [self.secret, "PRIVATE WATCH NOTE", "PRIVATE PERSON", "fixture-run-one", "fixture-run-two"]:
            self.assertNotIn(private.encode(), destination.read_bytes())
        self.assertFalse(Path(str(destination) + "-wal").exists())

    def test_backup_includes_committed_wal_and_missing_source_is_not_created(self):
        source = self.root / "live.sqlite3"
        db = sqlite3.connect(source)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE audit(value TEXT)")
        db.execute("INSERT INTO audit VALUES ('committed-in-wal')")
        db.commit()
        try:
            self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
            result = subprocess.run([sys.executable, "-c", exporter.CONTAINER_CODE, "backup"],
                                    env={**os.environ, "STOCK_VIEWER_DB": str(source)}, capture_output=True, check=True)
            snapshot = self.root / "snapshot.sqlite3"
            snapshot.write_bytes(result.stdout)
            with closing(sqlite3.connect(snapshot)) as copy:
                self.assertEqual(copy.execute("SELECT value FROM audit").fetchone()[0], "committed-in-wal")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 1)
        finally:
            db.close()
        missing = self.root / "does-not-exist.sqlite3"
        result = subprocess.run([sys.executable, "-c", exporter.CONTAINER_CODE, "backup"],
                                env={**os.environ, "STOCK_VIEWER_DB": str(missing)}, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())

    def test_logs_preserve_traceback_context_but_remove_authentication_and_contacts(self):
        source = self.root / "raw.log"
        destination = self.root / "clean.log"
        source.write_text('''2026-10-16T10:00:00Z ERROR 请求失败
Traceback (most recent call last):
  File "/app/backend/forecast_feedback.py", line 20
Authorization: Bearer fixture-log-token
Cookie: secret-session-value
2026-10-16T10:00:01Z {"cookie":"historical-cookie-value"}
2026-10-16T10:00:02Z {"authorization":"historical-auth-value"}
url=https://fixture-user:fixture-password@example.com/api?token=fixture-query-token&symbol=BK1234
contact person@example.com 13812345678 192.168.1.23
-----BEGIN PRIVATE KEY-----
fixture-private-key-bytes
-----END PRIVATE KEY-----
RemoteDisconnected: retry later
''', encoding="utf-8")
        exporter.sanitized_log(source, destination, self.redactor)
        clean = destination.read_text()
        for private in ["fixture-log-token", "secret-session-value", "fixture-user", "fixture-password", "fixture-query-token", "person@example.com", "13812345678", "192.168.1.23", "fixture-private-key-bytes", "historical-cookie-value", "historical-auth-value"]:
            self.assertNotIn(private, clean)
        for useful in ["2026-10-16T10:00:00Z", "Traceback", "forecast_feedback.py", "BK1234", "RemoteDisconnected"]:
            self.assertIn(useful, clean)

    def test_full_bundle_contains_only_sanitized_files_and_private_backup_stays_local(self):
        source = self.fixture()
        root = self.root / "export"
        args = SimpleNamespace(output=root, container="fixture-container", since="168h", tail=20000)

        def fake_run(command, **kwargs):
            if command[1] == "logs":
                kwargs["stdout"].write(("Traceback\nAuthorization: Bearer " + self.secret + "\n").encode())
                return subprocess.CompletedProcess(command, 0)
            if command[1] == "inspect":
                return subprocess.CompletedProcess(command, 0, ("sha256:" + "a" * 64).encode())
            if command[-1] == "backup":
                kwargs["stdout"].write(source.read_bytes())
                return subprocess.CompletedProcess(command, 0)
            if command[-1] == "secrets":
                return subprocess.CompletedProcess(command, 0, json.dumps([self.secret]).encode())
            if command[-1] == "version":
                return subprocess.CompletedProcess(command, 0, b'{"python":"fixture","sourceSha256":{}}')
            raise AssertionError(command)

        mask = os.umask(0o077)
        try:
            with patch.object(exporter.subprocess, "run", side_effect=fake_run), patch("sys.stdout", new=io.StringIO()) as output:
                exporter.export(args)
            self.assertNotIn(self.secret, output.getvalue())
            archive = root / "stock-diagnostics.tar.gz"
            with tarfile.open(archive) as bundle:
                self.assertEqual(set(bundle.getnames()), {
                    "stock-diagnostics/stockviewer.sanitized.sqlite3", "stock-diagnostics/application.sanitized.log", "stock-diagnostics/manifest.json"})
                for member in bundle.getmembers():
                    self.assertNotIn(self.secret.encode(), bundle.extractfile(member).read())
                    self.assertEqual(member.mode, 0o600)
            raw = root / "raw-server-only" / "stockviewer.sqlite3"
            self.assertIn(self.secret.encode(), raw.read_bytes())
            self.assertEqual(raw.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "stock-diagnostics.tar.gz.sha256").read_text().split()[0], exporter.sha256(archive))
        finally:
            os.umask(mask)

    def test_final_scan_blocks_known_secret_even_across_read_boundary(self):
        path = self.root / "leak.txt"
        path.write_bytes(b"x" * (1024 * 1024 - 4) + self.secret.encode())
        with self.assertRaises(exporter.ExportError):
            exporter.check_known_secrets([path], self.redactor)


if __name__ == "__main__":
    unittest.main()
