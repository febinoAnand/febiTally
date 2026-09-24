"""SQLite helpers and schema. Uses the stdlib sqlite3 module, one connection per request."""
import os
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ledgers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company         TEXT NOT NULL,
    name            TEXT NOT NULL,
    parent          TEXT,
    opening_balance REAL DEFAULT 0,
    closing_balance REAL DEFAULT 0,
    guid            TEXT,
    fetched_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company, name)
);

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT NOT NULL,
    bank_ledger TEXT NOT NULL,
    filename    TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    status      TEXT DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id      INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    txn_date       TEXT,
    narration      TEXT,
    ref_no         TEXT,
    debit          REAL DEFAULT 0,
    credit         REAL DEFAULT 0,
    balance        REAL,
    ledger         TEXT,
    voucher_type   TEXT,
    status         TEXT DEFAULT 'pending',
    tally_response TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_import ON entries(import_id);

CREATE TABLE IF NOT EXISTS ledger_rules (
    company TEXT NOT NULL,
    keyword TEXT NOT NULL,
    ledger  TEXT NOT NULL,
    PRIMARY KEY (company, keyword)
);
"""


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = _connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    conn = get_db()
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
