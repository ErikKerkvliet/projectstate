"""SQLite access layer: one WAL-mode connection, a lock for writes, schema creation, tiny helpers.

All monetary values are integers in micro-USD (1 USD = 1_000_000). Timestamps are ISO-8601 UTC strings.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT, created_at TEXT NOT NULL, last_seen_at TEXT);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, UNIQUE(tenant_id, slug));
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
  files TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, supersedes INTEGER, content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS entries_project ON entries(project_id, kind, status);
CREATE UNIQUE INDEX IF NOT EXISTS entries_hash ON entries(project_id, content_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  title, body, tags, files, content='entries', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, title, body, tags, files) VALUES (new.id, new.title, new.body, new.tags, new.files);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, body, tags, files)
  VALUES ('delete', old.id, old.title, old.body, old.tags, old.files);
END;
CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, body, tags, files)
  VALUES ('delete', old.id, old.title, old.body, old.tags, old.files);
  INSERT INTO entries_fts(rowid, title, body, tags, files) VALUES (new.id, new.title, new.body, new.tags, new.files);
END;
CREATE TABLE IF NOT EXISTS search_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, tenant_id TEXT, project_id INTEGER, query TEXT NOT NULL,
  filters TEXT, mode TEXT, n_candidates INTEGER, n_returned INTEGER, top_score REAL, chars_returned INTEGER);
CREATE TABLE IF NOT EXISTS wallets (
  tenant_id TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, daily_call_cap INTEGER, daily_spend_cap INTEGER,
  per_minute_cap INTEGER, blocked INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, tenant_id TEXT NOT NULL, rail TEXT NOT NULL, kind TEXT NOT NULL,
  tool TEXT, call_id INTEGER, amount INTEGER NOT NULL, balance_after INTEGER NOT NULL, ref TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ledger_tenant_ts ON ledger(tenant_id, ts);
CREATE INDEX IF NOT EXISTS ledger_ts ON ledger(ts);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, tenant_id TEXT, rail TEXT, tool TEXT NOT NULL, duration_ms REAL,
  ok INTEGER NOT NULL, error_kind TEXT, error_msg TEXT, charged INTEGER NOT NULL DEFAULT 0, request_key TEXT,
  session_id TEXT, deduped INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_tenant_ts ON calls(tenant_id, ts);
CREATE TABLE IF NOT EXISTS dedup (
  tenant_id TEXT NOT NULL, request_key TEXT NOT NULL, call_id INTEGER, result TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(tenant_id, request_key));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS x402_payments (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, payer TEXT, network TEXT, amount INTEGER NOT NULL, tx TEXT,
  nonce TEXT UNIQUE, session_id TEXT, tool TEXT, status TEXT NOT NULL, raw TEXT);
CREATE TABLE IF NOT EXISTS x402_credit_tokens (
  token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT,
  kind TEXT NOT NULL DEFAULT 'credit', label TEXT, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, tenant_id TEXT, kind TEXT NOT NULL, message TEXT NOT NULL,
  acknowledged INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY, ts REAL NOT NULL, ip TEXT NOT NULL, ok INTEGER NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        cur = self._conn.cursor()
        if str(self.path) != ":memory:":
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.executescript(SCHEMA)
        self._migrate(cur)
        self._migrate_tokens(cur)

    def _migrate_tokens(self, cur: sqlite3.Cursor) -> None:
        """Add the operator-key columns to an x402_credit_tokens table created before they existed."""
        have = {r[1] for r in cur.execute("PRAGMA table_info(x402_credit_tokens)").fetchall()}
        for col, ddl in (("kind", "kind TEXT NOT NULL DEFAULT 'credit'"), ("label", "label TEXT"), ("revoked_at", "revoked_at TEXT")):
            if col not in have:
                cur.execute(f"ALTER TABLE x402_credit_tokens ADD COLUMN {ddl}")

    # -- migrations ---------------------------------------------------------------------------
    def _migrate(self, cur: sqlite3.Cursor) -> None:
        """Drop the tables of the removed prepaid/account rail (v0.2: x402-only)."""
        legacy = ("users", "api_keys", "oauth_clients", "oauth_txns", "oauth_codes", "oauth_tokens", "payments")
        have = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        drop = [t for t in legacy if t in have]
        if drop:
            for t in drop:
                cur.execute(f"DROP TABLE IF EXISTS {t}")
            cur.execute("DELETE FROM settings WHERE key IN ('signup_credit','paypal.fee_fixed','paypal.fee_pct')")
            cur.execute("DELETE FROM ledger WHERE rail='paypal'")
            cur.execute("DELETE FROM tenants WHERE kind='user'")
            cur.execute("DELETE FROM wallets WHERE tenant_id NOT IN (SELECT id FROM tenants)")
            cur.execute("DELETE FROM ledger WHERE tenant_id NOT IN (SELECT id FROM tenants)")
            cur.execute("DELETE FROM calls WHERE tenant_id IS NOT NULL AND tenant_id NOT IN (SELECT id FROM tenants)")
            cur.execute("DELETE FROM projects WHERE tenant_id NOT IN (SELECT id FROM tenants)")
            cur.execute("DELETE FROM entries WHERE tenant_id NOT IN (SELECT id FROM tenants)")
            cur.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
            import logging

            logging.getLogger("projectstate.db").warning("dropped legacy account tables: %s", ", ".join(drop))

    # -- transactions -------------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (re-entrant: nested tx() joins the outer one)."""
        with self._lock:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("COMMIT")

    # -- helpers ------------------------------------------------------------------------------
    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def val(self, sql: str, params: tuple | dict = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if row is None:
            return default
        v = row[0]
        return default if v is None else v

    def exec(self, sql: str, params: tuple | dict = ()) -> int:
        """Execute a write inside a (possibly joined) transaction; returns lastrowid."""
        with self.tx() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid or 0

    # -- settings -----------------------------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        return self.val("SELECT value FROM settings WHERE key=?", (key,), default)

    def set_setting(self, key: str, value: str) -> None:
        self.exec(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )

    def seed_setting(self, key: str, value: str) -> None:
        self.exec("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)", (key, value, now_iso()))

    def settings_dict(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.q("SELECT key,value FROM settings")}

    # -- tenants ------------------------------------------------------------------------------
    def ensure_tenant(self, tenant_id: str, kind: str, label: str | None = None) -> None:
        self.exec(
            "INSERT OR IGNORE INTO tenants(id,kind,label,created_at,last_seen_at) VALUES(?,?,?,?,?)",
            (tenant_id, kind, label, now_iso(), now_iso()),
        )
        self.exec("INSERT OR IGNORE INTO wallets(tenant_id,balance,updated_at) VALUES(?,0,?)", (tenant_id, now_iso()))

    def touch_tenant(self, tenant_id: str) -> None:
        self.exec("UPDATE tenants SET last_seen_at=? WHERE id=?", (now_iso(), tenant_id))

    def backup_to(self, dest: str | Path) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    return json.loads(s)


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        from .config import settings

        _db = Database(settings.db_path)
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db


def monotonic() -> float:
    return time.monotonic()
