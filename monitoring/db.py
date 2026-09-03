"""SQLite storage for check history, page snapshots, changes, and alerts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "monitoring.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,
    status_code INTEGER,
    response_time_ms INTEGER,
    reason TEXT,
    ssl_days_remaining INTEGER
);
CREATE INDEX IF NOT EXISTS idx_checks_site_time ON checks(site_id, run_at DESC);

CREATE TABLE IF NOT EXISTS page_snapshots (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    run_at TEXT NOT NULL,
    title TEXT,
    meta_description TEXT,
    h1 TEXT,
    content_length INTEGER,
    dom_element_count INTEGER,
    nav_link_count INTEGER,
    structure_hash TEXT,
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_site_time ON page_snapshots(site_id, run_at DESC);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    detected_at TEXT NOT NULL,
    change_type TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_runs (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    run_at TEXT NOT NULL,
    pages_crawled INTEGER,
    broken_count INTEGER,
    redirect_count INTEGER
);

CREATE TABLE IF NOT EXISTS broken_links (
    id INTEGER PRIMARY KEY,
    link_run_id INTEGER NOT NULL REFERENCES link_runs(id),
    url TEXT NOT NULL,
    status_code INTEGER,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(site_id, alert_type, resolved_at);

-- Accounts + sessions for the live dashboard (serve_dashboard.py); see monitoring/auth.py.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def get_or_create_site(conn: sqlite3.Connection, name: str, url: str) -> int:
    row = conn.execute("SELECT id FROM sites WHERE url = ?", (url,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO sites (name, url) VALUES (?, ?)", (name, url))
    conn.commit()
    return cur.lastrowid


def insert_check(conn: sqlite3.Connection, site_id: int, status) -> None:
    conn.execute(
        "INSERT INTO checks (site_id, run_at, status, status_code, response_time_ms, reason, ssl_days_remaining) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            site_id,
            status.checked_at,
            status.status,
            status.status_code,
            status.response_time_ms,
            status.reason,
            getattr(status, "ssl_days_remaining", None),
        ),
    )
    conn.commit()


def get_recent_checks(conn: sqlite3.Connection, site_id: int, limit: int = 2) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM checks WHERE site_id = ? ORDER BY run_at DESC LIMIT ?", (site_id, limit)
    ).fetchall()


def insert_snapshot(conn: sqlite3.Connection, site_id: int, run_at: str, snap) -> None:
    conn.execute(
        "INSERT INTO page_snapshots (site_id, run_at, title, meta_description, h1, content_length, "
        "dom_element_count, nav_link_count, structure_hash, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            site_id,
            run_at,
            snap.title,
            snap.meta_description,
            snap.h1,
            snap.content_length,
            snap.dom_element_count,
            snap.nav_link_count,
            snap.structure_hash,
            snap.content_hash,
        ),
    )
    conn.commit()


def get_last_snapshot(conn: sqlite3.Connection, site_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM page_snapshots WHERE site_id = ? ORDER BY run_at DESC LIMIT 1", (site_id,)
    ).fetchone()


def insert_change(conn: sqlite3.Connection, site_id: int, detected_at: str, change_type: str, description: str, severity: str) -> None:
    conn.execute(
        "INSERT INTO changes (site_id, detected_at, change_type, description, severity) VALUES (?, ?, ?, ?, ?)",
        (site_id, detected_at, change_type, description, severity),
    )
    conn.commit()


def get_open_alert(conn: sqlite3.Connection, site_id: int, alert_type: str) -> sqlite3.Row | None:
    """The oldest still-open alert of this type -- for an ongoing outage that
    has fired hourly reminders there are several open rows, and callers want
    the first one (its created_at is when the outage started)."""
    return conn.execute(
        "SELECT * FROM alerts WHERE site_id = ? AND alert_type = ? AND resolved_at IS NULL "
        "ORDER BY created_at LIMIT 1",
        (site_id, alert_type),
    ).fetchone()


def get_last_alert(conn: sqlite3.Connection, site_id: int, alert_type: str) -> sqlite3.Row | None:
    """Most recent alert of this type, resolved or not -- used to space out
    repeat 'still down' reminders."""
    return conn.execute(
        "SELECT * FROM alerts WHERE site_id = ? AND alert_type = ? ORDER BY created_at DESC LIMIT 1",
        (site_id, alert_type),
    ).fetchone()


def get_open_outages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every site with an unresolved availability alert right now, with the
    latest message for each (collapses the hourly repeat-alert rows). Used to
    build the combined Teams digest card."""
    return conn.execute(
        """
        SELECT s.name, a.message
        FROM alerts a JOIN sites s ON s.id = a.site_id
        WHERE a.alert_type = 'availability' AND a.resolved_at IS NULL
          AND a.id IN (
              SELECT MAX(id) FROM alerts
              WHERE alert_type = 'availability' AND resolved_at IS NULL
              GROUP BY site_id
          )
        ORDER BY s.name
        """
    ).fetchall()


def create_alert(conn: sqlite3.Connection, site_id: int, alert_type: str, severity: str, message: str, created_at: str) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO alerts (site_id, alert_type, severity, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (site_id, alert_type, severity, message, created_at),
    )
    conn.commit()
    return conn.execute("SELECT * FROM alerts WHERE id = ?", (cur.lastrowid,)).fetchone()


def resolve_alert(conn: sqlite3.Connection, alert_id: int, resolved_at: str) -> None:
    conn.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (resolved_at, alert_id))
    conn.commit()


def resolve_open_alerts(conn: sqlite3.Connection, site_id: int, alert_type: str, resolved_at: str) -> None:
    """Close every open alert of this type for a site at once -- an outage that
    sent hourly reminders leaves several open rows that all clear on recovery."""
    conn.execute(
        "UPDATE alerts SET resolved_at = ? WHERE site_id = ? AND alert_type = ? AND resolved_at IS NULL",
        (resolved_at, site_id, alert_type),
    )
    conn.commit()


def insert_link_run(conn: sqlite3.Connection, site_id: int, run_at: str, pages_crawled: int, broken_count: int, redirect_count: int) -> int:
    cur = conn.execute(
        "INSERT INTO link_runs (site_id, run_at, pages_crawled, broken_count, redirect_count) VALUES (?, ?, ?, ?, ?)",
        (site_id, run_at, pages_crawled, broken_count, redirect_count),
    )
    conn.commit()
    return cur.lastrowid


def insert_broken_link(conn: sqlite3.Connection, link_run_id: int, url: str, status_code: int | None, reason: str) -> None:
    conn.execute(
        "INSERT INTO broken_links (link_run_id, url, status_code, reason) VALUES (?, ?, ?, ?)",
        (link_run_id, url, status_code, reason),
    )
    conn.commit()
